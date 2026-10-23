"""Run-bound tools exposed only to the self-learning trace analyst."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256

from pydantic import ValidationError

from viknow.core.eval.analysis import (
    RunAnalysisResult,
    RunAssessmentPackage,
    TraceReference,
    validate_run_analysis_result,
)
from viknow.core.observability.clickhouse_trace_reader import (
    ClickHouseTraceReader,
    TraceExportPage,
)
from viknow.core.observability.trace_projection import project_observation
from viknow.core.tools.domain import ToolSpec
from viknow.core.tools.workspace_artifacts import (
    download_workspace_artifact,
    upload_workspace_artifacts,
    workspace_artifact_path,
)


class TraceAnalysisToolSession:
    """Mutable audit state for exactly one analysis task."""

    def __init__(
        self,
        *,
        package: RunAssessmentPackage,
        readers: Mapping[str, ClickHouseTraceReader],
    ) -> None:
        self.package = package
        self._readers = dict(readers)
        self._materialized_observation_ids: set[str] = set()
        self.result: RunAnalysisResult | None = None

    def trace_materialize(self, arguments: dict) -> str:
        reference = self._reference(arguments)
        reader = self._reader(reference)
        identity = f"{reference.source_id}:{reference.project_id}:{reference.trace_id}"
        directory = f"/scratch/trace_analysis/{sha256(identity.encode()).hexdigest()[:16]}"
        pages: list[dict] = []
        materialized_ids: set[str] = set()
        offset = 0
        while True:
            page = reader.export_page(reference, offset=offset, limit=50)
            if page.trace_id != reference.trace_id:
                raise ValueError("trace export page does not match the bound trace")
            if page.observations:
                pages.append(self._write_trace_page(directory, page, offset=offset))
                materialized_ids.update(item.id for item in page.observations)
            if page.next_offset is None:
                break
            if page.next_offset <= offset:
                raise ValueError("trace export pagination did not advance")
            offset = page.next_offset
        if not materialized_ids:
            raise ValueError("bound trace has no observations")
        manifest_relative = f"{directory}/manifest.json"
        manifest = {
            "source_id": reference.source_id,
            "trace_id": reference.trace_id,
            "complete": True,
            "observation_count": len(materialized_ids),
            "pages": pages,
        }
        upload_workspace_artifacts(
            [(manifest_relative, json.dumps(manifest, ensure_ascii=False).encode())]
        )
        self._materialized_observation_ids.update(materialized_ids)
        return json.dumps(
            {
                "trace_id": reference.trace_id,
                "complete": True,
                "observation_count": len(materialized_ids),
                "page_count": len(pages),
                "manifest_path": workspace_artifact_path(manifest_relative),
            },
            ensure_ascii=False,
        )

    def _write_trace_page(self, directory: str, page: TraceExportPage, *, offset: int) -> dict:
        raw_relative = f"{directory}/page-{offset}-raw.json"
        view_relative = f"{directory}/page-{offset}-analysis.json"
        analysis_view = {
            "trace_id": page.trace_id,
            "observations": [project_observation(item) for item in page.observations],
            "next_offset": page.next_offset,
        }
        upload_workspace_artifacts(
            [
                (raw_relative, page.model_dump_json().encode()),
                (view_relative, json.dumps(analysis_view, ensure_ascii=False).encode()),
            ]
        )
        return {
            "offset": offset,
            "observation_count": len(page.observations),
            "raw_path": workspace_artifact_path(raw_relative),
            "analysis_view_path": workspace_artifact_path(view_relative),
        }

    def submit_run_analysis(self, arguments: dict) -> str:
        try:
            raw = download_workspace_artifact(arguments.get("draft_path"), max_bytes=131_072)
            result = RunAnalysisResult.model_validate_json(raw)
            validate_run_analysis_result(
                self.package,
                result,
                inspected_observation_ids=self._materialized_observation_ids,
                inspected_trace_fact_ids=set(),
            )
        except (ValueError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                errors = [
                    f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
                    for item in exc.errors(include_url=False)
                ]
            else:
                errors = [str(exc)]
            return json.dumps({"accepted": False, "errors": errors}, ensure_ascii=False)
        self.result = result
        return json.dumps({"accepted": True}, ensure_ascii=False)

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return (
            ToolSpec(
                id="trace_materialize",
                name="trace_materialize",
                description=(
                    "将当前 run 绑定的完整 Langfuse trace 分页写入隔离 workspace；"
                    "只返回 manifest 路径、节点数和完整性状态。manifest 列出原始和解析视图文件，"
                    "之后使用 execute 自行查询文件，不再调用 ClickHouse 查询工具。"
                ),
                schema={"parameters": _MATERIALIZE_SCHEMA},
                handler=self.trace_materialize,
                execution_timeout_seconds=180,
            ),
            ToolSpec(
                id="submit_run_analysis",
                name="submit_run_analysis",
                description=(
                    "读取本轮 workspace 的 /scratch/run-analysis.json 并提交完整分析；"
                    "校验失败返回字段或引用错误，修正文件后可再次提交。"
                ),
                schema={"parameters": _SUBMISSION_SCHEMA},
                handler=self.submit_run_analysis,
            ),
        )

    def _reference(self, arguments: dict) -> TraceReference:
        source_id = str(arguments.get("source_id") or "")
        trace_id = str(arguments.get("trace_id") or "")
        reference = next(
            (
                item
                for item in self.package.trace_references
                if item.source_id == source_id and item.trace_id == trace_id
            ),
            None,
        )
        if reference is None:
            raise ValueError("trace is not bound to the current run assessment")
        return reference

    def _reader(self, reference: TraceReference) -> ClickHouseTraceReader:
        try:
            return self._readers[reference.source_id]
        except KeyError as exc:
            raise ValueError(f"trace source is not configured: {reference.source_id}") from exc


_REFERENCE_PROPERTIES = {
    "source_id": {"type": "string", "minLength": 1},
    "trace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
}
_MATERIALIZE_SCHEMA = {
    "type": "object",
    "properties": _REFERENCE_PROPERTIES,
    "required": ["source_id", "trace_id"],
    "additionalProperties": False,
}
_SUBMISSION_SCHEMA = {
    "type": "object",
    "properties": {
        "draft_path": {
            "type": "string",
            "enum": ["/scratch/run-analysis.json"],
        }
    },
    "required": ["draft_path"],
    "additionalProperties": False,
}
