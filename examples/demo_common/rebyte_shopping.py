# Copyright 2026 ReByteAI
# SPDX-License-Identifier: Apache-2.0

"""Shopping-host facade backed by a managed Rebyte Agent.

The storefront host was intentionally written against the small surface below rather
than against an HTTP API.  This facade keeps that surface intact while moving the agent
loop, conversation, Skills, and MCP orchestration into Rebyte Agent Runtime.
"""

from __future__ import annotations

import contextlib
import logging
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
from shopping_agent.serialization import cart_payload
from shopping_agent.types import Product

from .rebyte_responses import RebyteMcpCall, RebyteResponsesAdapter

logger = logging.getLogger(__name__)

# These reads are replayed only to mirror the authoritative MCP executor's provenance
# into the browser session. They cannot change the cart, memory, or retailer systems.
_PROVENANCE_READS = frozenset(
    {"search_products", "get_product_details", "get_orders", "get_order_status"}
)
_CART_MUTATIONS = frozenset({"add_to_cart", "update_cart_item", "remove_from_cart"})
_PRESENTATION_TOOLS = frozenset(
    {
        "present_products",
        "present_comparison",
        "present_plan",
        "present_guide",
        "present_order_status",
        "checkout",
        "present_suggestions",
    }
)


@dataclass(frozen=True)
class _HostTurn:
    session: ShoppingSessionContext
    state: ShoppingSessionState


_ACTIVE_HOST_TURN: ContextVar[_HostTurn | None] = ContextVar(
    "rebyte_commerce_active_host_turn", default=None
)


def _remember_rendered_products(state: ShoppingSessionState, event: AgentEvent) -> None:
    """Mirror host-enriched products into the browser session's gates.

    The authoritative provenance remains in the MCP server's workspace-scoped executor.
    The mirror exists so a product card's direct Add button passes through the same local
    ``ShoppingToolExecutor`` checks as the original demo.
    """

    if event.type not in {"ui", "ui_partial"}:
        return

    products: list[Product] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if {"product_id", "title", "price", "in_stock"} <= value.keys():
                with contextlib.suppress(ValueError):
                    products.append(Product.model_validate(value))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(event.data.get("payload"))
    state.remember_products(products)


class RebyteShoppingAgent:
    """The storefront's agent interface, implemented by Rebyte Responses.

    Construction does not require credentials, so the API can start and expose ``/mcp``
    before its Agent is created.  The first chat turn reads ``REBYTE_API_KEY`` and
    ``REBYTE_AGENT_ID`` and returns a useful browser error when setup is incomplete.
    """

    # The generic host persists state before these interactive events reach the browser.
    # That makes the product provenance gate observable before a user can click Add.
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
        # Keep the original storefront host's compatibility surface. The supported
        # retail starter uses the built-in presentation tools exposed by its MCP server.
        self.extra_presentation_tools: tuple[Any, ...] = ()
        self.memory = build_memory(self.config, memory_store, memory_write_filter)
        self._responses = responses
        if isinstance(responses, RebyteResponsesAdapter):
            responses.presentation_hooks.setdefault("*", self._host_events_for_call)

    def _adapter(self) -> RebyteResponsesAdapter:
        if self._responses is None:
            self._responses = RebyteResponsesAdapter.from_env(
                presentation_hooks={"*": self._host_events_for_call}
            )
        return self._responses

    def _host_executor(
        self, turn: _HostTurn, runtime_scope: str
    ) -> tuple[ShoppingToolExecutor, ShoppingSessionContext]:
        runtime_session = turn.session.model_copy(update={"session_id": runtime_scope})
        executor = self.executor_class(
            backend=self.backend,
            config=self.config,
            skills=self.skills,
            session=runtime_session,
            state=turn.state,
            memory=self.memory,
            inline_context=True,
        )
        return executor, runtime_session

    async def _host_events_for_call(
        self, local_session_id: str, call: RebyteMcpCall
    ) -> list[AgentEvent]:
        """Rebuild browser events after a managed MCP call completes.

        The remote MCP executor is authoritative and is the only writer. The BFF may
        replay catalog/order reads to mirror provenance and may run retail presentation
        enrichment. Cart mutations are never replayed: their result is reflected by
        reading the shared backend cart after the remote write.
        """

        if call.status != "completed":
            return []
        turn = _ACTIVE_HOST_TURN.get()
        if turn is None or turn.session.session_id != local_session_id:
            logger.warning("Ignoring MCP host event outside its active storefront turn")
            return []
        runtime_scope = self.runtime_scope(local_session_id)
        if runtime_scope is None:
            logger.warning("Ignoring MCP host event before its Rebyte Conversation is known")
            return []
        executor, runtime_session = self._host_executor(turn, runtime_scope)

        if call.name in _PROVENANCE_READS:
            outcome = await executor.execute(call.name, call.arguments)
            if outcome.is_error:
                logger.warning("Local provenance replay failed for %s", call.name)
            return []

        if call.name in _PRESENTATION_TOOLS:
            outcome = await executor.execute(call.name, call.arguments)
            if outcome.refused:
                logger.warning("Local presentation replay did not render for %s", call.name)
                return []
            return outcome.events

        if call.name in _CART_MUTATIONS:
            try:
                cart = await self.backend.get_cart(runtime_session)
            except Exception:
                logger.warning("Could not refresh cart after remote mutation", exc_info=True)
                return []
            return [AgentEvent.cart_update(cart_payload(cart))]

        # save_memory and all other calls are intentionally not replayed.
        return []

    def runtime_scope(self, local_session_id: str) -> str | None:
        return None if self._responses is None else self._responses.runtime_scope(local_session_id)

    async def forget_session(self, local_session_id: str) -> None:
        if self._responses is not None:
            await self._responses.forget_session(local_session_id)

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

        token = _ACTIVE_HOST_TURN.set(_HostTurn(session=session, state=state))
        try:
            async for event in adapter.stream_turn(session.session_id, message):
                _remember_rendered_products(state, event)
                yield event
        finally:
            _ACTIVE_HOST_TURN.reset(token)

    async def update_memory(
        self, messages: list[dict[str, Any]], session: ShoppingSessionContext
    ) -> list[Any]:
        # The managed Agent calls save_memory through the workspace-scoped MCP server.
        del messages, session
        return []
