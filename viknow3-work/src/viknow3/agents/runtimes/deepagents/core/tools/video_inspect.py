"""Sample BaseTool subclass for video_inspect."""

from __future__ import annotations

from time import perf_counter
from typing import Any

from viknow3.agents.runtimes.deepagents.core.scheduler.config import VLLM_RESOURCE
from viknow3.agents.runtimes.deepagents.core.tools.base import BaseTool, ToolErrorCode, ToolResult

VIDEO_INSPECT_DESCRIPTION = (
    "Verify what actually happens in a video clip. Use before citing video content."
)


class VideoInspectTool(BaseTool):
    """Inspect a selected video evidence clip and return verified claims."""

    id = "video_inspect"
    name = "video_inspect"
    description = VIDEO_INSPECT_DESCRIPTION
    resource_class = VLLM_RESOURCE

    def __init__(
        self,
        multimodal: Any | None = None,
        *,
        execution_timeout_seconds: float | None = None,
    ) -> None:
        from viknow3.agents.runtimes.deepagents.core.tools.multimodal import VIDEO_INSPECT_SCHEMA

        self._multimodal = multimodal
        self.input_schema = VIDEO_INSPECT_SCHEMA
        self.execution_timeout_seconds = execution_timeout_seconds

    def run(self, args: dict[str, Any]) -> ToolResult:
        from viknow3.agents.runtimes.deepagents.core.tools.multimodal import (
            VideoInspectLocator,
            _enrich_evidence_with_video_url,
            _log_inspect_tool_completed,
            _log_inspect_tool_failed,
            _log_inspect_tool_started,
            _with_inspect_claim_citations,
        )

        handlers = self._multimodal
        if handlers is None:
            return ToolResult.fail(
                self.name,
                ToolErrorCode.NOT_CONFIGURED,
                "video_inspect requires a multimodal handler",
            )

        started = perf_counter()
        question = str(args.get("question", "")).strip()
        try:
            locator = VideoInspectLocator.model_validate(
                {
                    "library_id": args.get("library_id"),
                    "asset_id": args.get("asset_id"),
                    "time_range": args.get("time_range"),
                    "evidence_id": args.get("evidence_id"),
                    "title": args.get("title"),
                }
            )
            evidence = handlers._video_inspect_evidence_from_locator(locator)
        except Exception as exc:
            _log_inspect_tool_failed(
                "video_inspect",
                args,
                question,
                started=started,
                error=exc,
            )
            return ToolResult.fail(
                self.name,
                ToolErrorCode.INVALID_LOCATOR,
                (
                    "video_inspect requires library_id, asset_id, and time_range copied "
                    "from the selected knowledge_search video evidence."
                ),
                detail=str(exc),
            )

        _log_inspect_tool_started("video_inspect", args, question, evidence=evidence)
        try:
            result = handlers.get_video_review_extractor().extract(evidence, question)
        except Exception as exc:
            _log_inspect_tool_failed(
                "video_inspect",
                args,
                question,
                evidence=evidence,
                started=started,
                error=exc,
            )
            return ToolResult.fail(
                self.name,
                ToolErrorCode.INVALID_REVIEW_REQUEST,
                str(exc),
                detail=str(exc),
            )

        enriched_evidence = _enrich_evidence_with_video_url(evidence)
        result = _with_inspect_claim_citations("video_inspect", result, enriched_evidence)
        _log_inspect_tool_completed(
            "video_inspect",
            args,
            question,
            evidence=enriched_evidence,
            result=result,
            started=started,
        )
        return ToolResult.ok(
            self.name,
            data={
                "result": result.model_dump(mode="json"),
                "evidence": enriched_evidence.model_dump(mode="json"),
            },
        )
