# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from commerce_common.memory import InMemoryMemoryStore
from demo_common.rebyte_responses import RebyteFunctionCall, RebyteResponsesAdapter
from demo_common.rebyte_shopping import RebyteShoppingAgent
from retail.api.mock_retail import MockRetail
from shopping_agent import ShoppingSessionContext, ShoppingSessionState

CONVERSATION = "conv_33333333-3333-4333-8333-333333333333"
RUNTIME_SCOPE = "33333333-3333-4333-8333-333333333333"
YOGA_MAT = "AR-1301"


class FakeStream:
    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "response.created",
            "response": {
                "id": "resp-1",
                "conversation": {"id": CONVERSATION},
                "output": [],
                "usage": None,
            },
        }
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp-1",
                "conversation": {"id": CONVERSATION},
                "output": [],
                "usage": None,
            },
        }

    async def close(self) -> None:
        pass


class FakeResponseResource:
    async def create(self, **_: Any) -> AsyncIterator[Any]:
        return FakeStream()


class FakeResponseClient:
    def __init__(self) -> None:
        self.responses = FakeResponseResource()


async def test_conversation_binds_browser_state_to_shared_runtime_executor():
    adapter = RebyteResponsesAdapter(agent_id="agent-1", client=FakeResponseClient())
    agent = RebyteShoppingAgent(
        backend=MockRetail(), responses=adapter, memory_store=InMemoryMemoryStore()
    )
    state = ShoppingSessionState()
    session = ShoppingSessionContext(session_id="browser-session", user_id="demo-user")

    events = [
        event
        async for event in agent.stream_turn([{"role": "user", "content": "hello"}], session, state)
    ]

    assert [event.type for event in events] == ["turn_complete"]
    assert agent.runtime_scope("browser-session") == RUNTIME_SCOPE
    executor = agent.runtime_executor(RUNTIME_SCOPE)
    await executor.execute("search_products", {"query": "yoga mat"})
    assert YOGA_MAT in state.seen_products
    assert agent.runtime_executor(RUNTIME_SCOPE) is executor


async def test_presentation_client_tool_validates_enriches_and_returns_ui():
    adapter = RebyteResponsesAdapter(agent_id="agent-1", client=FakeResponseClient())
    agent = RebyteShoppingAgent(
        backend=MockRetail(), responses=adapter, memory_store=InMemoryMemoryStore()
    )
    state = ShoppingSessionState()
    session = ShoppingSessionContext(session_id="browser-session", user_id="demo-user")
    async for _ in agent.stream_turn([{"role": "user", "content": "hello"}], session, state):
        pass
    await agent.runtime_executor(RUNTIME_SCOPE).execute("search_products", {"query": "yoga mat"})

    result = await agent._execute_client_tool(
        "browser-session",
        RebyteFunctionCall(
            id="item-1",
            call_id="call-1",
            name="present_products",
            arguments={
                "title": "Yoga picks",
                "layout": None,
                "picks": [{"product_id": YOGA_MAT, "reason": None}],
            },
        ),
    )

    assert result.output == "Displayed to the customer."
    assert not result.is_error
    assert len(result.events) == 1
    rendered = result.events[0]
    assert rendered.type == "ui"
    assert rendered.data["component"] == "products"
    assert rendered.data["payload"]["items"][0]["product"]["product_id"] == YOGA_MAT


async def test_forget_session_drops_conversation_runtime():
    adapter = RebyteResponsesAdapter(agent_id="agent-1", client=FakeResponseClient())
    agent = RebyteShoppingAgent(
        backend=MockRetail(), responses=adapter, memory_store=InMemoryMemoryStore()
    )
    session = ShoppingSessionContext(session_id="browser-session", user_id="demo-user")
    async for _ in agent.stream_turn(
        [{"role": "user", "content": "hello"}], session, ShoppingSessionState()
    ):
        pass
    executor = agent.runtime_executor(RUNTIME_SCOPE)
    await agent.forget_session("browser-session")
    assert agent.runtime_scope("browser-session") is None
    assert agent.runtime_executor(RUNTIME_SCOPE) is not executor


async def test_unconfigured_agent_returns_browser_safe_setup_error(monkeypatch):
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
