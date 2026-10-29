#!/usr/bin/env python3
import json
from pathlib import Path
from datetime import datetime
import shutil

TARGET = Path("/mnt/P40_NFS/20_Research/30_算法项目/ViKnow/KBIndex/000820013618000002/kv_store_asset_status.json")

def main():
    if not TARGET.exists():
        raise FileNotFoundError(f"Not found: {TARGET}")

    with open(TARGET, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("kv_store_asset_status.json is not a dict at top-level")

    # 备份
    backup = TARGET.with_suffix(TARGET.suffix + f".{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak")
    shutil.copy2(TARGET, backup)

    updated = 0
    total = 0
    for asset_id, rec in list(data.items()):
        if not isinstance(rec, dict):
            continue
        total += 1
        # 判断是否视频：优先使用 parse_mode
        parse_mode = rec.get("parse_mode")
        is_video = (parse_mode == "video")
        file_path_value = f"files/{asset_id}.mp4" if is_video else f"files/{asset_id}.pdf"

        if not rec.get("file_path"):
            rec["file_path"] = file_path_value
            data[asset_id] = rec
            updated += 1

    # 写回
    with open(TARGET, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Processed: {total}, added file_path for: {updated}, backup: {backup}")

if __name__ == "__main__":
    main()