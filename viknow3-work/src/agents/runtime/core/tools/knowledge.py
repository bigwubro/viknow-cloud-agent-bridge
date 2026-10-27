"""Knowledge tools exposed to agent adapters."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field

from agents.runtime.core.config import ModelProvider, ToolConfigs
from agents.runtime.core.eval.candidate_evidence import (
    CandidateEvidenceFact,
    EvidenceCollectionTask,
    build_candidate_evidence_package,
)
from agents.runtime.core.eval.research_oracle import (
    ExclusionReview,
    InspectionReport,
    ResearchEvent,
    ResearchOutcome,
    ResearchPlan,
    ResearchReflection,
    ResearchState,
    ScoutReport,
)
from agents.runtime.core.evidence import (
    Evidence,
    EvidenceLocator,
    EvidencePart,
    EvidencePartKind,
)
from agents.runtime.core.knowledge.client import KnowledgeClient, KnowledgeSearchResult
from agents.runtime.core.knowledge.object_key import resolve_asset_object_key
from agents.runtime.core.model_gateway import model_connection_options
from agents.runtime.core.multimodal.structured_llm import invoke_structured_llm, invoke_text_llm
from agents.runtime.core.scheduler.domain import Priority, extra_body_with_priority
from agents.runtime.core.tools.base import ToolErrorCode, ToolResult, parse_tool_payload
from agents.runtime.core.tools.context import (
    acquire_knowledge_search_budget,
    get_allowed_library_ids,
    get_tool_run_context,
    is_library_allowed,
)
from agents.runtime.core.tools.segment_catalog import SegmentCatalog, SegmentRecord

PRECOMPILED_LIBRARY_ID = "PRECOMPILED"
_DISCOVER_DESCRIPTION_MAX_CHARS = 2_000
_DISCOVER_ASSET_ID_PREVIEW_LIMIT = 20

DISCOVER_SCHEMA = {
    "description": (
        "Discover the knowledge libraries most relevant to a query by combining library "
        "overviews with indexed document and video titles and summaries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional natural language query used to find relevant libraries.",
            },
            "top_k": {
                "type": "integer",
                "description": (
                    "Optional maximum number of libraries to return. When omitted, "
                    "return every available library or every matching library."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

LIST_KB_SCHEMA = {
    "description": (
        "List all available knowledge bases with their summaries. "
        "Use this to discover which knowledge bases exist and what they contain "
        "before deciding which one to search."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}


LIST_VIDEO_SEGMENTS_SCHEMA = {
    "description": (
        "Create, page, select, or audit a bounded catalog of indexed video segments. "
        "Segment summaries are locators only; use video_inspect before citing a video fact."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": "Knowledge library id.",
            },
            "asset_id": {
                "type": "string",
                "description": "Video asset id to list chunks for.",
            },
            "mode": {
                "type": "string",
                "enum": ["map", "page", "select", "coverage"],
                "description": "Required bounded catalog operation.",
            },
            "catalog_id": {"type": "string", "description": "Catalog id returned by map."},
            "cursor": {"type": "string", "description": "Cursor returned by page."},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 50},
            "query": {"type": "string", "description": "One evidence-plan subquestion for select."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "neighbor_count": {"type": "integer", "minimum": 0, "maximum": 2},
        },
        "required": ["library_id", "asset_id", "mode"],
        "additionalProperties": False,
    },
}

LIST_DOC_SEGMENTS_SCHEMA = {
    "description": (
        "Create, page, select, or audit a bounded catalog of indexed document segments. "
        "Segment summaries are locators only; use text_inspect before citing a document fact."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {"type": "string", "description": "Knowledge library id."},
            "asset_id": {"type": "string", "description": "Document asset id."},
            "mode": {
                "type": "string",
                "enum": ["map", "page", "select", "coverage"],
                "description": "Required bounded catalog operation.",
            },
            "catalog_id": {"type": "string", "description": "Catalog id returned by map."},
            "cursor": {"type": "string", "description": "Cursor returned by page."},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 50},
            "query": {"type": "string", "description": "One evidence-plan subquestion for select."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "neighbor_count": {"type": "integer", "minimum": 0, "maximum": 2},
        },
        "required": ["library_id", "asset_id", "mode"],
        "additionalProperties": False,
    },
}
SEARCH_SCHEMA = {
    "description": "Search a ViKnow knowledge library and return unified evidence.",
    "parameters": {
        "type": "object",
        "properties": {
            "library_id": {
                "type": "string",
                "description": (
                    "Knowledge library id. Defaults to the configured knowledge library."
                ),
            },
            "query": {
                "type": "string",
                "description": "Natural language query to search in the knowledge library.",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "description": "Maximum number of final evidence objects to return after ranking.",
            },
            "threshold": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Reranker score threshold applied during retrieval.",
            },
        },
        "required": ["library_id", "query"],
        "additionalProperties": False,
    },
}

QUERY_REFINE_SCHEMA = {
    "description": "Plan answer slots and candidate retrieval queries for ViKnow knowledge search.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Current user query."},
            "conversation_context": {
                "type": "string",
                "description": "Optional compact conversation context.",
            },
            "library_context": {
                "type": "string",
                "description": "Optional knowledge library summary context.",
            },
            "precompiled_knowledge_context": {
                "type": "string",
                "description": (
                    "Optional precompiled knowledge context injected before agent planning."
                ),
            },
            "max_queries": {
                "type": "integer",
                "description": "Maximum number of candidate retrieval queries.",
                "default": 3,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

EVIDENCE_PLAN_SCHEMA = {
    "description": (
        "Create a required, structured evidence-collection plan after lightweight discovery. "
        "It decomposes the original question but does not retrieve or answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Original question to collect evidence for.",
            },
            "discovery_context": {
                "type": "string",
                "description": "Compact library and asset candidates returned by discovery.",
            },
            "max_subquestions": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

EVIDENCE_PROGRESS_SCHEMA = {
    "description": (
        "Record the Oracle main Agent's mutable per-slot research progress after reviewing "
        "scout or inspector feedback. The runtime injects the immutable EvidencePlan. "
        "This does not create evidence or answer the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "progress": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "investigating",
                                "supported",
                                "contradicted",
                                "unresolved",
                                "no_evidence_in_scope",
                            ],
                        },
                        "completed_checks": {"type": "array", "items": {"type": "string"}},
                        "unresolved_checks": {"type": "array", "items": {"type": "string"}},
                        "inspected_frontier_ids": {"type": "array", "items": {"type": "string"}},
                        "evidence_refs": {"type": "array", "items": {"type": "object"}},
                        "gaps": {"type": "array", "items": {"type": "string"}},
                        "next_research_action": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["requirement_id", "status"],
                    "additionalProperties": False,
                },
            },
            "events": {
                "type": "array",
                "description": (
                    "Main-Agent event map. Group adjacent frontier locators into semantic "
                    "events before delegating Inspector verification."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "requirement_ids": {"type": "array", "items": {"type": "string"}},
                        "frontier_ids": {"type": "array", "items": {"type": "string"}},
                        "time_range": {
                            "type": "object",
                            "properties": {
                                "start_seconds": {"type": "number"},
                                "end_seconds": {"type": "number"},
                            },
                            "required": ["start_seconds", "end_seconds"],
                            "additionalProperties": False,
                        },
                        "verification_targets": {"type": "array", "items": {"type": "string"}},
                        "status": {
                            "type": "string",
                            "enum": ["candidate", "verified", "excluded", "unresolved"],
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "event_id",
                        "requirement_ids",
                        "frontier_ids",
                        "time_range",
                        "verification_targets",
                        "status",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["progress"],
        "additionalProperties": False,
    },
}

EVIDENCE_EVENT_MAP_SCHEMA = {
    "description": (
        "Submit only the main Oracle agent's semantic grouping decisions after catalog recall. "
        "The runtime resolves canonical event IDs, locators, and time ranges from Scout facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "requirement_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "frontier_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "verification_targets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "requirement_ids",
                        "frontier_ids",
                        "verification_targets",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["decisions"],
        "additionalProperties": False,
    },
}

EVIDENCE_REFLECTION_SCHEMA = {
    "description": (
        "Record the Oracle main Agent's interpretation of the latest factual research "
        "progress. Open hypotheses require one concrete next challenge; this does not "
        "create evidence or decide the final answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reflection_id": {"type": "string"},
            "hypotheses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "statement": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["supported", "challenged", "open"],
                        },
                        "basis_refs": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "statement", "status", "basis_refs"],
                    "additionalProperties": False,
                },
            },
            "counterexamples": {"type": "array", "items": {"type": "string"}},
            "open_questions": {"type": "array", "items": {"type": "string"}},
            "next_challenge": {"type": "string"},
        },
        "required": ["reflection_id", "hypotheses", "counterexamples", "open_questions"],
        "additionalProperties": False,
    },
}

EVIDENCE_EXCLUSION_REVIEW_SCHEMA = {
    "description": (
        "Record the Oracle main Agent's review of material Scout exclusions for an "
        "exhaustive research requirement. Reopened exclusions need a concrete challenge."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "frontier_id": {"type": "string"},
                        "requirement_ids": {"type": "array", "items": {"type": "string"}},
                        "locator": {"type": "object"},
                        "scout_reason": {"type": "string"},
                        "main_disposition": {
                            "type": "string",
                            "enum": ["accepted", "reopen"],
                        },
                        "rationale": {"type": "string"},
                        "challenge": {"type": "string"},
                    },
                    "required": [
                        "frontier_id",
                        "requirement_ids",
                        "locator",
                        "scout_reason",
                        "main_disposition",
                        "rationale",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["reviews"],
        "additionalProperties": False,
    },
}

EVIDENCE_SCOUT_DECISION_SCHEMA = {
    "type": "object",
    "description": (
        "One semantic classification for one segment from the current catalog page. "
        "The Oracle harness derives report structure and locators from these decisions."
    ),
    "properties": {
        "segment_id": {"type": "string"},
        "classification": {
            "type": "string",
            "enum": ["candidate", "uncertain", "excluded"],
        },
        "requirement_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "reason": {"type": "string"},
        "match_sources": {
            "type": "array",
            "items": {"type": "string", "enum": ["caption", "asr", "adjacent"]},
        },
        "match_excerpt": {"type": "string"},
        "material_exclusion": {"type": "boolean", "default": False},
        "why_material": {"type": "string"},
    },
    "required": ["segment_id", "classification", "requirement_ids", "reason"],
    "additionalProperties": False,
}

EVIDENCE_SCOUT_REPORT_SCHEMA = {
    "description": (
        "Submit one semantic decision per segment in the current bounded catalog page. "
        "The Oracle harness injects immutable page facts and builds the full ScoutReport."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "decisions": {"type": "array", "items": EVIDENCE_SCOUT_DECISION_SCHEMA},
        },
        "required": ["decisions"],
        "additionalProperties": False,
    },
}

EVIDENCE_INSPECTION_REPORT_SCHEMA = {
    "description": (
        "Submit Inspector judgments for every verification target in the current "
        "InspectionTask. The Oracle harness binds the task and actual inspect calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["complete", "incomplete"]},
            "findings": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "verification_target": {"type": "string"},
                        "disposition": {
                            "type": "string",
                            "enum": ["supports", "contradicts", "unknown"],
                        },
                        "observed_facts": {"type": "array", "items": {"type": "string"}},
                        "evidence_refs": {"type": "array", "items": {"type": "object"}},
                        "limitations": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "verification_target",
                        "disposition",
                        "observed_facts",
                        "evidence_refs",
                        "limitations",
                    ],
                    "additionalProperties": False,
                },
            },
            "coverage_gaps": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "findings", "coverage_gaps"],
        "additionalProperties": False,
    },
}

EVIDENCE_PACKAGE_SCHEMA = {
    "description": (
        "Materialize the final non-authoritative CandidateEvidencePackage for the current "
        "approved Evidence Oracle task. Every evidence reference must stay inside task scope."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fact_id": {"type": "string"},
                        "requirement_id": {"type": "string"},
                        "disposition": {
                            "type": "string",
                            "enum": ["supports", "contradicts", "unknown"],
                        },
                        "statement": {"type": "string"},
                        "evidence_refs": {"type": "array", "items": {"type": "object"}},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": [
                        "fact_id",
                        "requirement_id",
                        "disposition",
                        "statement",
                        "evidence_refs",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
            "candidate_answer": {
                "type": "string",
                "description": "Optional draft answer for Gate B review only.",
            },
            "research_frontier": {
                "type": "array",
                "description": (
                    "Deduplicated in-scope locator candidates considered by this research run. "
                    "Use the exact locator returned by list_*_segments: library_id, asset_id, "
                    "and segment_id."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "frontier_id": {"type": "string"},
                        "source": {
                            "type": "string",
                            "enum": ["qa_citation", "discovery", "catalog", "adjacent"],
                        },
                        "requirement_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "locator": {
                            "type": "object",
                            "properties": {
                                "library_id": {"type": "string"},
                                "asset_id": {"type": "string"},
                                "segment_id": {"type": "string"},
                            },
                            "required": ["library_id", "asset_id", "segment_id"],
                            "additionalProperties": True,
                        },
                        "research_round": {"type": "integer", "minimum": 1},
                        "selection_reason": {"type": "string"},
                    },
                    "required": [
                        "frontier_id",
                        "source",
                        "requirement_ids",
                        "locator",
                        "research_round",
                        "selection_reason",
                    ],
                    "additionalProperties": False,
                },
            },
            "research_outcome": {
                "type": "object",
                "description": (
                    "Structured Research terminal state. This is not a candidate fact and "
                    "cannot be used as an accepted answer."
                ),
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["completed", "research_incomplete", "no_evidence_in_scope"],
                    },
                    "coverage": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "requirement_id": {"type": "string"},
                                "required_checks": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                },
                                "completed_checks": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "remaining_frontier_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": [
                                "requirement_id",
                                "required_checks",
                                "completed_checks",
                                "remaining_frontier_ids",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "note": {"type": "string"},
                },
                "required": ["status", "coverage"],
                "additionalProperties": False,
            },
        },
        "required": ["facts"],
        "additionalProperties": False,
    },
}

DEFAULT_LIBRARY_ID = "default"


def _ok(tool: str, **data: Any) -> str:
    return ToolResult.ok(tool, data).to_json()


def _fail(
    tool: str,
    code: ToolErrorCode,
    message: str,
    *,
    detail: str | None = None,
    retryable: bool = False,
    **data: Any,
) -> str:
    return ToolResult.fail(
        tool,
        code,
        message,
        detail=detail,
        retryable=retryable,
        data=data,
    ).to_json()


class QueryRefineAnswerSlot(BaseModel):
    """One independently verifiable answer requirement."""

    id: str = Field(description="Stable slot id, such as s1.")
    role: Literal["evidence", "collection"] = Field(
        description=(
            "evidence for one independently inspected fact, collection for deduplicated "
            "answer items. Calculated answers keep each source value in an evidence slot."
        ),
    )
    subquestion: str = Field(description="Focused question this slot must answer.")
    context: str | None = Field(
        default=None,
        description="Shared context constraint, if any; not an independent subquestion.",
    )
    required_modalities: list[Literal["text", "video", "image", "asr", "ocr"]] = Field(
        default_factory=lambda: ["text"],
        description="Planner-suggested evidence modalities; not hard verification requirements.",
    )
    optional_modalities: list[Literal["text", "video", "image", "asr", "ocr"]] = Field(
        default_factory=list,
        description="Useful but not mandatory evidence modalities.",
    )
    coverage_condition: str = Field(
        description="Concrete condition for when verified claims cover this slot."
    )
    answer_unit: str | None = Field(
        description=(
            "Definition of one deduplicable item when this slot collects multiple facts, "
            "such as one chart, event, clause, or course; null for non-collection slots."
        ),
    )
    status: Literal["pending"] = "pending"


class QueryRefineRetrievalQuery(BaseModel):
    """One retrieval query tied to answer slots."""

    query: str = Field(description="Complete natural-language query for retrieval.")
    purpose: str = Field(description="Why this query is needed.")
    priority: int = Field(default=1, ge=1)
    slot_ids: list[str] = Field(default_factory=list)


class QueryRefinePlan(BaseModel):
    """Structured LLM output for query_refine."""

    reasoning: str = Field(
        description=(
            "Brief analysis of retrieval target, format constraints, decomposition, "
            "and evidence modalities."
        )
    )
    context: str | None = Field(
        default=None,
        description="Shared scope/context in the user question; not something to answer by itself.",
    )
    rewritten_query: str | None = Field(
        default=None,
        description="Standalone query preserving the full retrieval intent.",
    )
    query_intent: Literal["retrieval_with_format", "normal_retrieval"] = "normal_retrieval"
    format_instruction: str | None = None
    risk_level: Literal["low", "medium", "high"] = "low"
    coverage_requirement: Literal["local", "full_asset"] = Field(
        description=(
            "local when selected evidence can establish the answer; full_asset when correctness "
            "requires proving the selected document or video was completely covered."
        )
    )
    answer_slots: list[QueryRefineAnswerSlot] = Field(default_factory=list)
    retrieval_queries: list[QueryRefineRetrievalQuery] = Field(default_factory=list)
    decomposition_reason: str = ""


class ImageUnderstandVLMOutput(BaseModel):
    """Structured VLM output for one image-understand call."""

    image_description: str = Field(
        description="Image description grounded in the image, query, and retrieval context."
    )
    query_supplement: str = Field(
        default="",
        description="Additional background that complements the query; can be empty.",
    )


class ImageQueryRelevanceOutput(BaseModel):
    """Structured VLM output for uploaded image/query relevance."""

    image_description: str = Field(description="Objective description of the uploaded image.")
    query_summary: str = Field(description="Brief summary of what the user asks.")
    relevant: bool = Field(description="Whether the uploaded image is relevant to the query.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence for the relevance judgment.")
    reason: str = Field(description="Brief reason for the relevance judgment.")


class ImageSearchResultRelevanceOutput(BaseModel):
    """Structured VLM output for uploaded image/search-result relevance."""

    relevant: bool = Field(
        description="Whether the image search results are relevant to the uploaded image."
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence for the relevance judgment.")
    reason: str = Field(description="Brief reason for the relevance judgment.")
    background_for_query_refine: str = Field(
        default="",
        description="Concise background that can help query_refine plan text retrieval.",
    )
    relevant_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence ids from the image_search result judged relevant to the uploaded image."
        ),
    )


@dataclass
class KnowledgeToolHandlers:
    """Handlers for knowledge discovery and search tools."""

    repo_root: Path | None = None
    tool_configs: ToolConfigs | None = None
    knowledge_adapter: KnowledgeClient | None = None
    query_refine_model: Any | None = None
    image_understand_model: Any | None = None
    _local_segment_catalogs: dict[str, SegmentCatalog] = field(default_factory=dict, init=False)

    def resolved_repo_root(self) -> Path:
        return (self.repo_root or Path.cwd()).resolve()

    def knowledge_discover(self, args: dict[str, Any]) -> str:
        """Discover libraries using bounded library and indexed-asset metadata."""
        query = str(args.get("query") or "").strip()
        top_k = _coerce_optional_positive_int(args.get("top_k"))
        allowed_library_ids = get_allowed_library_ids()
        if self.knowledge_adapter is None:
            return _fail(
                "knowledge_discover",
                ToolErrorCode.NOT_CONFIGURED,
                "knowledge adapter is not configured",
            )
        # The run request is authoritative when it contains exactly one
        # allowed library. There is no discovery or ranking decision left to
        # make, so return that scope immediately without touching HyperRAG's
        # shared runtime loop. Existence/content errors, if any, remain the
        # responsibility of the subsequent knowledge operation.
        if (
            allowed_library_ids is not None
            and len(allowed_library_ids) <= 1
            and not _oracle_needs_asset_discovery()
        ):
            libraries = [
                {
                    "id": library_id,
                    "name": library_id,
                    "description": "Explicitly selected knowledge library for this run.",
                    "metadata": {"source": "run_allowlist", "explicit": True},
                }
                for library_id in allowed_library_ids
            ]
            if top_k is not None:
                libraries = libraries[:top_k]
            return json.dumps(
                {"tool": "knowledge_discover", "libraries": libraries},
                ensure_ascii=False,
            )
        try:
            if _oracle_needs_asset_discovery():
                libraries = self.knowledge_adapter.list_libraries(library_ids=allowed_library_ids)
                if top_k is not None:
                    libraries = libraries[:top_k]
            else:
                libraries = self.knowledge_adapter.discover_libraries(
                    query,
                    top_k=top_k,
                    library_ids=allowed_library_ids,
                )
        except TimeoutError as exc:
            return _fail(
                "knowledge_discover",
                ToolErrorCode.TIMEOUT,
                str(exc),
                retryable=True,
            )
        except Exception as e:
            return _fail(
                "knowledge_discover",
                ToolErrorCode.UPSTREAM_ERROR,
                str(e),
                detail=str(e),
                retryable=True,
            )
        return _ok(
            "knowledge_discover",
            libraries=[_discover_library_payload(item) for item in libraries],
        )

    async def knowledge_discover_async(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        top_k = _coerce_optional_positive_int(args.get("top_k"))
        allowed_library_ids = get_allowed_library_ids()
        if self.knowledge_adapter is None:
            return _fail(
                "knowledge_discover",
                ToolErrorCode.NOT_CONFIGURED,
                "knowledge adapter is not configured",
            )
        if (
            allowed_library_ids is not None
            and len(allowed_library_ids) <= 1
            and not _oracle_needs_asset_discovery()
        ):
            return self.knowledge_discover(args)
        adapter = self.knowledge_adapter
        try:
            if _oracle_needs_asset_discovery():
                libraries = await adapter.list_libraries_async(library_ids=allowed_library_ids)
                if top_k is not None:
                    libraries = libraries[:top_k]
            else:
                libraries = await adapter.discover_libraries_async(
                    query,
                    top_k=top_k,
                    library_ids=allowed_library_ids,
                )
        except TimeoutError as exc:
            return _fail(
                "knowledge_discover",
                ToolErrorCode.TIMEOUT,
                str(exc),
                retryable=True,
            )
        except Exception as e:
            return _fail(
                "knowledge_discover",
                ToolErrorCode.UPSTREAM_ERROR,
                str(e),
                detail=str(e),
                retryable=True,
            )
        return _ok(
            "knowledge_discover",
            libraries=[_discover_library_payload(item) for item in libraries],
        )

    def list_video_segments(self, args: dict[str, Any]) -> str:
        library_id = str(args.get("library_id") or "").strip()
        if _is_precompiled_library(library_id):
            return _hidden_library_error(tool="list_video_segments", library_id=library_id)
        if not args.get("mode"):
            return _segment_tool_error(
                "list_video_segments",
                "mode is required; use map, page, select, or coverage",
            )
        return self._list_segments(args, asset_kind="video", tool="list_video_segments")

    def list_doc_segments(self, args: dict[str, Any]) -> str:
        library_id = str(args.get("library_id") or "").strip()
        if _is_precompiled_library(library_id):
            return _hidden_library_error(tool="list_doc_segments", library_id=library_id)
        if not args.get("mode"):
            return _segment_tool_error(
                "list_doc_segments",
                "mode is required; use map, page, select, or coverage",
            )
        return self._list_segments(args, asset_kind="document", tool="list_doc_segments")

    def _list_segments(
        self,
        args: dict[str, Any],
        *,
        asset_kind: Literal["video", "document"],
        tool: str,
    ) -> str:
        library_id = str(args.get("library_id") or "").strip()
        asset_id = str(args.get("asset_id") or "").strip()
        mode = str(args.get("mode") or "select").strip()
        if not library_id or not asset_id:
            return _segment_tool_error(tool, "library_id and asset_id are required")
        if not is_library_allowed(library_id):
            return _library_not_allowed_error(tool, library_id)
        if _is_precompiled_library(library_id):
            return _hidden_library_error(tool=tool, library_id=library_id)
        if mode not in {"map", "page", "select", "coverage"}:
            return _segment_tool_error(tool, f"unsupported mode: {mode}")
        try:
            catalog = self._segment_catalog(
                library_id=library_id,
                asset_id=asset_id,
                asset_kind=asset_kind,
                catalog_id=str(args.get("catalog_id") or "").strip() or None,
            )
            object_key = resolve_asset_object_key(library_id, asset_id)
            base = {
                "tool": tool,
                "mode": mode,
                "library_id": library_id,
                "asset_id": asset_id,
                "catalog": _catalog_payload(catalog),
            }
            if object_key and mode == "map":
                base["asset_path"] = object_key
            if mode == "map":
                return json.dumps(base, ensure_ascii=False)
            if mode == "page":
                cursor = str(args.get("cursor") or "") or None
                page = catalog.page(
                    cursor=cursor,
                    page_size=_bounded_positive_int(args.get("page_size"), default=20, maximum=50),
                )
                return json.dumps(
                    {
                        **base,
                        "cursor": cursor,
                        "segments": [
                            _segment_preview_payload(record, object_key) for record in page.records
                        ],
                        "next_cursor": page.next_cursor,
                        "next_action": _segment_next_action(asset_kind, mode="page"),
                    },
                    ensure_ascii=False,
                )
            if mode == "select":
                selection = catalog.select(
                    query=str(args.get("query") or ""),
                    limit=_bounded_positive_int(args.get("limit"), default=8, maximum=20),
                    neighbor_count=_bounded_nonnegative_int(
                        args.get("neighbor_count"), default=1, maximum=2
                    ),
                )
                return json.dumps(
                    {
                        **base,
                        "segments": [
                            _segment_preview_payload(record, object_key)
                            for record in selection.matches
                        ],
                        "adjacent_segments": [
                            _segment_preview_payload(record, object_key)
                            for record in selection.neighbors
                        ],
                        "selection_basis": selection.selection_basis,
                        "next_action": _segment_next_action(asset_kind, mode="select"),
                    },
                    ensure_ascii=False,
                )
            verified_ids, attempted_ids = _catalog_coverage_state(catalog)
            coverage = catalog.coverage(
                verified_segment_ids=verified_ids,
                attempted_segment_ids=attempted_ids,
            )
            return json.dumps(
                {
                    **base,
                    "coverage": {
                        "total_segments": coverage.total_records,
                        "verified_segment_ids": list(coverage.verified_segment_ids),
                        "attempted_segment_ids": list(coverage.attempted_segment_ids),
                        "uninspected_segment_ids": list(coverage.uninspected_segment_ids),
                    },
                    "next_action": _segment_next_action(asset_kind, mode="coverage"),
                },
                ensure_ascii=False,
            )
        except (RuntimeError, ValueError) as exc:
            return _segment_tool_error(tool, str(exc))

    def _segment_catalog(
        self,
        *,
        library_id: str,
        asset_id: str,
        asset_kind: Literal["video", "document"],
        catalog_id: str | None,
    ) -> SegmentCatalog:
        store = self._segment_catalog_store()
        if catalog_id:
            catalog = store.get(catalog_id)
            if catalog is None:
                raise ValueError("catalog_id is not available in this run")
            if (catalog.library_id, catalog.asset_id, catalog.asset_kind) != (
                library_id,
                asset_id,
                asset_kind,
            ):
                raise ValueError("catalog_id does not match library_id, asset_id, and asset kind")
            return catalog
        if self.knowledge_adapter is None:
            raise RuntimeError("knowledge adapter is not configured")
        chunks = self.knowledge_adapter.list_asset_segments(
            library_id=library_id,
            asset_id=asset_id,
        )
        records = [_segment_record(chunk) for chunk in chunks]
        revision = _segment_index_revision(records)
        catalog = SegmentCatalog.create(
            catalog_id=_catalog_id(library_id, asset_id, asset_kind, revision),
            library_id=library_id,
            asset_id=asset_id,
            asset_kind=asset_kind,
            index_revision=revision,
            records=records,
        )
        _validate_oracle_catalog_revision(catalog)
        store[catalog.catalog_id] = catalog
        return catalog

    def resolve_asset_index_revision(self, *, library_id: str, asset_id: str) -> str:
        """Return the canonical revision used by Oracle catalog validation."""
        if self.knowledge_adapter is None:
            raise RuntimeError("knowledge adapter is not configured")
        chunks = self.knowledge_adapter.list_asset_segments(
            library_id=library_id,
            asset_id=asset_id,
        )
        return _segment_index_revision([_segment_record(chunk) for chunk in chunks])

    def _segment_catalog_store(self) -> dict[str, SegmentCatalog]:
        context = get_tool_run_context()
        if context is not None:
            return context.segment_catalogs
        return self._local_segment_catalogs

    def query_refine(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return _fail(
                "query_refine",
                ToolErrorCode.INVALID_ARGUMENTS,
                "query is required",
            )
        max_queries = _coerce_query_refine_max_queries(args.get("max_queries"))
        precompiled_knowledge_context = str(args.get("precompiled_knowledge_context") or "").strip()
        if not precompiled_knowledge_context:
            run_context = get_tool_run_context()
            if run_context is not None:
                precompiled_knowledge_context = str(
                    getattr(run_context, "precompiled_knowledge_context", "") or ""
                ).strip()
        warnings = []
        model = self.get_query_refine_model()
        if model is not None:
            plan = self._query_refine_with_llm(
                model=model,
                query=query,
                conversation_context=str(args.get("conversation_context") or "").strip(),
                library_context=str(args.get("library_context") or "").strip(),
                precompiled_knowledge_context=precompiled_knowledge_context,
                max_queries=max_queries,
            )
            if plan is not None:
                graph_issues = _query_refine_plan_graph_issues(plan)
                if graph_issues:
                    repaired = self._query_refine_with_llm(
                        model=model,
                        query=query,
                        conversation_context=str(args.get("conversation_context") or "").strip(),
                        library_context=str(args.get("library_context") or "").strip(),
                        precompiled_knowledge_context=precompiled_knowledge_context,
                        max_queries=max_queries,
                        validation_feedback=graph_issues,
                        previous_plan=plan,
                    )
                    repaired_issues = (
                        _query_refine_plan_graph_issues(repaired)
                        if repaired is not None
                        else ["repair attempt returned no structured plan"]
                    )
                    if repaired is not None and not repaired_issues:
                        plan = repaired
                        warnings.append("llm plan failed graph validation and was repaired")
                    else:
                        plan = None
                        warnings.append("llm plan failed graph validation after one repair attempt")
                        warnings.append("initial graph issues: " + "; ".join(graph_issues))
                        warnings.append("repaired graph issues: " + "; ".join(repaired_issues))
            if plan is not None:
                return _query_refine_payload_from_plan(
                    query=query,
                    plan=plan,
                    max_queries=max_queries,
                    planner="llm",
                    warnings=warnings,
                )
            warnings.append("llm planner failed; used conservative fallback")
        return _fallback_query_refine_payload(query, max_queries, warnings=warnings)

    def evidence_plan(self, args: dict[str, Any]) -> str:
        """Turn query_refine output into the Oracle's explicit coverage contract."""
        query = str(args.get("query") or "").strip()
        if not query:
            return json.dumps(
                {"tool": "evidence_plan", "error": "query is required"}, ensure_ascii=False
            )
        max_subquestions = _bounded_positive_int(args.get("max_subquestions"), default=6, maximum=8)
        refined = parse_tool_payload(
            self.query_refine(
                {
                    "query": query,
                    "library_context": str(args.get("discovery_context") or "").strip(),
                    "max_queries": max_subquestions,
                }
            )
        )
        if refined.get("error"):
            return json.dumps(
                {"tool": "evidence_plan", "error": str(refined["error"])}, ensure_ascii=False
            )
        slots = refined.get("answer_slots")
        slots = slots if isinstance(slots, list) else []
        subquestions = [
            {
                "id": str(slot.get("id") or f"s{index}"),
                "question": str(slot.get("subquestion") or query),
                "expected_modalities": list(slot.get("required_modalities") or []),
                "optional_modalities": list(slot.get("optional_modalities") or []),
                "coverage_condition": str(slot.get("coverage_condition") or ""),
                "must_support_before_answering": True,
            }
            for index, slot in enumerate(slots[:max_subquestions], start=1)
            if isinstance(slot, dict)
        ]
        research_subquestions = [
            {
                "id": subquestion["id"],
                "question": subquestion["question"],
                "coverage_condition": subquestion["coverage_condition"],
                "coverage_checks": [
                    "direct_expression",
                    "alternative_expression",
                    "neighbor_or_narrative_anchor",
                ],
                "evidence_angles": [
                    "direct_statement",
                    "narrative_premise",
                    "dialogue_or_on_screen_text",
                    "outcome_or_counterevidence",
                ],
                "requires_exhaustive_event_coverage": _query_requires_exhaustive_event_coverage(
                    subquestion["question"]
                ),
            }
            for subquestion in subquestions
        ]
        return json.dumps(
            {
                "tool": "evidence_plan",
                "original_query": query,
                "discovery_context_provided": bool(args.get("discovery_context")),
                "query_refine": refined,
                "subquestions": subquestions,
                "research_plan": {
                    "subquestions": research_subquestions,
                },
                "next_action": {
                    "type": "collect_scoped_evidence",
                    "instruction": (
                        "For every subquestion, collect inspectable candidate locators and record "
                        "supported, conflicting, and uncovered evidence before synthesis."
                    ),
                },
            },
            ensure_ascii=False,
        )

    def evidence_progress(self, args: dict[str, Any]) -> str:
        """Validate one full main-Agent research-state checkpoint."""
        try:
            if "research_state" not in args:
                raise ValueError(
                    "research_state is required; pass the complete nested plan and progress object"
                )
            raw_state = args["research_state"]
            if isinstance(raw_state, str):
                raise ValueError(
                    "research_state must be an object, not a JSON-encoded string; "
                    "pass the nested plan and progress directly"
                )
            state = ResearchState.model_validate(raw_state)
        except (KeyError, TypeError, ValueError) as exc:
            return _segment_tool_error("evidence_progress", str(exc))
        return json.dumps(
            {
                "tool": "evidence_progress",
                "research_state": state.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def evidence_reflection(self, args: dict[str, Any]) -> str:
        """Validate a main-Agent reflection against the latest factual checkpoint."""
        try:
            reflection = ResearchReflection.model_validate(args["reflection"])
        except (KeyError, TypeError, ValueError) as exc:
            return _segment_tool_error("evidence_reflection", str(exc))
        return json.dumps(
            {
                "tool": "evidence_reflection",
                "reflection": reflection.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def evidence_exclusion_review(self, args: dict[str, Any]) -> str:
        """Validate main-Agent treatment of material Scout exclusions."""
        try:
            review = ExclusionReview.model_validate(args["exclusion_review"])
        except (KeyError, TypeError, ValueError) as exc:
            return _segment_tool_error("evidence_exclusion_review", str(exc))
        return json.dumps(
            {
                "tool": "evidence_exclusion_review",
                "exclusion_review": review.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def evidence_scout_report(self, args: dict[str, Any]) -> str:
        """Validate and persist one Scout's bounded catalog handoff."""
        try:
            report = ScoutReport.model_validate(args["scout_report"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("evidence_scout_report validation failed: {}", exc)
            error_code, safe_message = _scout_report_validation_hint(exc)
            return json.dumps(
                {
                    "tool": "evidence_scout_report",
                    "success": False,
                    "error": {
                        "code": error_code,
                        "kind": "validation",
                        "message": safe_message,
                        "retryable": True,
                    },
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"tool": "evidence_scout_report", "scout_report": report.model_dump(mode="json")},
            ensure_ascii=False,
        )

    def evidence_inspection_report(self, args: dict[str, Any]) -> str:
        """Validate the canonical Inspector handoff assembled by the harness."""
        try:
            report = InspectionReport.model_validate(args["inspection_report"])
        except (KeyError, TypeError, ValueError) as exc:
            return _segment_tool_error("evidence_inspection_report", str(exc))
        return json.dumps(
            {
                "tool": "evidence_inspection_report",
                "inspection_report": report.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    def evidence_event_map(self, args: dict[str, Any]) -> str:
        """Validate the main agent's event hypotheses before Inspector delegation."""
        try:
            plan = ResearchPlan.model_validate(args["research_plan"])
            events = [ResearchEvent.model_validate(event) for event in args["events"]]
            if any(event.status == "verified" for event in events):
                raise ValueError(
                    "evidence_event_map defines candidate events, not verified facts; "
                    "submit candidate/unresolved events and use evidence_progress after "
                    "a successful Inspector handoff to mark them verified"
                )
            plan_ids = {item.id for item in plan.subquestions}
            if any(
                requirement_id not in plan_ids
                for event in events
                for requirement_id in event.requirement_ids
            ):
                raise ValueError("research event requirement ids must belong to ResearchPlan")
        except (KeyError, TypeError, ValueError) as exc:
            return _segment_tool_error("evidence_event_map", str(exc))
        return json.dumps(
            {
                "tool": "evidence_event_map",
                "event_map": {
                    "research_plan": plan.model_dump(mode="json"),
                    "events": [event.model_dump(mode="json") for event in events],
                },
            },
            ensure_ascii=False,
        )

    def evidence_package(self, args: dict[str, Any]) -> str:
        """Validate and materialize the one candidate package for this Oracle run."""
        context = get_tool_run_context()
        if context is None or context.oracle_task is None:
            return _segment_tool_error(
                "evidence_package", "an approved EvidenceCollectionTask is required"
            )
        try:
            if "research_state" not in args:
                raise ValueError("research_state is required")
            task = EvidenceCollectionTask.model_validate(context.oracle_task)
            task_payload = task.model_dump(mode="json")
            research_state = ResearchState.model_validate(args["research_state"])
            task_payload["research_plan"] = research_state.plan.model_dump(mode="json")
            if "research_frontier" in args:
                task_payload["research_frontier"] = args["research_frontier"]
            task = EvidenceCollectionTask.model_validate(task_payload)
            facts = [
                CandidateEvidenceFact.model_validate(fact)
                for fact in self._materialize_frontier_evidence_refs(
                    args.get("facts", []), task.research_frontier
                )
            ]
            research_outcome = None
            if "research_outcome" in args:
                outcome_payload = dict(args["research_outcome"])
                outcome_payload["event_state"] = research_state.model_dump(mode="json")
                research_outcome = ResearchOutcome.model_validate(outcome_payload)
            package = build_candidate_evidence_package(
                task=task,
                facts=facts,
                candidate_answer=(
                    str(args["candidate_answer"]).strip()
                    if args.get("candidate_answer") is not None
                    else None
                ),
                research_state=research_state,
                research_outcome=research_outcome,
                observed_asset_revisions={
                    catalog.asset_id: catalog.index_revision
                    for catalog in context.segment_catalogs.values()
                },
            )
        except (TypeError, ValueError) as exc:
            return _segment_tool_error("evidence_package", str(exc))
        return json.dumps(
            {
                "tool": "evidence_package",
                "status": "pending_gate_b",
                "package": package.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _materialize_frontier_evidence_refs(
        facts: list[dict[str, Any]],
        frontier: list[Any],
    ) -> list[dict[str, Any]]:
        """Promote the canonical locator for facts that cite a research frontier."""

        locators_by_frontier_id = {item.frontier_id: dict(item.locator) for item in frontier}
        materialized_facts: list[dict[str, Any]] = []
        for fact in facts:
            materialized_refs: list[dict[str, Any]] = []
            for evidence_ref in fact.get("evidence_refs", []):
                materialized_ref = dict(evidence_ref)
                frontier_id = str(materialized_ref.get("frontier_id") or "")
                locator = locators_by_frontier_id.get(frontier_id)
                if locator is not None:
                    for key in ("library_id", "asset_id", "segment_id"):
                        supplied = materialized_ref.get(key)
                        expected = locator.get(key)
                        if supplied is not None and expected is not None and supplied != expected:
                            raise ValueError(
                                "candidate evidence frontier locator conflicts with scope"
                            )
                        if expected is not None:
                            materialized_ref.setdefault(key, expected)
                materialized_refs.append(materialized_ref)
            materialized_facts.append({**fact, "evidence_refs": materialized_refs})
        return materialized_facts

    def get_query_refine_model(self):
        if self.query_refine_model is not None:
            return self.query_refine_model
        # Tests set this so the scripted chat model is the only model. Query
        # refine then uses its offline fallback instead of calling a provider.
        if os.environ.get("VIKNOW_USE_SCRIPT_MODEL", "").strip().lower() in {"1", "true", "yes"}:
            return None
        if self.tool_configs is None or self.tool_configs.multimodal is None:
            return None
        review_model = self.tool_configs.multimodal.review_model
        if review_model.provider != ModelProvider.OPENAI_COMPATIBLE:
            logger.warning(f"unsupported query_refine provider: {review_model.provider}")
            return None
        base_url = review_model.resolve_base_url()
        if not base_url:
            return None
        try:
            from langchain_openai import ChatOpenAI

            self.query_refine_model = ChatOpenAI(
                model=review_model.require_model(),
                base_url=base_url,
                api_key=review_model.require_api_key(),
                temperature=0,
                timeout=min(review_model.timeout_seconds, 30),
                extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
                **model_connection_options(review_model),
            )
        except Exception as exc:
            logger.warning(f"query_refine model unavailable: {exc}")
            return None
        return self.query_refine_model

    def get_image_understand_model(self):
        if self.image_understand_model is not None:
            return self.image_understand_model
        if self.tool_configs is None or self.tool_configs.multimodal is None:
            return None
        review_model = self.tool_configs.multimodal.review_model
        if review_model.provider != ModelProvider.OPENAI_COMPATIBLE:
            raise ValueError(f"unsupported image_understand provider: {review_model.provider}")
        base_url = review_model.resolve_base_url()
        if not base_url:
            raise RuntimeError("image_understand model base_url or base_url_env is required")
        try:
            from langchain_openai import ChatOpenAI

            self.image_understand_model = ChatOpenAI(
                model=review_model.require_model(),
                base_url=base_url,
                api_key=review_model.require_api_key(),
                temperature=0,
                timeout=review_model.timeout_seconds,
                extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
                **model_connection_options(review_model),
            )
        except Exception as exc:
            logger.warning(f"image_understand model unavailable: {exc}")
            return None
        return self.image_understand_model

    @staticmethod
    def _query_refine_with_llm(
        *,
        model,
        query: str,
        conversation_context: str,
        library_context: str,
        precompiled_knowledge_context: str,
        max_queries: int,
        validation_feedback: list[str] | None = None,
        previous_plan: QueryRefinePlan | None = None,
    ) -> QueryRefinePlan | None:
        from langchain_core.messages import HumanMessage, SystemMessage

        if previous_plan is None:
            interpretation_input = query
            if conversation_context:
                interpretation_input += f"\n\n# 对话上下文\n{conversation_context}"
            try:
                interpretation = invoke_text_llm(
                    model,
                    [
                        SystemMessage(content=QUERY_INTERPRETATION_SYSTEM_PROMPT),
                        HumanMessage(content=interpretation_input),
                    ],
                    max_tokens=512,
                    run_name="query_refine_interpretation",
                )
            except Exception as exc:
                logger.warning("query_refine interpretation failed: {}", type(exc).__name__)
                return None
            if (
                not isinstance(interpretation.content, str)
                or not interpretation.content.strip()
                or interpretation.response_metadata.get("finish_reason") == "length"
            ):
                logger.warning("query_refine interpretation was empty or incomplete")
                return None
            # Keep the original request and user context authoritative. This is
            # model-generated context, not a rewritten request or adopted goal.
            conversation_context = "\n\n".join(
                part
                for part in (
                    conversation_context,
                    "以下是初步题意说明，不是用户新增要求，也不是权威判定。"
                    "规划时核对原问题，可修正说明中的错误：\n" + interpretation.content,
                )
                if part
            )
        messages = [
            SystemMessage(content=QUERY_REFINE_SYSTEM_PROMPT),
            HumanMessage(
                content=_build_query_refine_prompt(
                    query=query,
                    conversation_context=conversation_context,
                    library_context=library_context,
                    precompiled_knowledge_context=precompiled_knowledge_context,
                    max_queries=max_queries,
                    validation_feedback=validation_feedback,
                    previous_plan=previous_plan,
                )
            ),
        ]
        return invoke_structured_llm(
            model,
            QueryRefinePlan,
            messages,
            run_name="query_refine",
            max_attempts=2,
            max_tokens=2048,
        )

    def knowledge_search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return _fail("knowledge_search", ToolErrorCode.INVALID_ARGUMENTS, "query is required")
        library_id = str(args.get("library_id") or self.default_library_id())
        raw_top_k = args.get("top_k")
        top_k = int(raw_top_k) if raw_top_k is not None else None
        threshold = _coerce_optional_threshold(args.get("threshold"))
        if top_k is not None and top_k < 1:
            return _fail(
                "knowledge_search", ToolErrorCode.INVALID_ARGUMENTS, "top_k must be positive"
            )
        if args.get("threshold") is not None and threshold is None:
            return _fail(
                "knowledge_search",
                ToolErrorCode.INVALID_ARGUMENTS,
                "threshold must be between 0 and 1",
            )
        if _is_precompiled_library(library_id):
            return _hidden_library_error(tool="knowledge_search", library_id=library_id)
        if not is_library_allowed(library_id):
            return _library_not_allowed_error("knowledge_search", library_id)
        if library_id != self.default_library_id() and self.knowledge_adapter is None:
            return _fail(
                "knowledge_search",
                ToolErrorCode.UNKNOWN_LIBRARY,
                f"unknown knowledge library: {library_id}",
                library_id=library_id,
                query=query,
            )
        if self.knowledge_adapter is None:
            return _fail(
                "knowledge_search",
                ToolErrorCode.NOT_CONFIGURED,
                "knowledge adapter is not configured",
                library_id=library_id,
                query=query,
            )
        budget = acquire_knowledge_search_budget()
        if budget is not None:
            return self._knowledge_search_budget_exhausted(
                library_id=library_id,
                query=query,
                budget=budget,
            )
        try:
            search_kwargs: dict[str, Any] = {
                "library_id": library_id,
                "query": query,
                "top_k": top_k,
            }
            if threshold is not None:
                search_kwargs["threshold"] = threshold
            result = self.knowledge_adapter.search(**search_kwargs)
        except Exception as exc:
            return _fail(
                "knowledge_search",
                ToolErrorCode.UPSTREAM_ERROR,
                str(exc),
                detail=str(exc),
                retryable=True,
                library_id=library_id,
                query=query,
            )
        return self._knowledge_search_payload(
            library_id=library_id,
            query=query,
            result=result,
        )

    async def knowledge_search_async(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return _fail("knowledge_search", ToolErrorCode.INVALID_ARGUMENTS, "query is required")
        library_id = str(args.get("library_id") or self.default_library_id())
        raw_top_k = args.get("top_k")
        top_k = int(raw_top_k) if raw_top_k is not None else None
        threshold = _coerce_optional_threshold(args.get("threshold"))
        if top_k is not None and top_k < 1:
            return _fail(
                "knowledge_search", ToolErrorCode.INVALID_ARGUMENTS, "top_k must be positive"
            )
        if args.get("threshold") is not None and threshold is None:
            return _fail(
                "knowledge_search",
                ToolErrorCode.INVALID_ARGUMENTS,
                "threshold must be between 0 and 1",
            )
        if _is_precompiled_library(library_id):
            return _hidden_library_error(tool="knowledge_search", library_id=library_id)
        if not is_library_allowed(library_id):
            return _library_not_allowed_error("knowledge_search", library_id)
        if self.knowledge_adapter is None:
            return self.knowledge_search(args)
        budget = acquire_knowledge_search_budget()
        if budget is not None:
            return self._knowledge_search_budget_exhausted(
                library_id=library_id,
                query=query,
                budget=budget,
            )
        try:
            search_kwargs: dict[str, Any] = {
                "library_id": library_id,
                "query": query,
                "top_k": top_k,
            }
            if threshold is not None:
                search_kwargs["threshold"] = threshold
            result = await self.knowledge_adapter.search_async(**search_kwargs)
        except Exception as exc:
            return _fail(
                "knowledge_search",
                ToolErrorCode.UPSTREAM_ERROR,
                str(exc),
                detail=str(exc),
                retryable=True,
                library_id=library_id,
                query=query,
            )
        return self._knowledge_search_payload(
            library_id=library_id,
            query=query,
            result=result,
        )

    async def load_precompiled_knowledge_context(
        self,
        *,
        library_id: str,
        query: str,
        top_k: int = 8,
        threshold: float = 0.0,
    ) -> str:
        if self.knowledge_adapter is None:
            return ""
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return ""
        payload = await self.knowledge_adapter.query_kg_async(
            library_id=library_id,
            query=normalized_query,
            pre_compile=True,
            top_k=max(1, int(top_k)),
            threshold=float(threshold),
        )
        return _format_precompiled_knowledge_context(payload, max_items=max(1, int(top_k)))

    def image_search(
        self,
        library_id: str,
        query: str,
        image_list: list[str],
        mix_search: bool = True,
    ) -> list[dict[str, Any]]:
        normalized_library_id = str(library_id or "").strip()
        if not normalized_library_id:
            raise ValueError("library_id is required")
        if _is_precompiled_library(normalized_library_id):
            raise ValueError("precompiled knowledge is only available through agent memory")
        if not image_list:
            raise ValueError("image_list is required")
        if self.knowledge_adapter is None:
            raise RuntimeError("knowledge adapter is not configured")

        normalized_query = str(query or "").strip()
        grouped_results: list[dict[str, Any]] = []
        for raw_path in image_list:
            image_path = str(raw_path or "").strip()
            if not image_path:
                raise ValueError("image_list contains an empty image path")
            data_url = _image_file_path_to_base64_data_url(
                image_path,
                repo_root=self.resolved_repo_root(),
            )
            query_for_this_image = normalized_query if mix_search else ""
            result = self.knowledge_adapter.search_by_image(
                library_id=normalized_library_id,
                image_data_url=data_url,
                query=query_for_this_image,
                mix_search=mix_search,
            )
            grouped_results.append(
                {
                    "library_id": normalized_library_id,
                    "image_path": str(Path(image_path)),
                    "query": query_for_this_image,
                    "mix_search": bool(mix_search),
                    "evidence": [item.model_dump(mode="json") for item in result.evidence],
                    "metadata": result.metadata,
                }
            )
        return grouped_results

    def image_query_relevance(self, query: str, image_path: str) -> ImageQueryRelevanceOutput:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("query is required")
        normalized_image_path = str(image_path or "").strip()
        if not normalized_image_path:
            raise ValueError("image_path is required")

        model = self.get_image_understand_model()
        if model is None:
            raise RuntimeError("image_understand model is not configured")
        from langchain_core.messages import HumanMessage, SystemMessage

        data_url = _image_file_path_to_base64_data_url(
            normalized_image_path,
            repo_root=self.resolved_repo_root(),
        )
        parsed = invoke_structured_llm(
            model,
            ImageQueryRelevanceOutput,
            [
                SystemMessage(content=IMAGE_QUERY_RELEVANCE_SYSTEM_PROMPT),
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": _build_image_query_relevance_prompt(
                                query=normalized_query,
                                image_name=_uploaded_image_name_from_path(normalized_image_path),
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]
                ),
            ],
            run_name="image_query_relevance",
            max_attempts=2,
            max_tokens=800,
        )
        if parsed is None:
            raise RuntimeError(
                f"image_query_relevance failed to parse output: {normalized_image_path}"
            )
        return parsed

    def image_search_result_relevance(
        self,
        query: str,
        image_path: str,
        search_result: list[Any],
    ) -> ImageSearchResultRelevanceOutput:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("query is required")
        normalized_image_path = str(image_path or "").strip()
        if not normalized_image_path:
            raise ValueError("image_path is required")
        if not isinstance(search_result, list):
            raise ValueError("search_result must be a list")

        model = self.get_image_understand_model()
        if model is None:
            raise RuntimeError("image_understand model is not configured")
        from langchain_core.messages import HumanMessage, SystemMessage

        data_url = _image_file_path_to_base64_data_url(
            normalized_image_path,
            repo_root=self.resolved_repo_root(),
        )
        parsed = invoke_structured_llm(
            model,
            ImageSearchResultRelevanceOutput,
            [
                SystemMessage(content=IMAGE_SEARCH_RESULT_RELEVANCE_SYSTEM_PROMPT),
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": _build_image_search_result_relevance_prompt(
                                query=normalized_query,
                                image_name=_uploaded_image_name_from_path(normalized_image_path),
                                retrieval_context=_build_image_understand_retrieval_context(
                                    search_result
                                ),
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]
                ),
            ],
            run_name="image_search_result_relevance",
            max_attempts=2,
            max_tokens=1000,
        )
        if parsed is None:
            raise RuntimeError(
                f"image_search_result_relevance failed to parse output: {normalized_image_path}"
            )
        return parsed

    def image_understand(
        self,
        query: str,
        image_list: list[str],
        search_result: list[Any],
    ) -> dict[str, list[dict[str, str]]]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ValueError("query is required")
        if not image_list:
            raise ValueError("image_list is required")
        if not isinstance(search_result, list):
            raise ValueError("search_result must be a list")

        model = self.get_image_understand_model()
        if model is None:
            raise RuntimeError("image_understand model is not configured")
        from langchain_core.messages import HumanMessage, SystemMessage

        repo_root = self.resolved_repo_root()
        retrieval_context = _build_image_understand_retrieval_context(search_result)
        results: list[dict[str, str]] = []
        for raw_path in image_list:
            image_path = str(raw_path or "").strip()
            if not image_path:
                raise ValueError("image_list contains an empty image path")
            data_url = _image_file_path_to_base64_data_url(image_path, repo_root=repo_root)
            prompt = _build_image_understand_prompt(
                query=normalized_query,
                retrieval_context=retrieval_context,
                image_path=image_path,
            )
            parsed = invoke_structured_llm(
                model,
                ImageUnderstandVLMOutput,
                [
                    SystemMessage(content=IMAGE_UNDERSTAND_SYSTEM_PROMPT),
                    HumanMessage(
                        content=[
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ]
                    ),
                ],
                run_name="image_understand",
                max_attempts=2,
                max_tokens=1000,
            )
            if parsed is None:
                raise RuntimeError(f"image_understand failed to parse output: {image_path}")
            results.append(
                {
                    "image_description": parsed.image_description.strip(),
                    "query_supplement": parsed.query_supplement.strip(),
                }
            )
        return {"results": results}

    @staticmethod
    def _knowledge_search_budget_exhausted(
        *,
        library_id: str,
        query: str,
        budget: Any,
    ) -> str:
        return _ok(
            "knowledge_search",
            library_id=library_id,
            query=query,
            evidence=[],
            metadata={
                "search_budget_exhausted": True,
                "search_calls": budget.calls,
                "search_max_calls": budget.max_calls,
                "message": (
                    "当前检索行为的次数预算已耗尽，未找到相关内容。请停止检索，"
                    "并基于现有已知信息总结收敛。"
                ),
                "reset_rule": (
                    "在调用任何其他非检索工具后，当前检索行为结束，"
                    "knowledge_search 次数预算将重置。"
                ),
            },
        )

    @staticmethod
    def _knowledge_search_payload(
        *,
        library_id: str,
        query: str,
        result: KnowledgeSearchResult,
    ) -> str:
        return _ok(
            "knowledge_search",
            library_id=library_id,
            query=query,
            evidence=[item.model_dump(mode="json") for item in result.evidence],
            metadata=result.metadata,
        )

    def resolve_evidence(self, locator: EvidenceLocator) -> Evidence:
        """Resolve a lightweight evidence locator into a full Evidence object."""
        if not is_library_allowed(locator.library_id):
            raise ValueError(f"knowledge library is not allowed for this run: {locator.library_id}")
        if self.knowledge_adapter is None:
            raise RuntimeError("knowledge adapter is not configured")
        return self._resolve_from_adapter(locator)

    def _resolve_from_adapter(self, locator: EvidenceLocator) -> Evidence:
        """Resolve evidence through the injected knowledge client."""
        adapter = self.knowledge_adapter
        if adapter is None:
            raise RuntimeError("knowledge adapter is not configured")
        if locator.evidence_id:
            chunk = adapter.get_chunk_by_id(
                library_id=locator.library_id,
                evidence_id=locator.evidence_id,
            )
            if chunk is not None:
                evidence = adapter.evidence_for_chunk(
                    library_id=locator.library_id,
                    chunk=chunk,
                )
                if evidence is not None:
                    if locator.title:
                        evidence = evidence.model_copy(update={"title": locator.title})
                    return evidence
            try:
                chunks = adapter.list_asset_segments(
                    library_id=locator.library_id,
                    asset_id=locator.asset_id,
                )
            except Exception:
                pass
            else:
                for chunk in chunks:
                    if _chunk_matches_evidence_id(chunk, locator.evidence_id):
                        evidence = adapter.evidence_for_chunk(
                            library_id=locator.library_id,
                            chunk=chunk,
                        )
                        if evidence is not None:
                            if locator.title:
                                evidence = evidence.model_copy(update={"title": locator.title})
                            return evidence

        status = adapter.get_asset_status(
            library_id=locator.library_id,
            asset_id=locator.asset_id,
        )
        if status is None:
            raise ValueError(f"asset not found: {locator.asset_id}")

        file_path = _dict_or_attr(status, "file_path")
        parse_mode = _dict_or_attr(status, "parse_mode")

        if not file_path:
            raise ValueError(f"asset {locator.asset_id} does not have a file_path")

        if parse_mode in {"video", "sop_video"}:
            kind = EvidencePartKind.VIDEO
            mime_type = "video/mp4"
        elif parse_mode == "image":
            kind = EvidencePartKind.IMAGE
            mime_type = "image/jpeg"
        else:
            kind = EvidencePartKind.FILE
            mime_type = None

        return Evidence(
            id=locator.evidence_id or locator.asset_id,
            source="knowledge_search",
            title=locator.title or locator.asset_id,
            parts=[
                EvidencePart(
                    kind=kind,
                    uri=file_path,
                    mime_type=mime_type,
                    time_range=locator.time_range,
                    metadata={"library_id": locator.library_id, "asset_id": locator.asset_id},
                )
            ],
            metadata={
                "library_id": locator.library_id,
                "asset_id": locator.asset_id,
                "file_path": file_path,
                "parse_mode": parse_mode,
            },
        )

    def default_library_id(self) -> str:
        if self.knowledge_adapter is not None:
            return str(self.knowledge_adapter.default_library_id)
        return DEFAULT_LIBRARY_ID


def _chunk_matches_evidence_id(chunk: dict[str, Any], evidence_id: str) -> bool:
    metadata = chunk.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    candidates = (
        chunk.get("chunk_id"),
        chunk.get("id"),
        metadata.get("chunk_id"),
        metadata.get("evidence_id"),
    )
    return any(str(candidate or "").strip() == evidence_id for candidate in candidates)


def _scout_report_validation_hint(error: Exception) -> tuple[str, str]:
    """Project Scout contract failures into a safe, actionable retry hint."""

    detail = str(error)
    if "locator frontier ids must be unique" in detail:
        return (
            "scout_locator_frontier_duplicate",
            "Locator frontier_id values must be unique; remove duplicate locators "
            "from the current page handoff and retry.",
        )
    if "requires a locator for every retained segment" in detail:
        return (
            "scout_retained_locator_missing",
            "Every candidate or uncertain segment needs a locator; "
            "correct the current page handoff and retry.",
        )
    if "exact partition" in detail or "segment partition" in detail:
        return (
            "scout_page_partition_invalid",
            "Classify every current-page segment exactly once as candidate, uncertain, "
            "or excluded; "
            "correct the current page handoff and retry.",
        )
    if "material exclusion" in detail:
        return (
            "scout_material_exclusion_invalid",
            "Each material exclusion needs its current-page locator and rationale; "
            "correct the current page handoff and retry.",
        )
    return (
        "invalid_scout_report",
        "Scout report is invalid; correct the current page handoff and retry.",
    )


def _segment_tool_error(tool: str, message: str) -> str:
    return json.dumps({"tool": tool, "error": message}, ensure_ascii=False)


def _segment_preview_payload(record: SegmentRecord, object_key: str | None) -> dict[str, Any]:
    payload = record.as_payload()
    if object_key:
        payload["asset_path"] = object_key
    return payload


def _catalog_payload(catalog: SegmentCatalog) -> dict[str, Any]:
    return {
        "catalog_id": catalog.catalog_id,
        "asset_kind": catalog.asset_kind,
        "index_revision": catalog.index_revision,
        "total_segments": len(catalog.records),
    }


def _validate_oracle_catalog_revision(catalog: SegmentCatalog) -> None:
    context = get_tool_run_context()
    scope = context.oracle_scope if context is not None else None
    if not isinstance(scope, dict):
        return
    revisions = scope.get("asset_revisions")
    revisions = revisions if isinstance(revisions, dict) else {}
    expected = str(revisions.get(catalog.asset_id) or "")
    if expected and expected != catalog.index_revision:
        raise ValueError("catalog index_revision does not match the approved oracle scope")


def _segment_next_action(
    asset_kind: Literal["video", "document"], *, mode: Literal["page", "select", "coverage"]
) -> dict[str, Any]:
    inspect_tool = "video_inspect" if asset_kind == "video" else "text_inspect"
    if mode == "coverage":
        instruction = (
            "Use the coverage gaps to decide whether another bounded page or selection is needed; "
            "do not claim absence from a selection miss alone."
        )
    elif mode == "page":
        instruction = (
            "This is a bounded catalog page and only provides locators. Select relevant segments "
            f"and call {inspect_tool} before using their content as evidence."
        )
    else:
        instruction = (
            "These are candidate locators, not evidence. Inspect the smallest relevant set before "
            "answering; use a bounded adjacent range only when needed for context."
        )
    return {
        "type": "select_then_inspect" if mode != "coverage" else "review_coverage",
        "evidence_role": "locator_only",
        "inspect_tool": inspect_tool,
        "instruction": instruction,
    }


def _segment_record(chunk: dict[str, Any]) -> SegmentRecord:
    metadata = chunk.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    segment_id = str(chunk.get("chunk_id") or chunk.get("id") or "").strip()
    if not segment_id:
        raise ValueError("indexed asset segment is missing chunk_id")
    content = str(chunk.get("content") or "")
    return SegmentRecord(
        segment_id=segment_id,
        order=_bounded_nonnegative_int(chunk.get("order"), default=0, maximum=2**31 - 1),
        summary=_segment_summary(content, metadata),
        content=content,
        metadata=metadata,
        time_range=_segment_time_range(metadata),
        page_range=_segment_page_range(metadata),
        section=str(metadata.get("section_title") or metadata.get("heading") or "").strip(),
        candidate_evidence=_segment_candidate_evidence(metadata),
    )


def _segment_summary(content: str, metadata: dict[str, Any]) -> str:
    summary = str(metadata.get("summary") or metadata.get("caption") or content)
    summary = re.sub(r"\s+", " ", summary).strip()
    return summary[:600]


def _segment_candidate_evidence(metadata: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    raw_candidates = metadata.get("candidate_evidence")
    if raw_candidates is None:
        return ()
    if not isinstance(raw_candidates, list) or not all(
        isinstance(candidate, dict) for candidate in raw_candidates
    ):
        raise ValueError("indexed candidate_evidence must be a list of objects")
    return tuple(dict(candidate) for candidate in raw_candidates)


def _segment_time_range(metadata: dict[str, Any]) -> dict[str, float] | None:
    value = metadata.get("time_range")
    if isinstance(value, dict):
        start = value.get("start_seconds")
        end = value.get("end_seconds")
    else:
        start = metadata.get("begin_ts")
        end = metadata.get("end_ts")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and start <= end:
        return {"start_seconds": float(start), "end_seconds": float(end)}
    return None


def _segment_page_range(metadata: dict[str, Any]) -> dict[str, int] | None:
    start = metadata.get("start_page", metadata.get("page_number", metadata.get("page")))
    end = metadata.get("end_page", start)
    if isinstance(start, int) and isinstance(end, int) and start > 0 and end >= start:
        return {"start_page": start, "end_page": end}
    return None


def _segment_index_revision(records: list[SegmentRecord]) -> str:
    payload = [
        {
            "segment_id": record.segment_id,
            "order": record.order,
            "time_range": record.time_range,
            "page_range": record.page_range,
            "summary": record.summary,
            "candidate_evidence": list(record.candidate_evidence),
        }
        for record in records
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]


def _catalog_id(
    library_id: str,
    asset_id: str,
    asset_kind: Literal["video", "document"],
    index_revision: str,
) -> str:
    digest = hashlib.sha256(
        f"{library_id}\0{asset_id}\0{asset_kind}\0{index_revision}".encode()
    ).hexdigest()[:20]
    return f"catalog-{digest}"


def _catalog_coverage_state(catalog: SegmentCatalog) -> tuple[set[str], set[str]]:
    """Project current-run inspect facts onto one catalog without retaining raw results."""
    context = get_tool_run_context()
    if context is None or context.tool_gate_context_factory is None:
        return set(), set()
    gate_context = context.tool_gate_context_factory()
    ledger = getattr(gate_context, "ledger", None)
    calls = getattr(gate_context, "completed_tool_calls", ())
    attempted: set[str] = set()
    for call in calls:
        if getattr(call, "name", "") not in {"video_inspect", "text_inspect"}:
            continue
        arguments = _json_mapping(getattr(call, "arguments", ""))
        if not _catalog_scope_matches(catalog, arguments):
            continue
        attempted.update(_catalog_segment_ids_for_arguments(catalog, arguments))
    verified: set[str] = set()
    for claim in getattr(ledger, "claims", ()):
        if str(getattr(claim, "status", "")) != "verified":
            continue
        locator = getattr(claim, "locator", None)
        if locator is None:
            continue
        if (
            getattr(locator, "library_id", "") != catalog.library_id
            or getattr(locator, "asset_id", "") != catalog.asset_id
        ):
            continue
        locator_ids = {
            str(value)
            for value in (
                getattr(locator, "evidence_id", ""),
                getattr(locator, "segment_id", ""),
            )
            if value
        }
        known_ids = {record.segment_id for record in catalog.records}
        matched_ids = locator_ids & known_ids
        if matched_ids:
            verified.update(matched_ids)
            continue
        verified.update(
            _catalog_segment_ids_for_time_range(catalog, getattr(locator, "time_range", None))
        )
    return verified, attempted


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


def _catalog_scope_matches(catalog: SegmentCatalog, arguments: dict[str, Any]) -> bool:
    return (
        str(arguments.get("library_id") or "") == catalog.library_id
        and str(arguments.get("asset_id") or "") == catalog.asset_id
    )


def _catalog_segment_ids_for_arguments(
    catalog: SegmentCatalog, arguments: dict[str, Any]
) -> set[str]:
    segment_id = str(arguments.get("evidence_id") or arguments.get("segment_id") or "")
    if segment_id:
        return {segment_id}
    return _catalog_segment_ids_for_time_range(catalog, arguments.get("time_range"))


def _catalog_segment_ids_for_time_range(catalog: SegmentCatalog, value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    start = value.get("start_seconds")
    end = value.get("end_seconds")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return set()
    return {
        record.segment_id
        for record in catalog.records
        if record.time_range is not None
        and float(record.time_range["start_seconds"]) < float(end)
        and float(start) < float(record.time_range["end_seconds"])
    }


def _bounded_positive_int(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("integer argument is invalid") from exc
    if not 1 <= parsed <= maximum:
        raise ValueError(f"integer argument must be between 1 and {maximum}")
    return parsed


def _bounded_nonnegative_int(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("integer argument is invalid") from exc
    if not 0 <= parsed <= maximum:
        raise ValueError(f"integer argument must be between 0 and {maximum}")
    return parsed


def _coerce_query_refine_max_queries(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 3
    return max(1, min(parsed, 3))


def _coerce_optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _coerce_optional_threshold(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0.0 <= parsed <= 1.0 else None


def _is_precompiled_library(library_id: str) -> bool:
    return str(library_id or "").strip() == PRECOMPILED_LIBRARY_ID


def _hidden_library_error(*, tool: str, library_id: str) -> str:
    return _fail(
        tool,
        ToolErrorCode.UNSUPPORTED,
        "precompiled knowledge is only available through agent memory",
        library_id=library_id,
    )


def _library_not_allowed_error(tool: str, library_id: str) -> str:
    return json.dumps(
        {
            "error": f"knowledge library is not allowed for this run: {library_id}",
            "error_code": "library_not_allowed",
            "tool": tool,
            "library_id": library_id,
        },
        ensure_ascii=False,
    )


def _discover_library_entry(item: Any) -> Any:
    library = getattr(item, "library", None)
    return library if library is not None else item


def _oracle_needs_asset_discovery() -> bool:
    context = get_tool_run_context()
    scope = context.oracle_scope if context is not None else None
    return isinstance(scope, dict) and scope.get("asset_ids") == []


def _discover_library_payload(item: Any) -> dict[str, Any]:
    library = _discover_library_entry(item)
    payload = library.model_dump(mode="json")
    score = getattr(item, "score", None)
    if score is not None:
        payload["score"] = score
    reason = getattr(item, "reason", None)
    if reason:
        payload["match_reason"] = str(reason)
    asset_matches = getattr(item, "asset_matches", None)
    if asset_matches:
        payload["asset_matches"] = [
            match.model_dump(mode="json") if hasattr(match, "model_dump") else dict(match)
            for match in asset_matches
        ]
    result_metadata = getattr(item, "metadata", None)
    if isinstance(result_metadata, dict) and result_metadata:
        payload["discovery_metadata"] = result_metadata
    description = payload.get("description")
    if isinstance(description, str) and len(description) > _DISCOVER_DESCRIPTION_MAX_CHARS:
        payload["description"] = description[: _DISCOVER_DESCRIPTION_MAX_CHARS - 1] + "…"
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return payload
    asset_ids = metadata.get("asset_ids")
    if not isinstance(asset_ids, list) or len(asset_ids) <= _DISCOVER_ASSET_ID_PREVIEW_LIMIT:
        return payload
    payload["metadata"] = {
        **metadata,
        "asset_ids": asset_ids[:_DISCOVER_ASSET_ID_PREVIEW_LIMIT],
        "asset_ids_total": len(asset_ids),
        "asset_ids_truncated": True,
    }
    return payload


IMAGE_UNDERSTAND_SYSTEM_PROMPT = """\
你是 ViKnow 2.0 的图片理解助手。
你的任务仅限：
1) 在给定 query 和检索上下文下，客观描述图片内容；
2) 提取可能帮助后续回答的背景补充信息。

边界要求：
- 不要直接回答用户问题；
- 不要编造图片中不存在的信息；
- 若检索上下文为空或无关，可忽略其内容并在结果中保持保守。
"""


QUERY_INTERPRETATION_SYSTEM_PROMPT = """\
只说明用户的问题是什么意思，不回答问题，不生成检索计划。
用简短的自然语言明确用户要求确定的未知信息，以及原句规定的这些信息之间的关系。
保留原句的限定。区分已知范围与待求事实，不能用一个常见问题代替原问题。
若存在会影响答案的歧义，指出歧义；不要无依据添加前提。最多三句话。
"""


QUERY_REFINE_SYSTEM_PROMPT = """\
你是 ViKnow 2.0 的知识库问答规划器。你的任务是把用户问题转换成可验证的回答计划，
只做规划，不回答问题，不编造证据。

你需要参考 1.0 query rewrite 的原则：保留用户原始语言，消解必要指代，提取格式约束，
生成完整自然语言检索 query，并在确有必要时分解检索目标。

2.0 新增要求：
- answer_slots 是供主 Agent 采纳或修改的调查建议，不是新增的用户要求。
  每个 slot 应对应一个可以被 inspect/evidence 工具验证的事实需求。
- 每个 slot 必须明确职责：`evidence` 是独立 inspect 事实，`collection` 是需要去重的答案
  项目集合。需要计算时，只为每个输入事实建立 slot；不要为计算结果建立 slot。
- 不要把上下文、范围、来源、标题、文档/视频名称、时间条件、动作条件本身拆成独立 slot；
  它们应进入 context、subquestion 或 retrieval query。
- 只有用户真正要求分别回答多个独立事实，或多个对象/维度可以分别被验证时，才拆成多个 slot。
- 对比、汇总、比率或其他派生计算问题，若最终结果依赖多组需要独立检索的输入事实，
  应为每组输入建立独立 evidence slot。所有输入核验后，由主 Agent 使用 `execute` 计算；
  Answer Plan 不描述或执行运算。
- `coverage_requirement` 只使用 local 或 full_asset。只需核验一个或多个局部事实时选 local；
  只有答案成立必须确认整份选定文档/视频没有遗漏时选 full_asset。
- full_asset 表示需要证明来源范围内没有遗漏；coverage_condition 应描述完整性与去重的核验目标，
  不预先强制逐段阅读。主 Agent 根据实际证据选择检索、目录或其他核验路径；检索命中条数不代表总数。
- 每个 retrieval query 必须绑定其服务的 slot_ids。
- retrieval_queries 应提供能定位各 slot 的有效查询；原始 query 可以保留为其中一条，
  但不要无条件加入，也不要用同义改写重复已有查询。
- Search 结果不是证据；coverage_condition 必须描述什么样的 verified claim 才能覆盖该 slot。
- 当一个 slot 需要收集并去重多个项目时，必须用 answer_unit 定义“一个项目”是什么；
  例如一张图表、一次事件、一项条款或一门课程。不要把 chunk/segment 当作 answer_unit。
- full_asset 计数必须建立一个带 answer_unit 的 collection slot；一个 chunk 可以包含多个
  answer items。全部项目核验并去重后，由主 Agent 使用 `execute` 对项目数量求和。
- required_modalities 是规划阶段推测的调查建议，不是核验或结束的硬性条件。
  应根据实际证据调整调查路径；已核验且足以支持答案的证据不需要为凑齐模态而重复检查。
- required_modalities 只填写预计有必要调查的模态：text、video、image、asr、ocr。
- required_modalities 必须按待验证的事实类型判断，而不是按检索结果容器判断：
  * 用户问说了什么、如何安抚/提醒/告知、对话、广播、语音指令、音频内容时，必需模态是 asr。
  * 用户问画面中发生了什么、动作、人数、姿态、物体状态、场景内容时，必需模态是 video 或 image。
  * 用户问屏幕/标签/标识/面板上的精确文字时，必需模态是 ocr。
  * 用户问文档/SOP/规范规定的步骤、参数、要求时，必需模态是 text。
  * 用户问文档中的视觉结构、图表、表格、图示、版面关系或其可见来源注记时，
    必需模态是 image 或 ocr；text 只能作为定位或补充模态，除非独立文本本身足以证明答案。
  * 多个模态只有在每一种都必须独立核验时才同时列入 required；互为替代或仅辅助定位时，
    不要同时列为 required，应把辅助模态放入 optional_modalities。
  * 视频检索结果可以为 asr/ocr 提供 asset_id 和 time_range；不要因为候选来自视频就自动要求 video。
- 输出必须简洁，不要超过 max_queries 个 retrieval_queries。
"""


IMAGE_QUERY_RELEVANCE_SYSTEM_PROMPT = """\
你是 ViKnow 2.0 的上传图片相关性判断器。
你的任务只判断用户上传图片是否与用户问题相关，不回答用户问题，不参考知识库。

判断原则：
- relevant=true 表示图片内容有助于理解或回答 query。
- relevant=false 表示图片内容与 query 指向的对象、场景、文字、设备、步骤或事实需求不匹配。
- 只根据图片可见内容和 query 判断；不要臆测图片外的信息。
- image_description 必须是客观描述。
"""


IMAGE_SEARCH_RESULT_RELEVANCE_SYSTEM_PROMPT = """\
你是 ViKnow 2.0 的上传图片与库内图片检索结果匹配判断器。
你的任务只判断 image_search 返回的候选结果是否与上传图片相关，不回答用户问题。

判断原则：
- relevant=true 表示检索结果与上传图片描述的是相同或高度相关的对象、场景、界面、设备、文字或操作。
- For device-anchored part identification, relevant=true does not require the
  same exact frame, crop, camera angle, or visible component. If the query names
  a device, asset, video, product, machine, or knowledge scope and image_search
  returns candidates from that same scope, treat them as relevant when they can
  help continue retrieval or identify the uploaded part. Put visual uncertainty,
  likely component cues, and useful search terms in background_for_query_refine.
- relevant=false 表示检索结果与上传图片不匹配，或检索上下文不足以建立图片与知识库的关系。
- relevant_evidence_ids 只能填写检索上下文中真实存在的 evidence id。
- background_for_query_refine 只写能帮助后续文本检索的问题背景，不写最终答案。
"""


def _build_query_refine_prompt(
    *,
    query: str,
    max_queries: int,
    conversation_context: str,
    library_context: str,
    precompiled_knowledge_context: str,
    validation_feedback: list[str] | None = None,
    previous_plan: QueryRefinePlan | None = None,
) -> str:
    sections = [
        f"# 用户问题\n{query}",
        f"# max_queries\n{max_queries}",
    ]
    if conversation_context:
        sections.append(f"# 对话上下文\n{conversation_context}")
    if library_context:
        sections.append(f"# 知识库摘要\n{library_context}")
    if precompiled_knowledge_context:
        if "ViKnow 专家大模型" in precompiled_knowledge_context:
            sections.append(precompiled_knowledge_context)
        else:
            sections.append(f"# ViKnow 专家大模型\n{precompiled_knowledge_context}")
    if previous_plan is not None:
        sections.append("# 前一份计划\n" + previous_plan.model_dump_json(exclude_none=False))
    if validation_feedback:
        sections.append(
            "# Harness 校验反馈\n前一份计划的图结构无效。请重新输出完整的 "
            "QueryRefinePlan，但保留与原问题一致且已合法的 slots、roles 和 "
            "retrieval_queries，只修复以下结构问题；不要删除无关的合法节点或边：\n"
            + "\n".join(f"- {issue}" for issue in validation_feedback)
        )
    sections.append(
        """\
# 输出要求
请输出 QueryRefinePlan。注意：
1. rewritten_query 和 retrieval_queries 必须保留回答所需的全部限定条件。
2. context 只放共享限定范围；如果没有明确共享范围，填 null。
3. answer_slots 只放真正需要回答的子问题，不放上下文片段。
4. coverage_condition 用一句可执行的英文条件描述 verified claim 怎样才算覆盖。
5. risk_level 根据事实错误风险选择 low / medium / high。
6. coverage_requirement 只描述证据覆盖范围：local 或 full_asset。若答案成立需要证明整份
   选定来源没有遗漏则选 full_asset，不能以若干 search/select 命中代替完整来源覆盖。
   需要计算时把每个待检索输入拆成独立 evidence slot；不要创建结果 slot 或 operation。
   输入全部核验后由主 Agent 使用 `execute` 计算。
8. 若问题语言与候选资料/视频描述语言可能不同，retrieval_queries 必须包含可检索的跨语言别名；
   这类别名只用于定位，不能作为答案事实。
9. 需要计数时不得统计 chunk 或检索命中；单个收集 slot 必须填写 role=collection 和
   answer_unit，并允许后续记录由 inspect claim 支持的多个 verified_items。项目核验和去重
   完成后由主 Agent 使用 `execute` 求和。
"""
    )
    return "\n\n".join(sections)


def _query_refine_plan_graph_issues(plan: QueryRefinePlan) -> list[str]:
    """Return structural issues that must be repaired before a plan can execute."""

    slot_ids = [str(slot.id).strip() for slot in plan.answer_slots]
    known_slot_ids = {slot_id for slot_id in slot_ids if slot_id}
    issues: list[str] = []
    if len(known_slot_ids) != len(slot_ids):
        issues.append("answer slot ids must be non-empty and unique")
    for slot in plan.answer_slots:
        if slot.role == "collection" and not _normalize_optional_text(slot.answer_unit):
            issues.append(f"collection slot {slot.id} requires answer_unit")
    return issues


def _format_precompiled_knowledge_context(
    payload: dict[str, Any],
    *,
    max_items: int,
) -> str:
    entities = payload.get("entities") if isinstance(payload, dict) else []
    relations = payload.get("relations") if isinstance(payload, dict) else []
    if not isinstance(entities, list):
        entities = []
    if not isinstance(relations, list):
        relations = []
    if not entities and not relations:
        return ""

    lines: list[str] = [
        "## ViKnow 专家大模型",
        "",
        "使用说明：",
        "- 可用于理解用户问题中的术语、实体和关系，并辅助规划检索。",
        "- 可作为 ViKnow 专家大模型参与概念、实体关系、专业背景类回答。",
        "- 使用本段内容回答时，应说明“根据 ViKnow 专家大模型”。",
        "- 具体文档、视频、SOP、参数出处仍需通过检索和复核工具验证。",
        "- 与当前复核证据冲突时，以当前复核证据为准。",
        "",
    ]
    if entities:
        lines.append("实体：")
        for item in entities[:max_items]:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            description = str(item.get("description") or "").strip()
            if description:
                lines.append(f"• - {name}: {description}")
            else:
                lines.append(f"• - {name}")
    if relations:
        lines.append("关系：")
        for item in relations[:max_items]:
            description = str(item.get("description") or "").strip()
            if not description:
                continue
            lines.append(f"• - {description}")
    return "\n".join(lines)


def _query_refine_payload_from_plan(
    *,
    query: str,
    plan: QueryRefinePlan,
    max_queries: int,
    planner: Literal["llm", "fallback"],
    warnings: list[str],
) -> str:
    answer_slots = _normalize_plan_answer_slots(plan.answer_slots, query=query)
    retrieval_queries = _normalize_plan_retrieval_queries(
        plan.retrieval_queries,
        answer_slots=answer_slots,
        rewritten_query=plan.rewritten_query,
        query=query,
        max_queries=max_queries,
    )
    rewritten_query = _normalize_refined_query(
        plan.rewritten_query or retrieval_queries[0]["query"] or query
    )
    return _ok(
        "query_refine",
        planner=planner,
        original_query=query,
        context=_normalize_optional_text(plan.context),
        rewritten_query=rewritten_query,
        query_intent=plan.query_intent,
        format_instruction=_normalize_optional_text(plan.format_instruction),
        risk_level=plan.risk_level,
        coverage_requirement=plan.coverage_requirement,
        answer_slots=answer_slots,
        retrieval_queries=retrieval_queries,
        decomposition_reason=plan.decomposition_reason
        or _fallback_decomposition_reason(answer_slots),
        reasoning=plan.reasoning,
        notes=[
            "answer_slots and coverage are suggestions; the main agent explicitly adopts them",
            "retrieval_queries are search inputs only and are not citable evidence",
        ],
        warnings=warnings,
    )


def _normalize_plan_answer_slots(
    slots: list[QueryRefineAnswerSlot],
    *,
    query: str,
) -> list[dict[str, Any]]:
    if not slots:
        slots = [
            QueryRefineAnswerSlot(
                id="s1",
                role="evidence",
                answer_unit=None,
                subquestion=_ensure_question_mark(query),
                coverage_condition=(
                    "At least one verified claim directly answers the user question."
                ),
            )
        ]
    normalized_slots = []
    for index, slot in enumerate(slots, start=1):
        slot_id = _normalize_refined_query(slot.id) or f"s{index}"
        subquestion = _ensure_question_mark(slot.subquestion or query)
        required_modalities = list(dict.fromkeys(slot.required_modalities or ["text"]))
        optional_modalities = [
            modality
            for modality in dict.fromkeys(slot.optional_modalities)
            if modality not in required_modalities
        ]
        item = {
            "id": slot_id,
            "role": slot.role,
            "subquestion": subquestion,
            "required_modalities": required_modalities,
            "optional_modalities": optional_modalities,
            "coverage_condition": slot.coverage_condition
            or f"At least one verified claim directly answers: {subquestion}",
            "status": "pending",
        }
        answer_unit = _normalize_optional_text(slot.answer_unit)
        if answer_unit:
            item["answer_unit"] = answer_unit
        context = _normalize_optional_text(slot.context)
        if context:
            item["context"] = context
        normalized_slots.append(item)
    return normalized_slots


def _normalize_plan_retrieval_queries(
    retrieval_queries: list[QueryRefineRetrievalQuery],
    *,
    answer_slots: list[dict[str, Any]],
    rewritten_query: str | None,
    query: str,
    max_queries: int,
) -> list[dict[str, Any]]:
    known_slot_ids = {str(slot["id"]) for slot in answer_slots}
    normalized_queries = []
    seen_query_keys: set[str] = set()
    for item in retrieval_queries:
        candidate = _normalize_refined_query(item.query)
        candidate_key = _query_execution_key(candidate)
        if not candidate or candidate_key in seen_query_keys:
            continue
        seen_query_keys.add(candidate_key)
        slot_ids = [slot_id for slot_id in item.slot_ids if slot_id in known_slot_ids]
        if not slot_ids:
            slot_ids = sorted(known_slot_ids)
        normalized_queries.append(
            {
                "query": candidate,
                "purpose": item.purpose or "retrieve candidate evidence for answer slots",
                "priority": len(normalized_queries) + 1,
                "slot_ids": slot_ids,
            }
        )
        if len(normalized_queries) >= max_queries:
            break
    if normalized_queries:
        return normalized_queries

    fallback_candidates = [
        (rewritten_query, sorted(known_slot_ids), "retrieve candidate evidence for answer slots")
    ]
    for slot in answer_slots:
        fallback_candidates.append(
            (
                str(slot.get("subquestion") or ""),
                [str(slot["id"])],
                f"retrieve candidate evidence for answer slot {slot['id']}",
            )
        )
    fallback_candidates.append(
        (query, sorted(known_slot_ids), "retrieve candidate evidence for answer slots")
    )
    for raw_candidate, slot_ids, purpose in fallback_candidates:
        candidate = _normalize_refined_query(str(raw_candidate or ""))
        candidate_key = _query_execution_key(candidate)
        if not candidate or candidate_key in seen_query_keys:
            continue
        seen_query_keys.add(candidate_key)
        normalized_queries.append(
            {
                "query": candidate,
                "purpose": purpose,
                "priority": len(normalized_queries) + 1,
                "slot_ids": slot_ids,
            }
        )
        if len(normalized_queries) >= max_queries:
            break
    return normalized_queries


def _fallback_query_refine_payload(
    query: str,
    max_queries: int,
    *,
    warnings: list[str],
) -> str:
    plan = QueryRefinePlan(
        reasoning="No query_refine LLM is available; using a conservative single-slot plan.",
        coverage_requirement="local",
        context=None,
        rewritten_query=_normalize_refined_query(query),
        query_intent="normal_retrieval",
        format_instruction=None,
        risk_level="medium",
        answer_slots=[
            QueryRefineAnswerSlot(
                id="s1",
                role="evidence",
                answer_unit=None,
                subquestion=_ensure_question_mark(query),
                required_modalities=["text"],
                optional_modalities=[],
                coverage_condition=(
                    "At least one verified claim directly answers the full user question."
                ),
            )
        ],
        retrieval_queries=[
            QueryRefineRetrievalQuery(
                query=_normalize_refined_query(query),
                purpose="retrieve candidate evidence for the full user question",
                priority=1,
                slot_ids=["s1"],
            )
        ],
        decomposition_reason="Fallback mode does not decompose the query.",
    )
    return _query_refine_payload_from_plan(
        query=query,
        plan=plan,
        max_queries=max_queries,
        planner="fallback",
        warnings=warnings,
    )


def _normalize_refined_query(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip(" ，,。？?")


def _query_execution_key(query: str) -> str:
    return _normalize_refined_query(query).casefold()


def _image_file_path_to_base64_data_url(path: str, *, repo_root: Path) -> str:
    image_path = Path(str(path or "").strip())
    if not image_path.is_absolute():
        image_path = (repo_root / image_path).resolve()
    if not image_path.exists() or not image_path.is_file():
        raise FileNotFoundError(f"image file not found: {image_path}")

    mime_type, _ = mimetypes.guess_type(str(image_path))
    if mime_type is None or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"
    payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def _build_image_understand_retrieval_context(search_result: list[Any]) -> str:
    if not search_result:
        return "无可用检索上下文。"
    serialized = json.dumps(search_result, ensure_ascii=False, default=str)
    max_chars = 100000
    if len(serialized) <= max_chars:
        return serialized
    return f"{serialized[:max_chars]}\n...(truncated)"


def _uploaded_image_name_from_path(image_path: str) -> str:
    basename = Path(str(image_path or "")).name
    prefix, separator, suffix = basename.partition("_")
    if separator and len(prefix) == 32 and all(char in "0123456789abcdefABCDEF" for char in prefix):
        return suffix
    return basename


def _build_image_query_relevance_prompt(*, query: str, image_name: str) -> str:
    return "\n".join(
        [
            "请判断上传图片是否与用户 query 相关。",
            "",
            f"query: {query}",
            f"upload_image_name: {image_name}",
            "",
            "只输出 ImageQueryRelevanceOutput。",
        ]
    )


def _build_image_search_result_relevance_prompt(
    *,
    query: str,
    image_name: str,
    retrieval_context: str,
) -> str:

    result = "\n".join(
        [
            "请判断 image_search 检索结果是否与上传图片相关。",
            "",
            f"query: {query}",
            f"upload_image_name: {image_name}",
            "image_search_result:",
            retrieval_context,
            "",
            "只输出 ImageSearchResultRelevanceOutput。",
        ]
    )
    logger.info(f"prompt结果： {result}")
    return result


def _build_image_understand_prompt(*, query: str, retrieval_context: str, image_path: str) -> str:
    return "\n".join(
        [
            "请基于给定 query 与检索上下文理解这张图片。",
            "",
            f"query: {query}",
            f"image_path: {image_path}",
            "retrieval_context:",
            retrieval_context,
            "",
            "请严格输出两个字段：",
            "1) image_description: 对图片内容的客观描述，优先关注与 query 相关的可见信息。",
            "2) query_supplement: 结合图片与上下文得到的问题背景补充；若无补充则输出空字符串。",
            "注意：不要回答用户问题本身。",
        ]
    )


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_refined_query(value)
    return normalized or None


def _ensure_question_mark(text: str) -> str:
    normalized = _normalize_refined_query(text)
    if normalized.endswith(("?", "？")):
        return normalized
    return f"{normalized}？"


def _fallback_decomposition_reason(answer_slots: list[dict[str, Any]]) -> str:
    if len(answer_slots) == 1:
        return "Question has one answer slot; no independent retrieval decomposition is needed."
    return (
        "Question contains multiple independently coverable answer slots; "
        "each slot receives its own retrieval query."
    )


# ── v1 bridge ──

BRIDGE_SEARCH_SCHEMA = {
    "description": (
        "Search the ViKnow v1 multimodal knowledge base. "
        "Returns content_chunks with text, image, and video evidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query.",
            },
            "knowledge_base": {
                "type": "string",
                "description": (
                    "Knowledge base id. Ask the user if not specified in their question."
                ),
            },
        },
        "required": ["query", "knowledge_base"],
        "additionalProperties": False,
    },
}

BRIDGE_REVIEW_SCHEMA = {
    "description": (
        "Deep video visual analysis via VLM. "
        "Select video chunk objects from bridge_search results. "
        "Use ONLY for video visual details (posture, object contact, "
        "state changes, what actually happened in the video). "
        "Do NOT use for text or image questions — search alone is sufficient."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Focused question about video content.",
            },
            "chunks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string"},
                        "time_range": {"type": "array"},
                        "video_uri": {"type": "string"},
                        "clip_path": {"type": "string"},
                        "content": {"type": "string"},
                        "context": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": ["id", "type"],
                },
                "description": (
                    "Complete video chunk objects from bridge_search result. "
                    "Pass exactly as returned; do not drop or select fields. "
                    "clip_path/video_uri and time_range are required for video lookup."
                ),
            },
            "knowledge_base": {
                "type": "string",
                "description": (
                    "Knowledge base id. Ask the user if not specified in their question."
                ),
            },
        },
        "required": ["question", "chunks", "knowledge_base"],
        "additionalProperties": False,
    },
}


def _call_viknow1(method: str, payload: dict) -> str:
    import urllib.request

    base = os.environ.get("VIKNOW1_API", "http://host.docker.internal:5603")
    url = f"{base}/{method}"
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("VIKNOW1_TOKEN", "")
    if token:
        headers["access-token"] = token
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode()


def bridge_search(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return _fail("bridge_search", ToolErrorCode.INVALID_ARGUMENTS, "query is required")
    kb = str(args.get("knowledge_base", "")).strip()
    if not kb:
        return _fail("bridge_search", ToolErrorCode.INVALID_ARGUMENTS, "knowledge_base is required")
    try:
        raw = _call_viknow1("retrieval", {"query": query, "knowledge_base": kb})
        data = json.loads(raw)
        return _ok(
            "bridge_search",
            query=query,
            summary=data.get("summary", {}),
            runtime_id=data.get("runtime_id", ""),
            content_chunks=data.get("content_chunks", []),
        )
    except Exception as exc:
        return _fail(
            "bridge_search",
            ToolErrorCode.UPSTREAM_ERROR,
            str(exc),
            detail=str(exc),
            retryable=True,
        )


def bridge_review(args: dict[str, Any]) -> str:
    question = str(args.get("question", "")).strip()
    chunks = args.get("chunks") or []
    kb = str(args.get("knowledge_base", "")).strip()
    if not question:
        return _fail("bridge_review", ToolErrorCode.INVALID_ARGUMENTS, "question is required")
    if not chunks:
        return _fail("bridge_review", ToolErrorCode.INVALID_ARGUMENTS, "chunks is required")
    if not kb:
        return _fail("bridge_review", ToolErrorCode.INVALID_ARGUMENTS, "knowledge_base is required")
    try:
        raw = _call_viknow1(
            "evidence_extraction",
            {
                "query": question,
                "chunks": chunks,
                "knowledge_base": kb,
            },
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return _fail(
                "bridge_review",
                ToolErrorCode.UPSTREAM_ERROR,
                "bridge_review returned non-JSON",
                detail=raw[:2000],
            )
        if isinstance(data, dict):
            data.pop("tool", None)
            return _ok("bridge_review", **data)
        return _ok("bridge_review", result=data)
    except Exception as exc:
        return _fail(
            "bridge_review",
            ToolErrorCode.UPSTREAM_ERROR,
            str(exc),
            detail=str(exc),
            retryable=True,
        )


def _dict_or_attr(obj: Any, key: str) -> str:
    if isinstance(obj, dict):
        return str(obj.get(key) or "")
    return str(getattr(obj, key, "") or "")


def _query_requires_exhaustive_event_coverage(query: str) -> bool:
    """Identify questions whose answer requires every distinct event to be adjudicated."""

    normalized = query.casefold()
    return bool(
        re.search(r"\bhow many\b|\bnumber of\b|\bcount\b", normalized)
        or any(token in normalized for token in ("多少次", "几次", "一共", "总共", "全部"))
    )
