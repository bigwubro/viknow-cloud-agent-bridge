"""Patch Actio media_input to rewrite 207 storage grant URLs via 236 nginx proxy."""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "rewrite_viknow_grant_url_for_actio"
SNIPPET = '''

def rewrite_viknow_grant_url_for_actio(url: str) -> str:
    """Map 207-only grant hosts to the 236 nginx proxy Actio can reach (docker bridge)."""
    from urllib.parse import urlparse, urlunparse

    rewrite_base = os.getenv("ACTIO_VIKNOW_GRANT_PROXY_BASE", "http://127.0.0.1:5220").strip().rstrip("/")
    if not rewrite_base:
        return url
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return url
    if "/libraries/" not in parsed.path or "grant=" not in (parsed.query or ""):
        return url
    host = (parsed.hostname or "").lower()
    if host not in {"10.168.1.207", "36.103.198.236"} and parsed.port not in {5219, 5220, None}:
        return url
    target = urlparse(rewrite_base)
    return urlunparse((target.scheme, target.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
'''

INJECT = "    url = rewrite_viknow_grant_url_for_actio(url)\n"


def patch(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return False
    anchor = "def read_http_limited(url: str, *, max_bytes: int, headers: dict[str, str] | None = None) -> bytes:"
    if anchor not in text:
        raise SystemExit(f"missing anchor in {path}")
    text = text.replace(anchor, SNIPPET.strip() + "\n\n\n" + anchor, 1)
    text = text.replace(
        anchor + "\n    parsed = urlparse(url)",
        anchor + "\n" + INJECT + "    parsed = urlparse(url)",
        1,
    )
    path.write_text(text, encoding="utf-8")
    return True


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/services/video_split/shared/media_input.py")
    changed = patch(target)
    print("patched" if changed else "already patched", target)


if __name__ == "__main__":
    main()
