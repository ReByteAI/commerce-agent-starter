# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import AsyncIterator
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from demo_common import mcp_gateway

TOKEN = "local-test-token-with-at-least-32-characters"
_RUN_DEMO_SPEC = spec_from_file_location(
    "commerce_starter_run_demo", Path(__file__).resolve().parents[3] / "scripts" / "run_demo.py"
)
assert _RUN_DEMO_SPEC is not None and _RUN_DEMO_SPEC.loader is not None
run_demo = module_from_spec(_RUN_DEMO_SPEC)
_RUN_DEMO_SPEC.loader.exec_module(run_demo)


class FakeResponse:
    status_code = 200
    headers = {
        "content-type": "text/event-stream",
        "mcp-session-id": "session-1",
        "connection": "keep-alive",
    }

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        yield b'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'

    async def aclose(self) -> None:
        return None


class FakeClient:
    last_request: httpx.Request | None = None
    closed = False

    def __init__(self, **_: Any) -> None:
        pass

    def build_request(self, method: str, url: str, **kwargs: Any) -> httpx.Request:
        request = httpx.Request(method, url, **kwargs)
        type(self).last_request = request
        return request

    async def send(self, request: httpx.Request, *, stream: bool) -> FakeResponse:
        assert stream
        assert request is self.last_request
        return FakeResponse()

    async def aclose(self) -> None:
        type(self).closed = True


def test_gateway_exposes_only_authenticated_mcp(monkeypatch):
    monkeypatch.setattr(mcp_gateway.httpx, "AsyncClient", FakeClient)
    app = mcp_gateway.create_app(token=TOKEN)

    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/health").status_code == 404
        unauthorized = client.post("/mcp/", json={})
        assert unauthorized.status_code == 401
        proxied = client.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-Rebyte-Workspace-Id": "workspace-1",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )

    assert proxied.status_code == 200
    assert proxied.headers["mcp-session-id"] == "session-1"
    assert "authorization" not in FakeClient.last_request.headers
    assert FakeClient.last_request.headers["x-rebyte-workspace-id"] == "workspace-1"
    assert FakeClient.last_request.url == httpx.URL("http://127.0.0.1:8000/mcp/")
    assert FakeClient.closed


def test_gateway_rejects_non_loopback_or_non_mcp_upstreams():
    for upstream in ("https://example.com/mcp/", "http://127.0.0.1:8000/api/chat"):
        try:
            mcp_gateway.create_app(token=TOKEN, upstream_url=upstream)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe upstream {upstream}")


def test_gateway_factory_reads_only_its_token_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_gateway, "REPO_ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        f"{mcp_gateway.TOKEN_ENV}={TOKEN}\nREBYTE_API_KEY=must-not-load\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(mcp_gateway.TOKEN_ENV, raising=False)
    monkeypatch.setenv("REBYTE_API_KEY", "must-be-removed")
    monkeypatch.setenv("REBYTE_AGENT_ID", "must-be-removed")

    app = mcp_gateway.create_app_from_env()

    assert app is not None
    assert "REBYTE_API_KEY" not in mcp_gateway.os.environ
    assert "REBYTE_AGENT_ID" not in mcp_gateway.os.environ


def test_gateway_launcher_does_not_inherit_bff_or_unrelated_secrets(monkeypatch):
    captured: dict[str, Any] = {}
    process = object()

    def fake_spawn(command, cwd, env):
        captured.update(command=command, cwd=cwd, env=env)
        return process

    monkeypatch.setattr(run_demo, "spawn", fake_spawn)
    monkeypatch.setenv("REBYTE_API_KEY", "organization-secret")
    monkeypatch.setenv("REBYTE_AGENT_ID", "agent-id")
    monkeypatch.setenv("REBYTE_MCP_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setenv("UNRELATED_SECRET", "also-secret")

    assert run_demo.start_mcp_gateway(8000, 8100) is process

    assert captured["env"]["REBYTE_MCP_GATEWAY_TOKEN"] == TOKEN
    assert captured["env"]["COMMERCE_MCP_UPSTREAM_URL"] == ("http://127.0.0.1:8000/mcp/")
    assert "REBYTE_API_KEY" not in captured["env"]
    assert "REBYTE_AGENT_ID" not in captured["env"]
    assert "UNRELATED_SECRET" not in captured["env"]
