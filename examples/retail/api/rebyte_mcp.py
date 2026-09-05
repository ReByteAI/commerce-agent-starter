# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""Mount the retail tools for calls made by one Rebyte Agent Conversation."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent

from commerce_common.streaming import ToolOutcome
from shopping_agent.executor import ShoppingToolExecutor

REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_SERVER_DIR = REPO_ROOT / "shopping-agent" / "managed-agents" / "storefront-mcp-server"
sys.path.insert(0, str(MCP_SERVER_DIR))
from storefront_mcp_server import build_server  # noqa: E402

REBYTE_CONVERSATION_HEADER = "X-Rebyte-Conversation-Id"
COMMERCE_RESULT_META = "commerce-agent"


def _call_result(outcome: ToolOutcome) -> CallToolResult:
    metadata = {
        COMMERCE_RESULT_META: {
            "events": [event.model_dump(mode="json") for event in outcome.events],
            "is_error": outcome.is_error,
            "blocked": outcome.blocked,
        }
    }
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=outcome.result_text,
                _meta=metadata,
            )
        ],
        structuredContent={"result": outcome.result_text},
        isError=outcome.is_error,
    )


class RebyteConversationTools:
    def __init__(self, executor_for_conversation: Any) -> None:
        self._executor_for_conversation = executor_for_conversation

    async def call(
        self,
        ctx: Context,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        request = ctx.request_context.request
        headers = getattr(request, "headers", None)
        conversation_id = headers.get(REBYTE_CONVERSATION_HEADER) if headers else None
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError(f"missing {REBYTE_CONVERSATION_HEADER} header")
        executor: ShoppingToolExecutor = self._executor_for_conversation(conversation_id.strip())
        return _call_result(await executor.execute(name, arguments))


def mount_rebyte_storefront_mcp(
    app: FastAPI,
    *,
    agent: Any,
    backend: Any,
    path: str = "/mcp",
) -> FastMCP:
    """Mount the MCP transport without changing the original storefront routes."""
    mount_path = "/" + path.strip("/")
    if mount_path == "/":
        raise ValueError("path must name a mount point")
    server = build_server(
        backend,
        config=agent.config,
        tool_caller=RebyteConversationTools(agent.executor_for_conversation),
        streamable_http_path="/",
        # The outer FastAPI app already validates Host through
        # TrustedHostMiddleware, including DEMO_ALLOWED_HOSTS.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with original_lifespan(application), server.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    app.mount(mount_path, server.streamable_http_app(), name="storefront-mcp")
    return server
