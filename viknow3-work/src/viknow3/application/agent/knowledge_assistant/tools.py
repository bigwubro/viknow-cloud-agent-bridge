"""ViKnow viknow-v2 tools.

Retrieval, discovery, and segment catalogs call the HyperRAG client. Inspect
and visual grounding use the Actio client with media from HyperRAG.
query_refine and answer_plan call the configured chat model (JSON planning).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from viknow3.application.agent.knowledge_assistant.planning import answer_plan as plan_answer
from viknow3.application.agent.knowledge_assistant.planning import refine_queries
from viknow3.application.agent.knowledge_assistant.profile import ViknowV2Profile, load_viknow_v2_profile
from viknow3.application.agent.knowledge_assistant.library_scope import (
    granted_library_ids,
    library_allowed,
    resolve_library_id,
)


def _library_ids() -> list[str]:
    return granted_library_ids()


def _require_library(library_id: str) -> bool:
    return library_allowed(library_id)


class DiscoverInput(BaseModel):
    query: str | None = Field(default=None, description="Optional natural language query to rank libraries")
    top_k: int | None = Field(default=None, ge=1)


class RefineInput(BaseModel):
    query: str = Field(default="", description="Current user query.")
    conversation_context: str | None = None
    library_context: str | None = None
    precompiled_knowledge_context: str | None = None
    max_queries: int = Field(default=3, ge=1)


class AnswerPlanInput(BaseModel):
    query: str
    library_id: str


class SearchInput(BaseModel):
    library_id: str = ""
    query: str = ""
    top_k: int | None = Field(default=None, ge=1)
    threshold: float | None = Field(default=None, ge=0, le=1)


class ListSegmentsInput(BaseModel):
    library_id: str
    asset_id: str
    mode: str = Field(description="map | page | select | coverage")
    catalog_id: str | None = None
    cursor: str | None = None
    page_size: int | None = Field(default=None, ge=1, le=50)
    query: str | None = None
    limit: int | None = Field(default=None, ge=1, le=20)
    neighbor_count: int | None = Field(default=None, ge=0, le=2)


class TimeRange(BaseModel):
    start_seconds: float
    end_seconds: float


class VideoInspectInput(BaseModel):
    library_id: str
    asset_id: str
    time_range: TimeRange
    question: str
    evidence_id: str | None = None
    title: str | None = None


class DocumentInspectInput(BaseModel):
    library_id: str
    asset_id: str
    question: str
    evidence_id: str | None = None


class VisualGroundInput(BaseModel):
    query: str
    evidence: dict[str, Any] | None = None


class UploadedImageSearchInput(BaseModel):
    query: str
    library_id: str | None = None
    image_data_url: str | None = Field(default=None, description="data: URL of the uploaded image")


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _hyperrag(action: Callable[[Any], dict[str, Any]]) -> str:
    from viknow3.application.hyperrag.client import HYPERAG, HyperragClientError

    try:
        return _json(action(HYPERAG))
    except HyperragClientError as exc:
        detail = exc.body.get("detail") if isinstance(exc.body, dict) else exc.body
        return _json({"error": "hyperrag_error", "status": exc.status, "detail": detail})


def _actio(action: Callable[[Any], dict[str, Any]]) -> str:
    from viknow3.application.actio.client import ActioClientError, get_actio_client

    try:
        return _json(action(get_actio_client()))
    except ActioClientError as exc:
        return _json({"error": "actio_error", "status": exc.status, "detail": exc.detail})


def knowledge_discover(query: str | None = None, top_k: int | None = None) -> str:
    libraries = _library_ids()
    if not libraries:
        return _json(
            {
                "error": "no_library_granted",
                "message": "当前会话未授权 library_id；请在 Playground 选择知识库后新建或继续会话。",
                "libraries": [],
            }
        )

    def run(client: Any) -> dict[str, Any]:
        payload = client.discover(query or "", library_ids=libraries, top_k=top_k)
        allowed = set(libraries)
        payload["libraries"] = [
            row for row in payload.get("libraries") or [] if row.get("library_id") in allowed
        ]
        return payload

    return _hyperrag(run)


def query_refine(
    query: str = "",
    conversation_context: str | None = None,
    library_context: str | None = None,
    precompiled_knowledge_context: str | None = None,
    max_queries: int = 3,
) -> str:
    del conversation_context, library_context, precompiled_knowledge_context
    if not (query or "").strip():
        return _json({"error": "missing_query", "message": "query is required"})
    library_id = resolve_library_id(None)
    if not _require_library(library_id):
        return _json({"error": "permission_denied"})
    try:
        payload = refine_queries(query, library_id, max_queries=max_queries)
    except Exception as exc:
        return _json({"error": "planning_error", "detail": str(exc)})
    return _json(payload)


def answer_plan(query: str, library_id: str) -> str:
    if not _require_library(library_id):
        return _json({"error": "permission_denied"})
    try:
        payload = plan_answer(query, library_id)
    except Exception as exc:
        return _json({"error": "planning_error", "detail": str(exc)})
    return _json(payload)


def knowledge_search(
    library_id: str = "",
    query: str = "",
    top_k: int | None = None,
    threshold: float | None = None,
) -> str:
    library_id = resolve_library_id(library_id)
    if not query.strip():
        return _json({"error": "query_required"})
    if not _require_library(library_id):
        return _json({"error": "permission_denied"})
    return _hyperrag(
        lambda client: client.search(library_id, query, None, top_k=top_k, threshold=threshold)
    )


def list_video_segments(
    library_id: str,
    asset_id: str,
    mode: str,
    catalog_id: str | None = None,
    cursor: str | None = None,
    page_size: int | None = None,
    query: str | None = None,
    limit: int | None = None,
    neighbor_count: int | None = None,
) -> str:
    if not _require_library(library_id):
        return _json({"error": "permission_denied"})
    return _hyperrag(
        lambda client: client.list_segments(
            library_id,
            asset_id,
            modality="video",
            mode=mode,
            catalog_id=catalog_id,
            cursor=cursor,
            page_size=page_size,
            query=query,
            limit=limit,
            neighbor_count=neighbor_count,
        )
    )


def list_doc_segments(
    library_id: str,
    asset_id: str,
    mode: str,
    catalog_id: str | None = None,
    cursor: str | None = None,
    page_size: int | None = None,
    query: str | None = None,
    limit: int | None = None,
    neighbor_count: int | None = None,
) -> str:
    if not _require_library(library_id):
        return _json({"error": "permission_denied"})
    return _hyperrag(
        lambda client: client.list_segments(
            library_id,
            asset_id,
            modality="document",
            mode=mode,
            catalog_id=catalog_id,
            cursor=cursor,
            page_size=page_size,
            query=query,
            limit=limit,
            neighbor_count=neighbor_count,
        )
    )


def video_inspect_2_0(
    library_id: str,
    asset_id: str,
    time_range: TimeRange,
    question: str,
    evidence_id: str | None = None,
    title: str | None = None,
) -> str:
    if not _require_library(library_id):
        return _json({"error": "permission_denied"})
    return _actio(
        lambda client: client.video_inspect_2_0(
            library_id=library_id,
            asset_id=asset_id,
            question=question,
            time_range=time_range.model_dump(),
            evidence_id=evidence_id,
            title=title,
        )
    )


def document_inspect(
    library_id: str,
    asset_id: str,
    question: str,
    evidence_id: str | None = None,
) -> str:
    if not _require_library(library_id):
        return _json({"error": "permission_denied"})
    return _actio(
        lambda client: client.document_inspect(
            library_id=library_id,
            asset_id=asset_id,
            question=question,
            evidence_id=evidence_id,
        )
    )


def visual_ground(query: str, evidence: dict[str, Any] | None = None) -> str:
    if not _library_ids():
        return _json({"error": "permission_denied"})
    return _actio(lambda client: client.visual_ground(query=query, evidence=evidence))


def uploaded_image_search(
    query: str,
    library_id: str | None = None,
    image_data_url: str | None = None,
) -> str:
    chosen = resolve_library_id(library_id)
    if not _require_library(chosen):
        return _json({"error": "permission_denied"})
    if not (image_data_url or "").strip():
        return _json({"error": "image_data_url is required", "source": "hyperrag"})
    return _hyperrag(
        lambda client: client.search_by_image(chosen, image_data_url or "", query)
    )


_TOOL_BUILDERS: dict[str, Any] = {
    "knowledge_discover": lambda: StructuredTool.from_function(
        knowledge_discover,
        name="knowledge_discover",
        description="List or rank knowledge libraries granted on this run.",
        args_schema=DiscoverInput,
    ),
    "query_refine": lambda: StructuredTool.from_function(
        query_refine,
        name="query_refine",
        description="Plan retrieval queries for a selected library.",
        args_schema=RefineInput,
    ),
    "answer_plan": lambda: StructuredTool.from_function(
        answer_plan,
        name="answer_plan",
        description="Plan answer slots and evidence subquestions.",
        args_schema=AnswerPlanInput,
    ),
    "knowledge_search": lambda: StructuredTool.from_function(
        knowledge_search,
        name="knowledge_search",
        description="Search a library; returns evidence candidates.",
        args_schema=SearchInput,
    ),
    "list_video_segments": lambda: StructuredTool.from_function(
        list_video_segments,
        name="list_video_segments",
        description="Bounded catalog of indexed video segments.",
        args_schema=ListSegmentsInput,
    ),
    "list_doc_segments": lambda: StructuredTool.from_function(
        list_doc_segments,
        name="list_doc_segments",
        description="Bounded catalog of indexed document segments.",
        args_schema=ListSegmentsInput,
    ),
    "video_inspect_2_0": lambda: StructuredTool.from_function(
        video_inspect_2_0,
        name="video_inspect_2_0",
        description="Inspect video evidence and extract claims via Actio.",
        args_schema=VideoInspectInput,
    ),
    "document_inspect": lambda: StructuredTool.from_function(
        document_inspect,
        name="document_inspect",
        description="Inspect document evidence and extract claims via Actio.",
        args_schema=DocumentInspectInput,
    ),
    "visual_ground": lambda: StructuredTool.from_function(
        visual_ground,
        name="visual_ground",
        description="Ground visual counts/locations on evidence via Actio locate.",
        args_schema=VisualGroundInput,
    ),
    "uploaded_image_search": lambda: StructuredTool.from_function(
        uploaded_image_search,
        name="uploaded_image_search",
        description="Search a library with an uploaded image data URL.",
        args_schema=UploadedImageSearchInput,
    ),
}


def build_tools(profile: ViknowV2Profile | None = None) -> list[StructuredTool]:
    resolved = profile or load_viknow_v2_profile()
    tools: list[StructuredTool] = []
    for tool_id in resolved.enabled_tools:
        factory = _TOOL_BUILDERS.get(tool_id)
        if factory is None:
            raise RuntimeError(f"unsupported tool id in profile: {tool_id}")
        tools.append(factory())
    return tools
