# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
# Modified by ReByteAI in 2026 to integrate the Rebyte managed Agent API.

import asyncio
import os

import pytest

from commerce_common.streaming import AgentEvent
from demo_common import host as host_module
from demo_common import host_approval_default, load_demo_env, spawn_background
from demo_common.host import _background_tasks
from demo_common.sessions import SessionStore
from shopping_agent import Product, ShoppingSessionContext, ShoppingSessionState


@pytest.mark.parametrize(
    ("value", "expected"), [(None, True), ("0", False), ("1", True), ("", True), ("true", True)]
)
def test_only_an_explicit_zero_turns_host_approval_off(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("MERCHANT_REQUIRE_HOST_APPROVAL", raising=False)
    else:
        monkeypatch.setenv("MERCHANT_REQUIRE_HOST_APPROVAL", value)
    assert host_approval_default() is expected


@pytest.fixture
def env_dirs(tmp_path, monkeypatch):
    """A repo root and an example directory under ``tmp_path``, with the loader pointed at
    the former and no credential variables in the environment."""
    repo_root, example_root = tmp_path / "repo", tmp_path / "repo" / "examples" / "retail"
    example_root.mkdir(parents=True)
    monkeypatch.setattr(host_module, "REPO_ROOT", repo_root)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "COMMERCE_DEMO_AUTH"):
        monkeypatch.delenv(name, raising=False)
    return repo_root, example_root


def test_a_key_in_the_environment_survives_a_blank_env_file(env_dirs, monkeypatch):
    repo_root, example_root = env_dirs
    (repo_root / ".env").write_text("ANTHROPIC_API_KEY=\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-shell")

    load_demo_env(example_root)

    assert os.environ["ANTHROPIC_API_KEY"] == "from-the-shell"


def test_the_example_env_file_fills_in_before_the_repo_root_one(env_dirs):
    repo_root, example_root = env_dirs
    (repo_root / ".env").write_text("ANTHROPIC_API_KEY=root-key\n")
    (example_root / ".env").write_text("ANTHROPIC_API_KEY=example-key\n")

    load_demo_env(example_root)

    assert os.environ["ANTHROPIC_API_KEY"] == "example-key"


def test_the_repo_root_env_file_is_read_when_the_example_has_none(env_dirs):
    repo_root, example_root = env_dirs
    (repo_root / ".env").write_text("ANTHROPIC_API_KEY=root-key\n")

    load_demo_env(example_root)

    assert os.environ["ANTHROPIC_API_KEY"] == "root-key"


def test_sdk_auth_clears_key_variables_and_reads_no_file(env_dirs, monkeypatch):
    repo_root, example_root = env_dirs
    (repo_root / ".env").write_text("ANTHROPIC_API_KEY=root-key\n")
    monkeypatch.setenv("COMMERCE_DEMO_AUTH", "sdk")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-shell")

    load_demo_env(example_root)

    assert "ANTHROPIC_API_KEY" not in os.environ


async def test_spawn_background_holds_the_task_until_it_finishes():
    started, release = asyncio.Event(), asyncio.Event()

    async def work() -> None:
        started.set()
        await release.wait()

    spawn_background(work())
    await asyncio.wait_for(started.wait(), 1)
    assert _background_tasks
    release.set()
    for _ in range(10):
        if not _background_tasks:
            break
        await asyncio.sleep(0)
    assert not _background_tasks


async def test_managed_interactive_state_is_saved_before_the_ui_event_is_yielded():
    release = asyncio.Event()
    store = SessionStore(ShoppingSessionState)
    record = store.start("demo-user")

    class ManagedAgent:
        persist_before_yield = frozenset({"ui", "turn_complete"})

        async def stream_turn(self, messages, session, state):
            del messages, session
            state.remember_products([Product(product_id="p-1", title="Tent", price=99)])
            yield AgentEvent.ui("products", {"items": []})
            await release.wait()
            yield AgentEvent.turn_complete("end_turn", {}, 1, 0)

        async def update_memory(self, messages, session):
            del messages, session
            return []

    response = host_module.stream_turn(
        ManagedAgent(),
        store,
        record,
        ShoppingSessionContext(session_id=record.session_id, user_id=record.user_id),
        env_hint=".env",
    )
    stream = response.body_iterator.__aiter__()

    first_chunk = await anext(stream)

    assert first_chunk.startswith("event: ui\n")
    assert store.require(record.session_id).state.seen_products["p-1"].title == "Tent"

    release.set()
    assert (await anext(stream)).startswith("event: turn_complete\n")
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
