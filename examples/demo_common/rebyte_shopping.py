# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""Shopping-host facade backed by one managed Rebyte Agent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from commerce_common.memory import MemoryStore, MemoryWriteFilter
from commerce_common.skills import SkillRegistry
from commerce_common.streaming import AgentEvent
from commerce_common.turn import latest_user_text
from shopping_agent import ShoppingAgentConfig, ShoppingSessionContext, ShoppingSessionState
from shopping_agent.executor import ShoppingToolExecutor, build_memory

from .rebyte_responses import (
    ClientToolResult,
    RebyteFunctionCall,
    RebyteResponsesAdapter,
)


@dataclass(frozen=True)
class _HostTurn:
    local_session_id: str
    session: ShoppingSessionContext
    state: ShoppingSessionState


@dataclass
class _RuntimeEntry:
    session: ShoppingSessionContext
    state: ShoppingSessionState
    executor: ShoppingToolExecutor


_ACTIVE_HOST_TURN: ContextVar[_HostTurn | None] = ContextVar(
    "rebyte_commerce_active_host_turn", default=None
)


def _without_nulls(value: Any) -> Any:
    """Restore the source tool schema's optional-field semantics after strict decoding."""

    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_nulls(item) for item in value]
    return value


class RebyteShoppingAgent:
    """The original storefront host surface implemented with Rebyte Responses.

    Storefront reads and writes remain remote MCP tools. Presentation calls are persistent
    client tools on the Agent and execute here against the same Conversation-scoped
    executor, so their validated UI events go directly to the browser.
    """

    persist_before_yield = frozenset({"ui", "ui_partial", "cart_update", "turn_complete"})

    def __init__(
        self,
        *,
        backend: Any,
        skills: SkillRegistry | None = None,
        skills_dir: Path | None = None,
        config: ShoppingAgentConfig | None = None,
        memory_store: MemoryStore | None = None,
        memory_write_filter: MemoryWriteFilter | None = None,
        responses: RebyteResponsesAdapter | None = None,
        executor_class: type[ShoppingToolExecutor] = ShoppingToolExecutor,
    ) -> None:
        if skills is None:
            skills = SkillRegistry.from_dir(skills_dir) if skills_dir else SkillRegistry([])
        self.backend = backend
        self.config = config or ShoppingAgentConfig()
        self.skills = skills
        self.executor_class = executor_class
        self.extra_presentation_tools: tuple[Any, ...] = ()
        self.memory = build_memory(self.config, memory_store, memory_write_filter)
        self._responses = responses
        self._runtime: dict[str, _RuntimeEntry] = {}
        if isinstance(responses, RebyteResponsesAdapter):
            responses.client_tool_handler = self._execute_client_tool
            responses.conversation_handler = self._bind_conversation

    def _adapter(self) -> RebyteResponsesAdapter:
        if self._responses is None:
            self._responses = RebyteResponsesAdapter.from_env(
                client_tool_handler=self._execute_client_tool,
                conversation_handler=self._bind_conversation,
            )
        return self._responses

    def _new_entry(
        self, session: ShoppingSessionContext, state: ShoppingSessionState
    ) -> _RuntimeEntry:
        executor = self.executor_class(
            backend=self.backend,
            config=self.config,
            skills=SkillRegistry([]),
            session=session,
            state=state,
            memory=self.memory,
            inline_context=True,
        )
        return _RuntimeEntry(session=session, state=state, executor=executor)

    def _bind_conversation(self, local_session_id: str, conversation_id: str) -> None:
        turn = _ACTIVE_HOST_TURN.get()
        if turn is None or turn.local_session_id != local_session_id:
            return
        scope = conversation_id.removeprefix("conv_")
        runtime_session = turn.session.model_copy(update={"session_id": scope})
        existing = self._runtime.get(scope)
        if (
            existing is not None
            and existing.state is turn.state
            and existing.session == runtime_session
        ):
            return
        if existing is not None and existing.state is not turn.state:
            turn.state.remember_products(list(existing.state.seen_products.values()))
            # A remote MCP call may have resolved this scope just before the BFF saw
            # response.created. Share the record's dictionary with that in-flight
            # executor before replacing the registry entry.
            existing.state.seen_products = turn.state.seen_products
        self._runtime[scope] = self._new_entry(runtime_session, turn.state)

    def runtime_executor(self, scope: str) -> ShoppingToolExecutor:
        """Return the executor shared by remote MCP and local client tools."""

        entry = self._runtime.get(scope)
        if entry is None:
            session = ShoppingSessionContext(session_id=scope, user_id="demo-user")
            entry = self._runtime[scope] = self._new_entry(session, ShoppingSessionState())
        return entry.executor

    async def _execute_client_tool(
        self, local_session_id: str, call: RebyteFunctionCall
    ) -> ClientToolResult:
        scope = self.runtime_scope(local_session_id)
        if scope is None:
            return ClientToolResult("The client tool has no active Conversation.", is_error=True)
        outcome = await self.runtime_executor(scope).execute(
            call.name, _without_nulls(call.arguments)
        )
        return ClientToolResult(
            output=outcome.result_text,
            events=tuple(outcome.events),
            is_error=outcome.is_error,
            blocked=outcome.blocked,
        )

    def runtime_scope(self, local_session_id: str) -> str | None:
        return None if self._responses is None else self._responses.runtime_scope(local_session_id)

    async def forget_session(self, local_session_id: str) -> None:
        if self._responses is None:
            return
        scope = self._responses.runtime_scope(local_session_id)
        await self._responses.forget_session(local_session_id)
        if scope is not None:
            self._runtime.pop(scope, None)

    async def stream_turn(
        self,
        messages: list[dict[str, Any]],
        session: ShoppingSessionContext,
        state: ShoppingSessionState | None = None,
    ) -> AsyncIterator[AgentEvent]:
        state = state if state is not None else ShoppingSessionState()
        message = latest_user_text(messages)
        try:
            adapter = self._adapter()
        except ValueError:
            yield AgentEvent.error(
                "Chat is not configured yet. Set REBYTE_API_KEY and REBYTE_AGENT_ID in "
                "the repo-root .env, then restart the API."
            )
            return

        token = _ACTIVE_HOST_TURN.set(
            _HostTurn(local_session_id=session.session_id, session=session, state=state)
        )
        try:
            async for event in adapter.stream_turn(session.session_id, message):
                yield event
        finally:
            _ACTIVE_HOST_TURN.reset(token)

    async def update_memory(
        self, messages: list[dict[str, Any]], session: ShoppingSessionContext
    ) -> list[Any]:
        del messages, session
        return []
