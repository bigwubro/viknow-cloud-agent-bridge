"""Run-level citation registry (A5 / simplified A17)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CitationRegistry:
    _items: list[dict[str, Any]] = field(default_factory=list)
    _verified: list[dict[str, Any]] = field(default_factory=list)

    def register_evidence(self, evidence: dict[str, Any], *, source_tool: str) -> dict[str, Any]:
        token = len(self._items) + 1
        entry = {
            "citation_token": f"[{token}]",
            "agent_citation": {
                "index": token,
                "source_tool": source_tool,
                "evidence": evidence,
            },
        }
        self._items.append(entry)
        return entry

    def add_verified_claim(self, claim: dict[str, Any]) -> None:
        self._verified.append(dict(claim))

    def export(self) -> dict[str, Any]:
        return {
            "items": list(self._items),
            "verified_claims": list(self._verified),
        }
