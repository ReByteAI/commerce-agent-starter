# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""The retail host's thin adapter for one pre-created Rebyte Agent.

Rebyte owns the agent loop and the Conversation. The existing commerce host still
executes presentation tools and renders its original ``AgentEvent`` protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from openai import AsyncOpenAI

from commerce_common.memory import MemoryStore, MemoryWriteFilter
from commerce_common.skills import SkillRegistry
from commerce_common.streaming import AgentEvent, ToolOutcome
from commerce_common.turn import latest_user_text, outcome_events, usage_totals
from shopping_agent import ShoppingAgentConfig, ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor, build_memory

DEFAULT_REBYTE_BASE_URL = "https://api.rebyte.ai"
COMMERCE_RESULT_META = "commerce-agent"


@dataclass(frozen=True)
class _StepComplete:
    continuation: list[dict[str, str]] | None
    usage: dict[str, int]


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except ValueError:
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _without_nulls(value: Any) -> Any:
    """Restore optional-field defaults after strict tool-schema decoding."""
    if isinstance(value, Mapping):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_nulls(item) for item in value]
    return value


def _result_text(value: Any) -> str:
    """Readable text from the Responses API's serialized MCP content blocks."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return _result_text(decoded)
    if isinstance(value, Mapping):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"]
        for key in ("content", "output"):
            if key in value:
                return _result_text(value[key])
    if isinstance(value, list):
        parts = [_result_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _commerce_result_meta(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            return _commerce_result_meta(json.loads(value))
        except ValueError:
            return {}
    if isinstance(value, list):
        for block in value:
            found = _commerce_result_meta(block)
            if found:
                return found
        return {}
    if not isinstance(value, Mapping):
        return {}
    metadata = value.get("_meta")
    if isinstance(metadata, Mapping):
        result = metadata.get(COMMERCE_RESULT_META)
        if isinstance(result, Mapping):
            return dict(result)
    return _commerce_result_meta(value.get("content")) if "content" in value else {}


def _mcp_outcome(item: Any) -> tuple[str, str, ToolOutcome]:
    name = str(_field(item, "name", "tool"))
    call_id = str(_field(item, "id", name))
    status = str(_field(item, "status", "completed"))
    error = _field(item, "error")
    output = _field(item, "output", "")
    metadata = _commerce_result_meta(output)
    events: list[AgentEvent] = []
    raw_events = metadata.get("events", [])
    if isinstance(raw_events, list):
        events = [AgentEvent.model_validate(event) for event in raw_events]
    blocked = metadata.get("blocked")
    if not isinstance(blocked, str) or not blocked:
        blocked = None
    failed = bool(metadata.get("is_error")) or status == "failed" or bool(error)
    rendered = _result_text(output)
    if not rendered:
        rendered = str(error or f"MCP call ended with status {status}")
    return (
        name,
        call_id,
        ToolOutcome(
            rendered,
            events=events,
            is_error=failed,
            blocked=blocked,
        ),
    )


def _response_usage(response: Any) -> dict[str, int]:
    totals = usage_totals()
    usage = _field(response, "usage")
    if usage is None:
        return totals
    totals["input_tokens"] = int(_field(usage, "input_tokens", 0) or 0)
    totals["output_tokens"] = int(_field(usage, "output_tokens", 0) or 0)
    input_details = _field(usage, "input_tokens_details")
    totals["cache_read_input_tokens"] = int(_field(input_details, "cached_tokens", 0) or 0)
    totals["cache_creation_input_tokens"] = int(_field(input_details, "cache_write_tokens", 0) or 0)
    return totals


class RebyteShoppingAgent:
    """The original storefront host surface, backed by Rebyte Responses."""

    def __init__(
        self,
        *,
        backend: Any,
        skills: SkillRegistry | None = None,
        skills_dir: Path | None = None,
        config: ShoppingAgentConfig | None = None,
        memory_store: MemoryStore | None = None,
        memory_write_filter: MemoryWriteFilter | None = None,
        executor_class: type[ShoppingToolExecutor] = ShoppingToolExecutor,
        client: Any = None,
    ) -> None:
        if skills is None:
            skills = SkillRegistry.from_dir(skills_dir) if skills_dir else SkillRegistry([])
        self.backend = backend
        self.config = config or ShoppingAgentConfig()
        if agent_id := os.environ.get("REBYTE_AGENT_ID", "").strip():
            # The host health route reports the identifier sent as Responses `model`.
            self.config = self.config.model_copy(update={"model": agent_id})
        self.skills = skills
        self.executor_class = executor_class
        self.extra_presentation_tools: tuple[Any, ...] = ()
        self.memory = build_memory(self.config, memory_store, memory_write_filter)
        self._client = client
        self._executors: dict[str, ShoppingToolExecutor] = {}
        self._conversations: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def configured(self) -> bool:
        """Whether chat credentials are present; catalog-only routes need none."""
        return bool(
            os.environ.get("REBYTE_API_KEY", "").strip()
            and os.environ.get("REBYTE_AGENT_ID", "").strip()
        )

    @staticmethod
    def _settings() -> tuple[str, str, str]:
        api_key = os.environ.get("REBYTE_API_KEY", "").strip()
        agent_id = os.environ.get("REBYTE_AGENT_ID", "").strip()
        base_url = os.environ.get("REBYTE_BASE_URL", DEFAULT_REBYTE_BASE_URL).strip().rstrip("/")
        if not api_key:
            raise ValueError("REBYTE_API_KEY is required")
        if not agent_id:
            raise ValueError("REBYTE_AGENT_ID is required")
        if not base_url:
            raise ValueError("REBYTE_BASE_URL cannot be empty")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        return api_key, agent_id, base_url

    def _responses_client(self) -> tuple[Any, str]:
        api_key, agent_id, base_url = self._settings()
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=self.config.request_timeout_s,
                max_retries=2,
            )
        return self._client, agent_id

    async def _conversation_for_session(self, browser_session_id: str) -> str:
        existing = self._conversations.get(browser_session_id)
        if existing is not None:
            return existing

        api_key, agent_id, base_url = self._settings()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{base_url}/sessions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"agentId": agent_id},
            )
        response.raise_for_status()
        session_id = response.json().get("session", {}).get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Rebyte Sessions API returned no session id")
        conversation = f"conv_{session_id}"
        self._conversations[browser_session_id] = conversation
        return conversation

    def executor_for_conversation(self, conversation_id: str) -> ShoppingToolExecutor:
        """Executor used by the MCP endpoint during the active Responses turn."""
        try:
            return self._executors[conversation_id]
        except KeyError as error:
            raise ValueError(
                f"No active host session for Conversation {conversation_id}"
            ) from error

    def _executor(
        self, session: ShoppingSessionContext, state: ShoppingSessionState
    ) -> ShoppingToolExecutor:
        return self.executor_class(
            backend=self.backend,
            config=self.config,
            skills=self.skills,
            session=session,
            state=state,
            memory=self.memory,
            inline_context=True,
        )

    async def _stream_step(
        self,
        *,
        conversation: str,
        input_value: str | list[dict[str, str]],
        executor: ShoppingToolExecutor,
    ) -> AsyncIterator[AgentEvent | _StepComplete]:
        client, agent_id = self._responses_client()
        stream = await client.responses.create(
            model=agent_id,
            conversation=conversation,
            input=input_value,
            stream=True,
            extra_headers={"Idempotency-Key": str(uuid4())},
        )
        mcp_calls: dict[str, dict[str, Any]] = {}
        function_calls: dict[str, dict[str, Any]] = {}
        announced: set[str] = set()
        function_outputs: list[dict[str, str]] = []
        terminal: str | None = None
        terminal_response: Any = None

        async for event in stream:
            kind = str(_field(event, "type", ""))
            if kind == "response.output_text.delta":
                delta = _field(event, "delta")
                if isinstance(delta, str) and delta:
                    yield AgentEvent.text_delta(delta)
                continue

            if kind == "response.output_item.added":
                item = _field(event, "item")
                item_type = _field(item, "type")
                if item_type == "mcp_call":
                    item_id = str(_field(item, "id", ""))
                    if item_id:
                        mcp_calls[item_id] = {
                            "id": item_id,
                            "name": _field(item, "name", "tool"),
                            "arguments": "",
                        }
                elif item_type == "function_call":
                    item_id = str(_field(item, "id", ""))
                    if item_id:
                        call = {
                            "id": item_id,
                            "call_id": str(_field(item, "call_id", item_id)),
                            "name": str(_field(item, "name", "tool")),
                            "arguments": "",
                        }
                        function_calls[item_id] = call
                        announced.add(item_id)
                        yield AgentEvent.tool_call(call["name"], call["call_id"], {})
                continue

            if kind in {"response.mcp_call_arguments.delta", "response.mcp_call_arguments.done"}:
                item_id = str(_field(event, "item_id", ""))
                if not item_id:
                    continue
                call = mcp_calls.setdefault(
                    item_id, {"id": item_id, "name": "tool", "arguments": ""}
                )
                if kind.endswith(".delta"):
                    call["arguments"] += str(_field(event, "delta", ""))
                else:
                    call["arguments"] = str(_field(event, "arguments", call["arguments"]))
                continue

            if kind in {
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
            }:
                item_id = str(_field(event, "item_id", ""))
                if not item_id:
                    continue
                call = function_calls.setdefault(
                    item_id,
                    {"id": item_id, "call_id": item_id, "name": "tool", "arguments": ""},
                )
                if kind.endswith(".delta"):
                    call["arguments"] += str(_field(event, "delta", ""))
                else:
                    call["arguments"] = str(_field(event, "arguments", call["arguments"]))
                continue

            if kind == "response.mcp_call.in_progress":
                item_id = str(_field(event, "item_id", ""))
                call = mcp_calls.get(item_id)
                if call is not None and item_id not in announced:
                    announced.add(item_id)
                    yield AgentEvent.tool_call(
                        str(call["name"]), item_id, _json_object(call["arguments"])
                    )
                continue

            if kind == "response.output_item.done":
                item = _field(event, "item")
                item_type = _field(item, "type")
                item_id = str(_field(item, "id", ""))
                if item_type == "mcp_call":
                    if item_id not in announced:
                        yield AgentEvent.tool_call(
                            str(_field(item, "name", "tool")),
                            item_id,
                            _json_object(_field(item, "arguments", "")),
                        )
                    name, call_id, outcome = _mcp_outcome(item)
                    for host_event in outcome_events(name, call_id, outcome):
                        yield host_event
                elif item_type == "function_call":
                    name = str(_field(item, "name", "tool"))
                    call_id = str(_field(item, "call_id", item_id))
                    arguments = _without_nulls(_json_object(_field(item, "arguments", "")))
                    if item_id not in announced:
                        yield AgentEvent.tool_call(name, call_id, arguments)
                    outcome = await executor.execute(name, arguments)
                    for host_event in outcome_events(name, call_id, outcome):
                        yield host_event
                    function_outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": outcome.result_text,
                        }
                    )
                continue

            if kind in {"response.completed", "response.incomplete", "response.failed"}:
                terminal = kind
                terminal_response = _field(event, "response")
                returned = _field(_field(terminal_response, "conversation"), "id")
                if returned not in (None, conversation):
                    raise ValueError("Rebyte changed the Conversation id during a turn")
                continue

            if kind in {"error", "response.error"}:
                raise RuntimeError(str(_field(event, "message", "Rebyte Responses API error")))

        if terminal != "response.completed":
            raise RuntimeError(
                f"Rebyte Responses turn ended with {terminal or 'no terminal event'}"
            )
        yield _StepComplete(
            continuation=function_outputs or None,
            usage=_response_usage(terminal_response),
        )

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        session: ShoppingSessionContext,
        state: ShoppingSessionState | None = None,
    ) -> AsyncIterator[AgentEvent]:
        state = state if state is not None else ShoppingSessionState()
        lock = self._locks.setdefault(session.session_id, asyncio.Lock())
        async with lock:
            started = time.monotonic()
            usage = usage_totals()
            executor = self._executor(session, state)
            conversation = await self._conversation_for_session(session.session_id)
            remote_session_id = conversation.removeprefix("conv_")
            self._executors[remote_session_id] = executor
            input_value: str | list[dict[str, str]] = latest_user_text(messages)
            try:
                for _round in range(self.config.max_tool_iterations + 1):
                    continuation: list[dict[str, str]] | None = None
                    async for event in self._stream_step(
                        conversation=conversation,
                        input_value=input_value,
                        executor=executor,
                    ):
                        if isinstance(event, AgentEvent):
                            yield event
                        else:
                            continuation = event.continuation
                            for key, count in event.usage.items():
                                usage[key] += count
                    if continuation is None:
                        yield AgentEvent.turn_complete(
                            "end_turn",
                            usage,
                            round((time.monotonic() - started) * 1000),
                            0,
                        )
                        return
                    input_value = continuation
                raise RuntimeError("Rebyte Agent exceeded the client-tool continuation limit")
            finally:
                self._executors.pop(remote_session_id, None)

    async def update_memory(
        self, messages: list[dict[str, Any]], session: ShoppingSessionContext
    ) -> list[Any]:
        del messages, session
        return []
