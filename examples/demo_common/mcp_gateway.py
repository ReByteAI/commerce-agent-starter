# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""Authenticated, MCP-only reverse proxy for the local commerce starter.

The storefront API holds an organization API key, so a temporary public tunnel must
never point at that process directly. This small ASGI app exposes only ``/mcp/``, checks
a shared bearer token configured on the Rebyte connector, and forwards the MCP stream to
the loopback-only storefront API.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN_ENV = "REBYTE_MCP_GATEWAY_TOKEN"
UPSTREAM_ENV = "COMMERCE_MCP_UPSTREAM_URL"
DEFAULT_UPSTREAM = "http://127.0.0.1:8000/mcp/"
_BFF_ONLY_ENV = ("REBYTE_API_KEY", "REBYTE_AGENT_ID", "REBYTE_BASE_URL")

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _validated_upstream(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{UPSTREAM_ENV} must be a loopback HTTP URL")
    if parsed.path.rstrip("/") != "/mcp" or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"{UPSTREAM_ENV} must point exactly to the loopback /mcp/ endpoint")
    return value.rstrip("/") + "/"


def create_app(*, token: str, upstream_url: str = DEFAULT_UPSTREAM) -> FastAPI:
    """Build the gateway over one bearer token and one loopback MCP endpoint."""

    if len(token) < 32:
        raise ValueError(f"{TOKEN_ENV} must contain at least 32 characters")
    upstream = _validated_upstream(upstream_url)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    async def close_upstream(response: httpx.Response, client: httpx.AsyncClient) -> None:
        await response.aclose()
        await client.aclose()

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    @app.api_route("/mcp/", methods=["GET", "POST", "DELETE"])
    async def proxy_mcp(request: Request):
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {token}"
        if not secrets.compare_digest(authorization, expected):
            return JSONResponse(
                {"error": "MCP gateway authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _HOP_BY_HOP | {"authorization", "host", "content-length"}
        }
        client = httpx.AsyncClient(timeout=None)
        try:
            upstream_request = client.build_request(
                request.method,
                upstream,
                headers=headers,
                content=await request.body(),
            )
            response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return JSONResponse({"error": "Local MCP server unavailable"}, status_code=502)

        response_headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in _HOP_BY_HOP | {"content-length"}
        }

        async def body() -> AsyncIterator[bytes]:
            async for chunk in response.aiter_raw():
                yield chunk

        return StreamingResponse(
            body(),
            status_code=response.status_code,
            headers=response_headers,
            background=BackgroundTask(close_upstream, response, client),
        )

    return app


def create_app_from_env() -> FastAPI:
    """Uvicorn factory that reads only its token from the ignored ``.env``.

    The BFF's Rebyte credentials are deliberately removed from this process. The normal
    launcher also prevents them from entering the gateway environment in the first place.
    """

    values = dotenv_values(REPO_ROOT / ".env")
    token = os.environ.get(TOKEN_ENV)
    if token is None:
        file_token = values.get(TOKEN_ENV)
        token = file_token if isinstance(file_token, str) else ""
    for name in _BFF_ONLY_ENV:
        os.environ.pop(name, None)
    if not token:
        raise ValueError(f"{TOKEN_ENV} is required; see the repo-root README")
    return create_app(token=token, upstream_url=os.environ.get(UPSTREAM_ENV, DEFAULT_UPSTREAM))
