#!/usr/bin/env python3
"""One-shot vendor turn with traceback (236 debug)."""
from __future__ import annotations

import traceback

from viknow3.ops.config.runtime_env import load_agent_runtime_env

load_agent_runtime_env()

from viknow3.service.agent.deepagents_adapter.types import AgentTurnInput
from viknow3.service.agent.deepagents_adapter.vendor_runtime import DeepAgentsTurnRuntime
from viknow3.service.host.environment.context import RunContext, RunState
from viknow3.service.host.events.sink import EventSink
from viknow3.service.host.runtime.events import RunEvents
from viknow3.service.host.runtime.loop import LOOP
from viknow3.clock import now


async def _turn() -> None:
    ctx = RunContext(
        "expertbuddy-deepagents",
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
    try:
        await DeepAgentsTurnRuntime().run_turn(turn, state, events)
    except Exception:
        traceback.print_exc()
        raise
    print("OK", state.status, (state.output or "")[:300])


if __name__ == "__main__":
    LOOP.submit(_turn()).result(timeout=300)
