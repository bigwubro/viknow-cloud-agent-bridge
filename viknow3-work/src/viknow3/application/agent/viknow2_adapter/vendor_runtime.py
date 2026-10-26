"""run_turn via agents.runtime DeepAgentsRuntime."""

from __future__ import annotations

import asyncio
import logging

from agents.runtime.core.runtime.domain import AgentOp, AgentOpType

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
        public_id = state.ctx.profile_id or "deepagents"
        runtime = await get_deepagents_runtime(public_id)
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
        log.info("vendor turn %s: submitting", turn.run_id)
        await runtime.submit(op)
        run_state = runtime._runs[turn.run_id]
        terminal: str | None = None
        while terminal is None:
            event_task = asyncio.create_task(runtime.next_event(turn.run_id))
            agent_task = run_state.task
            if agent_task is None:
                event_task.cancel()
                events.failed("vendor runtime did not start a task")
                break
            done, _pending = await asyncio.wait(
                {event_task, agent_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if event_task in done:
                event = event_task.result()
                terminal = apply_vendor_event(event, events, state)
                continue
            event_task.cancel()
            try:
                await event_task
            except asyncio.CancelledError:
                pass
            exc = agent_task.exception() if agent_task.done() and not agent_task.cancelled() else None
            message = f"{type(exc).__name__}: {exc}" if exc is not None else "vendor run ended without a terminal event"
            log.error("vendor turn %s failed before a terminal event: %s", turn.run_id, message)
            events.failed(message)
            terminal = "failed"
        return AgentTurnResult(
            output=state.output or "",
            citations=state.citations or {},
            token_usage=state.token_usage,
        )


_RUNTIME = ViknowVendorRuntime()


def get_vendor_runtime() -> Viknow2AgentRuntime:
    return _RUNTIME
