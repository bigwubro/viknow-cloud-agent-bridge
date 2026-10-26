"""ViKnow-owned tool registry shared by agent adapters."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agents.runtime.core.config import ToolConfigs
from agents.runtime.core.evidence import EvidencePart
from agents.runtime.core.knowledge.hyperrag_adapter import HyperRAGAdapter
from agents.runtime.core.multimodal.asr import EvidenceTranscriber
from agents.runtime.core.multimodal.document_page_renderer import MAX_DOCUMENT_INSPECT_PAGES
from agents.runtime.core.multimodal.ocr import EvidenceOCRReader
from agents.runtime.core.multimodal.review import EvidenceReviewer
from agents.runtime.core.multimodal.video_discovery import VideoDiscoveryClient, VideoDiscoveryService
from agents.runtime.core.runtime.answer_plan_tool import ANSWER_PLAN_SCHEMA
from agents.runtime.core.scheduler.config import (
    ASR_RESOURCE,
    EMBEDDING_RESOURCE,
    HYPERRAG_DB_RESOURCE,
    VLLM_RESOURCE,
)
from agents.runtime.core.tools.base import FunctionTool
from agents.runtime.core.tools.context import get_tool_run_context
from agents.runtime.core.tools.domain import ToolSpec
from agents.runtime.core.tools.knowledge import (
    BRIDGE_REVIEW_SCHEMA,
    BRIDGE_SEARCH_SCHEMA,
    DISCOVER_SCHEMA,
    EVIDENCE_EVENT_MAP_SCHEMA,
    EVIDENCE_EXCLUSION_REVIEW_SCHEMA,
    EVIDENCE_INSPECTION_REPORT_SCHEMA,
    EVIDENCE_PACKAGE_SCHEMA,
    EVIDENCE_PLAN_SCHEMA,
    EVIDENCE_PROGRESS_SCHEMA,
    EVIDENCE_REFLECTION_SCHEMA,
    EVIDENCE_SCOUT_REPORT_SCHEMA,
    LIST_DOC_SEGMENTS_SCHEMA,
    LIST_VIDEO_SEGMENTS_SCHEMA,
    QUERY_REFINE_SCHEMA,
    SEARCH_SCHEMA,
    KnowledgeToolHandlers,
    bridge_review,
    bridge_search,
)
from agents.runtime.core.tools.multimodal import (
    ADVANCED_VIDEO_INSPECT_SCHEMA,
    ASR_SCHEMA,
    CLIP_SEARCH_SCHEMA,
    DOCUMENT_INSPECT_SCHEMA,
    FRAME_INSPECT_SCHEMA,
    GLOBAL_BROWSE_SCHEMA,
    IMAGE_INSPECT_SCHEMA,
    LOCATE_ANYTHING_SCHEMA,
    OCR_SCHEMA,
    TEXT_INSPECT_SCHEMA,
    UPLOADED_IMAGE_SEARCH_SCHEMA,
    MultimodalToolHandlers,
)
from agents.runtime.core.tools.testing import TOOL_SCHEMA, viknow_test_answer
from agents.runtime.core.tools.video_inspect import VideoInspectTool

if TYPE_CHECKING:
    from agents.runtime.core.multimodal.advanced_video_extractor import AdvancedVideoEvidenceExtractor
    from agents.runtime.core.multimodal.video_review_extractor import VideoEvidenceExtractor


@dataclass
class ViKnowToolHandlers:
    """Aggregate ViKnow-owned tool handlers by core module."""

    repo_root: Path | None = None
    tool_configs: ToolConfigs | None = None
    knowledge_adapter: HyperRAGAdapter | None = None
    video_inspector_factory: Callable[[], EvidenceReviewer] | None = None
    video_review_extractor_factory: Callable[[], VideoEvidenceExtractor] | None = None
    advanced_video_extractor_factory: Callable[[], AdvancedVideoEvidenceExtractor] | None = None
    video_discovery_client_factory: Callable[[], VideoDiscoveryClient] | None = None
    video_url_builder: Callable[[EvidencePart], str] | None = None
    filesystem_manager: object | None = None

    def __post_init__(self) -> None:
        self.knowledge = KnowledgeToolHandlers(
            repo_root=self.repo_root,
            tool_configs=self.tool_configs,
            knowledge_adapter=self.knowledge_adapter,
        )
        self.multimodal = MultimodalToolHandlers(
            knowledge=self.knowledge,
            repo_root=self.repo_root,
            tool_configs=self.tool_configs,
            video_inspector_factory=self.video_inspector_factory,
            video_review_extractor_factory=self.video_review_extractor_factory,
            advanced_video_extractor_factory=self.advanced_video_extractor_factory,
            video_discovery_client_factory=self.video_discovery_client_factory,
            video_url_builder=self.video_url_builder,
            filesystem_manager=self.filesystem_manager,
        )

    def set_tool_configs(self, tool_configs: ToolConfigs | None) -> None:
        self.tool_configs = tool_configs
        self.knowledge.tool_configs = tool_configs
        self.multimodal.tool_configs = tool_configs

    def get_video_inspector(self) -> EvidenceReviewer:
        return self.multimodal.get_video_inspector()

    def get_evidence_transcriber(self) -> EvidenceTranscriber:
        return self.multimodal.get_evidence_transcriber()

    def get_evidence_ocr_reader(self) -> EvidenceOCRReader:
        return self.multimodal.get_evidence_ocr_reader()

    def get_video_discovery_service(self) -> VideoDiscoveryService:
        return self.multimodal.get_video_discovery_service()

    def viknow_test_answer(self, args: dict) -> str:
        return viknow_test_answer(args)

    def knowledge_discover(self, args: dict) -> str:
        return self.knowledge.knowledge_discover(args)

    async def knowledge_discover_async(self, args: dict) -> str:
        return await self.knowledge.knowledge_discover_async(args)

    def list_video_segments(self, args: dict) -> str:
        return self.knowledge.list_video_segments(args)

    def list_doc_segments(self, args: dict) -> str:
        return self.knowledge.list_doc_segments(args)

    def knowledge_search(self, args: dict) -> str:
        return self.knowledge.knowledge_search(args)

    async def knowledge_search_async(self, args: dict) -> str:
        return await self.knowledge.knowledge_search_async(args)

    def query_refine(self, args: dict) -> str:
        return self.knowledge.query_refine(args)

    def answer_plan(self, args: dict) -> str:
        context = get_tool_run_context()
        if context is None or context.answer_plan_handler is None:
            return json.dumps(
                {
                    "tool": "answer_plan",
                    "error": {
                        "code": "answer_plan_unavailable",
                        "message": "No run-scoped QA answer plan is active.",
                    },
                },
                ensure_ascii=False,
            )
        return context.answer_plan_handler(args)

    def evidence_plan(self, args: dict) -> str:
        return self.knowledge.evidence_plan(args)

    def evidence_event_map(self, args: dict) -> str:
        return self.knowledge.evidence_event_map(args)

    def evidence_progress(self, args: dict) -> str:
        return self.knowledge.evidence_progress(args)

    def evidence_reflection(self, args: dict) -> str:
        return self.knowledge.evidence_reflection(args)

    def evidence_exclusion_review(self, args: dict) -> str:
        return self.knowledge.evidence_exclusion_review(args)

    def evidence_scout_report(self, args: dict) -> str:
        return self.knowledge.evidence_scout_report(args)

    def evidence_inspection_report(self, args: dict) -> str:
        return self.knowledge.evidence_inspection_report(args)

    def evidence_package(self, args: dict) -> str:
        return self.knowledge.evidence_package(args)

    async def load_precompiled_knowledge_context(
        self,
        *,
        library_id: str,
        query: str,
        top_k: int = 8,
        threshold: float = 0.0,
    ) -> str:
        return await self.knowledge.load_precompiled_knowledge_context(
            library_id=library_id,
            query=query,
            top_k=top_k,
            threshold=threshold,
        )

    def video_inspect(self, args: dict) -> str:
        return self.multimodal.video_inspect(args)

    def video_inspect_2_0(self, args: dict) -> str:
        return self.multimodal.video_inspect_2_0(args)

    def evidence_asr(self, args: dict) -> str:
        return self.multimodal.evidence_asr(args)

    def evidence_ocr(self, args: dict) -> str:
        return self.multimodal.evidence_ocr(args)

    def image_inspect(self, args: dict) -> str:
        return self.multimodal.image_inspect(args)

    def document_inspect(self, args: dict) -> str:
        return self.multimodal.document_inspect(args)

    def locate_anything(self, args: dict) -> str:
        return self.multimodal.locate_anything(args)

    def uploaded_image_search(self, args: dict) -> str:
        return self.multimodal.uploaded_image_search(args)

    def text_inspect(self, args: dict) -> str:
        return self.multimodal.text_inspect(args)

    def clip_search(self, args: dict) -> str:
        return self.multimodal.clip_search(args)

    def global_browse(self, args: dict) -> str:
        return self.multimodal.global_browse(args)

    def frame_inspect(self, args: dict) -> str:
        return self.multimodal.frame_inspect(args)

    def parse_evidence_arg(self, value):
        return self.multimodal.parse_evidence_arg(value)


def _tool_spec(**kwargs) -> ToolSpec:
    return FunctionTool(**kwargs).spec()


def build_viknow_tool_specs(
    handlers: ViKnowToolHandlers | None = None,
) -> tuple[ToolSpec, ...]:
    tool_handlers = handlers or ViKnowToolHandlers()
    multimodal_review_timeout_seconds = _multimodal_review_timeout_seconds(tool_handlers)
    return (
        _tool_spec(
            id="viknow_test_answer",
            name="viknow_test_answer",
            schema=TOOL_SCHEMA,
            handler=tool_handlers.viknow_test_answer,
            description="Minimal ViKnow QA verification tool.",
        ),
        _tool_spec(
            id="knowledge_discover",
            name="knowledge_discover",
            schema=DISCOVER_SCHEMA,
            handler=tool_handlers.knowledge_discover,
            description=(
                "List all knowledge bases with their content summaries. "
                "Agent uses this to decide which KB to search."
            ),
            async_handler=tool_handlers.knowledge_discover_async,
            resource_class=HYPERRAG_DB_RESOURCE,
        ),
        _tool_spec(
            id="knowledge_search",
            name="knowledge_search",
            schema=SEARCH_SCHEMA,
            handler=tool_handlers.knowledge_search,
            description=(
                "Search a ViKnow knowledge library and return unified evidence. "
                "Must be used with the knowledge-qa-v2 skill."
            ),
            async_handler=tool_handlers.knowledge_search_async,
            resource_class=EMBEDDING_RESOURCE,
        ),
        _tool_spec(
            id="query_refine",
            name="query_refine",
            schema=QUERY_REFINE_SCHEMA,
            handler=tool_handlers.query_refine,
            description=(
                "Generate candidate retrieval queries for ViKnow knowledge search. "
                "Does not search or answer."
            ),
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="answer_plan",
            name="answer_plan",
            schema=ANSWER_PLAN_SCHEMA,
            handler=tool_handlers.answer_plan,
            description=ANSWER_PLAN_SCHEMA["description"],
        ),
        _tool_spec(
            id="evidence_plan",
            name="evidence_plan",
            schema=EVIDENCE_PLAN_SCHEMA,
            handler=tool_handlers.evidence_plan,
            description=(
                "Create the required, structured coverage contract for one Evidence Oracle task."
            ),
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="evidence_event_map",
            name="evidence_event_map",
            schema=EVIDENCE_EVENT_MAP_SCHEMA,
            handler=tool_handlers.evidence_event_map,
            description=(
                "Checkpoint the main Oracle agent's event map before Inspector verification."
            ),
        ),
        _tool_spec(
            id="evidence_progress",
            name="evidence_progress",
            schema=EVIDENCE_PROGRESS_SCHEMA,
            handler=tool_handlers.evidence_progress,
            description=(
                "Record the complete per-slot research progress for one Evidence Oracle task."
            ),
        ),
        _tool_spec(
            id="evidence_reflection",
            name="evidence_reflection",
            schema=EVIDENCE_REFLECTION_SCHEMA,
            handler=tool_handlers.evidence_reflection,
            description=(
                "Record the main Oracle agent's hypotheses, counterexamples, and next "
                "research challenge after a factual progress checkpoint."
            ),
        ),
        _tool_spec(
            id="evidence_exclusion_review",
            name="evidence_exclusion_review",
            schema=EVIDENCE_EXCLUSION_REVIEW_SCHEMA,
            handler=tool_handlers.evidence_exclusion_review,
            description=(
                "Record main-Agent acceptance or reopening of material Scout exclusions "
                "before exhaustive event modeling."
            ),
        ),
        _tool_spec(
            id="evidence_scout_report",
            name="evidence_scout_report",
            schema=EVIDENCE_SCOUT_REPORT_SCHEMA,
            handler=tool_handlers.evidence_scout_report,
            description="Persist one page-bounded Scout candidate and material-exclusion report.",
        ),
        _tool_spec(
            id="evidence_inspection_report",
            name="evidence_inspection_report",
            schema=EVIDENCE_INSPECTION_REPORT_SCHEMA,
            handler=tool_handlers.evidence_inspection_report,
            description="Persist one task-bound Inspector finding report.",
        ),
        _tool_spec(
            id="evidence_package",
            name="evidence_package",
            schema=EVIDENCE_PACKAGE_SCHEMA,
            handler=tool_handlers.evidence_package,
            description=(
                "Validate and materialize one scoped CandidateEvidencePackage pending Gate B."
            ),
        ),
        _tool_spec(
            id="list_video_segments",
            name="list_video_segments",
            schema=LIST_VIDEO_SEGMENTS_SCHEMA,
            handler=tool_handlers.list_video_segments,
            description=(
                "List all indexed chunks for a video asset without calling VLM. "
                "Use to understand video structure before deeper inspection."
            ),
            resource_class=HYPERRAG_DB_RESOURCE,
        ),
        _tool_spec(
            id="list_doc_segments",
            name="list_doc_segments",
            schema=LIST_DOC_SEGMENTS_SCHEMA,
            handler=tool_handlers.list_doc_segments,
            description=(
                "Create, page, select, or audit an indexed document segment catalog without "
                "treating indexed text as cited evidence."
            ),
            resource_class=HYPERRAG_DB_RESOURCE,
        ),
        VideoInspectTool(
            tool_handlers.multimodal,
            execution_timeout_seconds=multimodal_review_timeout_seconds,
        ).spec(),
        _tool_spec(
            id="video_inspect_2_0",
            name="video_inspect_2_0",
            schema=ADVANCED_VIDEO_INSPECT_SCHEMA,
            handler=tool_handlers.video_inspect_2_0,
            description=(
                "Experimentally inspect and compare one to eight selected video clips using "
                "adaptive FPS, temporal refinement, and spatial crops."
            ),
            execution_timeout_seconds=multimodal_review_timeout_seconds,
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="bridge_search",
            name="bridge_search",
            schema=BRIDGE_SEARCH_SCHEMA,
            handler=bridge_search,
            description=(
                "Search the ViKnow v1 knowledge base. "
                "Must be used with the viknow-common-query skill."
            ),
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="bridge_review",
            name="bridge_review",
            schema=BRIDGE_REVIEW_SCHEMA,
            handler=bridge_review,
            description="Deep video analysis. Must be used with the viknow-common-query skill.",
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="image_inspect",
            name="image_inspect",
            schema=IMAGE_INSPECT_SCHEMA,
            handler=tool_handlers.image_inspect,
            description=(
                "Extract evidence from image chunks via VLM. Caption text is supplementary context."
            ),
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="document_inspect",
            name="document_inspect",
            schema=DOCUMENT_INSPECT_SCHEMA,
            handler=tool_handlers.document_inspect,
            description=(
                "Inspect the complete original document page at 144 DPI and extract "
                "structured visual claims."
            ),
            resource_class=VLLM_RESOURCE,
            execution_timeout_seconds=(
                2 * MAX_DOCUMENT_INSPECT_PAGES * multimodal_review_timeout_seconds
                if multimodal_review_timeout_seconds is not None
                else None
            ),
        ),
        _tool_spec(
            id="visual_ground",
            name="visual_ground",
            schema=LOCATE_ANYTHING_SCHEMA,
            handler=tool_handlers.locate_anything,
            description=(
                "Locate or count objects, regions, text, or GUI elements in an "
                "image or video (visual grounding). Returns detections with pixel "
                "coordinates and a count. Use when the user asks where something is "
                "or how many of a target are visible."
            ),
        ),
        _tool_spec(
            id="uploaded_image_search",
            name="uploaded_image_search",
            schema=UPLOADED_IMAGE_SEARCH_SCHEMA,
            handler=tool_handlers.uploaded_image_search,
            description=(
                "Analyze one user-uploaded image and return a routing decision before "
                "query_refine or knowledge_search. Use for direct uploaded image "
                "understanding, including OCR, visible text reading, counting, object "
                "identification, scene description, screenshots, photos, charts, "
                "labels, devices, or interfaces."
            ),
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="text_inspect",
            name="text_inspect",
            schema=TEXT_INSPECT_SCHEMA,
            handler=tool_handlers.text_inspect,
            description=(
                "Extract factual claims from text evidence with source-verified supporting quotes. "
                "Use for long or dense text where specific facts need verification."
            ),
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="evidence_asr",
            name="evidence_asr",
            schema=ASR_SCHEMA,
            handler=tool_handlers.evidence_asr,
            description="Transcribe speech from evidence source media.",
            resource_class=ASR_RESOURCE,
        ),
        _tool_spec(
            id="evidence_ocr",
            name="evidence_ocr",
            schema=OCR_SCHEMA,
            handler=tool_handlers.evidence_ocr,
            description="Recognize visible text from evidence source media.",
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="clip_search",
            name="clip_search",
            schema=CLIP_SEARCH_SCHEMA,
            handler=tool_handlers.clip_search,
            description="Search video clip captions and return matching evidence.",
            resource_class=EMBEDDING_RESOURCE,
        ),
        _tool_spec(
            id="global_browse",
            name="global_browse",
            schema=GLOBAL_BROWSE_SCHEMA,
            handler=tool_handlers.global_browse,
            description="Answer a global question from browsed video clip context.",
            resource_class=VLLM_RESOURCE,
        ),
        _tool_spec(
            id="frame_inspect",
            name="frame_inspect",
            schema=FRAME_INSPECT_SCHEMA,
            handler=tool_handlers.frame_inspect,
            description="Inspect sampled frames from a video evidence object.",
            resource_class=VLLM_RESOURCE,
        ),
    )


def _multimodal_review_timeout_seconds(handlers: ViKnowToolHandlers) -> float | None:
    if handlers.tool_configs is None or handlers.tool_configs.multimodal is None:
        return None
    return handlers.tool_configs.multimodal.review_model.timeout_seconds
