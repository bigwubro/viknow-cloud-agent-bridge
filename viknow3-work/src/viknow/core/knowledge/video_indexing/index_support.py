"""Shared support for building and validating published video indexes."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from viknow.core.knowledge import KnowledgeEdge, KnowledgeGraph, KnowledgeNode

DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-VL-Embedding-8B"
DEFAULT_EMBEDDING_DIMENSIONS = 768
DEFAULT_EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_index_spec(*, repo_root: Path, spec_path: Path | None) -> dict[str, Any]:
    path = spec_path or repo_root / "data" / "elevator_maintenance_kb_spec.json"
    return read_json(path)


class OpenAICompatibleEmbeddingClient:
    """Small ViKnow OpenAI-compatible embedding client."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        timeout_seconds: float = 60,
    ) -> None:
        self.api_key = api_key or os.getenv("VIKNOW_EMBEDDING_API_KEY")
        self.base_url = (
            base_url or os.getenv("VIKNOW_EMBEDDING_BASE_URL") or DEFAULT_EMBEDDING_BASE_URL
        ).rstrip("/")
        self.model = model or os.getenv("VIKNOW_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
        self.dimensions = dimensions or int(
            os.getenv("VIKNOW_EMBEDDING_DIMENSIONS", str(DEFAULT_EMBEDDING_DIMENSIONS))
        )
        self.timeout_seconds = timeout_seconds

    def embed(self, input_content: Any) -> list[float]:
        if not self.api_key:
            raise RuntimeError("VIKNOW_EMBEDDING_API_KEY is required for embeddings")
        payload: dict[str, Any] = {
            "model": self.model,
            "input": input_content,
            "encoding_format": "float",
            "dimensions": self.dimensions,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"embedding request failed: {exc.code}. {detail}") from exc
        data = response_payload.get("data")
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"embedding response missing data: {response_payload}")
        embedding = data[0].get("embedding")
        if not isinstance(embedding, list):
            raise RuntimeError(f"embedding response missing embedding: {response_payload}")
        return [float(value) for value in embedding]


