"""Reconcile Viknow usage breakdown against vLLM Prometheus counters."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_PROMPT_LINE = re.compile(
    r"^vllm:prompt_tokens_total\{([^}]*)\}\s+(\S+)",
    re.MULTILINE,
)
_GENERATION_LINE = re.compile(
    r"^vllm:generation_tokens_total\{([^}]*)\}\s+(\S+)",
    re.MULTILINE,
)
_MODEL_NAME = re.compile(r"""model_name=\"([^\"]+)\"""")
_PROVIDER_PREFIXES = ("vllm/", "private:", "siliconflow:", "embedding:", "rerank:", "asr:")


@dataclass(frozen=True)
class TokenCounters:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: TokenCounters) -> TokenCounters:
        return TokenCounters(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )

    def __sub__(self, other: TokenCounters) -> TokenCounters:
        return TokenCounters(
            prompt_tokens=max(0, self.prompt_tokens - other.prompt_tokens),
            completion_tokens=max(0, self.completion_tokens - other.completion_tokens),
        )


@dataclass
class VllmMetricsSnapshot:
    """Token counters keyed by endpoint label then Prometheus model_name."""

    by_endpoint: dict[str, dict[str, TokenCounters]] = field(default_factory=dict)

    def models(self) -> dict[str, TokenCounters]:
        merged: dict[str, TokenCounters] = {}
        for per_model in self.by_endpoint.values():
            for model, counters in per_model.items():
                merged[model] = merged.get(model, TokenCounters()) + counters
        return merged

    def total_prompt_tokens(self) -> int:
        return sum(counter.prompt_tokens for counter in self.models().values())

    def total_tokens(self) -> int:
        return sum(counter.total_tokens for counter in self.models().values())


def parse_prometheus_vllm_metrics(
    text: str, *, endpoint: str = "default"
) -> dict[str, TokenCounters]:
    """Parse vLLM prompt/generation counter lines from Prometheus exposition text."""
    per_model: dict[str, TokenCounters] = {}
    for pattern, attr in ((_PROMPT_LINE, "prompt_tokens"), (_GENERATION_LINE, "completion_tokens")):
        for labels, raw_value in pattern.findall(text):
            match = _MODEL_NAME.search(labels)
            if not match:
                continue
            model = match.group(1)
            try:
                value = int(float(raw_value))
            except (TypeError, ValueError):
                continue
            entry = per_model.setdefault(model, TokenCounters())
            if attr == "prompt_tokens":
                per_model[model] = TokenCounters(
                    prompt_tokens=value,
                    completion_tokens=entry.completion_tokens,
                )
            else:
                per_model[model] = TokenCounters(
                    prompt_tokens=entry.prompt_tokens,
                    completion_tokens=value,
                )
    return per_model


def snapshot_from_metrics_texts(texts: dict[str, str]) -> VllmMetricsSnapshot:
    snapshot = VllmMetricsSnapshot()
    for endpoint, text in texts.items():
        snapshot.by_endpoint[endpoint] = parse_prometheus_vllm_metrics(text, endpoint=endpoint)
    return snapshot


def diff_snapshots(before: VllmMetricsSnapshot, after: VllmMetricsSnapshot) -> VllmMetricsSnapshot:
    delta = VllmMetricsSnapshot()
    endpoints = set(before.by_endpoint) | set(after.by_endpoint)
    for endpoint in sorted(endpoints):
        before_models = before.by_endpoint.get(endpoint, {})
        after_models = after.by_endpoint.get(endpoint, {})
        models = set(before_models) | set(after_models)
        delta.by_endpoint[endpoint] = {
            model: (
                after_models.get(model, TokenCounters()) - before_models.get(model, TokenCounters())
            )
            for model in sorted(models)
        }
    return delta


def normalize_breakdown_model(model: str, *, aliases: dict[str, str] | None = None) -> str:
    cleaned = str(model or "").strip()
    if not cleaned:
        return "unknown"
    if aliases and cleaned in aliases:
        cleaned = aliases[cleaned]
    changed = True
    while changed:
        changed = False
        for prefix in _PROVIDER_PREFIXES:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :]
                changed = True
                break
    return cleaned


def breakdown_to_counters(breakdown: dict[str, Any] | None) -> dict[str, TokenCounters]:
    if not isinstance(breakdown, dict):
        return {}
    by_model = breakdown.get("by_model")
    if not isinstance(by_model, list):
        return {}
    result: dict[str, TokenCounters] = {}
    for row in by_model:
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or "unknown")
        counters = TokenCounters(
            prompt_tokens=_int(row.get("prompt_tokens")),
            completion_tokens=_int(row.get("completion_tokens")),
        )
        result[model] = result.get(model, TokenCounters()) + counters
    return result


def _int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _ratio_delta(reported: int, observed: int) -> float | None:
    if observed <= 0 and reported <= 0:
        return 0.0
    if observed <= 0:
        return None
    return abs(reported - observed) / observed


def within_tolerance(
    reported: int,
    observed: int,
    *,
    tolerance_ratio: float,
    absolute_slack: int = 32,
) -> bool:
    if observed <= 0 and reported <= 0:
        return True
    if abs(reported - observed) <= absolute_slack:
        return True
    ratio = _ratio_delta(reported, observed)
    if ratio is None:
        return reported <= 0
    return ratio <= tolerance_ratio


def measure_prompt_token_rate(
    snapshot_a: VllmMetricsSnapshot,
    snapshot_b: VllmMetricsSnapshot,
    *,
    elapsed_seconds: float,
    endpoint: str | None = "chat",
) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    delta = diff_snapshots(snapshot_a, snapshot_b)
    if endpoint is not None:
        models = delta.by_endpoint.get(endpoint, {})
        prompt = sum(counter.prompt_tokens for counter in models.values())
    else:
        prompt = delta.total_prompt_tokens()
    return prompt / elapsed_seconds


