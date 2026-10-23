"""Langfuse usage aggregation reader for completed agent runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from viknow.agent_adapters.langfuse import build_langfuse_client

_BREAKDOWN_KEY = "viknow_usage_breakdown"
_FINALIZED_KEY = "usage_finalized"
_EMPTY_TOTALS: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
# Self-hosted Langfuse caps observations page size at 100.
_GENERATION_PAGE_SIZE = 100

# status values: ready / not_ready / not_found / error
_STATUS_READY = "ready"
_STATUS_NOT_READY = "not_ready"
_STATUS_NOT_FOUND = "not_found"
_STATUS_ERROR = "error"


@dataclass(frozen=True)
class UsageAggregate:
    """Aggregated token usage for one run/trace."""

    status: str
    totals: dict[str, int]
    by_model: list[dict[str, Any]]
    source: str = ""  # "breakdown" | "generations" | ""
    error: str = ""


def snapshot_has_usage(snapshot: dict[str, Any] | None) -> bool:
    """True when a locally captured snapshot already has non-zero token totals."""
    if not isinstance(snapshot, dict):
        return False
    totals = snapshot.get("totals")
    if not isinstance(totals, dict):
        return False
    return _normalize_totals(totals)["total_tokens"] > 0


def aggregate_from_snapshot(snapshot: dict[str, Any] | None) -> UsageAggregate:
    """Turn a locally captured usage snapshot into a ready aggregate."""
    totals, by_model = _parse_breakdown(snapshot if isinstance(snapshot, dict) else {})
    return UsageAggregate(
        status=_STATUS_READY,
        totals=totals,
        by_model=by_model,
        source="local",
    )


def get_run_usage_aggregate(trace_id: str, *, client: Any | None = None) -> UsageAggregate:
    """Read and aggregate a run's token usage from Langfuse.

    Prefers the run-end ``viknow_usage_breakdown`` written to trace metadata;
    falls back to summing GENERATION observations by model name. Never raises
    into callers except for hard Langfuse config errors (missing credentials).
    """
    lf = client or build_langfuse_client()
    trace = _fetch_trace(lf, trace_id)
    if trace is None:
        return UsageAggregate(
            status=_STATUS_NOT_FOUND,
            totals=dict(_EMPTY_TOTALS),
            by_model=[],
            error="trace not found",
        )
    breakdown = _extract_breakdown(trace)
    if breakdown is not None:
        totals, by_model = _parse_breakdown(breakdown)
        if totals["total_tokens"] > 0 or by_model:
            return UsageAggregate(
                status=_STATUS_READY,
                totals=totals,
                by_model=by_model,
                source="breakdown",
            )
    rows = _fetch_generation_rows(lf, trace_id)
    if not rows:
        return UsageAggregate(
            status=_STATUS_NOT_READY,
            totals=dict(_EMPTY_TOTALS),
            by_model=[],
            error="no generations ingested yet",
        )
    totals, by_model = _aggregate_generation_rows(rows)
    if not totals["total_tokens"] and not by_model:
        return UsageAggregate(
            status=_STATUS_NOT_READY,
            totals=dict(_EMPTY_TOTALS),
            by_model=[],
            error="generations without usable token usage",
        )
    return UsageAggregate(
        status=_STATUS_READY,
        totals=totals,
        by_model=by_model,
        source="generations",
    )


def _fetch_trace(lf: Any, trace_id: str) -> Any | None:
    try:
        return lf.api.trace.get(trace_id)
    except Exception as exc:
        if _is_not_found(exc):
            return None
        logger.opt(exception=True).warning(
            "[usage] failed to fetch Langfuse trace {}",
            trace_id,
        )
        return None


def _extract_breakdown(trace: Any) -> dict[str, Any] | None:
    metadata = getattr(trace, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    breakdown = metadata.get(_BREAKDOWN_KEY)
    return breakdown if isinstance(breakdown, dict) else None


def _parse_breakdown(breakdown: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    totals = _normalize_totals(breakdown.get("totals"))
    by_model: list[dict[str, Any]] = []
    raw_models = breakdown.get("by_model")
    if isinstance(raw_models, list):
        for entry in raw_models:
            if not isinstance(entry, dict):
                continue
            model = str(entry.get("model") or "").strip() or "unknown"
            entry_totals = _normalize_totals(entry)
            by_model.append(
                {
                    "model": model,
                    "prompt_tokens": entry_totals["prompt_tokens"],
                    "completion_tokens": entry_totals["completion_tokens"],
                    "total_tokens": entry_totals["total_tokens"],
                }
            )
    return totals, by_model


def _fetch_generation_rows(lf: Any, trace_id: str) -> list[dict[str, Any]]:
    """Read GENERATION observations via the public observations API.

    Uses the legacy ``observations.get_many`` endpoint: the newer
    ``observations_v_2`` API is only available on Langfuse Cloud and 404s on
    self-hosted deployments. Returns normalized dict rows for aggregation.
    """
    rows: list[dict[str, Any]] = []
    try:
        page = 1
        while True:
            response = lf.api.observations.get_many(
                trace_id=trace_id,
                type="GENERATION",
                page=page,
                limit=_GENERATION_PAGE_SIZE,
            )
            data = getattr(response, "data", None) or []
            for row in data:
                if isinstance(row, dict):
                    rows.append(row)
                else:
                    rows.append(
                        {
                            "model": getattr(row, "model", None),
                            "usageDetails": getattr(row, "usage_details", None),
                        }
                    )
            meta = getattr(response, "meta", None)
            total_pages = getattr(meta, "total_pages", 1) if meta is not None else 1
            if page >= total_pages or not data:
                break
            page += 1
    except Exception:
        logger.opt(exception=True).warning(
            "[usage] failed to read Langfuse generations for trace {}",
            trace_id,
        )
        return []
    return rows


def _aggregate_generation_rows(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    per_model: dict[str, dict[str, int]] = {}
    for row in rows:
        model = str(row.get("model") or "").strip() or "unknown"
        counts = _normalize_usage_details(row.get("usageDetails") or row.get("usage_details"))
        if not counts["total_tokens"]:
            continue
        entry = per_model.setdefault(model, dict(_EMPTY_TOTALS))
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            entry[field] += counts[field]
    totals = dict(_EMPTY_TOTALS)
    by_model: list[dict[str, Any]] = []
    for model in sorted(per_model):
        entry = per_model[model]
        by_model.append(
            {
                "model": model,
                "prompt_tokens": entry["prompt_tokens"],
                "completion_tokens": entry["completion_tokens"],
                "total_tokens": entry["total_tokens"],
            }
        )
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[field] += entry[field]
    return totals, by_model


def _normalize_totals(value: object) -> dict[str, int]:
    totals = dict(_EMPTY_TOTALS)
    if not isinstance(value, dict):
        return totals
    prompt = _int_value(value.get("prompt_tokens"))
    completion = _int_value(value.get("completion_tokens"))
    total = _int_value(value.get("total_tokens"))
    totals["prompt_tokens"] = prompt
    totals["completion_tokens"] = completion
    totals["total_tokens"] = total or prompt + completion
    return totals


def _normalize_usage_details(value: object) -> dict[str, int]:
    """Normalize Langfuse usageDetails (OpenAI completion/response or plain dict)."""
    if not isinstance(value, dict):
        return dict(_EMPTY_TOTALS)
    prompt = value.get("prompt_tokens")
    if prompt is None:
        prompt = value.get("input_tokens")
    if prompt is None:
        prompt = value.get("input")
    completion = value.get("completion_tokens")
    if completion is None:
        completion = value.get("output_tokens")
    if completion is None:
        completion = value.get("output")
    total = value.get("total_tokens")
    if total is None:
        total = value.get("total")
    prompt = _int_value(prompt)
    completion = _int_value(completion)
    total = _int_value(total)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or prompt + completion,
    }


def _int_value(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _is_not_found(exc: BaseException) -> bool:
    return getattr(exc, "status_code", None) == 404
