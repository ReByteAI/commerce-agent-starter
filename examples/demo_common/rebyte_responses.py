# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""Bridge Rebyte's OpenAI-compatible Responses stream to the demo ``AgentEvent`` stream.

The browser-facing demo protocol is intentionally kept out of the Rebyte API client. A
host creates one adapter, calls :meth:`stream_turn` with its own session id, and streams
the returned events with ``commerce_common.streaming.to_sse``. The adapter remembers the
Rebyte Conversation returned by the first Response and supplies it on later turns.

``presentation_hooks`` are keyed by MCP tool name (or ``server/name`` for an exact match;
``*`` is the fallback). They receive the local session id and completed call, including
decoded arguments and model-facing output, and can return ``ui`` or other ``AgentEvent``
objects. This is where a storefront rebuilds host events without placing UI payloads in
the model's MCP result.
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
from dataclasses import dataclass
from typing import Any, Protocol, cast

from openai import AsyncOpenAI

from commerce_common.streaming import AgentEvent

logger = logging.getLogger(__name__)

DEFAULT_REBYTE_BASE_URL = "https://api.rebyte.ai"
_SUMMARY_MAX_CHARS = 300
_EXCERPT_MAX_CHARS = 500
_SAFE_TURN_ERROR = "The Rebyte Agent request failed. Please try again."


class _ResponsesResource(Protocol):
    async def create(self, **kwargs: Any) -> AsyncIterator[Any]: ...


class ResponsesClient(Protocol):
    responses: _ResponsesResource


@dataclass(frozen=True)
class RebyteMcpCall:
    """One completed MCP call, normalized from the Responses API event stream."""

    id: str
    server_label: str
    name: str
    arguments: dict[str, Any]
    output: Any
    error: str | None
    status: str


PresentationEvents = AgentEvent | Iterable[AgentEvent] | AsyncIterator[AgentEvent] | None
PresentationHook = Callable[
    [str, RebyteMcpCall], PresentationEvents | Awaitable[PresentationEvents]
]


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
    """Return the workspace UUID encoded by a public Rebyte Conversation id."""

    matched = _CONVERSATION_ID.fullmatch(conversation_id)
    if matched is None:
        raise ValueError("Rebyte Conversation ID must be conv_ followed by a UUID")
    return matched.group(1).lower()


def _content_block_text(value: Any, *, _depth: int = 0) -> str | None:
    """Extract model-facing text from a Responses MCP output wrapper.

    Rebyte projects an MCP result as JSON-encoded content blocks. Only the text field is
    used for the browser's short tool summary; host events never travel in that result.
    """

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


