"""Optional timeout phase tracking for development eval scripts."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Protocol


class PhaseRecorder(Protocol):
    def set_phase(self, phase: str) -> None: ...


_RECORDER: ContextVar[PhaseRecorder | None] = ContextVar(
    "viknow_eval_phase_recorder",
    default=None,
)


@dataclass(frozen=True)
class EvalPhaseToken:
    context_token: Token


def activate_eval_phase_recorder(recorder: PhaseRecorder) -> EvalPhaseToken:
    context_token = _RECORDER.set(recorder)
    return EvalPhaseToken(context_token=context_token)


def reset_eval_phase_recorder(token: EvalPhaseToken) -> None:
    _RECORDER.reset(token.context_token)