def wait_for_quiet_metrics(
    fetch_snapshot: Callable[[], VllmMetricsSnapshot],
    *,
    max_prompt_tokens_per_second: float = 500.0,
    stable_seconds: float = 6.0,
    sample_interval_seconds: float = 2.0,
    timeout_seconds: float = 180.0,
    endpoint: str | None = "chat",
) -> VllmMetricsSnapshot:
    """Block until vLLM prompt counters stay below a rate threshold."""
    deadline = time.monotonic() + timeout_seconds
    quiet_started_at: float | None = None
    previous = fetch_snapshot()
    previous_at = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(sample_interval_seconds)
        current = fetch_snapshot()
        now = time.monotonic()
        rate = measure_prompt_token_rate(
            previous,
            current,
            elapsed_seconds=now - previous_at,
            endpoint=endpoint,
        )
        previous = current
        previous_at = now
        if rate <= max_prompt_tokens_per_second:
            if quiet_started_at is None:
                quiet_started_at = now
            elif now - quiet_started_at >= stable_seconds:
                return current
        else:
            quiet_started_at = None
    raise TimeoutError(
        f"vLLM metrics on endpoint {endpoint!r} did not stay below "
        f"{max_prompt_tokens_per_second} prompt tokens/s for {stable_seconds}s "
        f"within {timeout_seconds}s",
    )


@dataclass(frozen=True)
class ModelReconciliation:
    model: str
    breakdown: TokenCounters
    vllm: TokenCounters
    prompt_error_ratio: float | None
    completion_error_ratio: float | None
    total_error_ratio: float | None
    within_tolerance: bool


@dataclass(frozen=True)
class ReconciliationReport:
    models: tuple[ModelReconciliation, ...]
    unmapped_breakdown_models: tuple[str, ...]
    unmapped_vllm_models: tuple[str, ...]

    @property
    def relevant_models(self) -> tuple[ModelReconciliation, ...]:
        return tuple(row for row in self.models if row.breakdown.total_tokens > 0)

    @property
    def all_within_tolerance(self) -> bool:
        relevant = self.relevant_models
        if not relevant:
            return False
        return all(row.within_tolerance for row in relevant)

    @property
    def likely_shared_endpoint_noise(self) -> bool:
        """Heuristic: vLLM counter deltas vastly exceed breakdown on shared hosts."""
        for row in self.relevant_models:
            if row.vllm.total_tokens > row.breakdown.total_tokens * 2:
                return True
        return False


def reconcile_usage(
    breakdown: dict[str, Any] | None,
    vllm_delta: VllmMetricsSnapshot,
    *,
    tolerance_ratio: float = 0.10,
    model_aliases: dict[str, str] | None = None,
) -> ReconciliationReport:
    """Compare Viknow breakdown by_model totals with vLLM counter deltas."""
    breakdown_by_raw = breakdown_to_counters(breakdown)
    breakdown_by_norm: dict[str, TokenCounters] = {}
    for raw_model, counters in breakdown_by_raw.items():
        norm = normalize_breakdown_model(raw_model, aliases=model_aliases)
        breakdown_by_norm[norm] = breakdown_by_norm.get(norm, TokenCounters()) + counters

    vllm_by_model = vllm_delta.models()
    all_models = sorted(set(breakdown_by_norm) | set(vllm_by_model))

    rows: list[ModelReconciliation] = []
    unmapped_breakdown: list[str] = []
    unmapped_vllm: list[str] = []

    for model in all_models:
        b = breakdown_by_norm.get(model, TokenCounters())
        v = vllm_by_model.get(model, TokenCounters())
        if b.total_tokens > 0 and v.total_tokens <= 0:
            unmapped_breakdown.append(model)
        if v.total_tokens > 0 and b.total_tokens <= 0:
            unmapped_vllm.append(model)
        ok = within_tolerance(b.total_tokens, v.total_tokens, tolerance_ratio=tolerance_ratio)
        rows.append(
            ModelReconciliation(
                model=model,
                breakdown=b,
                vllm=v,
                prompt_error_ratio=_ratio_delta(b.prompt_tokens, v.prompt_tokens),
                completion_error_ratio=_ratio_delta(b.completion_tokens, v.completion_tokens),
                total_error_ratio=_ratio_delta(b.total_tokens, v.total_tokens),
                within_tolerance=ok,
            )
        )

    return ReconciliationReport(
        models=tuple(rows),
        unmapped_breakdown_models=tuple(unmapped_breakdown),
        unmapped_vllm_models=tuple(unmapped_vllm),
    )


def find_trace_id_for_run(run_id: str, *, client: Any | None = None, scan_limit: int = 100) -> str:
    """Best-effort resolve Langfuse trace id from recent traces' metadata."""
    from viknow.agent_adapters.langfuse import build_langfuse_client

    lf = client or build_langfuse_client()
    list_fn = getattr(getattr(getattr(lf, "api", None), "trace", None), "list", None)
    if not callable(list_fn):
        return ""
    try:
        response = list_fn(limit=scan_limit)
    except Exception:
        return ""
    for row in getattr(response, "data", None) or []:
        metadata = getattr(row, "metadata", None) or {}
        if str(metadata.get("viknow_run_id") or "") == run_id:
            return str(getattr(row, "id", "") or "").strip()
    return ""


DEFAULT_VLLM_METRICS_ENDPOINTS: dict[str, str] = {
    "chat": "http://127.0.0.1:8500/metrics",
    "rerank": "http://127.0.0.1:8602/metrics",
    "embedding_vl": "http://127.0.0.1:8601/metrics",
    "embedding": "http://127.0.0.1:8502/metrics",
}
