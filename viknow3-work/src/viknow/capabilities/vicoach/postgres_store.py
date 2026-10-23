"""Postgres persistence for intelligent video training."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg

from viknow.capabilities.vicoach.domain import (
    AnswerAttempt,
    Assessment,
    InternalQuestionSnapshot,
    QuestionType,
)
from viknow.core.postgres_pool import connection as pooled_connection
from viknow.core.session.mapping import dump_dt, load_dt


class PostgresVicoachStore:
    """Postgres-backed store with tables independent from question-answer sessions."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._initialize()

    def create_assessment(
        self,
        *,
        assessment: Assessment,
        questions: Sequence[InternalQuestionSnapshot],
    ) -> Assessment:
        if any(
            question.assessment_id != assessment.id
            or question.question_type != assessment.question_type
            for question in questions
        ):
            raise ValueError("questions must belong to the assessment and use its question type")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO vicoach_assessments (
                    id, user_id, library_id, asset_id, chapter_order, question_type, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                self._assessment_values(assessment),
            )
            for question in questions:
                conn.execute(
                    """
                    INSERT INTO vicoach_question_snapshots (
                        id, assessment_id, candidate_order, knowledge_point, question_type,
                        question_markdown, internal_question_json, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    self._question_values(question),
                )
        return assessment

    def get_assessment_for_user(self, *, user_id: str, assessment_id: str) -> Assessment | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM vicoach_assessments
                WHERE id = %s AND user_id = %s
                """,
                (assessment_id, user_id),
            ).fetchone()
        return self._assessment_from_row(row) if row is not None else None

    def get_question_for_user(
        self, *, user_id: str, question_id: str
    ) -> InternalQuestionSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT question.*
                FROM vicoach_question_snapshots AS question
                JOIN vicoach_assessments AS assessment
                    ON assessment.id = question.assessment_id
                WHERE question.id = %s AND assessment.user_id = %s
                """,
                (question_id, user_id),
            ).fetchone()
        return self._question_from_row(row) if row is not None else None

    def list_questions_for_user(
        self, *, user_id: str, assessment_id: str
    ) -> list[InternalQuestionSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT question.*
                FROM vicoach_question_snapshots AS question
                JOIN vicoach_assessments AS assessment
                    ON assessment.id = question.assessment_id
                WHERE question.assessment_id = %s AND assessment.user_id = %s
                ORDER BY question.candidate_order ASC, question.id ASC
                """,
                (assessment_id, user_id),
            ).fetchall()
        return [self._question_from_row(row) for row in rows]

    def append_answer_attempt(self, *, user_id: str, attempt: AnswerAttempt) -> AnswerAttempt:
        with self._connect() as conn:
            question = conn.execute(
                """
                SELECT question.assessment_id
                FROM vicoach_question_snapshots AS question
                JOIN vicoach_assessments AS assessment
                    ON assessment.id = question.assessment_id
                WHERE question.id = %s AND assessment.user_id = %s
                """,
                (attempt.question_id, user_id),
            ).fetchone()
            if question is None or question["assessment_id"] != attempt.assessment_id:
                raise KeyError(attempt.question_id)
            conn.execute(
                """
                INSERT INTO vicoach_answer_attempts (
                    id, assessment_id, question_id, answer_json, score, reason, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                self._attempt_values(attempt),
            )
        return attempt

    def list_answer_attempts_for_user(
        self, *, user_id: str, assessment_id: str
    ) -> list[AnswerAttempt]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT attempt.*
                FROM vicoach_answer_attempts AS attempt
                JOIN vicoach_assessments AS assessment
                    ON assessment.id = attempt.assessment_id
                WHERE attempt.assessment_id = %s AND assessment.user_id = %s
                ORDER BY attempt.created_at ASC, attempt.id ASC
                """,
                (assessment_id, user_id),
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vicoach_assessments (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    library_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    chapter_order INTEGER NOT NULL,
                    question_type INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vicoach_question_snapshots (
                    id TEXT PRIMARY KEY,
                    assessment_id TEXT NOT NULL REFERENCES vicoach_assessments(id),
                    candidate_order INTEGER NOT NULL,
                    knowledge_point TEXT NOT NULL,
                    question_type INTEGER NOT NULL,
                    question_markdown TEXT NOT NULL,
                    internal_question_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(assessment_id, candidate_order),
                    UNIQUE(assessment_id, id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vicoach_answer_attempts (
                    id TEXT PRIMARY KEY,
                    assessment_id TEXT NOT NULL REFERENCES vicoach_assessments(id),
                    question_id TEXT NOT NULL REFERENCES vicoach_question_snapshots(id),
                    answer_json TEXT NOT NULL,
                    score DOUBLE PRECISION,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (assessment_id, question_id)
                        REFERENCES vicoach_question_snapshots(assessment_id, id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vicoach_assessments_user_created
                    ON vicoach_assessments(user_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vicoach_attempts_assessment_created
                    ON vicoach_answer_attempts(assessment_id, created_at, id)
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection[Any]]:
        with pooled_connection(self.dsn) as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _assessment_values(assessment: Assessment) -> tuple[str | int, ...]:
        return (
            assessment.id,
            assessment.user_id,
            assessment.library_id,
            assessment.asset_id,
            assessment.chapter_order,
            int(assessment.question_type),
            dump_dt(assessment.created_at),
        )

    @staticmethod
    def _question_values(question: InternalQuestionSnapshot) -> tuple[str | int, ...]:
        return (
            question.id,
            question.assessment_id,
            question.candidate_order,
            question.knowledge_point,
            int(question.question_type),
            question.question_markdown,
            json.dumps(question.internal_question_json),
            dump_dt(question.created_at),
        )

    @staticmethod
    def _attempt_values(attempt: AnswerAttempt) -> tuple[str | float | None, ...]:
        return (
            attempt.id,
            attempt.assessment_id,
            attempt.question_id,
            json.dumps(attempt.answer_json),
            attempt.score,
            attempt.reason,
            dump_dt(attempt.created_at),
        )

    @staticmethod
    def _assessment_from_row(row: dict[str, Any]) -> Assessment:
        return Assessment(
            id=row["id"],
            user_id=row["user_id"],
            library_id=row["library_id"],
            asset_id=row["asset_id"],
            chapter_order=int(row["chapter_order"]),
            question_type=QuestionType(row["question_type"]),
            created_at=load_dt(row["created_at"]),
        )

    @staticmethod
    def _question_from_row(row: dict[str, Any]) -> InternalQuestionSnapshot:
        return InternalQuestionSnapshot(
            id=row["id"],
            assessment_id=row["assessment_id"],
            candidate_order=int(row["candidate_order"]),
            knowledge_point=row["knowledge_point"],
            question_type=QuestionType(row["question_type"]),
            question_markdown=row["question_markdown"],
            internal_question_json=json.loads(row["internal_question_json"]),
            created_at=load_dt(row["created_at"]),
        )

    @staticmethod
    def _attempt_from_row(row: dict[str, Any]) -> AnswerAttempt:
        return AnswerAttempt(
            id=row["id"],
            assessment_id=row["assessment_id"],
            question_id=row["question_id"],
            answer_json=json.loads(row["answer_json"]),
            score=float(row["score"]) if row["score"] is not None else None,
            reason=row["reason"],
            created_at=load_dt(row["created_at"]),
        )
