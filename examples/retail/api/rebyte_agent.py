# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""The retail host's thin adapter for one pre-created Rebyte Agent.

Rebyte owns the agent loop and the Conversation. The existing commerce host still
executes every commerce tool and renders its original ``AgentEvent`` protocol.
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
from commerce_common.turn import latest_user_text, outcome_events, round_closes_turn, usage_totals
from shopping_agent import ShoppingAgentConfig, ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor, build_memory

DEFAULT_REBYTE_BASE_URL = "https://api.rebyte.ai"


@dataclass(frozen=True)
class _StepComplete:
    continuation: list[dict[str, str]] | None
    usage: dict[str, int]
    closes_turn: bool


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


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
        calls: list[Any] = []
        terminal: str | None = None
        terminal_response: Any = None
        try:
            async for event in stream:
                kind = str(_field(event, "type", ""))
                if kind == "response.output_text.delta":
                    delta = _field(event, "delta")
                    if isinstance(delta, str) and delta:
                        yield AgentEvent.text_delta(delta)
                elif kind == "response.output_item.done":
                    item = _field(event, "item")
                    if _field(item, "type") == "function_call":
                        calls.append(item)
                elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                    terminal = kind
                    terminal_response = _field(event, "response")
                    returned = _field(_field(terminal_response, "conversation"), "id")
                    if returned not in (None, conversation):
                        raise ValueError("Rebyte changed the Conversation id during a turn")
                elif kind in {"error", "response.error"}:
                    raise RuntimeError(str(_field(event, "message", "Rebyte Responses API error")))
        finally:
            await stream.close()

        if terminal != "response.completed":
            raise RuntimeError(
                f"Rebyte Responses turn ended with {terminal or 'no terminal event'}"
            )

        # Execute only calls from a completed Response. The original executor owns
        # provenance, cart mutations, memory, validation, and presentation enrichment.
        function_outputs: list[dict[str, str]] = []
        outcomes: list[tuple[str, ToolOutcome]] = []
        for item in calls:
            name = str(_field(item, "name", "tool"))
            call_id = str(_field(item, "call_id", ""))
            if not call_id:
                raise ValueError("Rebyte returned a function call without call_id")
            try:
                arguments = json.loads(_field(item, "arguments", ""))
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be an object")
            except (TypeError, ValueError):
                yield executor.tool_call_event(name, call_id, {})
                outcome = ToolOutcome.error(
                    "Tool arguments must be a valid JSON object; retry the call."
                )
            else:
                yield executor.tool_call_event(name, call_id, arguments)
                outcome = await executor.execute(name, arguments)
            for host_event in outcome_events(name, call_id, outcome):
                yield host_event
            outcomes.append((name, outcome))
            function_outputs.append(
                {"type": "function_call_output", "call_id": call_id, "output": outcome.result_text}
            )
        yield _StepComplete(
            continuation=function_outputs or None,
            usage=_response_usage(terminal_response),
            closes_turn=self.config.close_on_presentation
            and round_closes_turn(outcomes, executor.ends_clean),
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
            input_value: str | list[dict[str, str]] = latest_user_text(messages)
            for round_index in range(self.config.max_tool_iterations + 1):
                continuation: list[dict[str, str]] | None = None
                closes_turn = False
                async for event in self._stream_step(
                    conversation=conversation,
                    input_value=input_value,
                    executor=executor,
                ):
                    if isinstance(event, AgentEvent):
                        yield event
                    else:
                        continuation = event.continuation
                        closes_turn = event.closes_turn
                        for key, count in event.usage.items():
                            usage[key] += count
                exhausted = round_index == self.config.max_tool_iterations
                if continuation and (closes_turn or exhausted):
                    client, _ = self._responses_client()
                    await client.conversations.items.create(
                        conversation_id=conversation,
                        items=continuation,
                    )
                if continuation is None or closes_turn:
                    yield AgentEvent.turn_complete(
                        "end_turn",
                        usage,
                        round((time.monotonic() - started) * 1000),
                        0,
                    )
                    return
                if exhausted:
                    raise RuntimeError("Rebyte Agent exceeded the client-tool continuation limit")
                input_value = continuation

    async def update_memory(
        self, messages: list[dict[str, Any]], session: ShoppingSessionContext
    ) -> list[Any]:
        """The hosted Agent writes through save_memory during its turn."""
        del messages, session
        return []
