# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from commerce_common.memory import InMemoryMemoryStore
from commerce_common.streaming import AgentEvent
from demo_common.rebyte_responses import RebyteResponsesAdapter
from demo_common.rebyte_shopping import RebyteShoppingAgent
from retail.api.mock_retail import MockRetail
from shopping_agent import ShoppingSessionContext, ShoppingSessionState

CONVERSATION = "conv_33333333-3333-4333-8333-333333333333"
RUNTIME_SCOPE = "33333333-3333-4333-8333-333333333333"
YOGA_MAT = "AR-1301"


class FakeResponses:
    def __init__(self, events: list[AgentEvent], scope: str = "conversation-1") -> None:
        self.events = events
        self.scope = scope
        self.calls: list[tuple[str, str]] = []
        self.forgotten: list[str] = []

    async def stream_turn(self, session_id: str, message: str) -> AsyncIterator[AgentEvent]:
        self.calls.append((session_id, message))
        for event in self.events:
            yield event

    def runtime_scope(self, session_id: str) -> str | None:
        return self.scope if session_id else None

    async def forget_session(self, session_id: str) -> None:
        self.forgotten.append(session_id)


class FakeResponseStream:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        for event in self.events:
            yield event

    async def close(self) -> None:
        pass


class FakeResponseResource:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    async def create(self, **_: Any) -> AsyncIterator[Any]:
        return FakeResponseStream(self.events)


class FakeResponseClient:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.responses = FakeResponseResource(events)


def response(*calls: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp-1",
        "status": "completed",
        "conversation": {"id": CONVERSATION},
        "output": list(calls),
        "usage": None,
    }


def completed_call(call_id: str, name: str, arguments: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "mcp_call",
        "server_label": "storefront",
        "name": name,
        "arguments": json.dumps(arguments),
        "output": json.dumps([{"type": "text", "text": text}]),
        "error": None,
        "status": "completed",
    }


def completed_turn_events(*calls: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "response.created", "response": response()},
        *({"type": "response.output_item.done", "item": call} for call in calls),
        {"type": "response.completed", "response": response(*calls)},
    ]


async def test_managed_agent_forwards_latest_turn_and_mirrors_rendered_products():
    product = {
        "product_id": "tent-1",
        "title": "Family Tent",
        "price": 219.0,
        "currency": "USD",
        "in_stock": True,
    }
    fake = FakeResponses(
        [
            AgentEvent.ui("products", {"items": [{"product": product}]}),
            AgentEvent.turn_complete("end_turn", {}, 10, 0),
        ]
    )
    agent = RebyteShoppingAgent(
        backend=object(), responses=cast(RebyteResponsesAdapter, cast(Any, fake))
    )
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "show me a tent"},
    ]
    session = ShoppingSessionContext(session_id="local-1", user_id="demo-user")
    state = ShoppingSessionState()

    events = [event async for event in agent.stream_turn(messages, session, state)]

    assert [event.type for event in events] == ["ui", "turn_complete"]
    assert fake.calls == [("local-1", "show me a tent")]
    assert state.seen_products["tent-1"].title == "Family Tent"
    assert agent.runtime_scope("local-1") == "conversation-1"

    await agent.forget_session("local-1")
    assert fake.forgotten == ["local-1"]


async def test_unconfigured_agent_returns_a_browser_safe_setup_error(monkeypatch):
    monkeypatch.delenv("REBYTE_API_KEY", raising=False)
    monkeypatch.delenv("REBYTE_AGENT_ID", raising=False)
    agent = RebyteShoppingAgent(backend=object())
    session = ShoppingSessionContext(session_id="local-1", user_id="demo-user")

    events = [
        event
        async for event in agent.stream_turn(
            [{"role": "user", "content": "hello"}], session, ShoppingSessionState()
        )
    ]

    assert len(events) == 1
    assert events[0].type == "error"
    assert "REBYTE_API_KEY" in events[0].data["message"]


