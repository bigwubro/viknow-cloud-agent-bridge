"""Drop retired video_embedding stage from VIDEO_JOB on viknow2-test (5175).

When enable_video_embedding=false, hyperrag_v2 unregisters the handler but legacy
models.py still schedules the task -> scheduler error.
"""
from pathlib import Path

MODELS = Path("/opt/viknow/external/HyperRAG/src/hyperrag/task/models.py")
text = MODELS.read_text(encoding="utf-8")
needle = '        TaskStage(name="video_embedding", task_type=TaskType.VIDEO_EMBEDDING),\n'
if needle not in text:
    if "video_embedding" not in text.split("VIDEO_JOB")[1].split("VIDEO_UPDATE_JOB")[0]:
        print("VIDEO_JOB already without video_embedding")
    else:
        raise SystemExit("video_embedding stage present but unexpected format")
else:
    count = text.count(needle)
    text = text.replace(needle, "", 1)
    MODELS.write_text(text, encoding="utf-8")
    print(f"removed video_embedding stage from VIDEO_JOB (occurrences in file before: {count})")
