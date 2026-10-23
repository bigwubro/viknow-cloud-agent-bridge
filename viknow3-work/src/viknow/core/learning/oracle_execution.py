"""Bridge approved learning cases into the existing Evidence Oracle contract."""

from __future__ import annotations

from typing import Protocol

from viknow.core.eval.candidate_evidence import EvidenceCollectionTask
from viknow.core.eval.evidence_oracle import EvidenceOracleScope
from viknow.core.learning.case_review import ApprovedCaseSnapshot, OracleRequest


class AssetRevisionResolver(Protocol):
    def resolve_asset_index_revision(self, *, library_id: str, asset_id: str) -> str: ...


def build_evidence_collection_task(
    *,
    request: OracleRequest,
    snapshot: ApprovedCaseSnapshot,
    revision_resolver: AssetRevisionResolver,
) -> EvidenceCollectionTask:
    """Freeze the approved assets before handing work to Evidence Oracle."""
    if request.source_run_id != snapshot.run_id:
        raise ValueError("Oracle request and approved snapshot must reference the same run")
    if not request.library_id:
        raise ValueError("Oracle request has no resolvable library access boundary")
    revisions = {
        asset_id: revision_resolver.resolve_asset_index_revision(
            library_id=request.library_id,
            asset_id=asset_id,
        )
        for asset_id in request.asset_ids
    }
    return EvidenceCollectionTask(
        task_id=request.id,
        case_id=request.source_run_id,
        material_revision=snapshot.created_at.isoformat(),
        source_run_ids=[request.source_run_id],
        scope=EvidenceOracleScope(
            library_id=request.library_id,
            asset_ids=request.asset_ids,
            asset_revisions=revisions,
        ),
        gate_a_status="approved",
        gate_a_decision_ref=request.gate_a_decision_ref,
    )
