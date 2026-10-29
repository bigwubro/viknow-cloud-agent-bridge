"""Per-run Harness facts (viknow2 HarnessEvidenceContext subset)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HarnessRunFacts:
    library_ids: list[str]
    selected_library_ids: set[str] = field(default_factory=set)
    skill_loaded: bool = False
    discover_called: bool = False
    search_called: bool = False
    plan_recorded: bool = False
    verified_claims: list[dict[str, object]] = field(default_factory=list)

    def run_library_ids(self) -> set[str]:
        ids = set(self.library_ids)
        ids.update(self.selected_library_ids)
        return ids
