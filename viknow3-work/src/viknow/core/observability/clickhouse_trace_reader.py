"""Project-bound, read-only access to Langfuse observations in ClickHouse."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from viknow.core.eval.analysis import TraceReference

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OBSERVATION_ID = re.compile(r"^[0-9a-fA-F]{16,64}$")
_FILTER_VALUE = re.compile(r"^[A-Za-z0-9_.:/ -]{1,128}$")


class ClickHouseTraceReaderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: str
    database: str = "default"
    observations_table: str = "observations"
    timeout_seconds: float = Field(default=30, gt=0)
    max_rows: int = Field(default=500, ge=1, le=5000)
    max_response_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)

    @field_validator("database", "observations_table")
    @classmethod
    def validate_identifier(cls, value: str, info) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"invalid ClickHouse {info.field_name}")
        return value


class TraceObservationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    parent_observation_id: str | None = None
    name: str
    type: str
    start_time: str
    end_time: str | None = None
    level: str
    status_message: str | None = None
    has_input: bool = False
    has_output: bool = False


class TraceOverview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str
    observations: list[TraceObservationSummary]
    next_offset: int | None = None


class TraceObservationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_ids: list[str] = Field(default_factory=list)
    parent_observation_id: str | None = None
    names: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)
    include_input: bool = False
    include_output: bool = False
    include_metadata: bool = False
    limit: int = Field(default=20, ge=1)


class TraceObservation(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    id: str
    trace_id: str
    parent_observation_id: str | None = None
    name: str
    type: str
    start_time: str
    end_time: str | None = None
    level: str
    status_message: str | None = None
    input: str | None = None
    output: str | None = None
    metadata: dict[str, Any] | None = None
    usage_details: dict[str, int] = Field(default_factory=dict)
    provided_model_name: str | None = None
    tool_call_names: list[str] = Field(default_factory=list)
    version: str | None = None
    model_parameters: str | None = None
    provided_usage_details: dict[str, int] = Field(default_factory=dict)
    provided_cost_details: dict[str, Any] = Field(default_factory=dict)
    cost_details: dict[str, Any] = Field(default_factory=dict)
    total_cost: str | None = None
    completion_start_time: str | None = None
    prompt_id: str | None = None
    prompt_name: str | None = None
    prompt_version: int | None = None
    tool_definitions: dict[str, str] = Field(default_factory=dict)
    tool_calls: list[str] = Field(default_factory=list)


class TraceExportPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str
    observations: list[TraceObservation]
    next_offset: int | None = None


class TraceTopologyGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    name: str
    parent_name: str
    observation_count: int = Field(ge=1)
    sample_observation_id: str
    sample_parent_id: str | None = None


class TraceTopologyPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str
    groups: list[TraceTopologyGroup]
    next_offset: int | None = None


Post = Callable[[str, bytes, dict[str, str], float], bytes]


class ClickHouseTraceReader:
    def __init__(self, config: ClickHouseTraceReaderConfig, *, post: Post | None = None) -> None:
        self._config = config
        self._post = post or self._http_post

    def overview(
        self,
        reference: TraceReference,
        *,
        offset: int = 0,
        limit: int = 80,
    ) -> TraceOverview:
        if offset < 0:
            raise ValueError("trace overview offset must be non-negative")
        if limit < 1 or limit > min(self._config.max_rows, 100):
            raise ValueError("trace overview limit exceeds configured maximum")
        table = self._qualified_table()
        query = f"""
SELECT id, parent_observation_id, name, type,
       toString(start_time) AS start_time, toString(end_time) AS end_time,
       level, status_message, notEmpty(ifNull(input, '')) AS has_input,
       notEmpty(ifNull(output, '')) AS has_output
FROM {table}
WHERE project_id = {{project_id:String}}
  AND trace_id = {{trace_id:String}}
  AND is_deleted = 0
ORDER BY start_time, id
LIMIT {limit}
OFFSET {offset}
FORMAT JSONEachRow
"""
        rows = self._query(query, reference)
        return TraceOverview(
            trace_id=reference.trace_id,
            observations=[TraceObservationSummary.model_validate(row) for row in rows],
            next_offset=offset + limit if len(rows) == limit else None,
        )

    def observations(
        self,
        reference: TraceReference,
        request: TraceObservationQuery,
    ) -> list[TraceObservation]:
        if request.limit > self._config.max_rows:
            raise ValueError("trace observation limit exceeds configured maximum")
        if not any(
            [
                request.observation_ids,
                request.parent_observation_id,
                request.names,
                request.types,
            ]
        ):
            raise ValueError("at least one observation filter is required")
        for observation_id in request.observation_ids:
            self._validate_observation_id(observation_id)
        if request.parent_observation_id:
            self._validate_observation_id(request.parent_observation_id)
        self._validate_filter_values(request.names)
        self._validate_filter_values(request.types)

        fields = [
            "id",
            "trace_id",
            "parent_observation_id",
            "name",
            "type",
            "toString(start_time) AS start_time",
            "toString(end_time) AS end_time",
            "level",
            "status_message",
            "usage_details",
            "provided_model_name",
            "tool_call_names",
        ]
        if request.include_input:
            fields.append("input")
        if request.include_output:
            fields.append("output")
        if request.include_metadata:
            fields.append("metadata")
        filters: list[str] = []
        parameters: dict[str, str] = {}
        if request.observation_ids:
            filters.append("id IN {observation_ids:Array(String)}")
            parameters["observation_ids"] = _clickhouse_array(request.observation_ids)
        if request.parent_observation_id:
            filters.append("parent_observation_id = {parent_id:String}")
            parameters["parent_id"] = request.parent_observation_id
        if request.names:
            filters.append("name IN {names:Array(String)}")
            parameters["names"] = _clickhouse_array(request.names)
        if request.types:
            filters.append("type IN {types:Array(String)}")
            parameters["types"] = _clickhouse_array(request.types)
        query = f"""
