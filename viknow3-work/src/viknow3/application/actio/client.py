"""Actio-backed inspect and visual grounding for knowledge-assistant tools."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

from viknow3.ops.config.model_resolve import resolve_openai_chat
from viknow3.ops.config.store import ConfigError, plain_provider


class ActioClientError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail


def _actio_row() -> dict[str, Any]:
    try:
        return plain_provider("actio")
    except ConfigError as exc:
        raise ActioClientError(exc.status, str(exc.body)) from exc


def _gateway_api_key() -> str:
    return (
        os.environ.get("ACTIO_API_KEY", "").strip()
        or str(_actio_row().get("api_key") or "").strip()
    )


def _locate_base_url() -> str:
    override = os.environ.get("ACTIO_LOCATE_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    base = str(_actio_row().get("base_url") or "http://127.0.0.1:8600").rstrip("/")
    return f"{base}/locate"


def _claim(text: str, *, confidence: float = 0.9, quote_verified: bool = True) -> dict[str, Any]:
    return {
        "text": text.strip(),
        "confidence": confidence,
        "category": "fact",
        "quote_verified": quote_verified,
        "source": "actio",
    }


def _chat_vision(
    *,
    image_path: Path,
    question: str,
    system: str,
) -> str:
    resolved = resolve_openai_chat("server-default")
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "model": resolved.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"},
                    },
                    {"type": "text", "text": question},
                ],
            },
        ],
    }
    headers = {"Authorization": f"Bearer {resolved.api_key}"}
    with httpx.Client(base_url=resolved.base_url.rstrip("/"), timeout=180.0) as client:
        response = client.post("/chat/completions", json=payload, headers=headers)
    if response.status_code >= 400:
        raise ActioClientError(response.status_code, response.text[:500])
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise ActioClientError(502, "empty chat completion")
    content = choices[0].get("message", {}).get("content", "")
    return str(content or "").strip()


def _parse_claims(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text:
        return []
    fence = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [_claim(text)]
    if isinstance(parsed, dict) and isinstance(parsed.get("claims"), list):
        rows = parsed["claims"]
    elif isinstance(parsed, list):
        rows = parsed
    else:
        return [_claim(text)]
    claims: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, str):
            claims.append(_claim(row))
        elif isinstance(row, dict) and row.get("text"):
            claims.append(
                {
                    "text": str(row["text"]),
                    "confidence": float(row.get("confidence") or 0.9),
                    "category": str(row.get("category") or "fact"),
                    "quote_verified": bool(row.get("quote_verified", True)),
                    "source": "actio",
                }
            )
    return claims or [_claim(raw)]


class ActioClient:
    """Resolve media through HyperRAG, then call Actio locate or the private VLM."""

    def __init__(self) -> None:
        from actio.locate_anything import LocateAnythingClient

        self._locate = LocateAnythingClient(
            base_url=_locate_base_url(),
            api_key=_gateway_api_key(),
        )

    def video_inspect_2_0(
        self,
        *,
        library_id: str,
        asset_id: str,
        question: str,
        time_range: dict[str, Any],
        evidence_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        from actio.video_inspection import OpenAICompatibleVideoInspectionClient

        from viknow3.application.hyperrag.client import HYPERAG

        resolved = resolve_openai_chat("server-default")
        start = float(time_range.get("start_seconds") or 0)
        end = float(time_range.get("end_seconds") or start)
        with tempfile.TemporaryDirectory(prefix="viknow-actio-") as tmp:
            path = Path(tmp) / f"{asset_id}.mp4"
            mime, data = HYPERAG.media(library_id, asset_id, "video")
            if not data:
                raise ActioClientError(404, "video media is empty")
            path.write_bytes(data)
            inspector = OpenAICompatibleVideoInspectionClient(
                model=resolved.model,
                base_url=resolved.base_url.rstrip("/"),
                api_key=resolved.api_key,
            )
            answer, _ = inspector.inspect_video(
                path,
                question,
                time_range=(start, end),
            )
        return {
            "tool": "video_inspect_2_0",
            "result": {"success": True, "claims": _parse_claims(answer)},
            "evidence": {
                "id": evidence_id or asset_id,
                "title": title or asset_id,
                "metadata": {"library_id": library_id, "asset_id": asset_id, "time_range": time_range},
            },
            "source": "actio",
        }

    def document_inspect(
        self,
        *,
        library_id: str,
        asset_id: str,
        question: str,
        evidence_id: str | None = None,
    ) -> dict[str, Any]:
        import fitz

        from viknow3.application.hyperrag.client import HYPERAG

        with tempfile.TemporaryDirectory(prefix="viknow-actio-doc-") as tmp:
            path = Path(tmp) / f"{asset_id}.pdf"
            _mime, data = HYPERAG.media(library_id, asset_id, "document")
            if not data:
                raise ActioClientError(404, "document media is empty")
            path.write_bytes(data)
            png = Path(tmp) / "page-1.png"
            with fitz.open(path) as doc:
                if doc.page_count < 1:
                    raise ActioClientError(422, "document has no pages")
                doc.load_page(0).get_pixmap().save(png)
            raw = _chat_vision(
                image_path=png,
                question=question,
                system=(
                    "You extract factual claims visible on this document page. "
                    "Reply with JSON: {\"claims\":[{\"text\":\"...\",\"confidence\":0.0-1.0,\"quote_verified\":true}]}. "
                    "Only facts supported by the page. If insufficient, return {\"claims\":[]}."
                ),
            )
        return {
            "tool": "document_inspect",
            "result": {"success": True, "claims": _parse_claims(raw)},
            "evidence": {
                "id": evidence_id or asset_id,
                "metadata": {"library_id": library_id, "asset_id": asset_id, "document_inspect_page": 1},
            },
            "source": "actio",
        }

    def visual_ground(self, *, query: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        from viknow3.application.hyperrag.client import HYPERAG

        metadata = (evidence or {}).get("metadata") if isinstance(evidence, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        library_id = str(metadata.get("library_id") or "").strip()
        asset_id = str(metadata.get("asset_id") or "").strip()
        if not library_id or not asset_id:
            raise ActioClientError(422, "visual_ground requires evidence.metadata.library_id and asset_id")
        with tempfile.TemporaryDirectory(prefix="viknow-locate-") as tmp:
            path = Path(tmp) / f"{asset_id}.png"
            try:
                _mime, data = HYPERAG.media(library_id, asset_id, "image")
            except Exception:
                _mime, data = HYPERAG.media(library_id, asset_id, "document")
            path.write_bytes(data)
            located = self._locate.locate_image(path, query=query)
        claims = [
            _claim(
                detection.text or detection.label or query,
                confidence=float(detection.score or 0.85),
            )
            for detection in located.detections
        ]
        if not claims and located.text:
            claims = [_claim(located.text)]
        return {
            "tool": "visual_ground",
            "result": {"success": bool(claims), "claims": claims},
            "evidence": evidence or {"id": asset_id, "metadata": metadata},
            "source": "actio",
        }


_client: ActioClient | None = None


def get_actio_client() -> ActioClient:
    global _client
    if _client is None:
        _client = ActioClient()
    return _client
