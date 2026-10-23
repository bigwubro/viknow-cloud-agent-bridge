"""run_turn via vendored viknow.agent_adapters.deepagents.DeepAgentsRuntime."""

from __future__ import annotations

import logging

from viknow.core.runtime.domain import AgentOp, AgentOpType

from viknow3.application.agent.viknow2_adapter.protocol import Viknow2AgentRuntime
from viknow3.application.agent.viknow2_adapter.types import AgentRuntimeDeps, AgentTurnInput, AgentTurnResult
from viknow3.application.agent.viknow2_adapter.vendor.events import apply_vendor_event
from viknow3.application.agent.viknow2_adapter.vendor.factory import get_deepagents_runtime
from viknow3.application.agentcore.environment.context import RunState
from viknow3.application.agentcore.runtime.events import RunEvents

log = logging.getLogger(__name__)


class ViknowVendorRuntime:
    async def run_turn(
        self,
        turn: AgentTurnInput,
        state: RunState,
        events: RunEvents,
        deps: AgentRuntimeDeps,
    ) -> AgentTurnResult:
        del deps
        runtime = await get_deepagents_runtime()
        library_ids = list(turn.library_ids)
        op = AgentOp(
            type=AgentOpType.USER_INPUT,
            input=turn.user_message,
            run_id=turn.run_id,
            session_id=turn.session_id,
            user_id=turn.user_id,
            workspace_id=None,
            agent_model=turn.model_selector,
            library_ids=library_ids or None,
            use_precompiled_knowledge=False,
        )
        await runtime.submit(op)
        terminal: str | None = None
        while terminal is None:
            event = await runtime.next_event(turn.run_id)
            terminal = apply_vendor_event(event, events, state)
        return AgentTurnResult(
            output=state.output or "",
            citations=state.citations or {},
            token_usage=state.token_usage,
        )


_RUNTIME = ViknowVendorRuntime()


def get_vendor_runtime() -> Viknow2AgentRuntime:
    return _RUNTIME
