"""Lossless-adjacent, derived views of Langfuse observations.

The projection is only an index for investigation. Callers must retain the
original observation alongside it for citation and unexpected payload shapes.
"""

from __future__ import annotations

import json
from typing import Any

from viknow.core.observability.clickhouse_trace_reader import TraceObservation


def project_observation(observation: TraceObservation) -> dict[str, Any]:
    input_value, input_path = _decode_field(observation.input)
    output_value, output_path = _decode_field(observation.output)
    return {
        "id": observation.id,
        "parent_observation_id": observation.parent_observation_id,
        "name": observation.name,
        "type": observation.type,
        "start_time": observation.start_time,
        "end_time": observation.end_time,
        "level": observation.level,
        "status_message": observation.status_message,
        "usage_details": observation.usage_details,
        "provided_model_name": observation.provided_model_name,
        "tool_call_names": observation.tool_call_names,
        "input": input_value,
        "input_decode_path": input_path,
        "output": output_value,
        "output_decode_path": output_path,
    }


def _decode_field(raw: str | None) -> tuple[Any, list[str]]:
    if raw is None:
        return None, []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw, []
    path = ["json"]
    if (
        isinstance(value, dict)
        and value.get("type") == "tool"
        and isinstance(value.get("content"), str)
    ):
        value = value["content"]
        path.append("tool_message.content")
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
        else:
            path.append("json")
    return value, path
