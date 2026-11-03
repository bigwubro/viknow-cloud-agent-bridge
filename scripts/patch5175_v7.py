from pathlib import Path

vg = Path("/usr/local/lib/python3.12/site-packages/viknow/core/storage/video_gateway.py")
st = vg.read_text(encoding="utf-8")
old = """        if requires_object:
            media = _wait_object_media_for_path(path_text)
            if media is None:
                raise _refused_local_error(path_text, candidates)
            # ALWAYS force object BEFORE original so path/base64 exists() never runs.
            config.video_split_upload_mode = "object"
        try:
            return original_segment(self, video_path, config)
"""
new = """        if requires_object:
            media = _wait_object_media_for_path(path_text)
            if media is None:
                local = Path(path)
                if local.is_file() and local.stat().st_size > 0:
                    # Actio split runs in-app; base64 small scratch files when object lagged.
                    return original_segment(self, video_path, config)
                raise _refused_local_error(path_text, candidates)
            # ALWAYS force object BEFORE original so path/base64 exists() never runs.
            config.video_split_upload_mode = "object"
        try:
            return original_segment(self, video_path, config)
"""
if old not in st:
    raise SystemExit("video_gateway segment wrap marker missing")
st = st.replace(old, new, 1)

old2 = """        if gateway_storage_enabled() and (
            _looks_like_private_scratch(path_text) or bool(candidates)
        ):
            media = _wait_object_media_for_path(path_text)
            if media is None:
                raise _refused_local_error(path_text, candidates)
            config.video_split_upload_mode = "object"
        try:
            return original_segment_video(config, video_path)
"""
new2 = """        if gateway_storage_enabled() and (
            _looks_like_private_scratch(path_text) or bool(candidates)
        ):
            media = _wait_object_media_for_path(path_text)
            if media is None:
                local = Path(str(video_path))
                if local.is_file() and local.stat().st_size > 0:
                    return original_segment_video(config, video_path)
                raise _refused_local_error(path_text, candidates)
            config.video_split_upload_mode = "object"
        try:
            return original_segment_video(config, video_path)
"""
if old2 not in st:
    raise SystemExit("video_gateway segment_video wrap marker missing")
st = st.replace(old2, new2, 1)

vg.write_text(st, encoding="utf-8")
print("video_gateway: allow base64 fallback when local scratch exists")
print("done")
