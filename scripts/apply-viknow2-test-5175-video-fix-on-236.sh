#!/usr/bin/env bash
# Apply 5175 (viknow2-test) gateway video index fix on 236 host.
set -euo pipefail

CONTAINER="${VIKNOW2_TEST_CONTAINER:-viknow2-test}"

docker exec "$CONTAINER" python3 <<'PY'
from pathlib import Path

# --- video_gateway.py ---
vg = Path("/usr/local/lib/python3.12/site-packages/viknow/core/storage/video_gateway.py")
text = vg.read_text(encoding="utf-8")
if "def ensure_gateway_video_upload_visible(" not in text:
    anchor = '_OBJECT_POLL_S = float(os.getenv("VIKNOW_GATEWAY_OBJECT_POLL_S", "0.2"))'
    block = anchor + """

def ensure_gateway_video_upload_visible(
    path_or_key: str | Path,
    *,
    wait_s: float | None = None,
) -> None:
    \"\"\"Block until the gateway object for a scratch video path is readable.\"\"\"
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
"""
    if anchor not in text:
        raise SystemExit("video_gateway anchor missing")
    vg.write_text(text.replace(anchor, block, 1), encoding="utf-8")
    print("patched video_gateway.py")
else:
    print("video_gateway.py ok")

# --- uploads manager ---
mgr = Path("/usr/local/lib/python3.12/site-packages/viknow/core/knowledge/uploads/manager.py")
text = mgr.read_text(encoding="utf-8")
if "ensure_gateway_video_upload_visible(index_path)" not in text:
    needle = """                self._publish_normalized_video(record.asset_id, index_path)
            except (VideoNormalizeError, OSError):"""
    repl = """                self._publish_normalized_video(record.asset_id, index_path)
                from viknow.core.storage.video_gateway import (
                    ensure_gateway_video_split_installed,
                    ensure_gateway_video_upload_visible,
                )

                ensure_gateway_video_upload_visible(index_path)
                ensure_gateway_video_split_installed()
            except (VideoNormalizeError, OSError):"""
    if needle not in text:
        raise SystemExit("upload manager needle missing")
    mgr.write_text(text.replace(needle, repl, 1), encoding="utf-8")
    print("patched uploads/manager.py")
else:
    print("uploads/manager.py ok")

# --- HyperRAG handler ---
hr = Path("/opt/viknow/external/HyperRAG/src/hyperrag/task/handler.py")
text = hr.read_text(encoding="utf-8")
if "ensure_gateway_video_upload_visible(Path(str(asset_path)))" not in text:
    needle = """        if not asset_path:
            return TaskResult(status="failed", error="asset_path not found in metadata")"""
    repl = """        if not asset_path:
            return TaskResult(status="failed", error="asset_path not found in metadata")

        try:
            from viknow.core.storage.video_gateway import (
                ensure_gateway_video_split_installed,
                ensure_gateway_video_upload_visible,
            )

            ensure_gateway_video_split_installed()
            ensure_gateway_video_upload_visible(Path(str(asset_path)))
        except Exception as prep_exc:
            err = _viknow_scratch_fail_closed(prep_exc)
            return TaskResult(status="failed", error=err)"""
    if needle not in text:
        raise SystemExit("handler needle missing")
    hr.write_text(text.replace(needle, repl, 1), encoding="utf-8")
    print("patched hyperrag handler.py")
else:
    print("hyperrag handler.py ok")
PY

echo "Restarting $CONTAINER ..."
docker restart "$CONTAINER" >/dev/null
echo "Waiting for health ..."
for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:5175/health" >/dev/null 2>&1 || curl -sf "http://127.0.0.1:5175/api/v1/health" >/dev/null 2>&1; then
    echo "5175 up after ${i}s"
    break
  fi
  sleep 2
done

curl -sf "http://127.0.0.1:5175/" >/dev/null || curl -sf "http://127.0.0.1:5175/docs" >/dev/null || true
echo "Patch applied."