SELECT {", ".join(fields)}
FROM {self._qualified_table()}
WHERE project_id = {{project_id:String}}
  AND trace_id = {{trace_id:String}}
  AND is_deleted = 0
  AND {" AND ".join(filters)}
ORDER BY start_time, id
LIMIT {request.limit}
FORMAT JSONEachRow
"""
        rows = self._query(query, reference, parameters)
        return [TraceObservation.model_validate(row) for row in rows]

    def export_page(
        self,
        reference: TraceReference,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> TraceExportPage:
        """Export one full-fidelity observation page for workspace investigation."""
        if offset < 0:
            raise ValueError("trace export offset must be non-negative")
        if limit < 1 or limit > min(self._config.max_rows, 100):
            raise ValueError("trace export limit exceeds configured maximum")
        query = f"""
SELECT id, trace_id, parent_observation_id, name, type,
       toString(start_time) AS start_time, toString(end_time) AS end_time,
       level, status_message, input, output, metadata, usage_details,
       provided_model_name, tool_call_names, version, model_parameters,
       provided_usage_details, provided_cost_details, cost_details,
       toString(total_cost) AS total_cost,
       toString(completion_start_time) AS completion_start_time,
       prompt_id, prompt_name, prompt_version, tool_definitions, tool_calls
FROM {self._qualified_table()}
WHERE project_id = {{project_id:String}}
  AND trace_id = {{trace_id:String}}
  AND is_deleted = 0
ORDER BY start_time, id
LIMIT {limit}
OFFSET {offset}
FORMAT JSONEachRow
"""
        rows = self._query(query, reference)
        return TraceExportPage(
            trace_id=reference.trace_id,
            observations=[TraceObservation.model_validate(row) for row in rows],
            next_offset=offset + limit if len(rows) == limit else None,
        )

    def topology_page(
        self,
        reference: TraceReference,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> TraceTopologyPage:
        """Count observations by name and direct parent without reading payloads."""
        if offset < 0:
            raise ValueError("trace topology offset must be non-negative")
        if limit < 1 or limit > min(self._config.max_rows, 100):
            raise ValueError("trace topology limit exceeds configured maximum")
        table = self._qualified_table()
        query = f"""
SELECT r.name AS name, ifNull(p.name, '') AS parent_name,
       count() AS observation_count,
       any(r.id) AS sample_observation_id,
       any(r.parent_observation_id) AS sample_parent_id
FROM {table} AS r
LEFT JOIN {table} AS p
  ON p.project_id = r.project_id
 AND p.trace_id = r.trace_id
 AND p.id = r.parent_observation_id
 AND p.is_deleted = 0
WHERE r.project_id = {{project_id:String}}
  AND r.trace_id = {{trace_id:String}}
  AND r.is_deleted = 0
GROUP BY r.name, p.name
ORDER BY observation_count DESC, name, parent_name
LIMIT {limit}
OFFSET {offset}
FORMAT JSONEachRow
"""
        rows = self._query(query, reference)
        return TraceTopologyPage(
            trace_id=reference.trace_id,
            groups=[
                TraceTopologyGroup(
                    fact_id=_topology_fact_id(reference.trace_id, row),
                    **row,
                )
                for row in rows
            ],
            next_offset=offset + limit if len(rows) == limit else None,
        )

    def _query(
        self,
        query: str,
        reference: TraceReference,
        parameters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        request_parameters = {
            "user": self._config.user,
            "password": self._config.password,
            "database": self._config.database,
            "param_project_id": reference.project_id,
            "param_trace_id": reference.trace_id,
        }
        request_parameters.update(
            {f"param_{key}": value for key, value in (parameters or {}).items()}
        )
        body = self._post(
            self._config.url.rstrip("/") + "/",
            query.encode(),
            request_parameters,
            self._config.timeout_seconds,
        )
        if len(body) > self._config.max_response_bytes:
            raise ValueError("ClickHouse trace response exceeds configured maximum")
        return [json.loads(line) for line in body.splitlines() if line.strip()]

    def _http_post(
        self,
        url: str,
        body: bytes,
        parameters: dict[str, str],
        timeout: float,
    ) -> bytes:
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(parameters)}",
            data=body,
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(self._config.max_response_bytes + 1)
        return payload

    def _qualified_table(self) -> str:
        return f"{self._config.database}.{self._config.observations_table}"

    @staticmethod
    def _validate_observation_id(value: str) -> None:
        if not _OBSERVATION_ID.fullmatch(value):
            raise ValueError(f"invalid observation id: {value}")

    @staticmethod
    def _validate_filter_values(values: list[str]) -> None:
        invalid = next((value for value in values if not _FILTER_VALUE.fullmatch(value)), None)
        if invalid is not None:
            raise ValueError(f"invalid observation filter value: {invalid}")


def _clickhouse_array(values: list[str]) -> str:
    return "[" + ",".join(f"'{value}'" for value in values) + "]"


def _topology_fact_id(trace_id: str, row: dict[str, Any]) -> str:
    value = [trace_id, row["name"], row["parent_name"], row["observation_count"]]
    digest = hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()
    return f"trace:{trace_id}:topology:{digest}"
