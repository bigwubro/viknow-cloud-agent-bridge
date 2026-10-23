"""CLI for extracting and building ViKnow video KB indexes."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from viknow.core.knowledge.video_indexing.describer import OpenAICompatibleSegmentDescriber
from viknow.core.knowledge.video_indexing.domain import (
    RealModelEnvironment,
    SegmentDescription,
    SopAlignment,
)
from viknow.core.knowledge.video_indexing.index_support import (
    OpenAICompatibleEmbeddingClient,
    read_json,
    write_json,
)
from viknow.core.knowledge.video_indexing.media import extract_clip
from viknow.core.knowledge.video_indexing.normalizer import SpecNormalizer
from viknow.core.knowledge.video_indexing.publisher import VideoIndexPublisher
from viknow.core.knowledge.video_indexing.segmenter import VideoSemanticSegmenter
from viknow.core.knowledge.video_indexing.sop import SopContext, SopContextExtractor, SopStepAligner
from viknow.core.knowledge.video_indexing.validator import SpecValidator


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract or build a ViKnow video KB index.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="Extract a draft spec from video and SOP.")
    extract.add_argument("--video", type=Path, required=True)
    extract.add_argument("--sop", type=Path)
    extract.add_argument("--index-id", required=True)
    extract.add_argument("--output-dir", type=Path, required=True)
    extract.add_argument("--window-seconds", type=float, default=3.0)
    extract.add_argument("--min-segment-seconds", type=float, default=3.0)
    extract.add_argument("--max-segment-seconds", type=float, default=18.0)
    extract.add_argument("--similarity-threshold", type=float, default=0.82)
    extract.add_argument("--max-segments", type=int)
    extract.add_argument("--no-publish", action="store_true")

    build = subparsers.add_parser("build-from-spec", help="Build an index from a spec file.")
    build.add_argument("--spec", type=Path, required=True)
    build.add_argument("--staging-dir", type=Path, required=True)
    build.add_argument("--publish-dir", type=Path)
    build.add_argument("--no-publish", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    repo_root = Path.cwd()
    RealModelEnvironment.from_env().require_ready()
    if args.command == "build-from-spec":
        build_from_spec(args=args, repo_root=repo_root)
        return
    if args.command == "extract":
        extract_index(args=args, repo_root=repo_root)
        return
    raise RuntimeError(f"unknown command: {args.command}")


def build_from_spec(*, args: argparse.Namespace, repo_root: Path) -> None:
    spec_path = resolve_path(repo_root, args.spec)
    staging_dir = resolve_path(repo_root, args.staging_dir)
    publish_dir = (
        None if args.no_publish else publish_dir_for_spec(repo_root, spec_path, args.publish_dir)
    )
    result = VideoIndexPublisher(repo_root=repo_root).build_from_spec(
        spec_path=spec_path,
        staging_dir=staging_dir,
        publish_dir=publish_dir,
    )
    if result.report.has_errors:
        print(result.report.to_markdown())
        raise SystemExit(1)
    print(f"wrote staging index to {result.staging_dir}")
    if result.published_dir is not None:
        print(f"published index to {result.published_dir}")


def extract_index(*, args: argparse.Namespace, repo_root: Path) -> None:
    output_dir = resolve_path(repo_root, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = resolve_path(repo_root, args.video)
    sop_path = resolve_path(repo_root, args.sop) if args.sop else None

    embedding_client = OpenAICompatibleEmbeddingClient(timeout_seconds=120)
    segmenter = VideoSemanticSegmenter(
        embedding_client=embedding_client,
        window_seconds=args.window_seconds,
        min_segment_seconds=args.min_segment_seconds,
        max_segment_seconds=args.max_segment_seconds,
        similarity_threshold=args.similarity_threshold,
    )
    windows = segmenter.sample_windows(video_path, output_dir / "window_frames")
    embedded = segmenter.embed_windows(windows)
    slices = segmenter.aggregate(embedded)
    if args.max_segments is not None:
        slices = slices[: args.max_segments]

    sop_context = (
        SopContextExtractor().extract(sop_path, document_uri=path_for_spec(repo_root, sop_path))
        if sop_path
        else SopContext()
    )
    descriptions = describe_slices(
        video_path=video_path,
        output_dir=output_dir,
        slices=slices,
        sop_context=sop_context,
    )
    alignments = align_slices(
        descriptions=descriptions,
        sop_context=sop_context,
        embedding_client=embedding_client,
    )
    spec = SpecNormalizer(
        index_id=args.index_id,
        source_video=path_for_spec(repo_root, video_path),
        source_index="",
    ).normalize(
        slices=slices,
        descriptions=descriptions,
        sop_steps=sop_context.steps,
        alignments=alignments,
    )

    draft_spec_path = output_dir / "draft_spec.json"
    write_json(draft_spec_path, spec)
    report = SpecValidator().validate_spec(spec)
    write_json(output_dir / "validation_report.json", report.model_dump(mode="json"))
    (output_dir / "validation_report.md").write_text(report.to_markdown(), encoding="utf-8")
    print(f"wrote draft spec to {draft_spec_path}")
    print(f"wrote validation report to {output_dir / 'validation_report.md'}")
    if report.has_errors:
        raise SystemExit(1)

    staging_dir = output_dir / "staging_index"
    publish_dir = None if args.no_publish else repo_root / "data" / "kb_index" / args.index_id
    result = VideoIndexPublisher(repo_root=repo_root).build_from_spec(
        spec_path=draft_spec_path,
        staging_dir=staging_dir,
        publish_dir=publish_dir,
    )
    if result.report.has_errors:
        print(result.report.to_markdown())
        raise SystemExit(1)
    print(f"wrote staging index to {result.staging_dir}")
    if result.published_dir is not None:
        print(f"published index to {result.published_dir}")


def describe_slices(
    *,
    video_path: Path,
    output_dir: Path,
    slices: list[Any],
    sop_context: SopContext,
) -> list[SegmentDescription]:
    describer = OpenAICompatibleSegmentDescriber.from_env()
    descriptions = []
    context_text = sop_prompt_context(sop_context)
    clip_dir = output_dir / "clips"
    for slice_item in slices:
        clip_path = clip_dir / f"{slice_item.id}.mp4"
        extract_clip(video_path, clip_path, slice_item.start_seconds, slice_item.end_seconds)
        descriptions.append(
            describer.describe(
                slice_id=slice_item.id,
                clip_path=clip_path,
                start_seconds=slice_item.start_seconds,
                end_seconds=slice_item.end_seconds,
                sop_context=context_text,
            )
        )
    return descriptions


def align_slices(
    *,
    descriptions: list[SegmentDescription],
    sop_context: SopContext,
    embedding_client: OpenAICompatibleEmbeddingClient,
) -> list[SopAlignment]:
    if not sop_context.steps:
        return []
    aligner = SopStepAligner(embedding_client=embedding_client)
    return [aligner.align(description, sop_context.steps) for description in descriptions]


def sop_prompt_context(context: SopContext) -> str:
    if not context.steps:
        return ""
    terms = "、".join(context.terms)
    return f"SOP摘要：{context.summary}\n关键术语：{terms}"


def publish_dir_for_spec(repo_root: Path, spec_path: Path, publish_dir: Path | None) -> Path:
    if publish_dir is not None:
        return resolve_path(repo_root, publish_dir)
    spec = read_json(spec_path)
    return repo_root / "data" / "kb_index" / str(spec["index_id"])


def resolve_path(repo_root: Path, value: Path | None) -> Path:
    if value is None:
        raise ValueError("path is required")
    path = value.expanduser()
    if path.is_absolute():
        return path
    return repo_root / path


def path_for_spec(repo_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)
