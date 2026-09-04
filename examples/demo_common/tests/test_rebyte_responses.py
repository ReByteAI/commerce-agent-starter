# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from commerce_common.streaming import AgentEvent
from demo_common import rebyte_responses as module
from demo_common.rebyte_responses import (
    ClientToolResult,
    RebyteFunctionCall,
    RebyteResponsesAdapter,
)

CONVERSATION = "conv_11111111-1111-4111-8111-111111111111"
RUNTIME_SCOPE = "11111111-1111-4111-8111-111111111111"


class FakeStream:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        for event in self.events:
            yield event

    async def close(self) -> None:
        self.closed = True


class FakeResponses:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []

    async def create(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.calls.append(kwargs)
        stream = FakeStream(self.responses.pop(0))
        self.streams.append(stream)
        return stream


class FakeClient:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = FakeResponses(responses)


def response(conversation: str = CONVERSATION, **updates: Any) -> dict[str, Any]:
    return {
        "id": "resp-1",
        "status": "completed",
        "conversation": {"id": conversation},
        "output": [],
        "usage": None,
    } | updates


async def collect(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in events]


async def test_turn_maps_server_mcp_events_and_reuses_the_conversation():
    mcp_id = "mcp-1"
    output = json.dumps([{"type": "text", "text": "Found one product."}])
    call = {
        "id": mcp_id,
        "type": "mcp_call",
        "server_label": "storefront",
        "name": "search_products",
        "arguments": '{"query":"tent"}',
        "output": output,
        "error": None,
        "status": "completed",
    }
    first = [
        {"type": "response.created", "response": response()},
        {
            "type": "response.output_item.added",
            "item": call | {"arguments": "", "output": None, "status": "in_progress"},
        },
        {
            "type": "response.mcp_call_arguments.done",
            "item_id": mcp_id,
            "arguments": '{"query":"tent"}',
        },
        {"type": "response.mcp_call.in_progress", "item_id": mcp_id},
        {"type": "response.output_item.done", "item": call},
        {"type": "response.output_text.delta", "delta": "I found one."},
        {
            "type": "response.completed",
            "response": response(
                output=[call],
                usage={
                    "input_tokens": 12,
                    "output_tokens": 4,
                    "input_tokens_details": {"cached_tokens": 3},
                },
            ),
        },
    ]
    second = [
        {"type": "response.created", "response": response()},
        {"type": "response.output_text.delta", "delta": "Still here."},
        {"type": "response.completed", "response": response()},
    ]
    client = FakeClient([first, second])
    adapter = RebyteResponsesAdapter(agent_id="agent-1", client=client)

    events = await collect(adapter.stream_turn("local-1", "find a tent", idempotency_key="k1"))

    assert [event.type for event in events] == [
        "tool_call",
        "tool_result",
        "text_delta",
        "turn_complete",
    ]
    assert events[0].data == {
        "tool": "search_products",
        "id": mcp_id,
        "input": {"query": "tent"},
    }
    assert events[1].data["summary"] == "Found one product."
    assert events[-1].data["usage"] == {
        "input_tokens": 12,
        "output_tokens": 4,
        "cache_read_input_tokens": 3,
    }
    assert adapter.conversation_id("local-1") == CONVERSATION
    assert adapter.runtime_scope("local-1") == RUNTIME_SCOPE
    assert client.responses.calls[0] == {
        "model": "agent-1",
        "input": "find a tent",
        "stream": True,
        "extra_headers": {"Idempotency-Key": "k1-1"},
    }

    follow_up = await collect(adapter.stream_turn("local-1", "compare it"))
    assert [event.type for event in follow_up] == ["text_delta", "turn_complete"]
    assert client.responses.calls[1]["conversation"] == CONVERSATION
    assert all(stream.closed for stream in client.responses.streams)


async def test_client_function_call_executes_in_host_and_continues_same_conversation():
    function_item = {
        "id": "fc-item-1",
        "type": "function_call",
        "call_id": "call-1",
        "name": "present_products",
        "arguments": '{"picks":[{"product_id":"p-1"}]}',
        "status": "completed",
    }
    first = [
        {"type": "response.created", "response": response()},
        {
            "type": "response.output_item.added",
            "item": function_item | {"arguments": "", "status": "in_progress"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc-item-1",
            "delta": '{"picks":[',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc-item-1",
            "arguments": function_item["arguments"],
        },
        {"type": "response.output_item.done", "item": function_item},
        {
            "type": "response.completed",
            "response": response(output=[function_item], usage={"input_tokens": 10}),
        },
    ]
    second = [
        {"type": "response.created", "response": response()},
        {"type": "response.output_text.delta", "delta": "Here are the options."},
        {
            "type": "response.completed",
            "response": response(usage={"input_tokens": 4, "output_tokens": 5}),
        },
    ]
    client = FakeClient([first, second])
    handled: list[RebyteFunctionCall] = []

    async def handle(_: str, call: RebyteFunctionCall) -> ClientToolResult:
        handled.append(call)
        return ClientToolResult(
            "Displayed to the customer.",
            events=(AgentEvent.ui("products", {"items": [{"product_id": "p-1"}]}),),
        )

    adapter = RebyteResponsesAdapter(agent_id="agent-1", client=client, client_tool_handler=handle)
    events = await collect(adapter.stream_turn("local-1", "show products", idempotency_key="k"))

    assert [event.type for event in events] == [
        "tool_call",
        "ui",
        "tool_result",
        "text_delta",
        "turn_complete",
    ]
    assert handled == [
        RebyteFunctionCall(
            id="fc-item-1",
            call_id="call-1",
            name="present_products",
            arguments={"picks": [{"product_id": "p-1"}]},
        )
    ]
    assert events[1].data["stream_id"] == "call-1"
    assert client.responses.calls[1] == {
        "model": "agent-1",
        "conversation": CONVERSATION,
        "input": [
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "Displayed to the customer.",
            }
        ],
        "stream": True,
        "extra_headers": {"Idempotency-Key": "k-2"},
    }
    assert "tools" not in client.responses.calls[0]
    assert "previous_response_id" not in client.responses.calls[1]
    assert events[-1].data["usage"] == {
        "input_tokens": 14,
        "output_tokens": 5,
    }


async def test_client_tool_continuations_have_a_finite_hard_limit():
    responses = []
    for index in range(module.MAX_CLIENT_TOOL_CONTINUATION_ROUNDS + 1):
        function_item = {
            "id": f"fc-item-{index}",
            "type": "function_call",
            "call_id": f"call-{index}",
            "name": "present_suggestions",
            "arguments": '{"suggestions":["Keep going"]}',
            "status": "completed",
        }
        responses.append(
            [
                {
                    "type": "response.completed",
                    "response": response(output=[function_item]),
                }
            ]
        )

    client = FakeClient(responses)
    adapter = RebyteResponsesAdapter(
        agent_id="agent-1",
        client=client,
        client_tool_handler=lambda _session, _call: ClientToolResult("Displayed."),
    )

    with pytest.raises(
        module.ClientToolContinuationLimitError,
        match=r"exceeded 8 client-tool continuation rounds",
    ):
        await collect(adapter.stream_turn("local-1", "loop forever"))

    assert len(client.responses.calls) == module.MAX_CLIENT_TOOL_CONTINUATION_ROUNDS + 1


async def test_failed_response_finishes_failed_mcp_call_then_emits_safe_error():
    failed_call = {
        "id": "mcp-failed",
        "type": "mcp_call",
        "server_label": "storefront",
        "name": "get_cart",
        "arguments": "{}",
        "output": None,
        "error": "store unavailable",
        "status": "failed",
    }
    client = FakeClient(
        [
            [
                {
                    "type": "response.output_item.added",
                    "item": failed_call | {"error": None, "status": "in_progress"},
                },
                {"type": "response.mcp_call.in_progress", "item_id": "mcp-failed"},
                {
                    "type": "response.failed",
                    "response": response(
                        status="failed",
                        output=[failed_call],
                        error={"message": "Agent failed."},
                    ),
                },
            ]
        ]
    )
    events = await collect(
        RebyteResponsesAdapter(agent_id="agent-1", client=client).stream_turn(
            "local-1", "show my cart"
        )
    )
    assert [event.type for event in events] == ["tool_call", "tool_result", "error"]
    assert events[1].data["summary"] == "store unavailable"
    assert events[1].data["is_error"] is True


async def test_same_session_turns_are_serialized_until_conversation_is_known():
    release = asyncio.Event()
    first_started = asyncio.Event()
    calls: list[dict[str, Any]] = []

    class Responses:
        async def create(self, **kwargs: Any) -> AsyncIterator[Any]:
            calls.append(kwargs)

            async def events() -> AsyncIterator[dict[str, Any]]:
                if len(calls) == 1:
                    first_started.set()
                    yield {"type": "response.created", "response": response()}
                    await release.wait()
                yield {"type": "response.completed", "response": response()}

            return events()

    client = type("Client", (), {"responses": Responses()})()
    adapter = RebyteResponsesAdapter(agent_id="agent-1", client=client)
    first = asyncio.create_task(collect(adapter.stream_turn("same", "one")))
    await first_started.wait()
    second = asyncio.create_task(collect(adapter.stream_turn("same", "two")))
    await asyncio.sleep(0)
    assert len(calls) == 1
    release.set()
    await asyncio.gather(first, second)
    assert calls[1]["conversation"] == CONVERSATION


def test_bind_conversation_requires_the_public_rebyte_id_shape():
    adapter = RebyteResponsesAdapter(agent_id="agent-1", client=FakeClient([]))
    with pytest.raises(ValueError, match="conv_ followed by a UUID"):
        adapter.bind_conversation("local-1", "workspace-1")


def test_from_env_builds_official_client_at_responses_base(monkeypatch):
    built: dict[str, Any] = {}
    fake = FakeClient([])

    def build(**kwargs: Any) -> FakeClient:
        built.update(kwargs)
        return fake

    monkeypatch.setattr(module, "AsyncOpenAI", build)
    monkeypatch.setenv("REBYTE_API_KEY", "rbk-secret")
    monkeypatch.setenv("REBYTE_AGENT_ID", "agent-1")
    monkeypatch.setenv("REBYTE_BASE_URL", "https://example.test/root/")
    adapter = RebyteResponsesAdapter.from_env()
    assert adapter.client is fake
    assert adapter.base_url == "https://example.test/root/v1"
    assert built == {
        "api_key": "rbk-secret",
        "base_url": "https://example.test/root/v1",
        "timeout": 360,
        "max_retries": 2,
    }


@pytest.mark.parametrize("missing", ["REBYTE_API_KEY", "REBYTE_AGENT_ID"])
def test_from_env_requires_both_credentials(monkeypatch, missing):
    monkeypatch.setenv("REBYTE_API_KEY", "rbk-secret")
    monkeypatch.setenv("REBYTE_AGENT_ID", "agent-1")
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match=missing):
        RebyteResponsesAdapter.from_env()
