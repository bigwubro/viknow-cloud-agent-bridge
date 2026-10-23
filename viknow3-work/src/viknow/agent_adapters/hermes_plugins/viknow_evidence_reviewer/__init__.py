"""ViKnow video inspect plugin for Hermes Agent."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from viknow.agent_adapters.hermes_plugins.viknow_knowledge_search import get_knowledge_adapter
from viknow.core.config import AgentModelConfig
from viknow.core.evidence import Evidence
from viknow.core.scheduler.domain import Priority, extra_body_with_priority
from viknow.core.tools import VIDEO_INSPECT_SCHEMA, ViKnowToolHandlers

if TYPE_CHECKING:
    from viknow.core.multimodal.video_review_extractor import VideoEvidenceExtractor

_extractor: VideoEvidenceExtractor | None = None


def _get_extractor() -> VideoEvidenceExtractor:
    global _extractor
    if _extractor is None:
        from langchain_openai import ChatOpenAI

        from viknow.core.multimodal.video_review_extractor import VideoEvidenceExtractor

        repo_root = Path.cwd().resolve()
        model_config = AgentModelConfig(
            model_env="VIKNOW_PRIVATE_MODEL",
            base_url_env="VIKNOW_PRIVATE_BASE_URL",
            api_key_env="VIKNOW_PRIVATE_API_KEY",
        )
        _extractor = VideoEvidenceExtractor(
            chat_model=ChatOpenAI(
                model=model_config.require_model(),
                base_url=model_config.resolve_base_url(),
                api_key=model_config.require_api_key(),
                temperature=0,
                extra_body=extra_body_with_priority(None, Priority.INTERACTIVE),
            ),
            repo_root=repo_root,
        )
    return _extractor


def video_inspect(args: dict[str, Any], **_: Any) -> str:
    """Inspect an evidence object's source video with a focused question."""

    return ViKnowToolHandlers(
        knowledge_adapter=get_knowledge_adapter(),
        video_review_extractor_factory=_get_extractor,
    ).video_inspect(args)


def parse_evidence_arg(value: Any) -> Evidence:
    return ViKnowToolHandlers(knowledge_adapter=get_knowledge_adapter()).parse_evidence_arg(value)


def register(ctx) -> None:
    ctx.register_skill(
        name="knowledge-qa",
        path=Path(__file__).resolve().parent / "SKILL.md",
        description="Video-grounded ViKnow question answering workflow.",
    )
    ctx.register_tool(
        name="video_inspect",
        toolset="viknow_multimodal",
        schema=VIDEO_INSPECT_SCHEMA,
        handler=video_inspect,
        description=(
            "Verify what actually happens in a video clip. Use before citing video content."
        ),
    )