async def test_completed_reads_rebuild_provenance_before_local_presentation_enrichment():
    search = completed_call(
        "mcp-search", "search_products", {"query": "yoga mat"}, "catalog result"
    )
    present = completed_call(
        "mcp-present",
        "present_products",
        {"title": "Yoga picks", "picks": [{"product_id": YOGA_MAT}]},
        "Displayed to the customer.",
    )
    adapter = RebyteResponsesAdapter(
        agent_id="agent-1",
        client=FakeResponseClient(completed_turn_events(search, present)),
    )
    backend = MockRetail()
    agent = RebyteShoppingAgent(
        backend=backend, responses=adapter, memory_store=InMemoryMemoryStore()
    )
    state = ShoppingSessionState()
    session = ShoppingSessionContext(session_id="browser-session", user_id="demo-user")

    events = [
        event
        async for event in agent.stream_turn(
            [{"role": "user", "content": "show yoga mats"}], session, state
        )
    ]

    assert [event.type for event in events] == [
        "tool_call",
        "tool_result",
        "tool_call",
        "ui",
        "tool_result",
        "turn_complete",
    ]
    rendered = events[3]
    assert rendered.data["stream_id"] == "mcp-present"
    assert rendered.data["component"] == "products"
    assert rendered.data["payload"]["items"][0]["product"]["product_id"] == YOGA_MAT
    assert state.seen_products[YOGA_MAT].title
    assert agent.runtime_scope("browser-session") == RUNTIME_SCOPE


@pytest.mark.parametrize(
    ("name", "arguments", "expected_product"),
    [
        ("search_products", {"query": "yoga mat"}, YOGA_MAT),
        ("get_product_details", {"product_id": YOGA_MAT}, YOGA_MAT),
        ("get_orders", {"limit": 1}, None),
        ("get_order_status", {"order_id": "AR-78214"}, "AR-1104"),
    ],
)
async def test_each_catalog_or_order_read_rebuilds_local_provenance(
    name: str, arguments: dict[str, Any], expected_product: str | None
):
    read = completed_call("mcp-read", name, arguments, "read completed")
    adapter = RebyteResponsesAdapter(
        agent_id="agent-1", client=FakeResponseClient(completed_turn_events(read))
    )
    agent = RebyteShoppingAgent(
        backend=MockRetail(), responses=adapter, memory_store=InMemoryMemoryStore()
    )
    state = ShoppingSessionState()

    await collect_agent_events(
        agent.stream_turn(
            [{"role": "user", "content": "read"}],
            ShoppingSessionContext(session_id="browser-session", user_id="demo-user"),
            state,
        )
    )

    assert state.seen_products
    if expected_product is not None:
        assert expected_product in state.seen_products


async def test_cart_mutation_is_not_replayed_and_emits_cart_from_shared_backend():
    class CountingRetail(MockRetail):
        def __init__(self) -> None:
            super().__init__()
            self.add_calls = 0

        async def add_to_cart(
            self, session: ShoppingSessionContext, product_id: str, quantity: int
        ):
            self.add_calls += 1
            return await super().add_to_cart(session, product_id, quantity)

    backend = CountingRetail()
    runtime_session = ShoppingSessionContext(session_id=RUNTIME_SCOPE, user_id="demo-user")
    await backend.add_to_cart(runtime_session, YOGA_MAT, 1)
    add = completed_call(
        "mcp-add",
        "add_to_cart",
        {"product_id": YOGA_MAT, "quantity": 1},
        f"Added {YOGA_MAT} x1.",
    )
    adapter = RebyteResponsesAdapter(
        agent_id="agent-1", client=FakeResponseClient(completed_turn_events(add))
    )
    agent = RebyteShoppingAgent(
        backend=backend, responses=adapter, memory_store=InMemoryMemoryStore()
    )
    session = ShoppingSessionContext(session_id="browser-session", user_id="demo-user")

    events = [
        event
        async for event in agent.stream_turn(
            [{"role": "user", "content": "add it"}], session, ShoppingSessionState()
        )
    ]

    assert backend.add_calls == 1
    cart_event = next(event for event in events if event.type == "cart_update")
    assert cart_event.data["cart"]["items"][0]["product_id"] == YOGA_MAT
    assert cart_event.data["cart"]["items"][0]["quantity"] == 1


async def test_memory_write_is_never_replayed_by_the_host():
    class CountingMemoryStore(InMemoryMemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.upserts = 0

        async def upsert_facts(self, subject_id, facts) -> None:
            self.upserts += 1
            await super().upsert_facts(subject_id, facts)

    memory = CountingMemoryStore()
    save = completed_call(
        "mcp-memory",
        "save_memory",
        {"key": "camping", "value": "Prefers light tents"},
        "Saved.",
    )
    adapter = RebyteResponsesAdapter(
        agent_id="agent-1", client=FakeResponseClient(completed_turn_events(save))
    )
    agent = RebyteShoppingAgent(backend=MockRetail(), responses=adapter, memory_store=memory)
    session = ShoppingSessionContext(session_id="browser-session", user_id="demo-user")

    await collect_agent_events(
        agent.stream_turn(
            [{"role": "user", "content": "remember that"}], session, ShoppingSessionState()
        )
    )

    assert memory.upserts == 0


async def collect_agent_events(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in events]
