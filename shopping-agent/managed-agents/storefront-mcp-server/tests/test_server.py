# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0
# Modified by ReByteAI in 2026 to integrate the Rebyte managed Agent API.

"""The storefront server's result mapping and executor scoping."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from storefront_mcp_server import build_server, mount_storefront_mcp

from commerce_common.memory import InMemoryMemoryStore
from commerce_common.skills import SkillRegistry
from commerce_common.testing import result_text
from shopping_agent import ShoppingAgentConfig, ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor, build_memory
from shopping_agent.fencing import STOREFRONT_FENCE
from shopping_agent.gates import provenance_error

YOGA_MAT = "AR-1301"
HEADPHONES = "AR-1105"
ESPRESSO_MACHINE = "AR-1002"


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
        unseen = await second.call_tool("add_to_cart", {"product_id": HEADPHONES, "quantity": 1})
        assert result_text(unseen) == provenance_error(HEADPHONES)
        updated = await second.call_tool(
            "update_cart_item", {"product_id": YOGA_MAT, "quantity": 3}
        )
        assert "Updated quantity" in result_text(updated)
        removed = await second.call_tool("remove_from_cart", {"product_id": YOGA_MAT})
        assert "Removed" in result_text(removed)


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
):
    headers = {
        "host": "localhost:8000",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "x-rebyte-workspace-id": "agent-workspace",
    }
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


def test_rebyte_mount_shares_one_executor_per_conversation_and_has_no_presentation_tools():
    from retail.api.mock_retail import MockRetail

    backend = MockRetail()
    config = ShoppingAgentConfig()
    memory = build_memory(config, InMemoryMemoryStore())
    executors: dict[str, ShoppingToolExecutor] = {}

    def executor_for_scope(scope: str) -> ShoppingToolExecutor:
        if scope not in executors:
            executors[scope] = ShoppingToolExecutor(
                backend=backend,
                config=config,
                skills=SkillRegistry([]),
                session=ShoppingSessionContext(session_id=scope, user_id="demo-user"),
                state=ShoppingSessionState(),
                memory=memory,
                inline_context=True,
            )
        return executors[scope]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield

    app = FastAPI(lifespan=lifespan)
    mount_storefront_mcp(
        app,
        backend=backend,
        memory_store=memory.store,
        executor_for_scope=executor_for_scope,
    )

    with TestClient(app) as client:
        searched = _http_tool_call(
            client, "conversation-a", 1, "search_products", {"query": "yoga mat"}
        )
        assert YOGA_MAT in result_text(searched)
        added = _http_tool_call(
            client,
            "conversation-a",
            10,
            "add_to_cart",
            {"product_id": YOGA_MAT, "quantity": 1},
        )
        assert f"Added {YOGA_MAT}" in result_text(added)
        unseen = _http_tool_call(
            client,
            "conversation-b",
            20,
            "add_to_cart",
            {"product_id": YOGA_MAT, "quantity": 1},
        )
        assert result_text(unseen) == provenance_error(YOGA_MAT)
        unknown = _http_tool_call(
            client,
            "conversation-a",
            30,
            "present_products",
            {"picks": [{"product_id": YOGA_MAT}]},
        )
        assert unknown["isError"] is True

        missing = _http_tool_call(client, None, 40, "search_products", {"query": "yoga mat"})
        assert missing["isError"] is True
        assert "X-Rebyte-Conversation-Id" in result_text(missing)
