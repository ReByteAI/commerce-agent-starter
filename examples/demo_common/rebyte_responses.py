# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""Translate a managed Rebyte Response into the demo's ``AgentEvent`` stream.

The Agent owns its MCP and client-tool definitions. Rebyte executes MCP tools. When the
Agent calls a client tool, this adapter hands the call to the storefront host, submits a
standard ``function_call_output`` to the same Conversation, and continues streaming.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from openai import AsyncOpenAI

from commerce_common.streaming import AgentEvent

logger = logging.getLogger(__name__)

DEFAULT_REBYTE_BASE_URL = "https://api.rebyte.ai"
MAX_CLIENT_TOOL_CONTINUATION_ROUNDS = 8
_SUMMARY_MAX_CHARS = 300
_EXCERPT_MAX_CHARS = 500
_SAFE_TURN_ERROR = "The Rebyte Agent request failed. Please try again."


class _ResponsesResource(Protocol):
    async def create(self, **kwargs: Any) -> AsyncIterator[Any]: ...


class ResponsesClient(Protocol):
    responses: _ResponsesResource


class ClientToolContinuationLimitError(RuntimeError):
    """The Agent kept requesting client tools beyond the host's finite limit."""


@dataclass(frozen=True)
class RebyteMcpCall:
    """One server-executed MCP call normalized from a Responses stream."""

    id: str
    server_label: str
    name: str
    arguments: dict[str, Any]
    output: Any
    error: str | None
    status: str


@dataclass(frozen=True)
class RebyteFunctionCall:
    """One client-executed function call emitted by the managed Agent."""

    id: str
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ClientToolResult:
    """The model-facing output and browser events produced by a client tool."""

    output: str
    events: tuple[AgentEvent, ...] = ()
    is_error: bool = False
    blocked: str | None = None


ClientToolHandler = Callable[
    [str, RebyteFunctionCall], ClientToolResult | Awaitable[ClientToolResult]
]
ConversationHandler = Callable[[str, str], None]


@dataclass
class _McpState:
    id: str
    server_label: str = "rebyte"
    name: str = "tool"
    arguments: str = ""
    output: Any = None
    error: str | None = None
    status: str = "in_progress"
    announced: bool = False
    completed: bool = False


@dataclass
class _FunctionState:
    id: str
    call_id: str = ""
    name: str = "tool"
    arguments: str = ""
    announced: bool = False
    completed: bool = False


@dataclass
class _ResponseStep:
    function_outputs: list[dict[str, Any]] = field(default_factory=list)
    terminal_kind: str | None = None
    terminal_response: Any = None


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _event_type(event: Any) -> str:
    value = _field(event, "type", "")
    return value if isinstance(value, str) else ""


