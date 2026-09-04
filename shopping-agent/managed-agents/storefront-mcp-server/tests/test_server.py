# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
# Modified by ReByteAI in 2026 to integrate the Rebyte managed Agent API.

"""The storefront server's own surface: the result mapping and per-connection provenance."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from storefront_mcp_server import (
    NEVER_ASK_META,
    PRESENTATION_TOOL_NAMES,
    build_server,
    mount_storefront_mcp,
)

from commerce_common.memory import InMemoryMemoryStore
from commerce_common.testing import result_text
from shopping_agent import ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor
from shopping_agent.fencing import STOREFRONT_FENCE
from shopping_agent.gates import provenance_error

YOGA_MAT = "AR-1301"  # returned by a "yoga mat" search of the retail fixture
HEADPHONES = "AR-1105"  # returned by a "headphones" search
ESPRESSO_MACHINE = "AR-1002"  # a "coffee maker" match priced well over 100


def server():
    return build_server(memory_store=InMemoryMemoryStore())


async def test_held_calls_are_plain_results_failures_set_is_error_and_reads_are_fenced():
    async with create_connected_server_and_client_session(server()) as client:
        held = await client.call_tool("add_to_cart", {"product_id": YOGA_MAT, "quantity": 1})
        assert not held.isError and result_text(held) == provenance_error(YOGA_MAT)
        failed = await client.call_tool("get_product_details", {"product_id": "AR-00000"})
        assert failed.isError
        search = await client.call_tool("search_products", {"query": "yoga mat"})
        assert STOREFRONT_FENCE.open in result_text(search) and YOGA_MAT in result_text(search)
        # The structured filters argument reaches the executor as sent.
        unfiltered = await client.call_tool("search_products", {"query": "coffee maker"})
        assert ESPRESSO_MACHINE in result_text(unfiltered)
        filtered = await client.call_tool(
            "search_products", {"query": "coffee maker", "filters": {"max_price": 100}}
        )
        assert not filtered.isError and ESPRESSO_MACHINE not in result_text(filtered)
        added = await client.call_tool("add_to_cart", {"product_id": YOGA_MAT, "quantity": 1})
        assert not added.isError and f"Added {YOGA_MAT}" in result_text(added)


async def test_provenance_is_scoped_to_the_connection_but_the_cart_is_shared():
    shared = server()
    async with create_connected_server_and_client_session(shared) as first:
        await first.call_tool("search_products", {"query": "yoga mat"})
        await first.call_tool("search_products", {"query": "headphones"})
        await first.call_tool("add_to_cart", {"product_id": YOGA_MAT, "quantity": 1})
    async with create_connected_server_and_client_session(shared) as second:
        # The first connection saw the headphones; this one did not.
        unseen = await second.call_tool("add_to_cart", {"product_id": HEADPHONES, "quantity": 1})
        assert result_text(unseen) == provenance_error(HEADPHONES)
        # The line the first connection added grants cart-membership edits.
        updated = await second.call_tool(
            "update_cart_item", {"product_id": YOGA_MAT, "quantity": 3}
        )
        assert "Updated quantity" in result_text(updated)
        removed = await second.call_tool("remove_from_cart", {"product_id": YOGA_MAT})
        assert "Removed" in result_text(removed)


async def test_rebyte_mode_publishes_presentation_tools_and_never_ask_without_creating_state():
    created: list[ShoppingToolExecutor] = []

    class CountingExecutor(ShoppingToolExecutor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.append(self)

    rebyte_server = build_server(
        memory_store=InMemoryMemoryStore(),
        executor_class=CountingExecutor,
        include_presentation_tools=True,
    )
    async with create_connected_server_and_client_session(rebyte_server) as client:
        listed = await client.list_tools()
        assert not created
        assert {tool.name for tool in listed.tools} >= PRESENTATION_TOOL_NAMES
        assert all(tool.meta == NEVER_ASK_META for tool in listed.tools)

        await client.call_tool("search_products", {"query": "yoga mat"})
        assert len(created) == 1


async def test_rebyte_mode_returns_only_model_text_and_reuses_state_across_connections():
    state = ShoppingSessionState()
    rebyte_server = build_server(
        memory_store=InMemoryMemoryStore(),
        session=ShoppingSessionContext(session_id="local-runtime", user_id="demo-user"),
        state=state,
        include_presentation_tools=True,
    )
    async with create_connected_server_and_client_session(rebyte_server) as first:
        search = await first.call_tool("search_products", {"query": "yoga mat"})
        assert not search.isError
        assert YOGA_MAT in result_text(search)
        added = await first.call_tool("add_to_cart", {"product_id": YOGA_MAT, "quantity": 1})
        assert result_text(added).startswith(f"Added {YOGA_MAT}")
    assert YOGA_MAT in state.seen_products

    async with create_connected_server_and_client_session(rebyte_server) as second:
        presented = await second.call_tool(
            "present_products",
            {"title": "Yoga picks", "picks": [{"product_id": YOGA_MAT, "reason": "Grippy"}]},
        )
    assert not presented.isError
    assert result_text(presented) == "Displayed to the customer."
    wire_result = json.dumps(presented.model_dump(mode="json"))
    assert '"events"' not in wire_result
    assert '"payload"' not in wire_result


def _rpc_data(response) -> dict:
    response.raise_for_status()
    messages = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert messages
    return messages[-1]


def _http_tool_call(
    client: TestClient,
    conversation_scope: str | None,
    request_id: int,
    name: str,
    arguments: dict,
    *,
    workspace_scope: str = "agent-workspace",
):
    headers = {
        "host": "localhost:8000",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    headers["x-rebyte-workspace-id"] = workspace_scope
    if conversation_scope is not None:
        headers["x-rebyte-conversation-id"] = conversation_scope
    initialized = client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "storefront-test", "version": "1"},
            },
        },
    )
    _rpc_data(initialized)
    session_headers = headers | {
        "mcp-session-id": initialized.headers["mcp-session-id"],
        "mcp-protocol-version": "2025-06-18",
    }
    ready = client.post(
        "/mcp/",
        headers=session_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    )
    assert ready.status_code == 202
    called = client.post(
        "/mcp/",
        headers=session_headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id + 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    return _rpc_data(called)["result"]


def test_mount_uses_conversation_header_across_connections_and_isolates_conversations():
    lifespan_events: list[str] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        lifespan_events.append("started")
        yield
        lifespan_events.append("stopped")

    app = FastAPI(lifespan=lifespan)
    mount_storefront_mcp(app, memory_store=InMemoryMemoryStore(), scope="fallback")

    with TestClient(app) as client:
        assert lifespan_events == ["started"]
        searched = _http_tool_call(
            client, "conversation-a", 1, "search_products", {"query": "yoga mat"}
        )
        assert YOGA_MAT in result_text(searched)

        # This is a fresh MCP session: provenance survives because the Conversation matches.
        shown = _http_tool_call(
            client,
            "conversation-a",
            10,
            "present_products",
            {"picks": [{"product_id": YOGA_MAT}]},
        )
        assert result_text(shown) == "Displayed to the customer."
        assert '"events"' not in json.dumps(shown)
        assert '"payload"' not in json.dumps(shown)

        # A different Conversation in the same Agent workspace gets its own executor and
        # provenance record.
        unseen = _http_tool_call(
            client,
            "conversation-b",
            20,
            "present_products",
            {"picks": [{"product_id": YOGA_MAT}]},
        )
        assert "catalog results" in result_text(unseen)
        assert "Search first" in result_text(unseen)

        # A workspace header alone must never select per-Conversation state on HTTP. The
        # missing Conversation-header failure is an MCP tool error.
        missing_header = _http_tool_call(client, None, 30, "search_products", {"query": "yoga mat"})
        assert missing_header["isError"] is True
        assert "X-Rebyte-Conversation-Id" in result_text(missing_header)

    assert lifespan_events == ["started", "stopped"]
