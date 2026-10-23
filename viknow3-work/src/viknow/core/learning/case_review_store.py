"""Persistence for candidate cases, human decisions, and immutable snapshots."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from viknow.core.learning.case_review import (
    ACTIVE_SKILL_OPTIMIZATION_STATUSES,
    AnswerReviewReplayOutcome,
    ApprovedCaseSnapshot,
    CandidateCase,
    CandidateSource,
    CandidateStatus,
    CaseReviewDecision,
    OptimizationAnalysisTask,
    OptimizationProposal,
    OptimizationProposalAnalysis,
    OracleGateBReview,
    OracleRequest,
    SkillOptimizationAcceptanceReview,
    SkillOptimizationExperiment,
    SkillOptimizationExperimentCase,
    SkillOptimizationExperimentConfig,
    SkillOptimizationTask,
)
from viknow.core.postgres_pool import connection as pooled_connection


class LeaseOwnershipLost(ValueError):
    """A worker tried to write after its claimed attempt was replaced or cancelled."""


class CaseReviewStore(Protocol):
    def add_candidate(
        self,
        *,
        run_id: str,
        source: CandidateSource,
        added_by: str,
        bucket: str | None = None,
        reason_codes: list[str] | None = None,
    ) -> CandidateCase: ...

    def get_candidate(self, run_id: str) -> CandidateCase | None: ...

    def get_candidates(self, run_ids: list[str]) -> dict[str, CandidateCase]: ...

    def list_candidates(self, *, limit: int) -> list[CandidateCase]: ...

    def update_candidate_classification(
        self, *, run_id: str, bucket: str, reason_codes: list[str]
    ) -> CandidateCase: ...

    def update_reference_answer(self, *, run_id: str, answer: str | None) -> CandidateCase: ...

    def record_decision(
        self,
        *,
        run_id: str,
        status: CandidateStatus,
        reviewer: str,
        reason: str | None,
        created_at: datetime,
        snapshot: ApprovedCaseSnapshot | None = None,
    ) -> CandidateCase: ...

    def list_decisions(self, run_id: str) -> list[CaseReviewDecision]: ...

    def get_snapshot(self, run_id: str) -> ApprovedCaseSnapshot | None: ...

    def get_oracle_request(self, run_id: str) -> OracleRequest | None: ...

    def enqueue_oracle_request(
        self, *, snapshot: ApprovedCaseSnapshot, decision: CaseReviewDecision
    ) -> OracleRequest: ...

    def claim_oracle_requests(self, *, limit: int, lease_seconds: float) -> list[OracleRequest]: ...

    def mark_oracle_request_run_started(
        self, request_id: str, *, attempt: int, oracle_run_id: str
    ) -> OracleRequest: ...

    def complete_oracle_request(
        self,
        request_id: str,
        *,
        attempt: int,
        evidence_collection_task: dict,
        oracle_run_id: str,
        candidate_evidence_package: dict,
    ) -> OracleRequest: ...

    def fail_oracle_request(
        self, request_id: str, *, attempt: int, error_code: str, max_attempts: int
    ) -> OracleRequest: ...

    def review_oracle_request(
        self,
        request_id: str,
        *,
        review: OracleGateBReview,
        accepted_oracle_package: dict | None,
    ) -> OracleRequest: ...

    def enqueue_skill_optimization_task(
        self,
        *,
        source_run_id: str,
        target_skill_id: str,
        input_source: str = "accepted_oracle",
        oracle_request_id: str | None = None,
    ) -> SkillOptimizationTask: ...

    def get_skill_optimization_task(self, source_run_id: str) -> SkillOptimizationTask | None: ...

    def list_skill_optimization_tasks(self, *, limit: int) -> list[SkillOptimizationTask]: ...

    def claim_skill_optimization_tasks(
        self, *, limit: int, lease_seconds: float
    ) -> list[SkillOptimizationTask]: ...

    def complete_skill_optimization_task(
        self,
        task_id: str,
        *,
        attempt: int,
        experiment_id: str,
        result_summary: dict,
        acceptance_manifest: dict,
    ) -> SkillOptimizationTask: ...

    def fail_skill_optimization_task(
        self, task_id: str, *, attempt: int, error_code: str, max_attempts: int
    ) -> SkillOptimizationTask: ...

    def review_skill_optimization_task(
        self, task_id: str, *, decision: str, reviewer: str, note: str | None
    ) -> SkillOptimizationTask: ...

    def create_skill_optimization_experiment(
        self,
        *,
        run_ids: list[str],
        config: SkillOptimizationExperimentConfig,
        created_by: str,
    ) -> SkillOptimizationExperiment: ...

    def get_skill_optimization_experiment(
        self, experiment_id: str
    ) -> SkillOptimizationExperiment | None: ...

    def list_skill_optimization_experiments(
        self, *, limit: int
    ) -> list[SkillOptimizationExperiment]: ...

    def review_skill_optimization_experiment(
        self, experiment_id: str, *, decision: str, reviewer: str, note: str | None
    ) -> SkillOptimizationExperiment: ...

    def create_proposal(
        self, *, run_id: str, bucket: str, risk: str, reason_codes: list[str]
    ) -> OptimizationProposal: ...

    def get_proposal(self, run_id: str) -> OptimizationProposal | None: ...

    def mark_proposal_running(self, run_id: str) -> OptimizationProposal: ...

    def mark_proposal_queued(
        self, run_id: str, *, error_code: str | None = None
    ) -> OptimizationProposal: ...

    def complete_proposal(
        self,
        run_id: str,
        *,
        analysis: OptimizationProposalAnalysis,
        analysis_model: str,
        answer_review_replay: AnswerReviewReplayOutcome | None = None,
    ) -> OptimizationProposal: ...

    def fail_proposal(self, run_id: str, *, error_code: str) -> OptimizationProposal: ...

    def enqueue_analysis_task(self, *, run_id: str) -> OptimizationAnalysisTask: ...

    def get_analysis_task(self, task_id: str) -> OptimizationAnalysisTask | None: ...

    def latest_analysis_task(self, run_id: str) -> OptimizationAnalysisTask | None: ...

    def update_analysis_activity(
        self, task_id: str, *, attempt: int, kind: str, text: str
    ) -> None: ...

    def claim_analysis_tasks(
        self, *, limit: int, lease_seconds: float
    ) -> list[OptimizationAnalysisTask]: ...

    def mark_analysis_task_completed(
        self, task_id: str, *, attempt: int
    ) -> OptimizationAnalysisTask: ...

    def mark_analysis_task_failed(
        self, task_id: str, *, attempt: int, error_code: str, max_attempts: int
    ) -> OptimizationAnalysisTask: ...

    def cancel_analysis_task(self, task_id: str) -> OptimizationAnalysisTask: ...

    def commit_analysis_result(
        self,
        task_id: str,
        *,
        attempt: int,
        analysis: OptimizationProposalAnalysis,
        analysis_model: str,
        answer_review_replay: AnswerReviewReplayOutcome | None = None,
    ) -> OptimizationAnalysisTask: ...

    def remove_candidate(
        self, *, run_id: str, reviewer: str, reason: str | None, created_at: datetime
    ) -> CandidateCase: ...


class _ExperimentExecutionStore:
    """Shared JSON transitions, with row locks owned by each database transaction."""

    _experiment_postgres = False

    def review_skill_optimization_experiment(
        self, experiment_id: str, *, decision: str, reviewer: str, note: str | None
    ) -> SkillOptimizationExperiment:
        review = SkillOptimizationAcceptanceReview(
            decision=decision, reviewer=reviewer, note=note, created_at=_now()
        )
        with self._connect() as conn:
            if not self._experiment_postgres:
                conn.execute("BEGIN IMMEDIATE")
            placeholder = "%s" if self._experiment_postgres else "?"
            lock = " FOR UPDATE" if self._experiment_postgres else ""
            row = conn.execute(
                "SELECT experiment_json FROM learning_skill_optimization_experiments "
                f"WHERE id={placeholder}{lock}",
                (experiment_id,),
            ).fetchone()
            if row is None:
                raise KeyError(experiment_id)
            current = SkillOptimizationExperiment.model_validate_json(row["experiment_json"])
            if current.status != "pending_acceptance":
                raise ValueError("experiment must be pending acceptance")
            return self._write_experiment(
                conn,
                current,
                {
                    "status": "accepted" if decision == "keep" else "rejected",
                    "acceptance_review": review,
                    "lease_until": None,
                    "acceptance_manifest": {
                        **current.acceptance_manifest,
                        "decision": decision,
                        "release_eligible": False,
                    },
                },
            )

    def claim_skill_optimization_experiments(self, *, limit: int, lease_seconds: float):
        if limit < 1 or lease_seconds <= 0:
            raise ValueError("claim limits must be positive")
        now = _now()
        claimed = []
        with self._connect() as conn:
            if not self._experiment_postgres:
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("learning-skill-optimization:global",),
                )
            lock = " FOR UPDATE" if self._experiment_postgres else ""
            rows = conn.execute(
                "SELECT experiment_json FROM learning_skill_optimization_experiments "
                "WHERE status IN ('queued','running','pending_acceptance') "
                "ORDER BY created_at,id" + lock
            ).fetchall()
            experiments = [
                SkillOptimizationExperiment.model_validate_json(row["experiment_json"])
                for row in rows
            ]
            if any(
                item.status == "pending_acceptance"
                or (item.status == "running" and item.lease_until and item.lease_until >= now)
                for item in experiments
            ):
                return []
            for current in experiments:
                claimed.append(
                    self._write_experiment(
                        conn,
                        current,
                        {
                            "status": "running",
                            "attempts": current.attempts + 1,
                            "lease_until": now + timedelta(seconds=lease_seconds),
                            "error_code": None,
                            "error_summary": None,
                            "artifact_id": f"learning-{current.id}-attempt-{current.attempts + 1}",
                            "progress": {"stage": "preparing"},
                        },
                    )
                )
                break
        return claimed

    def update_skill_optimization_experiment(self, experiment_id: str, *, attempt: int, **changes):
        allowed = {
            "status",
            "lease_until",
            "artifact_id",
            "progress",
            "result_summary",
            "acceptance_manifest",
            "error_code",
            "error_summary",
        }
        if changes.keys() - allowed:
            raise ValueError("immutable experiment fields cannot be updated")
        with self._connect() as conn:
            if not self._experiment_postgres:
                conn.execute("BEGIN IMMEDIATE")
            placeholder = "%s" if self._experiment_postgres else "?"
            lock = " FOR UPDATE" if self._experiment_postgres else ""
            row = conn.execute(
                "SELECT experiment_json FROM learning_skill_optimization_experiments "
                f"WHERE id={placeholder}{lock}",
                (experiment_id,),
            ).fetchone()
            if row is None:
                raise KeyError(experiment_id)
            current = SkillOptimizationExperiment.model_validate_json(row["experiment_json"])
            if current.status != "running" or current.attempts != attempt:
                raise LeaseOwnershipLost(
                    "experiment execution lease is no longer owned by this attempt"
                )
            return self._write_experiment(conn, current, changes)

    def _write_experiment(self, conn, current, changes):
        updated = SkillOptimizationExperiment.model_validate(
            {
                **current.model_dump(),
                **changes,
                "updated_at": _now(),
            }
        )
        placeholder = "%s" if self._experiment_postgres else "?"
        conn.execute(
            f"UPDATE learning_skill_optimization_experiments SET status={placeholder},"
            f"experiment_json={placeholder},updated_at={placeholder} WHERE id={placeholder}",
            (updated.status, updated.model_dump_json(), updated.updated_at.isoformat(), updated.id),
        )
        return updated


class SqliteCaseReviewStore(_ExperimentExecutionStore):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def add_candidate(
        self,
        *,
        run_id: str,
        source: CandidateSource,
        added_by: str,
        bucket: str | None = None,
        reason_codes: list[str] | None = None,
    ) -> CandidateCase:
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO learning_case_candidates
                (run_id,status,source,bucket,reason_codes_json,added_by,added_at,updated_at,current_reason)
                VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(run_id) DO UPDATE SET
                  status='pending',source=excluded.source,bucket=excluded.bucket,
                  reason_codes_json=excluded.reason_codes_json,added_by=excluded.added_by,
                  added_at=excluded.added_at,updated_at=excluded.updated_at,
                  current_reason=NULL,active=1
                WHERE learning_case_candidates.active=0""",
                (run_id, source, bucket, json.dumps(reason_codes or []), added_by, now, now),
            )
        candidate = self.get_candidate(run_id)
        if candidate is None:
            raise RuntimeError("candidate insert failed")
        return candidate

    def get_candidate(self, run_id: str) -> CandidateCase | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learning_case_candidates WHERE run_id=?", (run_id,)
            ).fetchone()
        return _candidate(row) if row else None

    def get_candidates(self, run_ids: list[str]) -> dict[str, CandidateCase]:
        if not run_ids:
            return {}
        placeholders = ",".join("?" for _ in run_ids)
        query = (
            f"SELECT * FROM learning_case_candidates WHERE active=1 AND run_id IN ({placeholders})"
        )
        with self._connect() as conn:
            rows = conn.execute(query, run_ids).fetchall()
        return {row["run_id"]: _candidate(row) for row in rows}

    def list_candidates(self, *, limit: int) -> list[CandidateCase]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM learning_case_candidates WHERE active=1
                ORDER BY updated_at DESC, run_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_candidate(row) for row in rows]

    def update_candidate_classification(
        self, *, run_id: str, bucket: str, reason_codes: list[str]
    ) -> CandidateCase:
        now = _now().isoformat()
        with self._connect() as conn:
            result = conn.execute(
                """UPDATE learning_case_candidates SET bucket=?,reason_codes_json=?,updated_at=?
                WHERE run_id=? AND active=1""",
                (bucket, json.dumps(reason_codes), now, run_id),
            )
            if result.rowcount != 1:
                raise KeyError(run_id)
        candidate = self.get_candidate(run_id)
        if candidate is None:
            raise RuntimeError("candidate classification update failed")
        return candidate

    def update_reference_answer(self, *, run_id: str, answer: str | None) -> CandidateCase:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                """UPDATE learning_case_candidates
                SET reference_answer=?,updated_at=?
                WHERE run_id=? AND status='approved' AND active=1
                AND NOT EXISTS (
                  SELECT 1 FROM learning_skill_optimization_tasks
                  WHERE source_run_id=learning_case_candidates.run_id
                    AND json_extract(task_json,'$.status') IN (?,?,?)
                )
                AND NOT EXISTS (
                  SELECT 1 FROM learning_skill_optimization_experiment_cases AS experiment_case
                  JOIN learning_skill_optimization_experiments AS experiment
                    ON experiment.id=experiment_case.experiment_id
                  WHERE experiment_case.run_id=learning_case_candidates.run_id
                    AND experiment.status IN ('queued','running','pending_acceptance')
                )
                """,
                (
                    answer,
                    _now().isoformat(),
                    run_id,
                    *sorted(ACTIVE_SKILL_OPTIMIZATION_STATUSES),
                ),
            )
            if result.rowcount != 1:
                raise ValueError("reference answer is frozen after optimization starts")
        candidate = self.get_candidate(run_id)
        if candidate is None:
            raise RuntimeError("reference answer update failed")
        return candidate

    def record_decision(
        self,
        *,
        run_id: str,
        status: CandidateStatus,
        reviewer: str,
        reason: str | None,
        created_at: datetime,
        snapshot: ApprovedCaseSnapshot | None = None,
    ) -> CandidateCase:
        if status == "approved" and snapshot is None:
            raise ValueError("approved decision requires snapshot")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate_row = conn.execute(
                "SELECT active FROM learning_case_candidates WHERE run_id=?", (run_id,)
            ).fetchone()
            if candidate_row is None:
                raise KeyError(run_id)
            if not candidate_row["active"]:
                raise ValueError("candidate is not active")
            row = conn.execute(
                "SELECT MAX(revision) AS revision FROM learning_case_decisions WHERE run_id=?",
                (run_id,),
            ).fetchone()
            decision = CaseReviewDecision(
                id=str(uuid4()),
                run_id=run_id,
                revision=int(row["revision"] or 0) + 1,
                status=status,
                reviewer=reviewer,
                reason=reason,
                created_at=created_at,
            )
            conn.execute(
                """INSERT INTO learning_case_decisions
                (id,run_id,revision,status,reviewer,reason,created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    decision.id,
                    decision.run_id,
                    decision.revision,
                    decision.status,
                    decision.reviewer,
                    decision.reason,
                    decision.created_at.isoformat(),
                ),
            )
            if snapshot is not None:
                conn.execute(
                    """INSERT OR IGNORE INTO learning_case_snapshots
                    (run_id,snapshot_json,created_at) VALUES (?,?,?)""",
                    (snapshot.run_id, snapshot.model_dump_json(), snapshot.created_at.isoformat()),
                )
            row = conn.execute(
                """UPDATE learning_case_candidates
                SET status=?,current_reason=?,updated_at=? WHERE run_id=?""",
                (
                    decision.status,
                    decision.reason,
                    decision.created_at.isoformat(),
                    decision.run_id,
                ),
            )
            if row.rowcount != 1:
                raise RuntimeError("candidate update failed")
            candidate_row = conn.execute(
                "SELECT * FROM learning_case_candidates WHERE run_id=?", (run_id,)
            ).fetchone()
            return _candidate(candidate_row)

    def list_decisions(self, run_id: str) -> list[CaseReviewDecision]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM learning_case_decisions WHERE run_id=? ORDER BY revision ASC",
                (run_id,),
            ).fetchall()
        return [_decision(row) for row in rows]

    def get_snapshot(self, run_id: str) -> ApprovedCaseSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM learning_case_snapshots WHERE run_id=?", (run_id,)
            ).fetchone()
        return ApprovedCaseSnapshot.model_validate_json(row["snapshot_json"]) if row else None

    def get_oracle_request(self, run_id: str) -> OracleRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_json FROM learning_oracle_requests WHERE source_run_id=?",
                (run_id,),
            ).fetchone()
        return OracleRequest.model_validate_json(row["request_json"]) if row else None

    def enqueue_oracle_request(
        self, *, snapshot: ApprovedCaseSnapshot, decision: CaseReviewDecision
    ) -> OracleRequest:
        with self._connect() as conn:
            _insert_sqlite_oracle_request(conn, snapshot=snapshot, decision=decision)
        request = self.get_oracle_request(snapshot.run_id)
        if request is None:
            raise RuntimeError("Oracle request enqueue failed")
        return request

    def claim_oracle_requests(self, *, limit: int, lease_seconds: float) -> list[OracleRequest]:
        now = _now()
        claimed = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM learning_oracle_requests ORDER BY created_at,id"
            ).fetchall()
            for row in rows:
                request = OracleRequest.model_validate_json(row["request_json"])
                expired = (
                    request.status == "running"
                    and request.lease_until
                    and request.lease_until < now
                )
                if request.status not in {"pending_scope_resolution", "queued"} and not expired:
                    continue
                updated = request.model_copy(
                    update={
                        "status": "running",
                        "attempts": request.attempts + 1,
                        "lease_until": now + timedelta(seconds=lease_seconds),
                        "error_code": None,
                        "updated_at": now,
                    }
                )
                conn.execute(
                    "UPDATE learning_oracle_requests SET request_json=?,updated_at=? WHERE id=?",
                    (updated.model_dump_json(), now.isoformat(), updated.id),
                )
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
        return claimed

    def mark_oracle_request_run_started(
        self, request_id: str, *, attempt: int, oracle_run_id: str
    ) -> OracleRequest:
        return self._replace_oracle_request(
            request_id, attempt=attempt, oracle_run_id=oracle_run_id
        )

    def complete_oracle_request(
        self,
        request_id: str,
        *,
        attempt: int,
        evidence_collection_task: dict,
        oracle_run_id: str,
        candidate_evidence_package: dict,
    ) -> OracleRequest:
        return self._replace_oracle_request(
            request_id,
            attempt=attempt,
            status="pending_gate_b",
            evidence_collection_task=evidence_collection_task,
            oracle_run_id=oracle_run_id,
            candidate_evidence_package=candidate_evidence_package,
            lease_until=None,
            error_code=None,
        )

    def fail_oracle_request(
        self, request_id: str, *, attempt: int, error_code: str, max_attempts: int
    ) -> OracleRequest:
        return self._replace_oracle_request(
            request_id,
            attempt=attempt,
            status="failed" if attempt >= max_attempts else "queued",
            lease_until=None,
            error_code=error_code,
        )

    def review_oracle_request(
        self,
        request_id: str,
        *,
        review: OracleGateBReview,
        accepted_oracle_package: dict | None,
    ) -> OracleRequest:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_json FROM learning_oracle_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            current = OracleRequest.model_validate_json(row["request_json"])
            if current.status != "pending_gate_b":
                raise ValueError("Gate B review requires pending candidate evidence")
            updated = current.model_copy(
                update={
                    "status": review.status,
                    "gate_b_review": review,
                    "accepted_oracle_package": accepted_oracle_package,
                    "updated_at": now,
                }
            )
            conn.execute(
                "UPDATE learning_oracle_requests SET request_json=?,updated_at=? WHERE id=?",
                (updated.model_dump_json(), now.isoformat(), request_id),
            )
        return updated

    def _oracle_request_by_id(self, request_id: str) -> OracleRequest:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_json FROM learning_oracle_requests WHERE id=?", (request_id,)
            ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return OracleRequest.model_validate_json(row["request_json"])

    def _replace_oracle_request(
        self, request_id: str, *, attempt: int, **changes: Any
    ) -> OracleRequest:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_json FROM learning_oracle_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            current = OracleRequest.model_validate_json(row["request_json"])
            if current.status != "running" or current.attempts != attempt:
                raise LeaseOwnershipLost("Oracle lease attempt is no longer owned")
            updated = OracleRequest.model_validate(
                {**current.model_dump(), **changes, "updated_at": _now()}
            )
            conn.execute(
                "UPDATE learning_oracle_requests SET request_json=?,updated_at=? WHERE id=?",
                (updated.model_dump_json(), updated.updated_at.isoformat(), request_id),
            )
        return updated

    def enqueue_skill_optimization_task(
        self,
        *,
        source_run_id: str,
        target_skill_id: str,
        input_source: str = "accepted_oracle",
        oracle_request_id: str | None = None,
    ) -> SkillOptimizationTask:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_experiment = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_experiments
                WHERE status IN ('queued','running','pending_acceptance') LIMIT 1""",
            ).fetchone()
            if active_experiment is not None:
                raise ValueError("target skill already has an active experiment")
            active_legacy = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_tasks
                WHERE source_run_id<>? AND json_extract(task_json,'$.status')
                  IN ('queued','running','pending_acceptance') LIMIT 1""",
                (source_run_id,),
            ).fetchone()
            if active_legacy is not None:
                raise ValueError("system already has an active legacy optimization")
            candidate = conn.execute(
                "SELECT reference_answer FROM learning_case_candidates WHERE run_id=?",
                (source_run_id,),
            ).fetchone()
            if candidate is None:
                raise KeyError(source_run_id)
            task = SkillOptimizationTask(
                id=str(uuid4()),
                source_run_id=source_run_id,
                input_source=input_source,
                oracle_request_id=oracle_request_id,
                target_skill_id=target_skill_id,
                reference_answer=candidate["reference_answer"],
                status="queued",
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                """INSERT OR IGNORE INTO learning_skill_optimization_tasks
                (id,source_run_id,oracle_request_id,task_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?)""",
                (
                    task.id,
                    source_run_id,
                    oracle_request_id,
                    task.model_dump_json(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT task_json FROM learning_skill_optimization_tasks WHERE source_run_id=?",
                (source_run_id,),
            ).fetchone()
        return SkillOptimizationTask.model_validate_json(row["task_json"])

    def get_skill_optimization_task(self, source_run_id: str) -> SkillOptimizationTask | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_json FROM learning_skill_optimization_tasks WHERE source_run_id=?",
                (source_run_id,),
            ).fetchone()
        return SkillOptimizationTask.model_validate_json(row["task_json"]) if row else None

    def list_skill_optimization_tasks(self, *, limit: int) -> list[SkillOptimizationTask]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT task_json FROM learning_skill_optimization_tasks
                ORDER BY updated_at DESC,id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [SkillOptimizationTask.model_validate_json(row["task_json"]) for row in rows]

    def claim_skill_optimization_tasks(
        self, *, limit: int, lease_seconds: float
    ) -> list[SkillOptimizationTask]:
        now = _now()
        claimed = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id,task_json FROM learning_skill_optimization_tasks ORDER BY created_at,id"
            ).fetchall()
            for row in rows:
                task = SkillOptimizationTask.model_validate_json(row["task_json"])
                expired = task.status == "running" and task.lease_until and task.lease_until < now
                if task.status != "queued" and not expired:
                    continue
                updated = task.model_copy(
                    update={
                        "status": "running",
                        "attempts": task.attempts + 1,
                        "lease_until": now + timedelta(seconds=lease_seconds),
                        "error_code": None,
                        "updated_at": now,
                    }
                )
                conn.execute(
                    """UPDATE learning_skill_optimization_tasks
                    SET task_json=?,updated_at=? WHERE id=?""",
                    (updated.model_dump_json(), now.isoformat(), updated.id),
                )
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
        return claimed

    def complete_skill_optimization_task(
        self,
        task_id: str,
        *,
        attempt: int,
        experiment_id: str,
        result_summary: dict,
        acceptance_manifest: dict,
    ) -> SkillOptimizationTask:
        return self._replace_skill_optimization_task(
            task_id,
            attempt=attempt,
            status="pending_acceptance",
            experiment_id=experiment_id,
            result_summary=result_summary,
            acceptance_manifest=acceptance_manifest,
            lease_until=None,
            error_code=None,
        )

    def fail_skill_optimization_task(
        self, task_id: str, *, attempt: int, error_code: str, max_attempts: int
    ) -> SkillOptimizationTask:
        return self._replace_skill_optimization_task(
            task_id,
            attempt=attempt,
            status="failed" if attempt >= max_attempts else "queued",
            error_code=error_code,
            lease_until=None,
        )

    def review_skill_optimization_task(
        self, task_id: str, *, decision: str, reviewer: str, note: str | None
    ) -> SkillOptimizationTask:
        if decision not in {"keep", "reject"}:
            raise ValueError("unsupported optimization acceptance decision")
        current = self._skill_optimization_task_by_id(task_id)
        if current.status != "pending_acceptance":
            raise ValueError("optimization review requires pending acceptance")
        review = SkillOptimizationAcceptanceReview(
            decision=decision,
            reviewer=reviewer,
            note=note,
            created_at=_now(),
        )
        return self._replace_skill_optimization_task(
            task_id,
            status="accepted" if decision == "keep" else "rejected",
            acceptance_review=review,
        )

    def _skill_optimization_task_by_id(self, task_id: str) -> SkillOptimizationTask:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_json FROM learning_skill_optimization_tasks WHERE id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return SkillOptimizationTask.model_validate_json(row["task_json"])

    def _replace_skill_optimization_task(
        self, task_id: str, *, attempt: int | None = None, **changes: Any
    ) -> SkillOptimizationTask:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT task_json FROM learning_skill_optimization_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = SkillOptimizationTask.model_validate_json(row["task_json"])
            if attempt is not None and (current.status != "running" or current.attempts != attempt):
                raise LeaseOwnershipLost("SkillOpt lease attempt is no longer owned")
            updated = SkillOptimizationTask.model_validate(
                {**current.model_dump(), **changes, "updated_at": _now()}
            )
            conn.execute(
                "UPDATE learning_skill_optimization_tasks SET task_json=?,updated_at=? WHERE id=?",
                (updated.model_dump_json(), updated.updated_at.isoformat(), task_id),
            )
        return updated

    def create_skill_optimization_experiment(
        self,
        *,
        run_ids: list[str],
        config: SkillOptimizationExperimentConfig,
        created_by: str,
    ) -> SkillOptimizationExperiment:
        _validate_experiment_run_ids(run_ids)
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_experiments
                WHERE status IN ('queued','running','pending_acceptance') LIMIT 1"""
            ).fetchone()
            if active is not None:
                raise ValueError("target skill already has an active experiment")
            active_legacy = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_tasks
                WHERE json_extract(task_json,'$.status')
                    IN ('queued','running','pending_acceptance') LIMIT 1"""
            ).fetchone()
            if active_legacy is not None:
                raise ValueError("target skill already has an active legacy task")
            cases = [self._freeze_sqlite_experiment_case(conn, run_id) for run_id in run_ids]
            experiment = SkillOptimizationExperiment(
                id=str(uuid4()),
                config=config,
                cases=cases,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                """INSERT INTO learning_skill_optimization_experiments
                (id,target_skill_id,status,experiment_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?)""",
                (
                    experiment.id,
                    experiment.target_skill_id,
                    experiment.status,
                    experiment.model_dump_json(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            conn.executemany(
                """INSERT INTO learning_skill_optimization_experiment_cases
                (experiment_id,run_id) VALUES (?,?)""",
                [(experiment.id, run_id) for run_id in run_ids],
            )
        return experiment

    @staticmethod
    def _freeze_sqlite_experiment_case(
        conn: sqlite3.Connection, run_id: str
    ) -> SkillOptimizationExperimentCase:
        candidate = conn.execute(
            """SELECT status,active,reference_answer FROM learning_case_candidates
            WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        if candidate is None:
            raise KeyError(run_id)
        if candidate["status"] != "approved" or not candidate["active"]:
            raise ValueError(f"experiment case {run_id} must be approved and active")
        snapshot_row = conn.execute(
            "SELECT snapshot_json FROM learning_case_snapshots WHERE run_id=?", (run_id,)
        ).fetchone()
        if snapshot_row is None:
            raise ValueError(f"approved experiment case {run_id} has no Gate A snapshot")
        oracle_row = conn.execute(
            "SELECT request_json FROM learning_oracle_requests WHERE source_run_id=?",
            (run_id,),
        ).fetchone()
        oracle = (
            OracleRequest.model_validate_json(oracle_row["request_json"])
            if oracle_row is not None
            else None
        )
        accepted = (
            oracle
            if oracle is not None
            and oracle.status in {"accepted", "revised"}
            and oracle.accepted_oracle_package is not None
            else None
        )
        return SkillOptimizationExperimentCase(
            run_id=run_id,
            reference_answer=candidate["reference_answer"],
            gate_a_snapshot=ApprovedCaseSnapshot.model_validate_json(snapshot_row["snapshot_json"]),
            oracle_request_id=accepted.id if accepted is not None else None,
            accepted_oracle_package=(
                accepted.accepted_oracle_package if accepted is not None else None
            ),
        )

    def get_skill_optimization_experiment(
        self, experiment_id: str
    ) -> SkillOptimizationExperiment | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT experiment_json FROM learning_skill_optimization_experiments
                WHERE id=?""",
                (experiment_id,),
            ).fetchone()
        return (
            SkillOptimizationExperiment.model_validate_json(row["experiment_json"])
            if row is not None
            else None
        )

    def list_skill_optimization_experiments(
        self, *, limit: int
    ) -> list[SkillOptimizationExperiment]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT experiment_json FROM learning_skill_optimization_experiments
                ORDER BY updated_at DESC,id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            SkillOptimizationExperiment.model_validate_json(row["experiment_json"]) for row in rows
        ]

    def create_proposal(
        self, *, run_id: str, bucket: str, risk: str, reason_codes: list[str]
    ) -> OptimizationProposal:
        now = _now()
        proposal = OptimizationProposal(
            run_id=run_id,
            status="queued",
            bucket=bucket,
            risk=risk,
            reason_codes=reason_codes,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO learning_optimization_proposals (run_id,proposal_json,updated_at)
                VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                proposal_json=excluded.proposal_json,updated_at=excluded.updated_at""",
                (run_id, proposal.model_dump_json(), now.isoformat()),
            )
        return proposal

    def get_proposal(self, run_id: str) -> OptimizationProposal | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT proposal_json FROM learning_optimization_proposals WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return OptimizationProposal.model_validate_json(row["proposal_json"]) if row else None

    def mark_proposal_running(self, run_id: str) -> OptimizationProposal:
        return self._replace_proposal(run_id, status="running")

    def mark_proposal_queued(
        self, run_id: str, *, error_code: str | None = None
    ) -> OptimizationProposal:
        return self._replace_proposal(run_id, status="queued", error_code=error_code)

    def complete_proposal(
        self,
        run_id: str,
        *,
        analysis: OptimizationProposalAnalysis,
        analysis_model: str,
        answer_review_replay: AnswerReviewReplayOutcome | None = None,
    ) -> OptimizationProposal:
        return self._replace_proposal(
            run_id,
            status="ready",
            analysis=analysis,
            analysis_model=analysis_model,
            answer_review_replay=answer_review_replay,
        )

    def fail_proposal(self, run_id: str, *, error_code: str) -> OptimizationProposal:
        return self._replace_proposal(run_id, status="failed", error_code=error_code)

    def enqueue_analysis_task(self, *, run_id: str) -> OptimizationAnalysisTask:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM learning_optimization_analysis_tasks
                WHERE run_id=? AND status IN ('queued','running')
                ORDER BY created_at DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
            if row is not None:
                return _analysis_task(row)
            task = OptimizationAnalysisTask(
                id=str(uuid4()),
                run_id=run_id,
                status="queued",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                """INSERT INTO learning_optimization_analysis_tasks
                (id,run_id,status,attempts,error_code,lease_until,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    task.id,
                    task.run_id,
                    task.status,
                    task.attempts,
                    None,
                    None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return task

    def get_analysis_task(self, task_id: str) -> OptimizationAnalysisTask | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learning_optimization_analysis_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return _analysis_task(row) if row else None

    def latest_analysis_task(self, run_id: str) -> OptimizationAnalysisTask | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM learning_optimization_analysis_tasks
                WHERE run_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        return _analysis_task(row) if row else None

    def update_analysis_activity(self, task_id: str, *, attempt: int, kind: str, text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET activity_kind=?,activity_text=?
                WHERE id=? AND status='running' AND attempts=?""",
                (kind, text[-4000:], task_id, attempt),
            )

    def claim_analysis_tasks(
        self, *, limit: int, lease_seconds: float
    ) -> list[OptimizationAnalysisTask]:
        if limit < 1 or lease_seconds <= 0:
            raise ValueError("limit and lease_seconds must be positive")
        now = _now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET status='queued',lease_until=NULL,updated_at=?
                WHERE status='running' AND lease_until<?""",
                (now.isoformat(), now.isoformat()),
            )
            rows = conn.execute(
                """SELECT id FROM learning_optimization_analysis_tasks
                WHERE status='queued' ORDER BY created_at,id LIMIT ?""",
                (limit,),
            ).fetchall()
            tasks = []
            for row in rows:
                conn.execute(
                    """UPDATE learning_optimization_analysis_tasks
                    SET status='running',attempts=attempts+1,error_code=NULL,
                    activity_kind=NULL,activity_text=NULL,
                    lease_until=?,updated_at=? WHERE id=?""",
                    (lease_until.isoformat(), now.isoformat(), row["id"]),
                )
                task = _analysis_task(
                    conn.execute(
                        "SELECT * FROM learning_optimization_analysis_tasks WHERE id=?",
                        (row["id"],),
                    ).fetchone()
                )
                if conn.execute(
                    "SELECT 1 FROM learning_optimization_proposals WHERE run_id=?",
                    (task.run_id,),
                ).fetchone():
                    self._update_proposal_in_connection(
                        conn, task.run_id, status="running", error_code=None
                    )
                tasks.append(task)
        return tasks

    def mark_analysis_task_completed(
        self, task_id: str, *, attempt: int
    ) -> OptimizationAnalysisTask:
        return self._update_analysis_task(
            task_id, attempt=attempt, status="completed", error_code=None
        )

    def mark_analysis_task_failed(
        self, task_id: str, *, attempt: int, error_code: str, max_attempts: int
    ) -> OptimizationAnalysisTask:
        task = self.get_analysis_task(task_id)
        if task is None:
            raise KeyError(task_id)
        status = "failed" if task.attempts >= max_attempts else "queued"
        return self._update_analysis_task(
            task_id, attempt=attempt, status=status, error_code=error_code
        )

    def cancel_analysis_task(self, task_id: str) -> OptimizationAnalysisTask:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM learning_optimization_analysis_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = _analysis_task(row)
            if task.status not in {"queued", "running"}:
                raise ValueError("only queued or running analysis tasks can be cancelled")
            conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET status='cancelled',lease_until=NULL,updated_at=? WHERE id=?""",
                (now.isoformat(), task_id),
            )
            self._update_proposal_in_connection(
                conn, task.run_id, status="cancelled", error_code="Cancelled"
            )
            row = conn.execute(
                "SELECT * FROM learning_optimization_analysis_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return _analysis_task(row)

    def commit_analysis_result(
        self,
        task_id: str,
        *,
        attempt: int,
        analysis: OptimizationProposalAnalysis,
        analysis_model: str,
        answer_review_replay: AnswerReviewReplayOutcome | None = None,
    ) -> OptimizationAnalysisTask:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM learning_optimization_analysis_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = _analysis_task(row)
            if task.attempts != attempt:
                raise LeaseOwnershipLost("analysis lease attempt is no longer owned")
            if task.status == "cancelled":
                return task
            if task.status != "running":
                raise ValueError("analysis result requires a running task")
            self._update_proposal_in_connection(
                conn,
                task.run_id,
                status="ready",
                analysis=analysis,
                analysis_model=analysis_model,
                answer_review_replay=answer_review_replay,
                error_code=None,
            )
            conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET status='completed',lease_until=NULL,updated_at=? WHERE id=?""",
                (now.isoformat(), task_id),
            )
            row = conn.execute(
                "SELECT * FROM learning_optimization_analysis_tasks WHERE id=?", (task_id,)
            ).fetchone()
        return _analysis_task(row)

    @staticmethod
    def _update_proposal_in_connection(
        conn: sqlite3.Connection, run_id: str, **changes: Any
    ) -> None:
        row = conn.execute(
            "SELECT proposal_json FROM learning_optimization_proposals WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        proposal = OptimizationProposal.model_validate_json(row["proposal_json"]).model_copy(
            update={**changes, "updated_at": _now()}
        )
        conn.execute(
            """UPDATE learning_optimization_proposals SET proposal_json=?,updated_at=?
            WHERE run_id=?""",
            (proposal.model_dump_json(), proposal.updated_at.isoformat(), run_id),
        )

    def _update_analysis_task(
        self, task_id: str, *, attempt: int, status: str, error_code: str | None
    ) -> OptimizationAnalysisTask:
        now = _now().isoformat()
        with self._connect() as conn:
            result = conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET status=?,error_code=?,lease_until=NULL,updated_at=?
                WHERE id=? AND status='running' AND attempts=?""",
                (status, error_code, now, task_id, attempt),
            )
            if result.rowcount != 1:
                raise LeaseOwnershipLost("analysis lease attempt is no longer owned")
            if status in {"queued", "failed"}:
                row = conn.execute(
                    "SELECT run_id FROM learning_optimization_analysis_tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
                if conn.execute(
                    "SELECT 1 FROM learning_optimization_proposals WHERE run_id=?",
                    (row["run_id"],),
                ).fetchone():
                    self._update_proposal_in_connection(
                        conn, row["run_id"], status=status, error_code=error_code
                    )
        task = self.get_analysis_task(task_id)
        if task is None:
            raise RuntimeError("analysis task update failed")
        return task

    def _replace_proposal(self, run_id: str, **changes: Any) -> OptimizationProposal:
        current = self.get_proposal(run_id)
        if current is None:
            raise KeyError(run_id)
        proposal = current.model_copy(update={**changes, "updated_at": _now()})
        with self._connect() as conn:
            conn.execute(
                """UPDATE learning_optimization_proposals SET proposal_json=?,updated_at=?
                WHERE run_id=?""",
                (proposal.model_dump_json(), proposal.updated_at.isoformat(), run_id),
            )
        return proposal

    def remove_candidate(
        self, *, run_id: str, reviewer: str, reason: str | None, created_at: datetime
    ) -> CandidateCase:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate_row = conn.execute(
                "SELECT * FROM learning_case_candidates WHERE run_id=?", (run_id,)
            ).fetchone()
            if candidate_row is None or not candidate_row["active"]:
                raise KeyError(run_id)
            if candidate_row["status"] != "pending":
                raise ValueError("only pending candidates can be removed")
            revision_row = conn.execute(
                "SELECT MAX(revision) AS revision FROM learning_case_decisions WHERE run_id=?",
                (run_id,),
            ).fetchone()
            decision = CaseReviewDecision(
                id=str(uuid4()),
                run_id=run_id,
                revision=int(revision_row["revision"] or 0) + 1,
                status="removed",
                reviewer=reviewer,
                reason=reason,
                created_at=created_at,
            )
            conn.execute(
                """INSERT INTO learning_case_decisions
                (id,run_id,revision,status,reviewer,reason,created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    decision.id,
                    run_id,
                    decision.revision,
                    decision.status,
                    reviewer,
                    reason,
                    created_at.isoformat(),
                ),
            )
            conn.execute(
                """UPDATE learning_case_candidates SET status='removed',active=0,
                current_reason=?,updated_at=? WHERE run_id=?""",
                (reason, created_at.isoformat(), run_id),
            )
            return _candidate(
                conn.execute(
                    "SELECT * FROM learning_case_candidates WHERE run_id=?", (run_id,)
                ).fetchone()
            )

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SQLITE_SCHEMA)
            analysis_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(learning_optimization_analysis_tasks)")
            }
            for column in ("activity_kind", "activity_text"):
                if column not in analysis_columns:
                    conn.execute(
                        f"ALTER TABLE learning_optimization_analysis_tasks ADD COLUMN {column} TEXT"
                    )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(learning_case_candidates)")
            }
            if "active" not in columns:
                conn.execute(
                    "ALTER TABLE learning_case_candidates "
                    "ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            if "reference_answer" not in columns:
                conn.execute(
                    "ALTER TABLE learning_case_candidates ADD COLUMN reference_answer TEXT"
                )
            optimization_columns = {
                row[1]: row
                for row in conn.execute("PRAGMA table_info(learning_skill_optimization_tasks)")
            }
            if optimization_columns["oracle_request_id"][3]:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """ALTER TABLE learning_skill_optimization_tasks
                    RENAME TO learning_skill_optimization_tasks_legacy"""
                )
                conn.execute(
                    """CREATE TABLE learning_skill_optimization_tasks (
                      id TEXT PRIMARY KEY,
                      source_run_id TEXT NOT NULL UNIQUE,
                      oracle_request_id TEXT UNIQUE,
                      task_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    )"""
                )
                conn.execute(
                    """INSERT INTO learning_skill_optimization_tasks
                      (id,source_run_id,oracle_request_id,task_json,created_at,updated_at)
                    SELECT id,source_run_id,oracle_request_id,task_json,created_at,updated_at
                    FROM learning_skill_optimization_tasks_legacy"""
                )
                conn.execute("DROP TABLE learning_skill_optimization_tasks_legacy")
            active_count = conn.execute(
                """SELECT COUNT(*) FROM learning_skill_optimization_experiments
                WHERE status IN ('queued','running','pending_acceptance')"""
            ).fetchone()[0]
            if active_count <= 1:
                conn.execute(_ACTIVE_EXPERIMENT_INDEX)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class PostgresCaseReviewStore(_ExperimentExecutionStore):
    _experiment_postgres = True

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        with self._connect() as conn:
            for statement in _POSTGRES_SCHEMA:
                conn.execute(statement)
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("learning-skill-optimization:global",),
            )
            active_count = conn.execute(
                """SELECT COUNT(*) AS active_count FROM learning_skill_optimization_experiments
                WHERE status IN ('queued','running','pending_acceptance')"""
            ).fetchone()["active_count"]
            if active_count <= 1:
                conn.execute(_ACTIVE_EXPERIMENT_INDEX)

    def add_candidate(
        self,
        *,
        run_id: str,
        source: CandidateSource,
        added_by: str,
        bucket: str | None = None,
        reason_codes: list[str] | None = None,
    ) -> CandidateCase:
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO learning_case_candidates
                (run_id,status,source,bucket,reason_codes_json,added_by,added_at,updated_at,current_reason)
                VALUES (%s,'pending',%s,%s,%s,%s,%s,%s,NULL)
                ON CONFLICT (run_id) DO UPDATE SET
                  status='pending',source=EXCLUDED.source,bucket=EXCLUDED.bucket,
                  reason_codes_json=EXCLUDED.reason_codes_json,added_by=EXCLUDED.added_by,
                  added_at=EXCLUDED.added_at,updated_at=EXCLUDED.updated_at,
                  current_reason=NULL,active=TRUE
                WHERE learning_case_candidates.active=FALSE""",
                (run_id, source, bucket, json.dumps(reason_codes or []), added_by, now, now),
            )
        candidate = self.get_candidate(run_id)
        if candidate is None:
            raise RuntimeError("candidate insert failed")
        return candidate

    def get_candidate(self, run_id: str) -> CandidateCase | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learning_case_candidates WHERE run_id=%s", (run_id,)
            ).fetchone()
        return _candidate(row) if row else None

    def get_candidates(self, run_ids: list[str]) -> dict[str, CandidateCase]:
        if not run_ids:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM learning_case_candidates WHERE active=TRUE AND run_id = ANY(%s)",
                (run_ids,),
            ).fetchall()
        return {row["run_id"]: _candidate(row) for row in rows}

    def list_candidates(self, *, limit: int) -> list[CandidateCase]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM learning_case_candidates WHERE active=TRUE
                ORDER BY updated_at DESC, run_id DESC LIMIT %s""",
                (limit,),
            ).fetchall()
        return [_candidate(row) for row in rows]

    def update_candidate_classification(
        self, *, run_id: str, bucket: str, reason_codes: list[str]
    ) -> CandidateCase:
        now = _now().isoformat()
        with self._connect() as conn:
            result = conn.execute(
                """UPDATE learning_case_candidates SET bucket=%s,reason_codes_json=%s,updated_at=%s
                WHERE run_id=%s AND active=TRUE""",
                (bucket, json.dumps(reason_codes), now, run_id),
            )
            if result.rowcount != 1:
                raise KeyError(run_id)
        candidate = self.get_candidate(run_id)
        if candidate is None:
            raise RuntimeError("candidate classification update failed")
        return candidate

    def update_reference_answer(self, *, run_id: str, answer: str | None) -> CandidateCase:
        with self._connect() as conn:
            candidate = conn.execute(
                "SELECT status,active FROM learning_case_candidates WHERE run_id=%s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if candidate is None:
                raise KeyError(run_id)
            if candidate["status"] != "approved" or not candidate["active"]:
                raise ValueError("reference answer requires an approved candidate")
            existing = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_tasks
                WHERE source_run_id=%s AND task_json::jsonb->>'status' IN (%s,%s,%s)""",
                (run_id, *sorted(ACTIVE_SKILL_OPTIMIZATION_STATUSES)),
            ).fetchone()
            if existing is not None:
                raise ValueError("reference answer is frozen after optimization starts")
            active_experiment = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_experiment_cases AS experiment_case
                JOIN learning_skill_optimization_experiments AS experiment
                  ON experiment.id=experiment_case.experiment_id
                WHERE experiment_case.run_id=%s
                  AND experiment.status IN ('queued','running','pending_acceptance')""",
                (run_id,),
            ).fetchone()
            if active_experiment is not None:
                raise ValueError("reference answer is frozen after optimization starts")
            result = conn.execute(
                """UPDATE learning_case_candidates
                SET reference_answer=%s,updated_at=%s WHERE run_id=%s""",
                (answer, _now(), run_id),
            )
            if result.rowcount != 1:
                raise KeyError(run_id)
        candidate = self.get_candidate(run_id)
        if candidate is None:
            raise RuntimeError("reference answer update failed")
        return candidate

    def record_decision(
        self,
        *,
        run_id: str,
        status: CandidateStatus,
        reviewer: str,
        reason: str | None,
        created_at: datetime,
        snapshot: ApprovedCaseSnapshot | None = None,
    ) -> CandidateCase:
        if status == "approved" and snapshot is None:
            raise ValueError("approved decision requires snapshot")
        with self._connect() as conn:
            candidate_row = conn.execute(
                "SELECT active FROM learning_case_candidates WHERE run_id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if candidate_row is None:
                raise KeyError(run_id)
            if not candidate_row["active"]:
                raise ValueError("candidate is not active")
            row = conn.execute(
                "SELECT MAX(revision) AS revision FROM learning_case_decisions WHERE run_id=%s",
                (run_id,),
            ).fetchone()
            decision = CaseReviewDecision(
                id=str(uuid4()),
                run_id=run_id,
                revision=int(row["revision"] or 0) + 1,
                status=status,
                reviewer=reviewer,
                reason=reason,
                created_at=created_at,
            )
            conn.execute(
                """INSERT INTO learning_case_decisions
                (id,run_id,revision,status,reviewer,reason,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    decision.id,
                    decision.run_id,
                    decision.revision,
                    decision.status,
                    decision.reviewer,
                    decision.reason,
                    decision.created_at.isoformat(),
                ),
            )
            if snapshot is not None:
                conn.execute(
                    """INSERT INTO learning_case_snapshots (run_id,snapshot_json,created_at)
                    VALUES (%s,%s,%s) ON CONFLICT (run_id) DO NOTHING""",
                    (snapshot.run_id, snapshot.model_dump_json(), snapshot.created_at.isoformat()),
                )
            result = conn.execute(
                """UPDATE learning_case_candidates SET status=%s,current_reason=%s,updated_at=%s
                WHERE run_id=%s""",
                (
                    decision.status,
                    decision.reason,
                    decision.created_at.isoformat(),
                    decision.run_id,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("candidate update failed")
            candidate_row = conn.execute(
                "SELECT * FROM learning_case_candidates WHERE run_id=%s", (run_id,)
            ).fetchone()
            return _candidate(candidate_row)

    def list_decisions(self, run_id: str) -> list[CaseReviewDecision]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM learning_case_decisions WHERE run_id=%s ORDER BY revision ASC",
                (run_id,),
            ).fetchall()
        return [_decision(row) for row in rows]

    def get_snapshot(self, run_id: str) -> ApprovedCaseSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM learning_case_snapshots WHERE run_id=%s", (run_id,)
            ).fetchone()
        return ApprovedCaseSnapshot.model_validate_json(row["snapshot_json"]) if row else None

    def get_oracle_request(self, run_id: str) -> OracleRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_json FROM learning_oracle_requests WHERE source_run_id=%s",
                (run_id,),
            ).fetchone()
        return OracleRequest.model_validate_json(row["request_json"]) if row else None

    def enqueue_oracle_request(
        self, *, snapshot: ApprovedCaseSnapshot, decision: CaseReviewDecision
    ) -> OracleRequest:
        with self._connect() as conn:
            _insert_postgres_oracle_request(conn, snapshot=snapshot, decision=decision)
        request = self.get_oracle_request(snapshot.run_id)
        if request is None:
            raise RuntimeError("Oracle request enqueue failed")
        return request

    def claim_oracle_requests(self, *, limit: int, lease_seconds: float) -> list[OracleRequest]:
        now = _now()
        claimed = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM learning_oracle_requests ORDER BY created_at,id "
                "FOR UPDATE SKIP LOCKED"
            ).fetchall()
            for row in rows:
                request = OracleRequest.model_validate_json(row["request_json"])
                expired = (
                    request.status == "running"
                    and request.lease_until
                    and request.lease_until < now
                )
                if request.status not in {"pending_scope_resolution", "queued"} and not expired:
                    continue
                updated = request.model_copy(
                    update={
                        "status": "running",
                        "attempts": request.attempts + 1,
                        "lease_until": now + timedelta(seconds=lease_seconds),
                        "error_code": None,
                        "updated_at": now,
                    }
                )
                conn.execute(
                    "UPDATE learning_oracle_requests SET request_json=%s,updated_at=%s WHERE id=%s",
                    (updated.model_dump_json(), now, updated.id),
                )
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
        return claimed

    def mark_oracle_request_run_started(
        self, request_id: str, *, attempt: int, oracle_run_id: str
    ) -> OracleRequest:
        return self._replace_postgres_oracle_request(
            request_id, attempt=attempt, oracle_run_id=oracle_run_id
        )

    def complete_oracle_request(
        self,
        request_id: str,
        *,
        attempt: int,
        evidence_collection_task: dict,
        oracle_run_id: str,
        candidate_evidence_package: dict,
    ) -> OracleRequest:
        return self._replace_postgres_oracle_request(
            request_id,
            attempt=attempt,
            status="pending_gate_b",
            evidence_collection_task=evidence_collection_task,
            oracle_run_id=oracle_run_id,
            candidate_evidence_package=candidate_evidence_package,
            lease_until=None,
            error_code=None,
        )

    def fail_oracle_request(
        self, request_id: str, *, attempt: int, error_code: str, max_attempts: int
    ) -> OracleRequest:
        return self._replace_postgres_oracle_request(
            request_id,
            attempt=attempt,
            status="failed" if attempt >= max_attempts else "queued",
            lease_until=None,
            error_code=error_code,
        )

    def review_oracle_request(
        self,
        request_id: str,
        *,
        review: OracleGateBReview,
        accepted_oracle_package: dict | None,
    ) -> OracleRequest:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_json FROM learning_oracle_requests WHERE id=%s FOR UPDATE",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            current = OracleRequest.model_validate_json(row["request_json"])
            if current.status != "pending_gate_b":
                raise ValueError("Gate B review requires pending candidate evidence")
            updated = current.model_copy(
                update={
                    "status": review.status,
                    "gate_b_review": review,
                    "accepted_oracle_package": accepted_oracle_package,
                    "updated_at": now,
                }
            )
            conn.execute(
                "UPDATE learning_oracle_requests SET request_json=%s,updated_at=%s WHERE id=%s",
                (updated.model_dump_json(), now, request_id),
            )
        return updated

    def _postgres_oracle_request_by_id(self, request_id: str) -> OracleRequest:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_json FROM learning_oracle_requests WHERE id=%s", (request_id,)
            ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return OracleRequest.model_validate_json(row["request_json"])

    def _replace_postgres_oracle_request(
        self, request_id: str, *, attempt: int, **changes: Any
    ) -> OracleRequest:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_json FROM learning_oracle_requests WHERE id=%s FOR UPDATE",
                (request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            current = OracleRequest.model_validate_json(row["request_json"])
            if current.status != "running" or current.attempts != attempt:
                raise LeaseOwnershipLost("Oracle lease attempt is no longer owned")
            updated = OracleRequest.model_validate(
                {**current.model_dump(), **changes, "updated_at": _now()}
            )
            conn.execute(
                "UPDATE learning_oracle_requests SET request_json=%s,updated_at=%s WHERE id=%s",
                (updated.model_dump_json(), updated.updated_at, request_id),
            )
        return updated

    def enqueue_skill_optimization_task(
        self,
        *,
        source_run_id: str,
        target_skill_id: str,
        input_source: str = "accepted_oracle",
        oracle_request_id: str | None = None,
    ) -> SkillOptimizationTask:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("learning-skill-optimization:global",),
            )
            active_experiment = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_experiments
                WHERE status IN ('queued','running','pending_acceptance') LIMIT 1""",
            ).fetchone()
            if active_experiment is not None:
                raise ValueError("target skill already has an active experiment")
            active_legacy = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_tasks
                WHERE source_run_id<>%s AND task_json::jsonb->>'status'
                  IN ('queued','running','pending_acceptance') LIMIT 1""",
                (source_run_id,),
            ).fetchone()
            if active_legacy is not None:
                raise ValueError("system already has an active legacy optimization")
            candidate = conn.execute(
                """SELECT reference_answer FROM learning_case_candidates
                WHERE run_id=%s FOR UPDATE""",
                (source_run_id,),
            ).fetchone()
            if candidate is None:
                raise KeyError(source_run_id)
            task = SkillOptimizationTask(
                id=str(uuid4()),
                source_run_id=source_run_id,
                input_source=input_source,
                oracle_request_id=oracle_request_id,
                target_skill_id=target_skill_id,
                reference_answer=candidate["reference_answer"],
                status="queued",
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                """INSERT INTO learning_skill_optimization_tasks
                (id,source_run_id,oracle_request_id,task_json,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(source_run_id) DO NOTHING""",
                (task.id, source_run_id, oracle_request_id, task.model_dump_json(), now, now),
            )
            row = conn.execute(
                """SELECT task_json FROM learning_skill_optimization_tasks
                WHERE source_run_id=%s""",
                (source_run_id,),
            ).fetchone()
        return SkillOptimizationTask.model_validate_json(row["task_json"])

    def get_skill_optimization_task(self, source_run_id: str) -> SkillOptimizationTask | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_json FROM learning_skill_optimization_tasks WHERE source_run_id=%s",
                (source_run_id,),
            ).fetchone()
        return SkillOptimizationTask.model_validate_json(row["task_json"]) if row else None

    def list_skill_optimization_tasks(self, *, limit: int) -> list[SkillOptimizationTask]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT task_json FROM learning_skill_optimization_tasks
                ORDER BY updated_at DESC,id DESC LIMIT %s""",
                (limit,),
            ).fetchall()
        return [SkillOptimizationTask.model_validate_json(row["task_json"]) for row in rows]

    def claim_skill_optimization_tasks(
        self, *, limit: int, lease_seconds: float
    ) -> list[SkillOptimizationTask]:
        now = _now()
        claimed = []
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id,task_json FROM learning_skill_optimization_tasks
                ORDER BY created_at,id FOR UPDATE SKIP LOCKED"""
            ).fetchall()
            for row in rows:
                task = SkillOptimizationTask.model_validate_json(row["task_json"])
                expired = task.status == "running" and task.lease_until and task.lease_until < now
                if task.status != "queued" and not expired:
                    continue
                updated = task.model_copy(
                    update={
                        "status": "running",
                        "attempts": task.attempts + 1,
                        "lease_until": now + timedelta(seconds=lease_seconds),
                        "error_code": None,
                        "updated_at": now,
                    }
                )
                conn.execute(
                    """UPDATE learning_skill_optimization_tasks
                    SET task_json=%s,updated_at=%s WHERE id=%s""",
                    (updated.model_dump_json(), now, updated.id),
                )
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
        return claimed

    def complete_skill_optimization_task(
        self,
        task_id: str,
        *,
        attempt: int,
        experiment_id: str,
        result_summary: dict,
        acceptance_manifest: dict,
    ) -> SkillOptimizationTask:
        return self._replace_postgres_skill_optimization_task(
            task_id,
            attempt=attempt,
            status="pending_acceptance",
            experiment_id=experiment_id,
            result_summary=result_summary,
            acceptance_manifest=acceptance_manifest,
            lease_until=None,
            error_code=None,
        )

    def fail_skill_optimization_task(
        self, task_id: str, *, attempt: int, error_code: str, max_attempts: int
    ) -> SkillOptimizationTask:
        return self._replace_postgres_skill_optimization_task(
            task_id,
            attempt=attempt,
            status="failed" if attempt >= max_attempts else "queued",
            error_code=error_code,
            lease_until=None,
        )

    def review_skill_optimization_task(
        self, task_id: str, *, decision: str, reviewer: str, note: str | None
    ) -> SkillOptimizationTask:
        if decision not in {"keep", "reject"}:
            raise ValueError("unsupported optimization acceptance decision")
        current = self._postgres_skill_optimization_task_by_id(task_id)
        if current.status != "pending_acceptance":
            raise ValueError("optimization review requires pending acceptance")
        return self._replace_postgres_skill_optimization_task(
            task_id,
            status="accepted" if decision == "keep" else "rejected",
            acceptance_review=SkillOptimizationAcceptanceReview(
                decision=decision, reviewer=reviewer, note=note, created_at=_now()
            ),
        )

    def _postgres_skill_optimization_task_by_id(self, task_id: str) -> SkillOptimizationTask:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_json FROM learning_skill_optimization_tasks WHERE id=%s", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return SkillOptimizationTask.model_validate_json(row["task_json"])

    def _replace_postgres_skill_optimization_task(
        self, task_id: str, *, attempt: int | None = None, **changes: Any
    ) -> SkillOptimizationTask:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT task_json FROM learning_skill_optimization_tasks WHERE id=%s FOR UPDATE",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = SkillOptimizationTask.model_validate_json(row["task_json"])
            if attempt is not None and (current.status != "running" or current.attempts != attempt):
                raise LeaseOwnershipLost("SkillOpt lease attempt is no longer owned")
            updated = SkillOptimizationTask.model_validate(
                {**current.model_dump(), **changes, "updated_at": _now()}
            )
            conn.execute(
                """UPDATE learning_skill_optimization_tasks
                SET task_json=%s,updated_at=%s WHERE id=%s""",
                (updated.model_dump_json(), updated.updated_at, task_id),
            )
        return updated

    def create_skill_optimization_experiment(
        self,
        *,
        run_ids: list[str],
        config: SkillOptimizationExperimentConfig,
        created_by: str,
    ) -> SkillOptimizationExperiment:
        _validate_experiment_run_ids(run_ids)
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("learning-skill-optimization:global",),
            )
            active = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_experiments
                WHERE status IN ('queued','running','pending_acceptance') LIMIT 1"""
            ).fetchone()
            if active is not None:
                raise ValueError("target skill already has an active experiment")
            active_legacy = conn.execute(
                """SELECT 1 FROM learning_skill_optimization_tasks
                WHERE task_json::jsonb->>'status'
                    IN ('queued','running','pending_acceptance') LIMIT 1"""
            ).fetchone()
            if active_legacy is not None:
                raise ValueError("target skill already has an active legacy task")
            cases = [self._freeze_postgres_experiment_case(conn, run_id) for run_id in run_ids]
            experiment = SkillOptimizationExperiment(
                id=str(uuid4()),
                config=config,
                cases=cases,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                """INSERT INTO learning_skill_optimization_experiments
                (id,target_skill_id,status,experiment_json,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    experiment.id,
                    experiment.target_skill_id,
                    experiment.status,
                    experiment.model_dump_json(),
                    now,
                    now,
                ),
            )
            for run_id in run_ids:
                conn.execute(
                    """INSERT INTO learning_skill_optimization_experiment_cases
                    (experiment_id,run_id) VALUES (%s,%s)""",
                    (experiment.id, run_id),
                )
        return experiment

    @staticmethod
    def _freeze_postgres_experiment_case(conn: Any, run_id: str) -> SkillOptimizationExperimentCase:
        candidate = conn.execute(
            """SELECT status,active,reference_answer FROM learning_case_candidates
            WHERE run_id=%s FOR UPDATE""",
            (run_id,),
        ).fetchone()
        if candidate is None:
            raise KeyError(run_id)
        if candidate["status"] != "approved" or not candidate["active"]:
            raise ValueError(f"experiment case {run_id} must be approved and active")
        snapshot_row = conn.execute(
            "SELECT snapshot_json FROM learning_case_snapshots WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        if snapshot_row is None:
            raise ValueError(f"approved experiment case {run_id} has no Gate A snapshot")
        oracle_row = conn.execute(
            """SELECT request_json FROM learning_oracle_requests
            WHERE source_run_id=%s FOR UPDATE""",
            (run_id,),
        ).fetchone()
        oracle = (
            OracleRequest.model_validate_json(oracle_row["request_json"])
            if oracle_row is not None
            else None
        )
        accepted = (
            oracle
            if oracle is not None
            and oracle.status in {"accepted", "revised"}
            and oracle.accepted_oracle_package is not None
            else None
        )
        return SkillOptimizationExperimentCase(
            run_id=run_id,
            reference_answer=candidate["reference_answer"],
            gate_a_snapshot=ApprovedCaseSnapshot.model_validate_json(snapshot_row["snapshot_json"]),
            oracle_request_id=accepted.id if accepted is not None else None,
            accepted_oracle_package=(
                accepted.accepted_oracle_package if accepted is not None else None
            ),
        )

    def get_skill_optimization_experiment(
        self, experiment_id: str
    ) -> SkillOptimizationExperiment | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT experiment_json FROM learning_skill_optimization_experiments
                WHERE id=%s""",
                (experiment_id,),
            ).fetchone()
        return (
            SkillOptimizationExperiment.model_validate_json(row["experiment_json"])
            if row is not None
            else None
        )

    def list_skill_optimization_experiments(
        self, *, limit: int
    ) -> list[SkillOptimizationExperiment]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT experiment_json FROM learning_skill_optimization_experiments
                ORDER BY updated_at DESC,id DESC LIMIT %s""",
                (limit,),
            ).fetchall()
        return [
            SkillOptimizationExperiment.model_validate_json(row["experiment_json"]) for row in rows
        ]

    def create_proposal(
        self, *, run_id: str, bucket: str, risk: str, reason_codes: list[str]
    ) -> OptimizationProposal:
        now = _now()
        proposal = OptimizationProposal(
            run_id=run_id,
            status="queued",
            bucket=bucket,
            risk=risk,
            reason_codes=reason_codes,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO learning_optimization_proposals (run_id,proposal_json,updated_at)
                VALUES (%s,%s,%s) ON CONFLICT(run_id) DO UPDATE SET
                proposal_json=EXCLUDED.proposal_json,updated_at=EXCLUDED.updated_at""",
                (run_id, proposal.model_dump_json(), now.isoformat()),
            )
        return proposal

    def get_proposal(self, run_id: str) -> OptimizationProposal | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT proposal_json FROM learning_optimization_proposals WHERE run_id=%s",
                (run_id,),
            ).fetchone()
        return OptimizationProposal.model_validate_json(row["proposal_json"]) if row else None

    def mark_proposal_running(self, run_id: str) -> OptimizationProposal:
        return self._replace_proposal(run_id, status="running")

    def mark_proposal_queued(
        self, run_id: str, *, error_code: str | None = None
    ) -> OptimizationProposal:
        return self._replace_proposal(run_id, status="queued", error_code=error_code)

    def complete_proposal(
        self,
        run_id: str,
        *,
        analysis: OptimizationProposalAnalysis,
        analysis_model: str,
        answer_review_replay: AnswerReviewReplayOutcome | None = None,
    ) -> OptimizationProposal:
        return self._replace_proposal(
            run_id,
            status="ready",
            analysis=analysis,
            analysis_model=analysis_model,
            answer_review_replay=answer_review_replay,
        )

    def fail_proposal(self, run_id: str, *, error_code: str) -> OptimizationProposal:
        return self._replace_proposal(run_id, status="failed", error_code=error_code)

    def _replace_proposal(self, run_id: str, **changes: Any) -> OptimizationProposal:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT proposal_json FROM learning_optimization_proposals
                WHERE run_id=%s FOR UPDATE""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = OptimizationProposal.model_validate_json(row["proposal_json"])
            proposal = current.model_copy(update={**changes, "updated_at": _now()})
            conn.execute(
                """UPDATE learning_optimization_proposals SET proposal_json=%s,updated_at=%s
                WHERE run_id=%s""",
                (proposal.model_dump_json(), proposal.updated_at.isoformat(), run_id),
            )
        return proposal

    def enqueue_analysis_task(self, *, run_id: str) -> OptimizationAnalysisTask:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM learning_optimization_analysis_tasks
                WHERE run_id=%s AND status IN ('queued','running')
                ORDER BY created_at DESC LIMIT 1 FOR UPDATE""",
                (run_id,),
            ).fetchone()
            if row is not None:
                return _analysis_task(row)
            task = OptimizationAnalysisTask(
                id=str(uuid4()),
                run_id=run_id,
                status="queued",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            conn.execute(
                """INSERT INTO learning_optimization_analysis_tasks
                (id,run_id,status,attempts,error_code,lease_until,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (task.id, run_id, "queued", 0, None, None, now, now),
            )
        return task

    def get_analysis_task(self, task_id: str) -> OptimizationAnalysisTask | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learning_optimization_analysis_tasks WHERE id=%s", (task_id,)
            ).fetchone()
        return _analysis_task(row) if row else None

    def latest_analysis_task(self, run_id: str) -> OptimizationAnalysisTask | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM learning_optimization_analysis_tasks
                WHERE run_id=%s ORDER BY created_at DESC,id DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        return _analysis_task(row) if row else None

    def update_analysis_activity(self, task_id: str, *, attempt: int, kind: str, text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET activity_kind=%s,activity_text=%s
                WHERE id=%s AND status='running' AND attempts=%s""",
                (kind, text[-4000:], task_id, attempt),
            )

    def claim_analysis_tasks(
        self, *, limit: int, lease_seconds: float
    ) -> list[OptimizationAnalysisTask]:
        if limit < 1 or lease_seconds <= 0:
            raise ValueError("limit and lease_seconds must be positive")
        now = _now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as conn:
            conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET status='queued',lease_until=NULL,updated_at=%s
                WHERE status='running' AND lease_until<%s""",
                (now, now),
            )
            rows = conn.execute(
                """SELECT id FROM learning_optimization_analysis_tasks
                WHERE status='queued' ORDER BY created_at,id
                FOR UPDATE SKIP LOCKED LIMIT %s""",
                (limit,),
            ).fetchall()
            tasks = []
            for row in rows:
                updated = conn.execute(
                    """UPDATE learning_optimization_analysis_tasks
                    SET status='running',attempts=attempts+1,error_code=NULL,
                    activity_kind=NULL,activity_text=NULL,
                    lease_until=%s,updated_at=%s WHERE id=%s RETURNING *""",
                    (lease_until, now, row["id"]),
                ).fetchone()
                task = _analysis_task(updated)
                if conn.execute(
                    "SELECT 1 FROM learning_optimization_proposals WHERE run_id=%s",
                    (task.run_id,),
                ).fetchone():
                    self._update_postgres_proposal(
                        conn, task.run_id, status="running", error_code=None
                    )
                tasks.append(task)
        return tasks

    def mark_analysis_task_completed(
        self, task_id: str, *, attempt: int
    ) -> OptimizationAnalysisTask:
        return self._update_analysis_task(
            task_id, attempt=attempt, status="completed", error_code=None
        )

    def mark_analysis_task_failed(
        self, task_id: str, *, attempt: int, error_code: str, max_attempts: int
    ) -> OptimizationAnalysisTask:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT status,attempts FROM learning_optimization_analysis_tasks
                WHERE id=%s FOR UPDATE""",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] != "running" or row["attempts"] != attempt:
                raise LeaseOwnershipLost("analysis lease attempt is no longer owned")
            status = "failed" if row["attempts"] >= max_attempts else "queued"
            updated = conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET status=%s,error_code=%s,lease_until=NULL,updated_at=%s
                WHERE id=%s RETURNING *""",
                (status, error_code, _now(), task_id),
            ).fetchone()
            if conn.execute(
                "SELECT 1 FROM learning_optimization_proposals WHERE run_id=%s",
                (updated["run_id"],),
            ).fetchone():
                self._update_postgres_proposal(
                    conn, updated["run_id"], status=status, error_code=error_code
                )
        return _analysis_task(updated)

    def cancel_analysis_task(self, task_id: str) -> OptimizationAnalysisTask:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learning_optimization_analysis_tasks WHERE id=%s FOR UPDATE",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = _analysis_task(row)
            if task.status not in {"queued", "running"}:
                raise ValueError("only queued or running analysis tasks can be cancelled")
            task_row = conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET status='cancelled',lease_until=NULL,updated_at=%s
                WHERE id=%s RETURNING *""",
                (now, task_id),
            ).fetchone()
            self._update_postgres_proposal(
                conn, task.run_id, status="cancelled", error_code="Cancelled"
            )
        return _analysis_task(task_row)

    def commit_analysis_result(
        self,
        task_id: str,
        *,
        attempt: int,
        analysis: OptimizationProposalAnalysis,
        analysis_model: str,
        answer_review_replay: AnswerReviewReplayOutcome | None = None,
    ) -> OptimizationAnalysisTask:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learning_optimization_analysis_tasks WHERE id=%s FOR UPDATE",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = _analysis_task(row)
            if task.attempts != attempt:
                raise LeaseOwnershipLost("analysis lease attempt is no longer owned")
            if task.status == "cancelled":
                return task
            if task.status != "running":
                raise ValueError("analysis result requires a running task")
            self._update_postgres_proposal(
                conn,
                task.run_id,
                status="ready",
                analysis=analysis,
                analysis_model=analysis_model,
                answer_review_replay=answer_review_replay,
                error_code=None,
            )
            task_row = conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET status='completed',lease_until=NULL,updated_at=%s
                WHERE id=%s RETURNING *""",
                (_now(), task_id),
            ).fetchone()
        return _analysis_task(task_row)

    @staticmethod
    def _update_postgres_proposal(conn: Any, run_id: str, **changes: Any) -> None:
        row = conn.execute(
            """SELECT proposal_json FROM learning_optimization_proposals
            WHERE run_id=%s FOR UPDATE""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        proposal = OptimizationProposal.model_validate_json(row["proposal_json"]).model_copy(
            update={**changes, "updated_at": _now()}
        )
        conn.execute(
            """UPDATE learning_optimization_proposals SET proposal_json=%s,updated_at=%s
            WHERE run_id=%s""",
            (proposal.model_dump_json(), proposal.updated_at, run_id),
        )

    def _update_analysis_task(
        self, task_id: str, *, attempt: int, status: str, error_code: str | None
    ) -> OptimizationAnalysisTask:
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE learning_optimization_analysis_tasks
                SET status=%s,error_code=%s,lease_until=NULL,updated_at=%s
                WHERE id=%s AND status='running' AND attempts=%s RETURNING *""",
                (status, error_code, _now(), task_id, attempt),
            ).fetchone()
            if row is None:
                raise LeaseOwnershipLost("analysis lease attempt is no longer owned")
        return _analysis_task(row)

    def remove_candidate(
        self, *, run_id: str, reviewer: str, reason: str | None, created_at: datetime
    ) -> CandidateCase:
        with self._connect() as conn:
            candidate_row = conn.execute(
                "SELECT * FROM learning_case_candidates WHERE run_id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if candidate_row is None or not candidate_row["active"]:
                raise KeyError(run_id)
            if candidate_row["status"] != "pending":
                raise ValueError("only pending candidates can be removed")
            revision_row = conn.execute(
                "SELECT MAX(revision) AS revision FROM learning_case_decisions WHERE run_id=%s",
                (run_id,),
            ).fetchone()
            decision = CaseReviewDecision(
                id=str(uuid4()),
                run_id=run_id,
                revision=int(revision_row["revision"] or 0) + 1,
                status="removed",
                reviewer=reviewer,
                reason=reason,
                created_at=created_at,
            )
            conn.execute(
                """INSERT INTO learning_case_decisions
                (id,run_id,revision,status,reviewer,reason,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (
                    decision.id,
                    run_id,
                    decision.revision,
                    decision.status,
                    reviewer,
                    reason,
                    created_at.isoformat(),
                ),
            )
            conn.execute(
                """UPDATE learning_case_candidates SET status='removed',active=FALSE,
                current_reason=%s,updated_at=%s WHERE run_id=%s""",
                (reason, created_at.isoformat(), run_id),
            )
            return _candidate(
                conn.execute(
                    "SELECT * FROM learning_case_candidates WHERE run_id=%s", (run_id,)
                ).fetchone()
            )

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        with pooled_connection(self.dsn) as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_case_candidates (
 run_id TEXT PRIMARY KEY,status TEXT NOT NULL,source TEXT NOT NULL,bucket TEXT,
 reason_codes_json TEXT NOT NULL,added_by TEXT NOT NULL,added_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,current_reason TEXT,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS learning_case_decisions (
 id TEXT PRIMARY KEY,run_id TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,
 reviewer TEXT NOT NULL,reason TEXT,created_at TEXT NOT NULL,UNIQUE(run_id,revision));
CREATE TABLE IF NOT EXISTS learning_case_snapshots (
 run_id TEXT PRIMARY KEY,snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_optimization_proposals (
 run_id TEXT PRIMARY KEY,proposal_json TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_optimization_analysis_tasks (
 id TEXT PRIMARY KEY,run_id TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL,
 error_code TEXT,activity_kind TEXT,activity_text TEXT,lease_until TEXT,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_oracle_requests (
 id TEXT PRIMARY KEY,source_run_id TEXT NOT NULL UNIQUE,request_json TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_skill_optimization_tasks (
 id TEXT PRIMARY KEY,source_run_id TEXT NOT NULL UNIQUE,oracle_request_id TEXT UNIQUE,
 task_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_skill_optimization_experiments (
 id TEXT PRIMARY KEY,target_skill_id TEXT NOT NULL,status TEXT NOT NULL,
 experiment_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_skill_optimization_experiment_cases (
 experiment_id TEXT NOT NULL,run_id TEXT NOT NULL,
 PRIMARY KEY(experiment_id,run_id),
 FOREIGN KEY(experiment_id) REFERENCES learning_skill_optimization_experiments(id));
DROP INDEX IF EXISTS learning_skill_optimization_one_active_target;
CREATE INDEX IF NOT EXISTS learning_analysis_tasks_status_created
 ON learning_optimization_analysis_tasks(status,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS learning_analysis_tasks_one_active_run
 ON learning_optimization_analysis_tasks(run_id) WHERE status IN ('queued','running');
"""

_POSTGRES_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS learning_case_candidates (
      run_id TEXT PRIMARY KEY,status TEXT NOT NULL,source TEXT NOT NULL,bucket TEXT,
      reason_codes_json TEXT NOT NULL,added_by TEXT NOT NULL,added_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,current_reason TEXT,active BOOLEAN NOT NULL DEFAULT TRUE)""",
    """ALTER TABLE learning_case_candidates ADD COLUMN IF NOT EXISTS active
      BOOLEAN NOT NULL DEFAULT TRUE""",
    """ALTER TABLE learning_case_candidates ADD COLUMN IF NOT EXISTS reference_answer TEXT""",
    """CREATE TABLE IF NOT EXISTS learning_case_decisions (
      id TEXT PRIMARY KEY,run_id TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,
      reviewer TEXT NOT NULL,reason TEXT,created_at TEXT NOT NULL,UNIQUE(run_id,revision))""",
    """CREATE TABLE IF NOT EXISTS learning_case_snapshots (
      run_id TEXT PRIMARY KEY,snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS learning_optimization_proposals (
      run_id TEXT PRIMARY KEY,proposal_json TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS learning_optimization_analysis_tasks (
      id TEXT PRIMARY KEY,run_id TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL,
      error_code TEXT,activity_kind TEXT,activity_text TEXT,lease_until TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL)""",
    "ALTER TABLE learning_optimization_analysis_tasks ADD COLUMN IF NOT EXISTS activity_kind TEXT",
    "ALTER TABLE learning_optimization_analysis_tasks ADD COLUMN IF NOT EXISTS activity_text TEXT",
    """CREATE TABLE IF NOT EXISTS learning_oracle_requests (
      id TEXT PRIMARY KEY,source_run_id TEXT NOT NULL UNIQUE,request_json TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS learning_skill_optimization_tasks (
      id TEXT PRIMARY KEY,source_run_id TEXT NOT NULL UNIQUE,oracle_request_id TEXT UNIQUE,
      task_json TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL)""",
    """ALTER TABLE learning_skill_optimization_tasks
      ALTER COLUMN oracle_request_id DROP NOT NULL""",
    """CREATE TABLE IF NOT EXISTS learning_skill_optimization_experiments (
      id TEXT PRIMARY KEY,target_skill_id TEXT NOT NULL,status TEXT NOT NULL,
      experiment_json TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS learning_skill_optimization_experiment_cases (
      experiment_id TEXT NOT NULL REFERENCES learning_skill_optimization_experiments(id),
      run_id TEXT NOT NULL,PRIMARY KEY(experiment_id,run_id))""",
    "DROP INDEX IF EXISTS learning_skill_optimization_one_active_target",
    """CREATE INDEX IF NOT EXISTS learning_analysis_tasks_status_created
      ON learning_optimization_analysis_tasks(status,created_at)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS learning_analysis_tasks_one_active_run
      ON learning_optimization_analysis_tasks(run_id)
      WHERE status IN ('queued','running')""",
)

_ACTIVE_EXPERIMENT_INDEX = """CREATE UNIQUE INDEX IF NOT EXISTS
learning_skill_optimization_one_active_global ON learning_skill_optimization_experiments((1))
WHERE status IN ('queued','running','pending_acceptance')"""


def _candidate(row: Mapping[str, Any]) -> CandidateCase:
    return CandidateCase(
        run_id=row["run_id"],
        status=row["status"],
        source=row["source"],
        bucket=row["bucket"],
        reason_codes=json.loads(row["reason_codes_json"]),
        added_by=row["added_by"],
        added_at=datetime.fromisoformat(row["added_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        current_reason=row["current_reason"],
        reference_answer=row["reference_answer"],
        active=bool(row["active"]),
    )


def _decision(row: Mapping[str, Any]) -> CaseReviewDecision:
    return CaseReviewDecision(
        id=row["id"],
        run_id=row["run_id"],
        revision=row["revision"],
        status=row["status"],
        reviewer=row["reviewer"],
        reason=row["reason"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _analysis_task(row: Mapping[str, Any]) -> OptimizationAnalysisTask:
    columns = set(row.keys())
    return OptimizationAnalysisTask(
        id=row["id"],
        run_id=row["run_id"],
        status=row["status"],
        attempts=row["attempts"],
        error_code=row["error_code"],
        activity_kind=row["activity_kind"] if "activity_kind" in columns else None,
        activity_text=row["activity_text"] if "activity_text" in columns else None,
        lease_until=_stored_datetime(row["lease_until"]) if row["lease_until"] else None,
        created_at=_stored_datetime(row["created_at"]),
        updated_at=_stored_datetime(row["updated_at"]),
    )


def _stored_datetime(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _validate_experiment_run_ids(run_ids: list[str]) -> None:
    if not run_ids:
        raise ValueError("experiment requires at least one run id")
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("experiment contains duplicate run id")


def _oracle_request_for_snapshot(
    snapshot: ApprovedCaseSnapshot, decision: CaseReviewDecision
) -> OracleRequest | None:
    analysis = snapshot.optimization_proposal.analysis if snapshot.optimization_proposal else None
    if analysis is None:
        return None
    knowledge = snapshot.behavior_snapshot.get("knowledge_start_state") or {}
    selected = knowledge.get("selected_knowledge_assets") or []
    libraries = {str(item.get("library_id") or "") for item in selected if isinstance(item, dict)}
    libraries.discard("")
    if not libraries:
        raw_citations = snapshot.citations
        citations = (
            list(raw_citations.values()) if isinstance(raw_citations, dict) else raw_citations
        )
        libraries = {
            str(item.get("library_id") or ((item.get("source") or {}).get("library_id")) or "")
            for item in citations
            if isinstance(item, dict)
        }
        libraries.discard("")
    library_id = next(iter(libraries)) if len(libraries) == 1 else None
    asset_ids = list(
        dict.fromkeys(
            str(item.get("asset_id") or "") for item in selected if isinstance(item, dict)
        )
    )
    asset_ids = [asset_id for asset_id in asset_ids if asset_id]
    now = decision.created_at
    return OracleRequest(
        id=str(uuid4()),
        source_run_id=snapshot.run_id,
        gate_a_decision_ref=f"review://self-learning/{decision.id}",
        status="pending_scope_resolution",
        library_id=library_id,
        asset_ids=asset_ids,
        oracle_reason=analysis.oracle_reason or "人工请求查找参考答案和支持证据。",
        created_at=now,
        updated_at=now,
    )


def _insert_sqlite_oracle_request(
    conn: sqlite3.Connection, *, snapshot: ApprovedCaseSnapshot, decision: CaseReviewDecision
) -> None:
    request = _oracle_request_for_snapshot(snapshot, decision)
    if request is None:
        return
    conn.execute(
        """INSERT INTO learning_oracle_requests
        (id,source_run_id,request_json,created_at,updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(source_run_id) DO UPDATE SET
          id=excluded.id,request_json=excluded.request_json,
          created_at=excluded.created_at,updated_at=excluded.updated_at
        WHERE json_extract(learning_oracle_requests.request_json,'$.status')='failed'""",
        (
            request.id,
            request.source_run_id,
            request.model_dump_json(),
            request.created_at.isoformat(),
            request.updated_at.isoformat(),
        ),
    )


def _insert_postgres_oracle_request(
    conn: Any, *, snapshot: ApprovedCaseSnapshot, decision: CaseReviewDecision
) -> None:
    request = _oracle_request_for_snapshot(snapshot, decision)
    if request is None:
        return
    conn.execute(
        """INSERT INTO learning_oracle_requests
        (id,source_run_id,request_json,created_at,updated_at) VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (source_run_id) DO UPDATE SET
          id=EXCLUDED.id,request_json=EXCLUDED.request_json,
          created_at=EXCLUDED.created_at,updated_at=EXCLUDED.updated_at
        WHERE (learning_oracle_requests.request_json::jsonb->>'status')='failed'""",
        (
            request.id,
            request.source_run_id,
            request.model_dump_json(),
            request.created_at,
            request.updated_at,
        ),
    )


def _now() -> datetime:
    return datetime.now(UTC)