def _string(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _decoded_json(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return default if value is None else value
    if not value.strip():
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _arguments(value: Any) -> dict[str, Any]:
    decoded = _decoded_json(value, {})
    return dict(decoded) if isinstance(decoded, Mapping) else {}


_CONVERSATION_ID = re.compile(
    r"^conv_([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def _runtime_scope_from_conversation(conversation_id: str) -> str:
    matched = _CONVERSATION_ID.fullmatch(conversation_id)
    if matched is None:
        raise ValueError("Rebyte Conversation ID must be conv_ followed by a UUID")
    return matched.group(1).lower()


def _content_block_text(value: Any, *, _depth: int = 0) -> str | None:
    if _depth > 5:
        return None
    if isinstance(value, str):
        decoded = _decoded_json(value, None)
        if isinstance(decoded, (Mapping, list, tuple)):
            nested = _content_block_text(decoded, _depth=_depth + 1)
            if nested is not None:
                return nested
        return value
    if isinstance(value, Mapping):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"]
        for key in ("content", "output"):
            if key in value:
                nested = _content_block_text(value[key], _depth=_depth + 1)
                if nested is not None:
                    return nested
        return None
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        parts = [
            text
            for item in value
            if (text := _content_block_text(item, _depth=_depth + 1)) is not None
        ]
        return "\n".join(parts) if parts else None
    return None


def _openai_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("Rebyte base URL cannot be empty")
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


def _conversation_id(response: Any) -> str | None:
    conversation = _field(response, "conversation")
    if isinstance(conversation, str):
        return conversation or None
    candidate = _field(conversation, "id")
    return candidate if isinstance(candidate, str) and candidate else None


def _error_message(response: Any, fallback: str) -> str:
    error = _field(response, "error")
    message = _field(error, "message")
    return message if isinstance(message, str) and message else fallback


def _usage(response: Any) -> dict[str, int]:
    usage = _field(response, "usage")
    if usage is None:
        return {}
    result: dict[str, int] = {}
    for source, target in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = _field(usage, source)
        if isinstance(value, int):
            result[target] = value
    cached = _field(_field(usage, "input_tokens_details"), "cached_tokens")
    if isinstance(cached, int):
        result["cache_read_input_tokens"] = cached
    return result


def _add_usage(total: dict[str, int], response: Any) -> None:
    for name, value in _usage(response).items():
        total[name] = total.get(name, 0) + value


def _result_summary(
    output: Any, *, is_error: bool = False, blocked: str | None = None
) -> tuple[str, str | None, str, str | None]:
    if blocked is not None:
        return str(output), None, "blocked", blocked
    rendered = _content_block_text(output)
    if rendered is None:
        rendered = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    status = "error" if is_error else "ok"
    if len(rendered) <= _SUMMARY_MAX_CHARS:
        return rendered, None, status, None
    summary = "Tool execution failed." if is_error else "ok"
    return summary, rendered[:_EXCERPT_MAX_CHARS], status, None


class RebyteResponsesAdapter:
    """A thin Responses client with browser-session to Conversation mapping."""

    def __init__(
        self,
        *,
        agent_id: str,
        api_key: str | None = None,
        base_url: str = DEFAULT_REBYTE_BASE_URL,
        client: ResponsesClient | None = None,
        client_tool_handler: ClientToolHandler | None = None,
        conversation_handler: ConversationHandler | None = None,
    ) -> None:
        if not agent_id.strip():
            raise ValueError("Rebyte Agent ID cannot be empty")
        if client is None and not api_key:
            raise ValueError("Rebyte API key cannot be empty")
        self.agent_id = agent_id
        self.base_url = _openai_base_url(base_url)
        self.client: ResponsesClient = client or cast(
            ResponsesClient,
            AsyncOpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=6 * 60,
                max_retries=2,
            ),
        )
        self.client_tool_handler = client_tool_handler
        self.conversation_handler = conversation_handler
        self._conversations: dict[str, str] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @classmethod
    def from_env(
        cls,
        *,
        client_tool_handler: ClientToolHandler | None = None,
        conversation_handler: ConversationHandler | None = None,
    ) -> RebyteResponsesAdapter:
        api_key = os.environ.get("REBYTE_API_KEY")
        agent_id = os.environ.get("REBYTE_AGENT_ID")
        if not api_key:
            raise ValueError("REBYTE_API_KEY is required")
        if not agent_id:
            raise ValueError("REBYTE_AGENT_ID is required")
        return cls(
            api_key=api_key,
            agent_id=agent_id,
            base_url=os.environ.get("REBYTE_BASE_URL", DEFAULT_REBYTE_BASE_URL),
            client_tool_handler=client_tool_handler,
            conversation_handler=conversation_handler,
        )

    def conversation_id(self, local_session_id: str) -> str | None:
        return self._conversations.get(local_session_id)

    def runtime_scope(self, local_session_id: str) -> str | None:
        conversation_id = self._conversations.get(local_session_id)
        return (
            None if conversation_id is None else _runtime_scope_from_conversation(conversation_id)
        )

    def bind_conversation(self, local_session_id: str, conversation_id: str) -> None:
        if not local_session_id or not conversation_id:
            raise ValueError("Session and Conversation IDs cannot be empty")
        _runtime_scope_from_conversation(conversation_id)
        existing = self._conversations.get(local_session_id)
        if existing is not None and existing != conversation_id:
            raise ValueError(
                f"Local session {local_session_id} is already bound to a different Conversation"
            )
        self._conversations[local_session_id] = conversation_id
        if self.conversation_handler is not None:
            self.conversation_handler(local_session_id, conversation_id)

    async def forget_session(self, local_session_id: str) -> None:
        lock = await self._lock_for(local_session_id)
        async with lock:
            self._conversations.pop(local_session_id, None)

    async def _lock_for(self, local_session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._session_locks.setdefault(local_session_id, asyncio.Lock())

    def _remember_response(self, local_session_id: str, response: Any) -> None:
        candidate = _conversation_id(response)
        if candidate is not None:
            self.bind_conversation(local_session_id, candidate)

    @staticmethod
    def _update_mcp(state: _McpState, item: Any) -> None:
        state.server_label = _string(_field(item, "server_label"), state.server_label)
        state.name = _string(_field(item, "name"), state.name)
        arguments = _field(item, "arguments")
        if isinstance(arguments, str) and arguments:
            state.arguments = arguments
        if _field(item, "output") is not None:
            state.output = _field(item, "output")
        error = _field(item, "error")
        if isinstance(error, str) and error:
            state.error = error
        state.status = _string(_field(item, "status"), state.status)

    @staticmethod
    def _update_function(state: _FunctionState, item: Any) -> None:
        state.call_id = _string(_field(item, "call_id"), state.call_id)
        state.name = _string(_field(item, "name"), state.name)
        arguments = _field(item, "arguments")
        if isinstance(arguments, str):
            state.arguments = arguments

    @staticmethod
    def _mcp_call(state: _McpState) -> RebyteMcpCall:
        return RebyteMcpCall(
            id=state.id,
            server_label=state.server_label,
            name=state.name,
            arguments=_arguments(state.arguments),
            output=_decoded_json(state.output, state.output),
            error=state.error,
            status=state.status,
        )

    @staticmethod
    def _function_call(state: _FunctionState) -> RebyteFunctionCall:
        return RebyteFunctionCall(
            id=state.id,
            call_id=state.call_id or state.id,
            name=state.name,
            arguments=_arguments(state.arguments),
        )

    @staticmethod
    def _stamp_ui(event: AgentEvent, call_id: str) -> AgentEvent:
        if event.type not in {"ui", "ui_partial"} or "stream_id" in event.data:
            return event
        return AgentEvent(type=event.type, data={**event.data, "stream_id": call_id})

    async def _complete_mcp(self, state: _McpState) -> AsyncIterator[AgentEvent]:
        if state.completed:
            return
        state.completed = True
        call = self._mcp_call(state)
        if not state.announced:
            state.announced = True
            yield AgentEvent.tool_call(call.name, call.id, call.arguments)
        is_error = call.status == "failed"
        blocked = call.status if call.status in {"incomplete", "cancelled", "canceled"} else None
        if is_error:
            output = call.error or "Tool execution failed."
        else:
            output = call.output if call.output is not None else "ok"
        summary, excerpt, status, reason = _result_summary(
            output, is_error=is_error, blocked=blocked
        )
        yield AgentEvent.tool_result(
            call.name,
            call.id,
            summary,
            is_error=is_error,
            status=status,
            reason=reason,
            excerpt=excerpt,
        )

    async def _complete_function(
        self, local_session_id: str, state: _FunctionState, step: _ResponseStep
    ) -> AsyncIterator[AgentEvent]:
        if state.completed:
            return
        state.completed = True
        call = self._function_call(state)
        if not state.announced:
            state.announced = True
            yield AgentEvent.tool_call(call.name, call.call_id, call.arguments)

        if self.client_tool_handler is None:
            result = ClientToolResult(
                f"Client tool {call.name} is not implemented by this application.",
                is_error=True,
            )
        else:
            try:
                handled = self.client_tool_handler(local_session_id, call)
                result = await handled if inspect.isawaitable(handled) else handled
            except Exception:
                logger.exception("client tool %s failed", call.name)
                result = ClientToolResult(
                    f"Client tool {call.name} failed in the host application.",
                    is_error=True,
                )

        for event in result.events:
            yield self._stamp_ui(event, call.call_id)
        summary, excerpt, status, reason = _result_summary(
            result.output, is_error=result.is_error, blocked=result.blocked
        )
        yield AgentEvent.tool_result(
            call.name,
            call.call_id,
            summary,
            is_error=result.is_error,
            status=status,
            reason=reason,
            excerpt=excerpt,
        )
        step.function_outputs.append(
            {
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": result.output,
            }
        )

    async def _complete_snapshot_items(
        self,
        local_session_id: str,
        response: Any,
        mcp_states: dict[str, _McpState],
        function_states: dict[str, _FunctionState],
        step: _ResponseStep,
    ) -> AsyncIterator[AgentEvent]:
        output = _field(response, "output", [])
        if not isinstance(output, Iterable) or isinstance(output, (str, bytes, Mapping)):
            return
        for item in output:
            item_type = _field(item, "type")
            item_id = _string(_field(item, "id"))
            if not item_id:
                continue
            if item_type == "mcp_call":
                state = mcp_states.setdefault(item_id, _McpState(id=item_id))
                self._update_mcp(state, item)
                async for event in self._complete_mcp(state):
                    yield event
            elif item_type == "function_call":
                state = function_states.setdefault(item_id, _FunctionState(id=item_id))
                self._update_function(state, item)
                async for event in self._complete_function(local_session_id, state, step):
                    yield event

    @staticmethod
    def _progress_event(event: Any) -> AgentEvent | None:
        envelope = _field(event, "data", {})
        payload = _field(envelope, "data", envelope)
        action = _field(payload, "action", {})
        message = next(
            (
                value
                for value in (
                    _field(action, "statusMessage"),
                    _field(payload, "message"),
                    _field(payload, "text"),
                )
                if isinstance(value, str) and value.strip()
            ),
            None,
        )
        if message is None:
            return None
        tool = _field(action, "toolName") or _field(action, "functionCallName")
        return AgentEvent.progress(message, tool=tool if isinstance(tool, str) else None)

    async def _stream_response_step(
        self, local_session_id: str, request: dict[str, Any]
    ) -> AsyncIterator[AgentEvent | _ResponseStep]:
        mcp_states: dict[str, _McpState] = {}
        function_states: dict[str, _FunctionState] = {}
        step = _ResponseStep()
        stream: Any = None
        try:
            stream = await self.client.responses.create(**request)
            async for raw_event in stream:
                kind = _event_type(raw_event)
                response = _field(raw_event, "response")
                if response is not None:
                    self._remember_response(local_session_id, response)

                if kind == "response.output_text.delta":
                    delta = _field(raw_event, "delta")
                    if isinstance(delta, str) and delta:
                        yield AgentEvent.text_delta(delta)
                    continue

                if kind == "response.output_item.added":
                    item = _field(raw_event, "item")
                    item_id = _string(_field(item, "id"))
                    if not item_id:
                        continue
                    if _field(item, "type") == "mcp_call":
                        self._update_mcp(
                            mcp_states.setdefault(item_id, _McpState(id=item_id)), item
                        )
                    elif _field(item, "type") == "function_call":
                        self._update_function(
                            function_states.setdefault(item_id, _FunctionState(id=item_id)), item
                        )
                    continue

                if kind in {
                    "response.mcp_call_arguments.delta",
                    "response.mcp_call_arguments.done",
                }:
                    item_id = _string(_field(raw_event, "item_id"))
                    if not item_id:
                        continue
                    state = mcp_states.setdefault(item_id, _McpState(id=item_id))
                    if kind.endswith(".delta"):
                        state.arguments += _string(_field(raw_event, "delta"))
                    else:
                        state.arguments = _string(_field(raw_event, "arguments"), state.arguments)
                    continue

                if kind in {
                    "response.function_call_arguments.delta",
                    "response.function_call_arguments.done",
                }:
                    item_id = _string(_field(raw_event, "item_id"))
                    if not item_id:
                        continue
                    state = function_states.setdefault(item_id, _FunctionState(id=item_id))
                    if kind.endswith(".delta"):
                        state.arguments += _string(_field(raw_event, "delta"))
                    else:
                        state.arguments = _string(_field(raw_event, "arguments"), state.arguments)
                    continue

                if kind == "response.mcp_call.in_progress":
                    item_id = _string(_field(raw_event, "item_id"))
                    if item_id:
                        state = mcp_states.setdefault(item_id, _McpState(id=item_id))
                        if not state.announced:
                            state.announced = True
                            call = self._mcp_call(state)
                            yield AgentEvent.tool_call(call.name, call.id, call.arguments)
                    continue

                if kind in {"response.mcp_call.completed", "response.mcp_call.failed"}:
                    item_id = _string(_field(raw_event, "item_id"))
                    if item_id:
                        mcp_states.setdefault(item_id, _McpState(id=item_id)).status = (
                            "failed" if kind.endswith(".failed") else "completed"
                        )
                    continue

                if kind == "response.output_item.done":
                    item = _field(raw_event, "item")
                    item_id = _string(_field(item, "id"))
                    if not item_id:
                        continue
                    if _field(item, "type") == "mcp_call":
                        state = mcp_states.setdefault(item_id, _McpState(id=item_id))
                        self._update_mcp(state, item)
                        async for event in self._complete_mcp(state):
                            yield event
                    elif _field(item, "type") == "function_call":
                        state = function_states.setdefault(item_id, _FunctionState(id=item_id))
                        self._update_function(state, item)
                        async for event in self._complete_function(local_session_id, state, step):
                            yield event
                    continue

                if kind == "response.rebyte_tool_call.progress":
                    if progress := self._progress_event(raw_event):
                        yield progress
                    continue

                if kind in {"response.completed", "response.incomplete", "response.failed"}:
                    step.terminal_kind = kind
                    step.terminal_response = response
                    async for event in self._complete_snapshot_items(
                        local_session_id,
                        response,
                        mcp_states,
                        function_states,
                        step,
                    ):
                        yield event
                    break

                if kind in {"error", "response.error"}:
                    step.terminal_kind = kind
                    step.terminal_response = raw_event
                    break
            yield step
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                closed = close()
                if inspect.isawaitable(closed):
                    await closed

    async def stream_turn(
        self,
        local_session_id: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one user turn, including any client-tool continuation Responses."""

        if not local_session_id:
            raise ValueError("Local session ID cannot be empty")
        if not message.strip():
            raise ValueError("Message cannot be empty")

        lock = await self._lock_for(local_session_id)
        async with lock:
            started = time.monotonic()
            total_usage: dict[str, int] = {}
            next_input: str | list[dict[str, Any]] = message
            request_number = 0
            continuation_rounds = 0
            try:
                while True:
                    request_number += 1
                    base_key = idempotency_key or f"commerce-{uuid.uuid4()}"
                    request: dict[str, Any] = {
                        "model": self.agent_id,
                        "input": next_input,
                        "stream": True,
                        "extra_headers": {"Idempotency-Key": f"{base_key}-{request_number}"},
                    }
                    conversation_id = self._conversations.get(local_session_id)
                    if conversation_id is not None:
                        request["conversation"] = conversation_id

                    step: _ResponseStep | None = None
                    async for item in self._stream_response_step(local_session_id, request):
                        if isinstance(item, _ResponseStep):
                            step = item
                        else:
                            yield item

                    if step is None or step.terminal_kind is None:
                        yield AgentEvent.error(
                            "The Rebyte Responses stream ended before completion."
                        )
                        return
                    _add_usage(total_usage, step.terminal_response)

                    if step.function_outputs:
                        if continuation_rounds >= MAX_CLIENT_TOOL_CONTINUATION_ROUNDS:
                            raise ClientToolContinuationLimitError(
                                "Rebyte Agent exceeded "
                                f"{MAX_CLIENT_TOOL_CONTINUATION_ROUNDS} client-tool "
                                "continuation rounds"
                            )
                        continuation_rounds += 1
                        next_input = step.function_outputs
                        continue

                    if step.terminal_kind == "response.completed":
                        yield AgentEvent.turn_complete(
                            "end_turn",
                            total_usage,
                            round((time.monotonic() - started) * 1000),
                            0,
                        )
                        return

                    logger.error(
                        "Rebyte Responses turn ended with %s: %s",
                        step.terminal_kind,
                        _error_message(step.terminal_response, "no error detail"),
                    )
                    yield AgentEvent.error(_SAFE_TURN_ERROR)
                    return
            except ClientToolContinuationLimitError:
                raise
            except Exception:
                logger.exception("Rebyte Responses turn failed")
                yield AgentEvent.error(_SAFE_TURN_ERROR)
