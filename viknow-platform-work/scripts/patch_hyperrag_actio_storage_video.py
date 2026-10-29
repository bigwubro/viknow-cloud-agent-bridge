#!/usr/bin/env python3
"""Hot-patch hyperrag-test container actio + handler for storage grant URL video index."""

from __future__ import annotations

import subprocess
import sys

CONTAINER = "hyperrag-test"
HANDLER = "/opt/hyperrag/.venv/lib/python3.12/site-packages/hyperrag/task/handler.py"
SDK = "/opt/hyperrag/.venv/lib/python3.12/site-packages/actio/sdk.py"
SERVICE = "/opt/hyperrag/.venv/lib/python3.12/site-packages/actio/service.py"


def docker_python(code: str) -> None:
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-c", code],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode())
        raise SystemExit(proc.returncode)
    out = proc.stdout.decode().strip()
    if out:
        print(out)


def main() -> int:
    docker_python(
        f"""
from pathlib import Path
path = Path({SDK!r})
text = path.read_text(encoding='utf-8')
old = '''        store = self._active_store()
        video = store.download(key, work / Path(key).name)
        sop_path = store.download(sop_key, work / Path(sop_key).name) if sop_key else None'''
new = '''        store = self._active_store()
        if key.startswith(("http://", "https://")):
            video = Path(work / "source.mp4")
            sop_path = store.download(sop_key, work / Path(sop_key).name) if sop_key else None
        else:
            video = store.download(key, work / Path(key).name)
            sop_path = store.download(sop_key, work / Path(sop_key).name) if sop_key else None'''
if 'key.startswith(("http://", "https://"))' in text:
    print('sdk already patched')
elif old not in text:
    raise SystemExit('sdk marker missing')
else:
    path.write_text(text.replace(old, new, 1), encoding='utf-8')
    print('sdk patched')
"""
    )

    docker_python(
        f"""
from pathlib import Path
path = Path({SERVICE!r})
text = path.read_text(encoding='utf-8')
changed = False
old_attach = '''    if object_key:
        attach_split_object_source(Path(paths["ACTIO_SEGMENT_CONFIG"]), video, object_key)'''
new_attach = '''    if object_key and not str(object_key).startswith(("http://", "https://")):
        attach_split_object_source(Path(paths["ACTIO_SEGMENT_CONFIG"]), video, object_key)'''
if old_attach in text:
    text = text.replace(old_attach, new_attach, 1)
    changed = True
elif 'startswith(("http://", "https://"))' in text and 'attach_split_object_source' in text:
    pass
else:
    raise SystemExit('service attach marker missing')
old = '''    parser = VideoParser(config)
    if sop is None:
        result = parser.parse_video_only(video, video_id=asset_id, output_path=video.parent / "sop-full.json")'''
new = '''    parser = VideoParser(config)
    parse_target = object_key if object_key and str(object_key).startswith(("http://", "https://")) else video
    if sop is None:
        result = parser.parse_video_only(
            parse_target, video_id=asset_id, output_path=video.parent / "sop-full.json"
        )'''
if old in text:
    text = text.replace(old, new, 1)
    changed = True
if not changed and 'parse_target = object_key' in text:
    print('service already patched')
elif not changed:
    raise SystemExit('service marker missing')
else:
    path.write_text(text, encoding='utf-8')
    print('service patched')
"""
    )

    docker_python(
        f"""
from pathlib import Path
path = Path({HANDLER!r})
text = path.read_text(encoding='utf-8')
needle = '''        if not asset_path:
            return TaskResult(status="failed", error="asset_path not found in metadata")

        return await _parse_with_actio_service(
            self,
            task,
            source_key=str(asset_path),'''
insert = '''        if not asset_path:
            return TaskResult(status="failed", error="asset_path not found in metadata")

        source_key = str(asset_path)
        resolved = _get_param("source_download_url") or _get_param("source_media_url")
        if resolved:
            source_key = str(resolved).strip()
        elif source_key.startswith("storage://"):
            import os
            import httpx
            base = os.environ.get("VIKNOW_STORAGE_URL", "").strip().rstrip("/")
            token = os.environ.get("VIKNOW_STORAGE_TOKEN", "").strip()
            lib = str(task.workspace or "").strip()
            aid = str(task.asset_id or "").strip()
            if base and token and lib and aid:
                try:
                    resp = httpx.post(
                        f"{{base}}/libraries/{{lib}}/assets/{{aid}}/download-url",
                        headers={{"Authorization": f"Bearer {{token}}"}},
                        json={{}},
                        timeout=60.0,
                    )
                    if resp.status_code < 400:
                        url = str((resp.json() or {{}}).get("url") or "").strip()
                        if url:
                            source_key = url
                except Exception:
                    pass

        return await _parse_with_actio_service(
            self,
            task,
            source_key=source_key,'''
if 'source_download_url' in text and 'source_key=source_key' in text:
    print('handler already patched')
elif needle not in text:
    raise SystemExit('handler marker missing')
else:
    path.write_text(text.replace(needle, insert, 1), encoding='utf-8')
    print('handler patched')
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
