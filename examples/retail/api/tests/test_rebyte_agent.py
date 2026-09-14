# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""The host's tool-result handoff preserves the original execution and ending rules."""

import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from retail.api.rebyte_agent import RebyteShoppingAgent
from shopping_agent import ShoppingAgentConfig
from shopping_agent.tools.registry import INLINE_CONTEXT_DESCRIPTIONS, build_tools


class SessionStream:
    def __init__(self, rounds, terminal="cancelled"):
        self.events = [
            SimpleNamespace(
                type="agent.session.requires_action",
                session=SimpleNamespace(required_actions=calls),
            )
            for calls in rounds
        ] + [
            SimpleNamespace(
                type=f"agent.session.turn.{terminal}",
                turn=SimpleNamespace(status=terminal, usage=None, error=None),
            ),
            SimpleNamespace(type="agent.session.idle"),
        ]
        self.close = AsyncMock()

    async def __aiter__(self):
        for event in self.events:
            yield event


def call(name, arguments, call_id):
    return SimpleNamespace(
        type="function_call", call_id=call_id, turn_id="turn_test", name=name, arguments=arguments
    )


def make_agent(monkeypatch, backend, session, streams, config=None):
    monkeypatch.setenv("REBYTE_API_KEY", "test-key")
    monkeypatch.setenv("REBYTE_AGENT_ID", "test-agent")
    events = SimpleNamespace(stream=AsyncMock(side_effect=streams), create=AsyncMock())
    client = SimpleNamespace(
        beta=SimpleNamespace(agents=SimpleNamespace(sessions=SimpleNamespace(events=events)))
    )
    agent = RebyteShoppingAgent(backend=backend, config=config, client=client)
    agent._sessions[session.session_id] = "sess_test"
    return agent, events


async def turn(agent, session, state, text="Find a tent"):
    return [
        event
        async for event in agent.stream_turn([{"role": "user", "content": text}], session, state)
    ]


def test_manifest_preserves_every_original_host_tool():
    root = Path(__file__).resolve().parents[4]
    manifest = tomllib.loads((root / "rebyte/agent.toml").read_text())
    expected = [
        {
            "type": "function",
            "name": tool["name"],
            "description": INLINE_CONTEXT_DESCRIPTIONS.get(tool["name"], tool["description"]),
            "parameters": tool["input_schema"],
            "strict": False,
        }
        for tool in build_tools(ShoppingAgentConfig(), [])
        if tool["name"] != "load_skill"
    ]
    assert manifest["client_tools"] == expected
    assert not manifest.get("mcp_servers")


async def test_read_continues_but_presentation_stores_results_and_cancels_atomically(
    monkeypatch, backend, session, state
):
    stream = SessionStream(
        [
            [call("search_products", {"query": "tent"}, "search")],
            [
                call("present_products", {"picks": [{"product_id": "AR-1201"}]}, "cards"),
                call("present_suggestions", {"suggestions": ["Add the tent"]}, "chips"),
            ],
        ]
    )
    followup = SessionStream([], terminal="completed")
    agent, events_api = make_agent(monkeypatch, backend, session, [stream, followup])
    events = await turn(agent, session, state)
    assert events[-1].type == "turn_complete"
    assert sum(event.type == "ui" for event in events) == 2
    batches = [c.kwargs["events"] for c in events_api.create.await_args_list]
    assert batches[1][0]["call_id"] == "search"
    assert "AR-1201" in batches[1][0]["output"]
    assert [e["type"] for e in batches[2]] == [
        "agent.session.input.tool_result",
        "agent.session.input.tool_result",
        "agent.session.input.cancel",
    ]
    await turn(agent, session, state, "Thanks")
    assert (
        events_api.create.await_args.kwargs["events"][0]["input"][0]["content"][0]["text"]
        == "Thanks"
    )
    assert stream.close.await_count == followup.close.await_count == 1


@pytest.mark.parametrize("mixed_read", [False, True])
async def test_failed_presentation_or_mixed_read_round_continues(
    monkeypatch, backend, session, state, mixed_read
):
    first = (
        call("get_cart", {}, "read")
        if mixed_read
        else call("present_products", {"picks": [{"product_id": "unknown"}]}, "bad-card")
    )
    agent, events_api = make_agent(
        monkeypatch,
        backend,
        session,
        [
            SessionStream(
                [[first, call("present_suggestions", {"suggestions": ["Find a tent"]}, "chips")]],
                terminal="completed",
            )
        ],
    )
    await turn(agent, session, state)
    outputs = events_api.create.await_args.kwargs["events"]
    assert len(outputs) == 2
    assert all(e["type"] == "agent.session.input.tool_result" for e in outputs)


@pytest.mark.parametrize("cancel_error", [None, RuntimeError("cancellation also failed")])
async def test_storage_failure_does_not_report_turn_complete(
    monkeypatch, backend, session, state, cancel_error
):
    agent, events_api = make_agent(
        monkeypatch,
        backend,
        session,
        [SessionStream([[call("present_suggestions", {"suggestions": ["Find a tent"]}, "chips")]])],
    )
    events_api.create.side_effect = [None, RuntimeError("storage unavailable"), cancel_error]
    events = []
    with pytest.raises(RuntimeError, match="storage unavailable"):
        async for event in agent.stream_turn([], session, state):
            events.append(event)
    assert all(event.type != "turn_complete" for event in events)


async def test_item_done_without_requires_action_never_executes_cart_write(
    monkeypatch, backend, session, state
):
    backend.add_to_cart = AsyncMock()
    stream = SessionStream([], terminal="failed")
    stream.events.insert(
        0,
        SimpleNamespace(
            type="agent.session.turn.item.done",
            item=call("add_to_cart", {"product_id": "AR-1201"}, "write"),
        ),
    )
    agent, _ = make_agent(monkeypatch, backend, session, [stream])
    with pytest.raises(RuntimeError, match="failed"):
        await turn(agent, session, state)
    backend.add_to_cart.assert_not_awaited()
