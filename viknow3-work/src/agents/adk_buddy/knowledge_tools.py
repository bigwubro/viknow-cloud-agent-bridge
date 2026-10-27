"""Knowledge tools for the ADK turn. They call the injected client and nothing else."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agents.qa_text_plan import apply_text_plan, empty_plan, final_check as submit_final_check, present_skill

_SNIPPET_LIMIT = 1200
_HIT_LIMIT = 8
_CHUNK_MARK = "### chunk text"
_CONTEXT_MARK = "### context"


class CitationLog:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.plan: dict[str, Any] = empty_plan()
        self.submitted_answer: str = ""

    def add(self, row: dict[str, Any]) -> None:
        self.rows.append(row)


def build_knowledge_tools(
    client: Any,
    library_ids: tuple[str, ...],
    citations: CitationLog,
    question: str = "",
) -> list[Callable[..., Any]]:
    allowed = tuple(dict.fromkeys(library_id for library_id in library_ids if library_id))
    enabled = set(load_enabled_tools())

    async def knowledge_discover(query: str) -> str:
        """List granted libraries and the assets that match the question."""
        try:
            rows = await _discover(client, query, allowed)
        except Exception as exc:
            return _error(exc)
        return json.dumps({"libraries": rows}, ensure_ascii=False)

    async def knowledge_search(query: str, library_id: str = "") -> str:
        """Search granted libraries. Quote chunk_text. Do not add facts that are not in chunk_text."""
        targets, refusal = _targets(library_id, allowed)
        if refusal:
            return refusal
        hits: list[dict[str, Any]] = []
        try:
            for target in targets:
                result = await _search(client, library_id=target, query=query)
                for item in getattr(result, "evidence", None) or []:
                    row = _evidence_row(item, target)
                    if not row["chunk_text"] and not row["title"]:
                        continue
                    citations.add(row)
                    hits.append(row)
                    if len(hits) >= _HIT_LIMIT:
                        break
                if len(hits) >= _HIT_LIMIT:
                    break
        except Exception as exc:
            return _error(exc)
        return json.dumps({"evidence": hits}, ensure_ascii=False)

    async def list_doc_segments(library_id: str, asset_id: str) -> str:
        """List document segments for one asset. Copy both ids from discovery or search."""
        return await _segments(client, library_id, asset_id, allowed, citations, video=False)

    async def list_video_segments(library_id: str, asset_id: str) -> str:
        """List video segments for one asset. Copy both ids from discovery or search."""
        return await _segments(client, library_id, asset_id, allowed, citations, video=True)

    async def answer_plan(
        action: str,
        content: str = "",
        findings: list[dict[str, Any]] | None = None,
        template_id: str = "",
    ) -> str:
        """Record the knowledge-qa-v2 plan. Use content and findings, not update objects."""
        args: dict[str, Any] = {"action": action}
        if content:
            args["content"] = content
        if findings is not None:
            args["findings"] = findings
        if template_id:
            args["template_id"] = template_id
        updated, error = apply_text_plan(citations.plan, args)
        if error:
            return json.dumps({"tool": "answer_plan", "error": error}, ensure_ascii=False)
        citations.plan = updated
        return json.dumps({"tool": "answer_plan", "text_plan": updated}, ensure_ascii=False)

    async def final_check() -> str:
        """Submit the final answer. No arguments. The runtime renders it from the plan."""
        payload, rendered = submit_final_check(question, citations.plan)
        if rendered:
            citations.submitted_answer = rendered
        return json.dumps(payload, ensure_ascii=False)

    async def read_file(file_path: str) -> str:
        """Read one skill or reference file. Pass the path under the skill directory."""
        try:
            return _read_skill_file(file_path)
        except FileNotFoundError:
            return json.dumps({"error": f"file not found: {file_path}"}, ensure_ascii=False)

    async def uploaded_image_search(query: str = "", workspace_path: str = "") -> str:
        """Analyze one user-uploaded image. This ADK runtime does not execute it."""
        return _unavailable("uploaded_image_search")

    async def video_inspect(
        library_id: str,
        asset_id: str,
        time_range: dict[str, Any],
        question: str,
        evidence_id: str = "",
        title: str = "",
    ) -> str:
        """Inspect one video range. This ADK runtime does not execute it."""
        del library_id, asset_id, time_range, question, evidence_id, title
        return _unavailable("video_inspect")

    async def visual_ground(query: str, file_path: str = "", frame_time: float = 0) -> str:
        """Locate or count visible objects. This ADK runtime does not execute it."""
        del query, file_path, frame_time
        return _unavailable("visual_ground")

    async def document_inspect(library_id: str, asset_id: str, page: int = 1, question: str = "") -> str:
        """Inspect one document page. This ADK runtime does not execute it."""
        del library_id, asset_id, page, question
        return _unavailable("document_inspect")

    tools = {
        "knowledge_discover": knowledge_discover,
        "knowledge_search": knowledge_search,
        "list_doc_segments": list_doc_segments,
        "list_video_segments": list_video_segments,
        "answer_plan": answer_plan,
        "uploaded_image_search": uploaded_image_search,
        "video_inspect": video_inspect,
        "visual_ground": visual_ground,
        "document_inspect": document_inspect,
        "read_file": read_file,
    }
    selected = [tools[name] for name in load_enabled_tools() if name in tools]
    if "knowledge-qa-v2" in _enabled_skills():
        selected.append(final_check)
        if read_file not in selected:
            selected.append(read_file)
    return selected


def load_enabled_tools() -> tuple[str, ...]:
    from agents.adk_buddy.instruction import load_profile

    return tuple(str(item) for item in load_profile().get("enabled_tools") or ())


def _enabled_skills() -> tuple[str, ...]:
    from agents.adk_buddy.instruction import load_profile

    return tuple(str(item) for item in load_profile().get("enabled_skills") or ())


def _unavailable(name: str) -> str:
    return json.dumps(
        {
            "tool": name,
            "error": (
                f"{name} is named by the shared skill, but this ADK runtime does not "
                "execute it. Record the gap and do not invent the missing fact."
            ),
        },
        ensure_ascii=False,
    )


def _read_skill_file(file_path: str) -> str:
    root = Path(__file__).resolve().parent / "skills"
    raw = file_path.strip().replace("\\", "/").lstrip("/")
    matches = [
        path
        for path in root.rglob("*")
        if path.is_file() and (raw == path.relative_to(root).as_posix() or raw.endswith(path.relative_to(root).as_posix()))
    ]
    if not matches:
        raise FileNotFoundError(file_path)
    chosen = max(matches, key=lambda path: len(path.relative_to(root).as_posix()))
    return present_skill(chosen.read_text(encoding="utf-8"))


def _targets(library_id: str, allowed: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
    chosen = library_id.strip()
    if chosen:
        if chosen not in allowed:
            return (), f"library {chosen} is not granted for this turn"
        return (chosen,), ""
    if not allowed:
        return (), "no library is granted for this turn"
    return allowed, ""


async def _discover(client: Any, query: str, library_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    method = getattr(client, "discover_libraries_async", None)
    if method is not None:
        rows = await method(query, library_ids=library_ids)
    else:
        rows = client.discover_libraries(query, library_ids=library_ids)
    return [_library_row(row) for row in rows or []]


async def _search(client: Any, *, library_id: str, query: str) -> Any:
    method = getattr(client, "search_async", None)
    if method is not None:
        return await method(library_id=library_id, query=query, top_k=_HIT_LIMIT)
    return client.search(library_id=library_id, query=query, top_k=_HIT_LIMIT)


async def _segments(
    client: Any,
    library_id: str,
    asset_id: str,
    allowed: tuple[str, ...],
    citations: CitationLog,
    *,
    video: bool,
) -> str:
    refusal = _targets(library_id, allowed)[1]
    if refusal:
        return refusal
    if not asset_id.strip():
        return "asset_id is required"
    try:
        chunks = client.list_asset_segments(library_id=library_id, asset_id=asset_id)
    except Exception as exc:
        return _error(exc)
    rows = []
    for chunk in chunks or []:
        if not isinstance(chunk, dict) or _is_video(chunk) != video:
            continue
        row = _segment_row(chunk, library_id, asset_id)
        citations.add(row)
        rows.append(row)
        if len(rows) >= _HIT_LIMIT:
            break
    label = "video_segments" if video else "document_segments"
    return json.dumps({label: rows}, ensure_ascii=False)


def _library_row(row: Any) -> dict[str, Any]:
    library = getattr(row, "library", None)
    assets = []
    for match in getattr(row, "asset_matches", None) or []:
        assets.append(
            {
                "asset_id": str(getattr(match, "asset_id", "") or ""),
                "title": str(getattr(match, "title", "") or ""),
                "modality": str(getattr(match, "modality", "") or ""),
            }
        )
    return {
        "library_id": str(getattr(library, "id", "") or ""),
        "name": str(getattr(library, "name", "") or ""),
        "assets": assets,
    }


def _evidence_row(item: Any, library_id: str) -> dict[str, Any]:
    meta = _meta(getattr(item, "metadata", None))
    texts = []
    for part in getattr(item, "parts", None) or []:
        text = getattr(part, "text", None)
        if text:
            texts.append(str(text))
    chunk_text, _index_note = split_indexed_text("\n".join(texts))
    row = {
        "id": str(getattr(item, "id", "") or meta.get("chunk_id") or ""),
        "library_id": library_id,
        "title": str(getattr(item, "title", "") or meta.get("title") or ""),
        "asset_id": str(meta.get("full_doc_id") or meta.get("asset_id") or ""),
        "score": getattr(item, "score", None),
        "page": meta.get("page"),
        "begin_ts": meta.get("begin_ts"),
        "end_ts": meta.get("end_ts"),
        "chunk_text": chunk_text[:_SNIPPET_LIMIT],
    }
    return row


def _segment_row(chunk: dict[str, Any], library_id: str, asset_id: str) -> dict[str, Any]:
    meta = _meta(chunk.get("metadata"))
    chunk_text, _index_note = split_indexed_text(str(chunk.get("content") or ""))
    return {
        "id": str(chunk.get("chunk_id") or chunk.get("id") or meta.get("chunk_id") or ""),
        "library_id": library_id,
        "asset_id": asset_id,
        "title": str(meta.get("title") or chunk.get("full_doc_id") or ""),
        "chunk_text": chunk_text[:_SNIPPET_LIMIT],
        "page": meta.get("page", meta.get("page_number")),
        "begin_ts": meta.get("begin_ts"),
        "end_ts": meta.get("end_ts"),
    }


def split_indexed_text(raw: str) -> tuple[str, str]:
    """Separate indexer context from the source text stored in one chunk."""
    text = raw.strip()
    if _CHUNK_MARK not in text:
        return text, ""
    before, after = text.split(_CHUNK_MARK, 1)
    note = ""
    if _CONTEXT_MARK in before:
        note = _unwrap_quoted(before.split(_CONTEXT_MARK, 1)[1])
    return _unwrap_quoted(after), note


def _unwrap_quoted(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _is_video(chunk: dict[str, Any]) -> bool:
    meta = _meta(chunk.get("metadata"))
    chunk_type = str(meta.get("chunk_type") or chunk.get("chunk_type") or "").lower()
    return chunk_type == "video" or meta.get("begin_ts") is not None or meta.get("end_ts") is not None


def _meta(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _error(exc: Exception) -> str:
    return f"tool error: {type(exc).__name__}: {exc}"
