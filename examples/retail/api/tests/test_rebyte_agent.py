# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""The host's tool-result handoff preserves the original execution and ending rules."""

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from retail.api.rebyte_agent import RebyteShoppingAgent
from shopping_agent import ShoppingAgentConfig
from shopping_agent.tools.registry import INLINE_CONTEXT_DESCRIPTIONS, build_tools


class ResponseStream:
    def __init__(self, calls, terminal="response.completed"):
        self.events = [{"type": "response.output_item.done", "item": call} for call in calls] + [
            {"type": terminal, "response": {"conversation": {"id": "conv_test"}}}
        ]
        self.close = AsyncMock()

    async def __aiter__(self):
        for event in self.events:
            yield event


def call(name, arguments, call_id):
    return {
        "type": "function_call",
        "id": f"item_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }


def make_agent(monkeypatch, backend, session, streams, config=None):
    monkeypatch.setenv("REBYTE_API_KEY", "test-key")
    monkeypatch.setenv("REBYTE_AGENT_ID", "test-agent")
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock(side_effect=streams)),
        conversations=SimpleNamespace(items=SimpleNamespace(create=AsyncMock())),
    )
    agent = RebyteShoppingAgent(backend=backend, config=config, client=client)
    agent._conversations[session.session_id] = "conv_test"
    return agent, client


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


async def test_read_continues_but_clean_presentation_saves_without_model_call(
    monkeypatch, backend, session, state
):
    streams = [
        ResponseStream([call("search_products", {"query": "tent"}, "search")]),
        ResponseStream(
            [
                call("present_products", {"picks": [{"product_id": "AR-1201"}]}, "cards"),
                call("present_suggestions", {"suggestions": ["Add the tent"]}, "chips"),
            ]
        ),
        ResponseStream([]),
    ]
    agent, client = make_agent(monkeypatch, backend, session, streams)
    events = await turn(agent, session, state)
    assert events[-1].type == "turn_complete"
    assert sum(event.type == "ui" for event in events) == 2
    assert client.responses.create.await_count == 2
    result = client.responses.create.await_args_list[1].kwargs["input"]
    assert result[0]["call_id"] == "search"
    assert "AR-1201" in result[0]["output"]
    stored = client.conversations.items.create.await_args.kwargs
    assert stored["conversation_id"] == "conv_test"
    assert [item["call_id"] for item in stored["items"]] == ["cards", "chips"]
    await turn(agent, session, state, "Thanks")
    assert client.responses.create.await_args.kwargs["input"] == "Thanks"
    assert all(stream.close.await_count == 1 for stream in streams)


@pytest.mark.parametrize("mixed_read", [False, True])
async def test_failed_presentation_or_mixed_read_round_continues(
    monkeypatch, backend, session, state, mixed_read
):
    first = (
        call("get_cart", {}, "read")
        if mixed_read
        else call("present_products", {"picks": [{"product_id": "unknown"}]}, "bad-card")
    )
    agent, client = make_agent(
        monkeypatch,
        backend,
        session,
        [
            ResponseStream(
                [
                    first,
                    call("present_suggestions", {"suggestions": ["Find a tent"]}, "chips"),
                ]
            ),
            ResponseStream([]),
        ],
    )
    await turn(agent, session, state)
    assert client.responses.create.await_count == 2
    assert len(client.responses.create.await_args.kwargs["input"]) == 2
    client.conversations.items.create.assert_not_awaited()


async def test_storage_failure_does_not_report_turn_complete(monkeypatch, backend, session, state):
    agent, client = make_agent(
        monkeypatch,
        backend,
        session,
        [
            ResponseStream(
                [
                    call("present_suggestions", {"suggestions": ["Find a tent"]}, "chips"),
                ]
            )
        ],
    )
    client.conversations.items.create.side_effect = RuntimeError("storage unavailable")
    events = []
    with pytest.raises(RuntimeError, match="storage unavailable"):
        async for event in agent.stream_turn([], session, state):
            events.append(event)
    assert all(event.type != "turn_complete" for event in events)
    assert client.responses.create.await_count == 1


async def test_failed_response_never_executes_cart_write(monkeypatch, backend, session, state):
    backend.add_to_cart = AsyncMock()
    agent, client = make_agent(
        monkeypatch,
        backend,
        session,
        [
            ResponseStream(
                [
                    call("add_to_cart", {"product_id": "AR-1201"}, "write"),
                ],
                terminal="response.failed",
            )
        ],
    )
    with pytest.raises(RuntimeError, match="response.failed"):
        await turn(agent, session, state)
    backend.add_to_cart.assert_not_awaited()
    client.conversations.items.create.assert_not_awaited()