def _output_summary(call: RebyteMcpCall) -> tuple[str, str | None, bool, str, str | None]:
    if call.status == "failed":
        return call.error or "Tool execution failed.", None, True, "error", None
    if call.status in {"incomplete", "cancelled", "canceled"}:
        return (
            call.error or "Tool execution did not complete.",
            None,
            False,
            "blocked",
            call.status,
        )
    if call.output is None:
        return "ok", None, False, "ok", None
    if (rendered := _content_block_text(call.output)) is None:
        rendered = json.dumps(call.output, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= _SUMMARY_MAX_CHARS:
        return rendered, None, False, "ok", None
    return "ok", rendered[:_EXCERPT_MAX_CHARS], False, "ok", None


class RebyteResponsesAdapter:
    """A server-side Responses client with an in-memory host-session mapping.

    Construct it once per API process. ``stream_turn`` serializes turns belonging to the
    same local session so two first messages cannot create two Rebyte Conversations. The
    map is process-local; a production host should restore it from its session store after
    a restart with :meth:`bind_conversation`.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        api_key: str | None = None,
        base_url: str = DEFAULT_REBYTE_BASE_URL,
        client: ResponsesClient | None = None,
        presentation_hooks: Mapping[str, PresentationHook] | None = None,
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
        self.presentation_hooks = dict(presentation_hooks or {})
        self._conversations: dict[str, str] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @classmethod
    def from_env(
        cls,
        *,
        presentation_hooks: Mapping[str, PresentationHook] | None = None,
    ) -> RebyteResponsesAdapter:
        """Read ``REBYTE_API_KEY``, ``REBYTE_AGENT_ID``, and optional ``REBYTE_BASE_URL``."""

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
            presentation_hooks=presentation_hooks,
        )

    def conversation_id(self, local_session_id: str) -> str | None:
        return self._conversations.get(local_session_id)

    def runtime_scope(self, local_session_id: str) -> str | None:
        """Return the workspace UUID encoded by this session's Conversation id."""

        conversation_id = self._conversations.get(local_session_id)
        return (
            None if conversation_id is None else _runtime_scope_from_conversation(conversation_id)
        )

    def bind_conversation(self, local_session_id: str, conversation_id: str) -> None:
        """Restore a mapping held by the host's durable session store."""

        if not local_session_id or not conversation_id:
            raise ValueError("Session and Conversation IDs cannot be empty")
        _runtime_scope_from_conversation(conversation_id)
        existing = self._conversations.get(local_session_id)
        if existing is not None and existing != conversation_id:
            raise ValueError(
                f"Local session {local_session_id} is already bound to a different Conversation"
            )
        self._conversations[local_session_id] = conversation_id

    async def forget_session(self, local_session_id: str) -> None:
        """Forget a local session after waiting for any turn currently using it."""

        lock = await self._lock_for(local_session_id)
        async with lock:
            self._conversations.pop(local_session_id, None)

    async def _lock_for(self, local_session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._session_locks.setdefault(local_session_id, asyncio.Lock())

    def _remember_response(self, local_session_id: str, response: Any) -> None:
        candidate = _conversation_id(response)
        if candidate is None:
            return
        self.bind_conversation(local_session_id, candidate)

    @staticmethod
    def _state(states: dict[str, _McpState], item_id: str) -> _McpState:
        return states.setdefault(item_id, _McpState(id=item_id))

    @staticmethod
    def _update_item(state: _McpState, item: Any) -> None:
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
    def _call(state: _McpState) -> RebyteMcpCall:
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
    def _announce(state: _McpState) -> AgentEvent | None:
        if state.announced:
            return None
        state.announced = True
        call = RebyteResponsesAdapter._call(state)
        return AgentEvent.tool_call(call.name, call.id, call.arguments)

    def _hook(self, call: RebyteMcpCall) -> PresentationHook | None:
        return (
            self.presentation_hooks.get(f"{call.server_label}/{call.name}")
            or self.presentation_hooks.get(call.name)
            or self.presentation_hooks.get("*")
        )

    async def _hook_events(
        self, local_session_id: str, call: RebyteMcpCall
    ) -> AsyncIterator[AgentEvent]:
        hook = self._hook(call)
        if hook is None:
            return
        result = hook(local_session_id, call)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return
        if isinstance(result, AgentEvent):
            events: Iterable[AgentEvent] | AsyncIterator[AgentEvent] = (result,)
        else:
            events = result
        if hasattr(events, "__aiter__"):
            async for event in cast(AsyncIterator[AgentEvent], events):
                yield self._stamp_ui(event, call.id)
            return
        for event in cast(Iterable[AgentEvent], events):
            yield self._stamp_ui(event, call.id)

    @staticmethod
    def _stamp_ui(event: AgentEvent, call_id: str) -> AgentEvent:
        if event.type not in {"ui", "ui_partial"} or "stream_id" in event.data:
            return event
        return AgentEvent(type=event.type, data={**event.data, "stream_id": call_id})

    async def _complete(self, local_session_id: str, state: _McpState) -> AsyncIterator[AgentEvent]:
        if state.completed:
            return
        state.completed = True
        if announced := self._announce(state):
            yield announced
        call = self._call(state)
        async for event in self._hook_events(local_session_id, call):
            yield event
        summary, excerpt, is_error, status, reason = _output_summary(call)
        yield AgentEvent.tool_result(
            call.name,
            call.id,
            summary,
            is_error=is_error,
            status=status,
            reason=reason,
            excerpt=excerpt,
        )

    async def _complete_response_calls(
        self, local_session_id: str, response: Any, states: dict[str, _McpState]
    ) -> AsyncIterator[AgentEvent]:
        output = _field(response, "output", [])
        if not isinstance(output, Iterable) or isinstance(output, (str, bytes, Mapping)):
            return
        for item in output:
            if _field(item, "type") != "mcp_call":
                continue
            item_id = _string(_field(item, "id"))
            if not item_id:
                continue
            state = self._state(states, item_id)
            self._update_item(state, item)
            async for event in self._complete(local_session_id, state):
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

    async def stream_turn(
        self,
        local_session_id: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one turn and yield the event protocol consumed by ``web-shared``."""

        if not local_session_id:
            raise ValueError("Local session ID cannot be empty")
        if not message.strip():
            raise ValueError("Message cannot be empty")

        lock = await self._lock_for(local_session_id)
        async with lock:
            started = time.monotonic()
            states: dict[str, _McpState] = {}
            terminal = False
            stream: Any = None
            try:
                request: dict[str, Any] = {
                    "model": self.agent_id,
                    "input": message,
                    "stream": True,
                    "extra_headers": {
                        "Idempotency-Key": idempotency_key or f"commerce-{uuid.uuid4()}"
                    },
                }
                conversation_id = self._conversations.get(local_session_id)
                if conversation_id is not None:
                    request["conversation"] = conversation_id
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
                        if _field(item, "type") == "mcp_call":
                            item_id = _string(_field(item, "id"))
                            if item_id:
                                self._update_item(self._state(states, item_id), item)
                        continue

                    if kind in {
                        "response.mcp_call_arguments.delta",
                        "response.mcp_call_arguments.done",
                    }:
                        item_id = _string(_field(raw_event, "item_id"))
                        if not item_id:
                            continue
                        state = self._state(states, item_id)
                        if kind.endswith(".delta"):
                            state.arguments += _string(_field(raw_event, "delta"))
                        else:
                            state.arguments = _string(
                                _field(raw_event, "arguments"), state.arguments
                            )
                        continue

                    if kind == "response.mcp_call.in_progress":
                        item_id = _string(_field(raw_event, "item_id"))
                        if item_id and (announced := self._announce(self._state(states, item_id))):
                            yield announced
                        continue

                    if kind in {"response.mcp_call.completed", "response.mcp_call.failed"}:
                        item_id = _string(_field(raw_event, "item_id"))
                        if item_id:
                            self._state(states, item_id).status = (
                                "failed" if kind.endswith(".failed") else "completed"
                            )
                        continue

                    if kind == "response.output_item.done":
                        item = _field(raw_event, "item")
                        if _field(item, "type") != "mcp_call":
                            continue
                        item_id = _string(_field(item, "id"))
                        if not item_id:
                            continue
                        state = self._state(states, item_id)
                        self._update_item(state, item)
                        async for event in self._complete(local_session_id, state):
                            yield event
                        continue

                    if kind == "response.rebyte_tool_call.progress":
                        if progress := self._progress_event(raw_event):
                            yield progress
                        continue

                    if kind == "response.completed":
                        async for event in self._complete_response_calls(
                            local_session_id, response, states
                        ):
                            yield event
                        yield AgentEvent.turn_complete(
                            "end_turn",
                            _usage(response),
                            round((time.monotonic() - started) * 1000),
                            0,
                        )
                        terminal = True
                        break

                    if kind in {"response.failed", "response.incomplete"}:
                        async for event in self._complete_response_calls(
                            local_session_id, response, states
                        ):
                            yield event
                        logger.error(
                            "Rebyte Responses turn ended with %s: %s",
                            kind,
                            _error_message(response, "no error detail"),
                        )
                        yield AgentEvent.error(_SAFE_TURN_ERROR)
                        terminal = True
                        break

                    if kind in {"error", "response.error"}:
                        message_text = _field(raw_event, "message")
                        logger.error(
                            "Rebyte Responses stream emitted %s: %s",
                            kind,
                            message_text
                            if isinstance(message_text, str) and message_text
                            else "no error detail",
                        )
                        yield AgentEvent.error(_SAFE_TURN_ERROR)
                        terminal = True
                        break

                if not terminal:
                    yield AgentEvent.error("The Rebyte Responses stream ended before completion.")
            except Exception:
                logger.exception("Rebyte Responses turn failed")
                yield AgentEvent.error(_SAFE_TURN_ERROR)
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
