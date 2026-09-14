# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""The retail host's thin adapter for one pre-created Rebyte Agent.

Rebyte owns the agent loop and the Session. The existing commerce host still
executes every commerce tool and renders its original ``AgentEvent`` protocol.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from openai import AsyncOpenAI

from commerce_common.memory import MemoryStore, MemoryWriteFilter
from commerce_common.skills import SkillRegistry
from commerce_common.streaming import AgentEvent
from commerce_common.turn import latest_user_text, outcome_events, round_closes_turn, usage_totals
from shopping_agent import ShoppingAgentConfig, ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor, build_memory

from .rebyte_config import session_environment

logger = logging.getLogger(__name__)

DEFAULT_REBYTE_BASE_URL = "https://api.rebyte.ai"


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _turn_usage(response: Any) -> dict[str, int]:
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
    """The original storefront host surface, backed by Rebyte Agents and Sessions."""

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
            # The host health route reports the saved API Agent identifier.
            self.config = self.config.model_copy(update={"model": agent_id})
        self.skills = skills
        self.executor_class = executor_class
        self.extra_presentation_tools: tuple[Any, ...] = ()
        self.memory = build_memory(self.config, memory_store, memory_write_filter)
        self._client = client
        self._sessions: dict[str, str] = {}
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

    def _agents_client(self) -> tuple[Any, str]:
        api_key, agent_id, base_url = self._settings()
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=self.config.request_timeout_s,
                max_retries=0,
            )
        return self._client, agent_id

    async def _session_for_browser(self, browser_session_id: str) -> str:
        existing = self._sessions.get(browser_session_id)
        if existing is not None:
            return existing
        client, agent_id = self._agents_client()
        created = await client.beta.agents.sessions.create(
            agent_id=agent_id,
            environment=session_environment(),
            metadata={"commerce_browser_session": browser_session_id},
        )
        self._sessions[browser_session_id] = created.id
        return created.id

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
            executor = self._executor(session, state)
            session_id = await self._session_for_browser(session.session_id)
            client, _ = self._agents_client()
            # Subscribe before submitting input so even the first delta is observed.
            stream = await client.beta.agents.sessions.events.stream(session_id)
            terminal = None
            expected_cancel = False
            exhausted = False
            rounds = 0
            settled = False
            try:
                await client.beta.agents.sessions.events.create(
                    session_id,
                    idempotency_key=str(uuid4()),
                    events=[
                        {
                            "type": "agent.session.input.message",
                            "input": [
                                {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": latest_user_text(messages),
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                )
                async for event in stream:
                    kind = event.type
                    if kind == "agent.session.turn.output_text.delta":
                        yield AgentEvent.text_delta(event.delta)
                    elif kind == "agent.session.requires_action":
                        # Only the authoritative requires_action snapshot authorizes
                        # host execution; item.done alone is insufficient.
                        outputs = []
                        outcomes = []
                        rounds += 1
                        for action in event.session.required_actions:
                            if action.type != "function_call":
                                raise ValueError("Unsupported required action")
                            if not isinstance(action.arguments, dict):
                                raise ValueError("Tool arguments must be an object")
                            yield executor.tool_call_event(
                                action.name,
                                action.call_id,
                                action.arguments,
                            )
                            outcome = await executor.execute(action.name, action.arguments)
                            for host_event in outcome_events(action.name, action.call_id, outcome):
                                yield host_event
                            outcomes.append((action.name, outcome))
                            outputs.append(
                                {
                                    "type": "agent.session.input.tool_result",
                                    "call_id": action.call_id,
                                    "turn_id": action.turn_id,
                                    "success": not outcome.is_error,
                                    "output": outcome.result_text,
                                    "error": outcome.result_text if outcome.is_error else None,
                                }
                            )
                        closes = self.config.close_on_presentation and round_closes_turn(
                            outcomes,
                            executor.ends_clean,
                        )
                        exhausted = rounds > self.config.max_tool_iterations
                        if closes or exhausted:
                            # One atomic input batch stores results and cancels before
                            # the loop can start another model call.
                            outputs.append({"type": "agent.session.input.cancel"})
                            expected_cancel = True
                        await client.beta.agents.sessions.events.create(
                            session_id,
                            events=outputs,
                            idempotency_key=str(uuid4()),
                        )
                    elif kind in {
                        "agent.session.turn.completed",
                        "agent.session.turn.cancelled",
                        "agent.session.turn.failed",
                    }:
                        terminal = event.turn
                    elif kind == "agent.session.failed":
                        raise RuntimeError(event.session.error)
                    elif kind == "agent.session.idle" and terminal is not None:
                        settled = True
                        if exhausted:
                            raise RuntimeError(
                                "Rebyte Agent exceeded the client-tool continuation limit"
                            )
                        expected = "cancelled" if expected_cancel else "completed"
                        if terminal.status != expected:
                            raise RuntimeError(
                                f"Rebyte turn ended with {terminal.status}: {terminal.error}"
                            )
                        yield AgentEvent.turn_complete(
                            "end_turn",
                            _turn_usage(terminal),
                            round((time.monotonic() - started) * 1000),
                            0,
                        )
                        return
                raise RuntimeError("Agents event stream closed before the turn settled")
            finally:
                await stream.close()
                if not settled:
                    try:
                        await client.beta.agents.sessions.events.create(
                            session_id,
                            events=[{"type": "agent.session.input.cancel"}],
                            idempotency_key=str(uuid4()),
                        )
                    except Exception:
                        # Preserve the original stream/host failure. A racing terminal
                        # Turn or an unavailable API may reject cleanup cancellation.
                        logger.exception(
                            "Could not cancel unfinished Rebyte turn in %s", session_id
                        )

    async def update_memory(
        self, messages: list[dict[str, Any]], session: ShoppingSessionContext
    ) -> list[Any]:
        """The hosted Agent writes through save_memory during its turn."""
        del messages, session
        return []
