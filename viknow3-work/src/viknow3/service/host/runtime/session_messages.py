"""User-visible turns for one session, taken from run events."""

from __future__ import annotations

from viknow3.store import STORE


def session_messages(profile_id: str, session_id: str) -> list[dict[str, str]]:
    """Questions and answers already written for this session. The agent runtime keeps its own context."""

    del profile_id
    return STORE.session_turns(session_id)
