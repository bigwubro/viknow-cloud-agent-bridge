from pathlib import Path

hr = Path("/opt/viknow/external/HyperRAG/src/hyperrag/task/handler.py")
text = hr.read_text(encoding="utf-8")

# Remove any previously misplaced gateway prep blocks (keep VideoParseHandler placement).
bad = """
        try:
            from viknow.core.storage.video_gateway import (
                ensure_gateway_video_split_installed,
                ensure_gateway_video_upload_visible,
            )

            ensure_gateway_video_split_installed()
            ensure_gateway_video_upload_visible(Path(str(asset_path)))
        except Exception as prep_exc:
            err = _viknow_scratch_fail_closed(prep_exc)
            return TaskResult(status="failed", error=err)

"""
while bad in text:
    text = text.replace(bad, "\n", 1)
    print("removed misplaced gateway prep block")

cls = "class VideoParseHandler(BaseTaskHandler):"
idx = text.find(cls)
if idx < 0:
    raise SystemExit("VideoParseHandler missing")
sub = text[idx:]
needle = """        if not asset_path:
            return TaskResult(status="failed", error="asset_path not found in metadata")

        title = str(_get_param("title", "") or Path(asset_path).stem)"""
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
            return TaskResult(status="failed", error=err)

        title = str(_get_param("title", "") or Path(asset_path).stem)"""
if needle not in sub:
    raise SystemExit("VideoParseHandler needle missing after cleanup")
text = text[:idx] + sub.replace(needle, repl, 1)
hr.write_text(text, encoding="utf-8")
print("VideoParseHandler gateway prep installed")

# Force segment client patch reinstall on next import
vg = Path("/usr/local/lib/python3.12/site-packages/viknow/core/storage/video_gateway.py")
vgt = vg.read_text(encoding="utf-8")
vgt = vgt.replace("global _installed\n", "global _installed\n_installed = False\n", 1)
if "_installed = False\n_installed = False" not in vgt:
    vg.write_text(vgt, encoding="utf-8")
    print("reset video_gateway _installed flag")
else:
    print("video_gateway install flag ok")

print("done")
