from pathlib import Path

# Patch scheduler to normalize scratch errors when handlers raise.
sched = Path("/opt/viknow/external/HyperRAG/src/hyperrag/task/scheduler.py")
st = sched.read_text(encoding="utf-8")
old = '                    result = TaskResult(status="failed", error=str(e))'
new = '''                    try:
                        from hyperrag.task.handler import _viknow_scratch_fail_closed

                        err = _viknow_scratch_fail_closed(e)
                    except Exception:
                        err = str(e)
                    result = TaskResult(status="failed", error=err)'''
if old in st and new not in st:
    sched.write_text(st.replace(old, new, 1), encoding="utf-8")
    print("patched scheduler fail_closed")
else:
    print("scheduler ok")

hr = Path("/opt/viknow/external/HyperRAG/src/hyperrag/task/handler.py")
text = hr.read_text(encoding="utf-8")
old_block = """            prepared_video = _viknow_prepare_gateway_video(asset_path)
            parse_result = await asyncio.to_thread(
                video_parser.parse_video_only,
                prepared_video,
                video_id=task.asset_id,
                title=title,
                output_path=sop_full_path,
                **parse_kwargs,
            )"""
new_block = """            def _run_video_parse():
                from viknow.core.storage.video_gateway import (
                    ensure_gateway_video_split_installed,
                    ensure_gateway_video_upload_visible,
                )

                ensure_gateway_video_split_installed()
                ensure_gateway_video_upload_visible(Path(str(asset_path)))
                prepared = _viknow_prepare_gateway_video(asset_path)
                return video_parser.parse_video_only(
                    prepared,
                    video_id=task.asset_id,
                    title=title,
                    output_path=sop_full_path,
                    **parse_kwargs,
                )

            parse_result = await asyncio.to_thread(_run_video_parse)"""
if old_block in text:
    hr.write_text(text.replace(old_block, new_block, 1), encoding="utf-8")
    print("patched handler to run gateway+parse in worker thread")
else:
    print("handler parse block ok or already patched")

print("done")
