"""Factory for the always-available human case review store."""

from viknow.core.config import AppConfig, PersistenceBackend, resolve_learning_postgres_dsn
from viknow.core.learning.case_review_store import (
    CaseReviewStore,
    PostgresCaseReviewStore,
    SqliteCaseReviewStore,
)
from viknow.core.postgres_bootstrap import ensure_learning_postgres_schema


def build_case_review_store(config: AppConfig) -> CaseReviewStore:
    if config.persistence.backend == PersistenceBackend.SQLITE:
        return SqliteCaseReviewStore(config.app.state_dir / "case-reviews.sqlite")
    if config.persistence.backend == PersistenceBackend.POSTGRES:
        ensure_learning_postgres_schema(config.persistence)
        return PostgresCaseReviewStore(resolve_learning_postgres_dsn(config.persistence))
    raise ValueError(f"unsupported case review persistence backend: {config.persistence.backend}")
