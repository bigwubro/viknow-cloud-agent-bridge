#!/usr/bin/env python3
"""One-shot vendor turn with traceback (236 debug)."""
from __future__ import annotations

import traceback

from viknow3.ops.config.runtime_env import load_agent_runtime_env

load_agent_runtime_env()

from viknow3.application.agent.viknow2_adapter.types import AgentRuntimeDeps, AgentTurnInput
from viknow3.application.agent.viknow2_adapter.vendor_runtime import ViknowVendorRuntime
from viknow3.application.agentcore.environment.context import RunContext, RunState
from viknow3.application.agentcore.events.sink import EventSink
from viknow3.application.agentcore.runtime.events import RunEvents
from viknow3.application.agentcore.runtime.loop import LOOP
from viknow3.clock import now


async def _turn() -> None:
    ctx = RunContext(
        "deepagents",
        "u-dbg",
        "s-dbg-qa",
        "r-dbg-qa",
        "知识库文档验收短语是什么？",
        None,
        [],
        ["doc"],
        [],
    )
    state = RunState(ctx, "running", None, {}, None, now(), now())
    sink = EventSink()
    events = RunEvents(state, sink)
    turn = AgentTurnInput(
        session_id=ctx.session_id,
        run_id=ctx.run_id,
        user_id=ctx.user_id,
        user_message=ctx.message,
        library_ids=("doc",),
    )
    deps = AgentRuntimeDeps(library_allowed=lambda _: True, emit_event=events.emit)
    try:
        await ViknowVendorRuntime().run_turn(turn, state, events, deps)
    except Exception:
        traceback.print_exc()
        raise
    print("OK", state.status, (state.output or "")[:300])


if __name__ == "__main__":
    LOOP.submit(_turn()).result(timeout=300)
