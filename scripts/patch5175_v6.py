from pathlib import Path

seg = Path("/opt/viknow/external/actio/src/actio/tasks/segment.py")
st = seg.read_text(encoding="utf-8")

old = """        is_scratch = ("/viknow-scratch/" in lowered) or ("/tmp/viknow-scratch/" in lowered)
        # Hotpatch v4: scratch must never take path/base64 exists() -> bare FNF.
        if is_scratch and upload_mode != "object":
            config.video_split_upload_mode = "object"
            upload_mode = "object"
"""
new = """        is_scratch = ("/viknow-scratch/" in lowered) or ("/tmp/viknow-scratch/" in lowered)
        max_bytes = int(max(0.0, config.video_split_upload_max_mb) * 1024 * 1024)
        size = path.stat().st_size if path.exists() else 0
        # Gateway mode: prefer object media; small local scratch files use base64 to Actio.
        if is_scratch and upload_mode != "object":
            if path.exists() and size > 0 and (not max_bytes or size <= max_bytes):
                upload_mode = "base64"
                config.video_split_upload_base64 = True
            else:
                config.video_split_upload_mode = "object"
                upload_mode = "object"
"""
if old not in st:
    raise SystemExit("segment scratch block marker missing")
st = st.replace(old, new, 1)

old2 = """        elif upload_mode == "base64" or (
            upload_mode == "auto" and config.video_split_upload_base64
        ):
            if is_scratch:
                raise FileNotFoundError(
                    f"refused local scratch fallback: {path}"
                )
            if not path.exists():"""
new2 = """        elif upload_mode == "base64" or (
            upload_mode == "auto" and config.video_split_upload_base64
        ):
            if not path.exists():"""
if old2 not in st:
    raise SystemExit("segment base64 scratch refuse marker missing")
st = st.replace(old2, new2, 1)

seg.write_text(st, encoding="utf-8")
print("patched actio segment scratch -> base64 when local file present")
print("done")
