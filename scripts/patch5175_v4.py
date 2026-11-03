from pathlib import Path

vg = Path("/usr/local/lib/python3.12/site-packages/viknow/core/storage/video_gateway.py")
text = vg.read_text(encoding="utf-8")
if "def ensure_gateway_video_upload_visible(" in text:
    print("video_gateway already has ensure_gateway_video_upload_visible")
else:
    anchor = "    return True\n\n\ndef install_gateway_video_split() -> None:"
    block = """    return True


def ensure_gateway_video_upload_visible(
    path_or_key: str | Path,
    *,
    wait_s: float | None = None,
) -> None:
    if not gateway_storage_enabled():
        return
    ensure_gateway_video_split_installed()
    path_text = str(path_or_key)
    deadline_wait = float(
        wait_s if wait_s is not None else os.getenv("VIKNOW_GATEWAY_UPLOAD_WAIT_S", "60")
    )
    media = _wait_object_media_for_path(path_text, wait_s=deadline_wait, poll_s=_OBJECT_POLL_S)
    if media is None:
        candidates = gateway_object_key_candidates(path_text)
        raise _refused_local_error(path_text, candidates)


def install_gateway_video_split() -> None:"""
    if anchor not in text:
        raise SystemExit("install_gateway anchor missing")
    text = text.replace(anchor, block, 1)
    vg.write_text(text, encoding="utf-8")
    print("inserted ensure_gateway_video_upload_visible")

import ast

ast.parse(vg.read_text(encoding="utf-8"))
print("syntax ok")
