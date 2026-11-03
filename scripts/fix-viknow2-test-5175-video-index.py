#!/usr/bin/env python3
"""Patch viknow2-test (5175) for gateway video index: wait for MinIO object + Actio patch."""

from __future__ import annotations

import subprocess
import sys
import textwrap

CONTAINER = "viknow2-test"

VIDEO_GATEWAY_MARKER = "def ensure_gateway_video_upload_visible("
MANAGER_MARKER = "ensure_gateway_video_upload_visible(index_path)"
HANDLER_MARKER = "ensure_gateway_video_upload_visible(Path(str(asset_path)))"


def docker_exec_python(code: str) -> None:
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-c", code],
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)


def patch_video_gateway() -> None:
    docker_exec_python(
        textwrap.dedent(
            """
            from pathlib import Path
            path = Path("/usr/local/lib/python3.12/site-packages/viknow/core/storage/video_gateway.py")
            text = path.read_text(encoding="utf-8")
            if {marker!r} in text:
                print("video_gateway: ensure_gateway_video_upload_visible already present")
            else:
                old = "_OBJECT_POLL_S = float(os.getenv(\\"VIKNOW_GATEWAY_OBJECT_POLL_S\\", \\"0.2\\"))"
                new = old + '''

def ensure_gateway_video_upload_visible(
    path_or_key: str | Path,
    *,
    wait_s: float | None = None,
) -> None:
    """Block until the gateway object for a scratch video path is readable.

    Upload completes write normalized bytes to scratch then ``put_file``; MinIO
    listing/stat can lag briefly. Index jobs must not enter ``video_parse`` until
    ``store.exists`` is true or Actio split fails in ~100ms with a bare path error.
    """
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
'''
                if old not in text:
                    raise SystemExit("video_gateway: poll env marker missing")
                text = text.replace(old, new, 1)
                path.write_text(text, encoding="utf-8")
                print("video_gateway: added ensure_gateway_video_upload_visible")
            """
        ).format(marker=VIDEO_GATEWAY_MARKER)
    )


def patch_upload_manager() -> None:
    docker_exec_python(
        textwrap.dedent(
            """
            from pathlib import Path
            path = Path("/usr/local/lib/python3.12/site-packages/viknow/core/knowledge/uploads/manager.py")
            text = path.read_text(encoding="utf-8")
            if {marker!r} in text:
                print("upload manager: already patched")
                return
            needle = "                self._publish_normalized_video(record.asset_id, index_path)\\n            except (VideoNormalizeError, OSError):"
            insert = '''                self._publish_normalized_video(record.asset_id, index_path)
                from viknow.core.storage.video_gateway import (
                    ensure_gateway_video_split_installed,
                    ensure_gateway_video_upload_visible,
                )

                ensure_gateway_video_upload_visible(index_path)
                ensure_gateway_video_split_installed()
            except (VideoNormalizeError, OSError):'''
            if needle not in text:
                raise SystemExit("upload manager: normalize block marker missing")
            text = text.replace(needle, insert, 1)
            path.write_text(text, encoding="utf-8")
            print("upload manager: wait for gateway object before submit_index")
            """
        ).format(marker=MANAGER_MARKER)
    )


def patch_hyperrag_handler() -> None:
    docker_exec_python(
        textwrap.dedent(
            """
            from pathlib import Path
            path = Path("/opt/viknow/external/HyperRAG/src/hyperrag/task/handler.py")
            text = path.read_text(encoding="utf-8")
            if {marker!r} in text:
                print("handler: already patched")
                return
            needle = "        if not asset_path:\\n            return TaskResult(status=\\"failed\\", error=\\"asset_path not found in metadata\\")"
            insert = '''        if not asset_path:
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
            return TaskResult(status="failed", error=err)'''
            if needle not in text:
                raise SystemExit("handler: asset_path marker missing")
            text = text.replace(needle, insert, 1)
            path.write_text(text, encoding="utf-8")
            print("handler: gateway visibility + patch before video_parse")
            """
        ).format(marker=HANDLER_MARKER)
    )


def main() -> int:
    patch_video_gateway()
    patch_upload_manager()
    patch_hyperrag_handler()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
