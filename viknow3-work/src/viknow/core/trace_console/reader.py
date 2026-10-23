"""Read existing session tables without schema initialization or business writes."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from time import monotonic

from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from viknow.core.learning.candidates import learning_candidate_risk, select_learning_candidates
from viknow.core.learning.projection import project_learning_record
from viknow.core.runtime.domain import AgentEvent
from viknow.core.trace_console.models import (
    EventPage,
    EventSummary,
    LangfuseTraceReference,
    TraceDetail,
    TracePage,
    TraceQuery,
    TraceRow,
    TraceRuleAssessment,
    TraceSemanticAlignment,
    decode_cursor,
    encode_cursor,
)

_ROW = (
    "r.id, r.session_id, s.profile_id, r.status, substr(r.message,1,240) AS question_preview, "
    "r.created_at, r.started_at, r.ended_at"
)
_JOIN = "FROM runs r JOIN sessions s ON s.id=r.session_id"
_VISIBLE = "s.status='active'"


class TraceReader:
    def __init__(
        self,
        *,
        path: Path | None = None,
        dsn: str | None = None,
        timeout: float = 3,
        pool_size: int = 2,
    ):
        self.path = path
        self.timeout = timeout
        self.pool = None
        self.lock = threading.Lock()
        if dsn:
            kw = conninfo_to_dict(dsn)
            kw["options"] = (
                kw.get("options", "")
                + f" -c default_transaction_read_only=on -c statement_timeout={int(timeout * 1000)}"
            )
            self.pool = ConnectionPool(
                "",
                kwargs={
                    **kw,
                    "row_factory": dict_row,
                    "autocommit": True,
                    "connect_timeout": max(1, int(timeout)),
                },
                min_size=0,
                max_size=pool_size,
                timeout=timeout,
                max_waiting=pool_size,
                open=False,
            )

    @classmethod
    def sqlite(cls, path: Path):
        return cls(path=path)

    @contextmanager
    def connection(self):
        if self.pool is not None:
            with self.lock:
                if self.pool.closed:
                    self.pool.open()
            with self.pool.connection() as conn:
                yield conn
        else:
            assert self.path is not None
            conn = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=self.timeout
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            deadline = monotonic() + self.timeout
            conn.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
            try:
                yield conn
            finally:
                conn.close()

    def close(self):
        if self.pool is not None:
            self.pool.close()

    def _rows(self, sql: str, params: list | tuple = ()):
        with self.connection() as conn:
            statement = sql if self.pool is not None else sql.replace("%s", "?")
            return [dict(row) for row in conn.execute(statement, params).fetchall()]

    def list_runs(self, query: TraceQuery) -> TracePage:
        where = [_VISIBLE]
        params = []
        if query.run_id:
            where.append("r.id=%s")
            params.append(query.run_id)
        else:
            where.extend(["r.created_at>=%s", "r.created_at<=%s"])
            params.extend(
                [query.since.astimezone(UTC).isoformat(), query.until.astimezone(UTC).isoformat()]
            )
        for name, value in [("r.status", query.status), ("s.profile_id", query.profile)]:
            if value:
                where.append(f"{name}=%s")
                params.append(value)
        if query.query:
            where.append("lower(r.message) LIKE lower(%s) ESCAPE '\\'")
            params.append(
                "%"
                + query.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
        if query.cursor:
            created, run_id = decode_cursor(query.cursor)
            where.append("(r.created_at, r.id)<(%s,%s)")
            params.extend([created, run_id])
        params.append(query.limit + 1)
        rows = self._rows(
            f"SELECT {_ROW} {_JOIN} WHERE {' AND '.join(where)} "
            "ORDER BY r.created_at DESC,r.id DESC LIMIT %s",
            params,
        )
        visible_rows = rows[: query.limit]
        assessments = self._rule_assessments([row["id"] for row in visible_rows])
        items = [
            TraceRow.model_validate({**row, "rule_assessment": assessments[row["id"]]})
            for row in visible_rows
        ]
        return TracePage(
            items=items,
            next_cursor=encode_cursor(items[-1].created_at, items[-1].id)
            if len(rows) > query.limit
            else None,
        )

    def _rule_assessments(self, run_ids: list[str]) -> dict[str, TraceRuleAssessment]:
        if not run_ids:
            return {}
        placeholders = ",".join("%s" for _ in run_ids)
        rows = self._rows(
            f"SELECT run_id,payload_json FROM events WHERE run_id IN ({placeholders}) "
            "ORDER BY run_id,sequence",
            run_ids,
        )
        events_by_run: dict[str, list[AgentEvent]] = {run_id: [] for run_id in run_ids}
        for row in rows:
            events_by_run[row["run_id"]].append(AgentEvent.model_validate_json(row["payload_json"]))

        assessments = {}
        for run_id, events in events_by_run.items():
            if events:
                candidate = select_learning_candidates([project_learning_record(events)])[0]
                assessments[run_id] = TraceRuleAssessment(
                    bucket=candidate.bucket,
                    risk=learning_candidate_risk(candidate),
                    reason_codes=candidate.reason_codes[:2],
                )
            else:
                assessments[run_id] = TraceRuleAssessment(
                    bucket="uncertain",
                    risk="medium",
                    reason_codes=["insufficient_signals"],
                )
        return assessments

    def _run(self, run_id: str):
        rows = self._rows(
            f"SELECT {_ROW},r.message,r.error {_JOIN} WHERE {_VISIBLE} AND r.id=%s", [run_id]
        )
        if not rows:
            raise KeyError(run_id)
        return rows[0]

    def run(self, run_id: str) -> TraceRow:
        return TraceRow.model_validate(self._run(run_id))

    def _latest(self, run_id: str, types: tuple[str, ...]) -> dict:
        placeholders = ",".join("%s" for _ in types)
        rows = self._rows(
            f"SELECT payload_json FROM events WHERE run_id=%s AND type IN ({placeholders}) "
            "ORDER BY sequence DESC LIMIT 1",
            [run_id, *types],
        )
        return json.loads(rows[0]["payload_json"]).get("payload", {}) if rows else {}

    def _latest_tool(self, run_id: str, name: str) -> dict:
        if self.pool is not None:
            name_clause = "payload_json::jsonb #>> '{payload,name}'=%s"
        else:
            name_clause = "json_extract(payload_json,'$.payload.name')=%s"
        rows = self._rows(
            "SELECT payload_json FROM events WHERE run_id=%s AND type=%s AND "
            f"{name_clause} ORDER BY sequence DESC LIMIT 1",
            [run_id, "tool_call_completed", name],
        )
        return json.loads(rows[0]["payload_json"]).get("payload", {}) if rows else {}

    def _semantic_alignment(self, run_id: str) -> TraceSemanticAlignment:
        from viknow.core.tools.base import parse_tool_payload

        event = self._latest_tool(run_id, "answer_plan")
        plan_view = parse_tool_payload(event.get("result"))
        review = plan_view.get("review")
        if not isinstance(review, dict):
            return TraceSemanticAlignment()
        uncertainties = review.get("uncertainties") or []
        return TraceSemanticAlignment(
            state="unresolved" if uncertainties else "clear",
            original_question_check=review.get("original_question_check"),
            uncertainties=uncertainties,
            review_is_current=bool(plan_view.get("review_is_current")),
            authority=plan_view.get("review_authority"),
        )

    def detail(self, run_id: str) -> TraceDetail:
        row = self._run(run_id)
        start = self._latest(run_id, ("run_started",))
        answer = self._latest(run_id, ("message_completed",))
        terminal = self._latest(run_id, ("run_completed", "run_failed", "run_interrupted"))
        review = self._latest(run_id, ("answer_review_completed",))
        gate = self._latest(run_id, ("stop_gate_evaluated",))
        snapshot = start.get("behavior_snapshot") or {}
        if "result" in review:
            review_result = review["result"]
        elif "answer_review" in review:
            review_result = review["answer_review"]
        else:
            review_result = review
        review_input = review.get("review_input") or {}
        review_config = review_input.get("config") or {}
        links = terminal.get("trace_links") or []
        import re

        references = []
        seen = set()
        rejected_reason = None
        for link in links:
            if (
                not isinstance(link, dict)
                or link.get("state") != "resolved"
                or not re.fullmatch("[0-9a-f]{32}", str(link.get("trace_id", "")))
            ):
                continue
            identity_values = tuple(
                link.get(field)
                for field in ("service_id", "environment", "source_id", "project_id")
            )
            if link.get("provider") != "langfuse" or not all(identity_values):
                rejected_reason = (
                    "historical_trace_source_unknown"
                    if link.get("provider") is None and not any(identity_values)
                    else "invalid_trace_source_identity"
                )
                continue
            identity = (
                link["trace_id"],
                link.get("source_id"),
                link.get("project_id"),
            )
            if identity in seen:
                continue
            seen.add(identity)
            references.append(
                LangfuseTraceReference(
                    trace_id=link["trace_id"],
                    service_id=link.get("service_id"),
                    environment=link.get("environment"),
                    source_id=link.get("source_id"),
                    project_id=link.get("project_id"),
                )
            )
        return TraceDetail(
            run=TraceRow.model_validate(row),
            question=row["message"],
            error=row["error"],
            answer=answer.get("text"),
            citations=answer.get("citations") or {},
            behavior_snapshot=snapshot,
            run_kind=snapshot.get("run_kind", "unknown"),
            answer_review=review_result,
            answer_review_config=review_config,
            stop_gate=gate,
            token_usage=answer.get("token_usage") or {},
            semantic_alignment=self._semantic_alignment(run_id),
            langfuse_traces=references,
            langfuse_reason=next(
                (
                    link.get("reason")
                    for link in links
                    if isinstance(link, dict) and link.get("reason")
                ),
                rejected_reason,
            ),
        )

    def events(self, run_id: str, *, after: int = 0, limit: int = 30) -> EventPage:
        self._run(run_id)
        rows = self._rows(
            "SELECT sequence,type,created_at FROM events WHERE run_id=%s AND sequence>%s "
            "ORDER BY sequence LIMIT %s",
            [run_id, after, limit + 1],
        )
        items = [EventSummary.model_validate(row) for row in rows[:limit]]
        return EventPage(
            items=items, next_sequence=items[-1].sequence if len(rows) > limit else None
        )

    def event(self, run_id: str, sequence: int) -> AgentEvent:
        self._run(run_id)
        rows = self._rows(
            "SELECT payload_json FROM events WHERE run_id=%s AND sequence=%s", [run_id, sequence]
        )
        if not rows:
            raise KeyError(sequence)
        return AgentEvent.model_validate_json(rows[0]["payload_json"])
