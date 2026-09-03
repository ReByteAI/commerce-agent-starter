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
from demo_common.rebyte_responses import RebyteMcpCall, RebyteResponsesAdapter

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
    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []

    async def create(self, **kwargs: Any) -> AsyncIterator[Any]:
        self.calls.append(kwargs)
        stream = FakeStream(self.turns.pop(0))
        self.streams.append(stream)
        return stream


class FakeClient:
    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self.responses = FakeResponses(turns)


def response(conversation: str, **updates: Any) -> dict[str, Any]:
    return {
        "id": "resp-1",
        "status": "completed",
        "conversation": {"id": conversation},
        "output": [],
        "usage": None,
    } | updates


async def collect(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in events]


async def test_turn_maps_responses_events_binds_the_conversation_and_runs_the_hook():
    conversation = CONVERSATION
    mcp_id = "mcp-1"
    output = json.dumps([{"type": "text", "text": "Found one product."}])
    completed_call = {
        "id": mcp_id,
        "type": "mcp_call",
        "server_label": "storefront",
        "name": "search_products",
        "arguments": '{"query":"tent"}',
        "output": output,
        "error": None,
        "status": "completed",
    }
    first_turn = [
        {"type": "response.created", "response": response(conversation)},
        {
            "type": "response.output_item.added",
            "item": completed_call | {"arguments": "", "output": None, "status": "in_progress"},
        },
        {
            "type": "response.mcp_call_arguments.delta",
            "item_id": mcp_id,
            "delta": '{"query":',
        },
        {
            "type": "response.mcp_call_arguments.done",
            "item_id": mcp_id,
            "arguments": '{"query":"tent"}',
        },
        {"type": "response.mcp_call.in_progress", "item_id": mcp_id},
        {"type": "response.mcp_call.completed", "item_id": mcp_id},
        {"type": "response.output_item.done", "item": completed_call},
        {"type": "response.output_text.delta", "delta": "I found one."},
        {
            "type": "response.completed",
            "response": response(
                conversation,
                output=[completed_call],
                usage={
                    "input_tokens": 12,
                    "output_tokens": 4,
                    "input_tokens_details": {"cached_tokens": 3},
                },
            ),
        },
    ]
    second_turn = [
        {"type": "response.created", "response": response(conversation)},
        {"type": "response.output_text.delta", "delta": "Still here."},
        {"type": "response.completed", "response": response(conversation)},
    ]
    client = FakeClient([first_turn, second_turn])
    seen: list[tuple[str, RebyteMcpCall]] = []

    async def present(local_session_id: str, call: RebyteMcpCall) -> list[AgentEvent]:
        seen.append((local_session_id, call))
        return [
            AgentEvent.ui("products", {"items": [{"product": {"product_id": "p-1"}}]}),
            AgentEvent.progress("Joined canonical product details"),
        ]

    adapter = RebyteResponsesAdapter(
        agent_id="agent-1",
        client=client,
        presentation_hooks={"storefront/search_products": present},
    )
    events = await collect(adapter.stream_turn("local-1", "find a tent", idempotency_key="k1"))

    assert [event.type for event in events] == [
        "tool_call",
        "ui",
        "progress",
        "tool_result",
        "text_delta",
        "turn_complete",
    ]
    assert events[0].data == {
        "tool": "search_products",
        "id": mcp_id,
        "input": {"query": "tent"},
    }
    assert events[1].data["stream_id"] == mcp_id
    assert events[1].data["component"] == "products"
    assert events[2].data == {"message": "Joined canonical product details"}
    assert events[3].data == {
        "tool": "search_products",
        "id": mcp_id,
        "summary": "Found one product.",
        "is_error": False,
        "status": "ok",
    }
    assert events[4].data == {"text": "I found one."}
    assert events[5].data["usage"] == {
        "input_tokens": 12,
        "output_tokens": 4,
        "cache_read_input_tokens": 3,
    }
    assert seen == [
        (
            "local-1",
            RebyteMcpCall(
                id=mcp_id,
                server_label="storefront",
                name="search_products",
                arguments={"query": "tent"},
                output=[{"type": "text", "text": "Found one product."}],
                error=None,
                status="completed",
            ),
        )
    ]
    assert adapter.conversation_id("local-1") == conversation
    assert adapter.runtime_scope("local-1") == RUNTIME_SCOPE
    assert client.responses.calls[0] == {
        "model": "agent-1",
        "input": "find a tent",
        "stream": True,
        "extra_headers": {"Idempotency-Key": "k1"},
    }

    follow_up = await collect(adapter.stream_turn("local-1", "compare it"))
    assert [event.type for event in follow_up] == ["text_delta", "turn_complete"]
    assert client.responses.calls[1]["conversation"] == conversation
    assert adapter.runtime_scope("local-1") == RUNTIME_SCOPE
    assert all(stream.closed for stream in client.responses.streams)

    await adapter.forget_session("local-1")
    assert adapter.conversation_id("local-1") is None
    assert adapter.runtime_scope("local-1") is None


async def test_failed_response_finishes_a_failed_mcp_call_then_emits_a_safe_error():
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
                {
                    "type": "response.mcp_call_arguments.done",
                    "item_id": "mcp-failed",
                    "arguments": "{}",
                },
                {"type": "response.mcp_call.in_progress", "item_id": "mcp-failed"},
                {
                    "type": "response.failed",
                    "response": response(
                        "conv_22222222-2222-4222-8222-222222222222",
                        status="failed",
                        output=[failed_call],
                        error={"code": "agent_execution_failed", "message": "Agent failed."},
                    ),
                },
            ]
        ]
    )
    adapter = RebyteResponsesAdapter(agent_id="agent-1", client=client)

    events = await collect(adapter.stream_turn("local-1", "show my cart"))

    assert [event.type for event in events] == ["tool_call", "tool_result", "error"]
    assert events[1].data == {
        "tool": "get_cart",
        "id": "mcp-failed",
        "summary": "store unavailable",
        "is_error": True,
        "status": "error",
    }
    assert events[2].data == {"message": "The Rebyte Agent request failed. Please try again."}
    assert adapter.conversation_id("local-1") == ("conv_22222222-2222-4222-8222-222222222222")


async def test_same_session_turns_are_serialized_until_the_first_conversation_is_known():
    release = asyncio.Event()
    first_started = asyncio.Event()
    calls: list[dict[str, Any]] = []

    class Responses:
        async def create(self, **kwargs: Any) -> AsyncIterator[Any]:
            calls.append(kwargs)

            async def events() -> AsyncIterator[dict[str, Any]]:
                if len(calls) == 1:
                    first_started.set()
                    yield {"type": "response.created", "response": response(CONVERSATION)}
                    await release.wait()
                yield {"type": "response.completed", "response": response(CONVERSATION)}

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

    assert len(calls) == 2
    assert calls[1]["conversation"] == CONVERSATION


def test_bind_conversation_requires_the_public_rebyte_id_shape():
    adapter = RebyteResponsesAdapter(agent_id="agent-1", client=FakeClient([]))

    with pytest.raises(ValueError, match="conv_ followed by a UUID"):
        adapter.bind_conversation("local-1", "workspace-1")


def test_from_env_builds_the_official_client_at_the_responses_base(monkeypatch):
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
