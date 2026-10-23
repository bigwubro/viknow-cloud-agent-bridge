"""Knowledge domain contracts and demo adapters."""

from viknow.core.knowledge.domain import (
    KnowledgeEdge,
    KnowledgeEdgeType,
    KnowledgeEvidenceRef,
    KnowledgeGraph,
    KnowledgeNode,
    KnowledgeNodeType,
)
from viknow.core.knowledge.provider import (
    KnowledgeAssetDiscovery,
    KnowledgeLibrary,
    KnowledgeLibrarySearchResult,
    KnowledgeProvider,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
)

__all__ = [
    "KnowledgeEdge",
    "KnowledgeEdgeType",
    "KnowledgeEvidenceRef",
    "KnowledgeGraph",
    "KnowledgeAssetDiscovery",
    "KnowledgeLibrary",
    "KnowledgeLibrarySearchResult",
    "KnowledgeNode",
    "KnowledgeNodeType",
    "KnowledgeProvider",
    "KnowledgeSearchRequest",
    "KnowledgeSearchResult",
]
