"""Stable knowledge graph contracts for ViKnow.

This module defines ViKnow-facing data contracts only. Index construction,
storage, embedding, ranking, and graph algorithms belong to adapters, plugins,
or application services.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeNodeType(str, Enum):
    """Supported knowledge graph node categories."""

    ACTION = "action"
    OBJECT = "object"
    STATE = "state"
    EVIDENCE = "evidence"
    MEDIA_EVIDENCE = "media_evidence"
    DOCUMENT_EVIDENCE = "document_evidence"


class KnowledgeEdgeType(str, Enum):
    """Supported knowledge graph relationship categories."""

    SUPPORTS = "supports"
    IMPLEMENTS = "implements"
    USES = "uses"
    OBSERVES = "observes"
    HAS_STATE = "has_state"
    STATE_TRANSITION = "state_transition"
    NEXT = "next"


class KnowledgeEvidenceRef(BaseModel):
    """Reference to source evidence backing a node or relationship."""

    evidence_id: str
    kind: str
    uri: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    start_line: int | None = None
    end_line: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeNode(BaseModel):
    """One node in a ViKnow knowledge graph."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: KnowledgeNodeType | str
    label: str
    evidence_text: str | None = None
    evidence_refs: list[KnowledgeEvidenceRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeEdge(BaseModel):
    """One directed relationship in a ViKnow knowledge graph."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: KnowledgeEdgeType | str
    source: str
    target: str
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[KnowledgeEvidenceRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeGraph(BaseModel):
    """Portable graph payload consumed by APIs, tools, and visualizations."""

    nodes: list[KnowledgeNode] = Field(default_factory=list)
    edges: list[KnowledgeEdge] = Field(default_factory=list)
