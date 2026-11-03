from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_timestamp() -> str:
    return datetime.now(tz=UTC).isoformat()


def load_json(path: str | Path) -> Any:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload


def save_json(path: str | Path, payload: Any) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def latency_summary(latencies_s: list[float]) -> dict[str, float]:
    if not latencies_s:
        return {
            "count": 0,
            "avg_seconds": 0.0,
            "p50_seconds": 0.0,
            "p95_seconds": 0.0,
            "max_seconds": 0.0,
        }

    xs = sorted(float(x) for x in latencies_s)

    def _percentile(ratio: float) -> float:
        index = max(0, min(len(xs) - 1, math.ceil(ratio * len(xs)) - 1))
        return float(xs[index])

    return {
        "count": len(xs),
        "avg_seconds": round(float(statistics.mean(xs)), 4),
        "p50_seconds": round(_percentile(0.5), 4),
        "p95_seconds": round(_percentile(0.95), 4),
        "max_seconds": round(float(xs[-1]), 4),
    }
