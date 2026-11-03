#!/usr/bin/env python3
"""Use local video path for Actio split after HTTP download (207 worker + 236 split)."""
from pathlib import Path
import sys

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else "src/actio/video_parser.py")
text = TARGET.read_text(encoding="utf-8")
old = "segment_video_input = raw_video_input"
new = "segment_video_input = video_path"
if new in text:
    print("already patched", TARGET)
    raise SystemExit(0)
if old not in text:
    raise SystemExit(f"missing {old!r} in {TARGET}")
TARGET.write_text(text.replace(old, new, 1), encoding="utf-8")
print("patched", TARGET)