@dataclass
class VideoKnowledgeIndexBuilder:
    repo_root: Path
    index_dir: Path
    spec_path: Path | None = None
    embedding_client: Any | None = None

    def build(
        self, *, force: bool = False, no_embed: bool = False, dimensions: int | None = None
    ) -> None:
        spec = load_index_spec(repo_root=self.repo_root, spec_path=self.spec_path)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        frames_dir = self.index_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        segments = []
        vectors = []
        graph = build_graph(spec)
        client = self.embedding_client
        if client is None and not no_embed:
            client = OpenAICompatibleEmbeddingClient(dimensions=dimensions)

        for segment in spec["segments"]:
            source_video = source_video_for_segment(segment, spec)
            frame_path = frames_dir / f"{segment['id']}.jpg"
            if force or not frame_path.exists():
                extract_segment_frame(self.repo_root / source_video, frame_path, segment)
            try:
                frame_uri = frame_path.relative_to(self.repo_root)
            except ValueError:
                frame_uri = frame_path
            enriched = enrich_segment(segment, frame_uri, spec)
            segments.append(enriched)
            if no_embed:
                continue
            input_content = segment_embedding_input(
                enriched,
                self.repo_root / enriched["frame_uri"],
            )
            vector = client.embed(input_content)
            vectors.append(
                {
                    "segment_id": segment["id"],
                    "embedding": vector,
                    "model": getattr(
                        client,
                        "model",
                        os.getenv("VIKNOW_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
                    ),
                    "base_url": getattr(client, "base_url", None),
                    "dimensions": len(vector),
                    "input_hash": stable_hash(input_content),
                }
            )

        spec_file = self.spec_path or self.repo_root / "data" / "elevator_maintenance_kb_spec.json"
        try:
            spec_uri = str(spec_file.relative_to(self.repo_root))
        except ValueError:
            spec_uri = str(spec_file)
        manifest = {
            "index_id": spec["index_id"],
            "source_video": spec["source_video"],
            "source_index": spec["source_index"],
            "spec": spec_uri,
            "embedding_model": getattr(
                client,
                "model",
                os.getenv("VIKNOW_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            ),
            "embedding_base_url": getattr(client, "base_url", None),
            "embedding_dimensions": getattr(
                client,
                "dimensions",
                dimensions or DEFAULT_EMBEDDING_DIMENSIONS,
            ),
            "created_at": datetime.now(UTC).isoformat(),
            "has_vectors": bool(vectors),
        }
        write_json(self.index_dir / "manifest.json", manifest)
        write_json(self.index_dir / "segments.json", {"segments": segments})
        write_json(self.index_dir / "vectors.json", {"vectors": vectors})
        write_json(self.index_dir / "graph.json", graph)


def build_graph(spec: dict[str, Any]) -> dict[str, Any]:
    nodes = []
    edges = []
    sop_steps = {item["id"]: item for item in spec["sop_steps"]}
    item_states = {item["id"]: item for item in spec["item_states"]}
    objects = {item["id"]: item for item in spec["objects"]}

    for segment in spec["segments"]:
        action_id = action_node_id(segment)
        nodes.append(
            {
                "id": segment["id"],
                "type": "video_segment",
                "label": segment["title"],
                "evidence_text": segment["action"],
                "time_range": {
                    "start_seconds": segment["start_seconds"],
                    "end_seconds": segment["end_seconds"],
                },
            }
        )
        nodes.append(
            {
                "id": action_id,
                "type": "action",
                "label": segment["semantic"],
                "evidence_text": segment["action"],
                "segment_id": segment["id"],
            }
        )
        edges.append(
            {
                "id": f"{segment['id']}__supports__{action_id}",
                "source": segment["id"],
                "target": action_id,
                "type": "supports_action",
                "evidence_ids": [segment["id"]],
            }
        )
    for sop in spec["sop_steps"]:
        nodes.append(
            {
                "id": sop["id"],
                "type": "sop_step",
                "label": sop["id"],
                "evidence_text": sop["text"],
                "metadata": {
                    "document_uri": sop["document_uri"],
                    "start_line": sop["start_line"],
                    "end_line": sop["end_line"],
                },
            }
        )
    for object_item in spec["objects"]:
        nodes.append(
            {
                "id": object_item["id"],
                "type": "object",
                "label": object_item["label"],
                "evidence_text": f"{object_item['label']} 是视频知识库中的关键物体。",
            }
        )
    for state in spec["item_states"]:
        item_id = state["id"]
        object_id = state["object_id"]
        state_label = item_id.replace("Item_", "").replace(f"{objects[object_id]['label']}_", "")
        nodes.append(
            {
                "id": item_id,
                "type": "item_state",
                "label": state_label,
                "object_id": object_id,
                "evidence_text": state["text"],
            }
        )
        edges.append(
            {
                "id": f"{item_id}__state_of__{object_id}",
                "source": object_id,
                "target": item_id,
                "type": "has_state",
                "evidence_ids": [item_id],
            }
        )
    for segment in spec["segments"]:
        action_id = action_node_id(segment)
        for sop_id in segment["sop_ids"]:
            _ = sop_steps[sop_id]
            edges.append(
                {
                    "id": f"{action_id}__implements__{sop_id}",
                    "source": action_id,
                    "target": sop_id,
                    "type": "implements_sop",
                    "evidence_ids": [segment["id"], sop_id],
                }
            )
        for item_id in segment["item_state_ids"]:
            object_id = item_states[item_id]["object_id"]
            edges.append(
                {
                    "id": f"{action_id}__observes__{item_id}",
                    "source": action_id,
                    "target": item_id,
                    "type": "observes_state",
                    "evidence_ids": [segment["id"], item_id],
                }
            )
            edges.append(
                {
                    "id": f"{action_id}__uses__{object_id}",
                    "source": action_id,
                    "target": object_id,
                    "type": "uses_object",
                    "evidence_ids": [segment["id"], object_id],
                }
            )
    for previous, current in zip(spec["segments"], spec["segments"][1:], strict=False):
        if source_video_for_segment(previous, spec) != source_video_for_segment(current, spec):
            continue
        edges.append(
            {
                "id": f"{action_node_id(previous)}__next__{action_node_id(current)}",
                "source": action_node_id(previous),
                "target": action_node_id(current),
                "type": "next_action",
                "evidence_ids": [previous["id"], current["id"]],
            }
        )
    edges.extend(build_state_transition_edges(spec))
    graph = KnowledgeGraph(
        nodes=[KnowledgeNode.model_validate(node) for node in nodes],
        edges=[KnowledgeEdge.model_validate(edge) for edge in edges],
    )
    return graph.model_dump(mode="json")


def action_node_id(segment: dict[str, Any]) -> str:
    return f"Action_{segment['id']}"


def build_state_transition_edges(spec: dict[str, Any]) -> list[dict[str, Any]]:
    seen_by_object: dict[tuple[str, str], list[tuple[str, str]]] = {}
    item_states = {item["id"]: item for item in spec["item_states"]}
    for segment in spec["segments"]:
        for item_id in segment["item_state_ids"]:
            object_id = item_states[item_id]["object_id"]
            key = (source_video_for_segment(segment, spec), object_id)
            state_sequence = seen_by_object.setdefault(key, [])
            if not state_sequence or state_sequence[-1][0] != item_id:
                state_sequence.append((item_id, segment["id"]))

    edges = []
    for (_, object_id), states in seen_by_object.items():
        for (source_state, source_segment), (target_state, target_segment) in zip(
            states, states[1:], strict=False
        ):
            edges.append(
                {
                    "id": f"{source_state}__transitions__{target_state}",
                    "source": source_state,
                    "target": target_state,
                    "type": "state_transition",
                    "object_id": object_id,
                    "evidence_ids": [source_segment, target_segment, object_id],
                }
            )
    return edges


def enrich_segment(
    segment: dict[str, Any], frame_uri: Path, spec: dict[str, Any]
) -> dict[str, Any]:
    sop_steps = {item["id"]: item for item in spec["sop_steps"]}
    item_states = {item["id"]: item for item in spec["item_states"]}
    sop_texts = [sop_steps[sop_id]["text"] for sop_id in segment["sop_ids"]]
    item_texts = [item_states[item_id]["text"] for item_id in segment["item_state_ids"]]
    search_text = "\n".join(
        [
            segment["title"],
            segment["action"],
            segment["semantic"],
            *sop_texts,
            *item_texts,
        ]
    )
    return {
        **segment,
        "source_video_id": source_video_id_for_segment(segment, spec),
        "video_uri": source_video_for_segment(segment, spec),
        "frame_uri": str(frame_uri),
        "sop_texts": sop_texts,
        "item_state_texts": item_texts,
        "search_text": search_text,
    }


def source_video_for_segment(segment: dict[str, Any], spec: dict[str, Any]) -> str:
    return str(segment.get("source_video") or spec["source_video"])


def source_video_id_for_segment(segment: dict[str, Any], spec: dict[str, Any]) -> str:
    return str(segment.get("source_video_id") or spec["index_id"])


def segment_embedding_input(segment: dict[str, Any], frame_path: Path) -> list[dict[str, str]]:
    return [
        {"text": segment["search_text"]},
        {"image": image_data_url(frame_path)},
    ]


def extract_segment_frame(video_path: Path, frame_path: Path, segment: dict[str, Any]) -> None:
    midpoint = (float(segment["start_seconds"]) + float(segment["end_seconds"])) / 2
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{midpoint:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(frame_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def image_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def stable_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)
