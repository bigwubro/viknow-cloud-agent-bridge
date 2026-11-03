from pathlib import Path

p = Path("/opt/hyperrag/.venv/lib/python3.12/site-packages/actio/tasks/segment.py")
t = p.read_text(encoding="utf-8")
old = """        if parsed.scheme in {\"http\", \"https\"}:
            execution_timeout = config.video_split_service_execution_timeout_s
            execution_timeout = self.timeout_s if execution_timeout is None else execution_timeout
            http_timeout = config.video_split_service_http_timeout_s
            queue_timeout = config.video_split_service_queue_timeout_s
            admission_timeout = config.video_split_service_admission_timeout_s
            headers = {\"X-API-Key\": self.api_key} if self.api_key else None
            filename = Path(parsed.path).name or \"uploaded.mp4\""""
new = """        if parsed.scheme in {\"http\", \"https\"}:
            execution_timeout = self.timeout_s
            queue_timeout = config.video_split_service_timeout_s
            headers = {\"X-API-Key\": self.api_key} if self.api_key else None
            filename = Path(parsed.path).name or \"uploaded.mp4\"
            if filename == \"content\":
                filename = \"uploaded.mp4\""""
if old not in t:
    if "if filename == \"content\"" in t:
        print("already fixed")
    else:
        raise SystemExit("block not found")
else:
    p.write_text(t.replace(old, new, 1), encoding="utf-8")
    print("fixed")
