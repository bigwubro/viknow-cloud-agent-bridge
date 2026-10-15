#!/usr/bin/env python3
"""Import the 3P+1D board onto Grafana :8091, same uid as the old one-instance dash."""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path

GRAFANA = os.environ.get("GRAFANA_URL", "http://127.0.0.1:8091")
USER = os.environ.get("GRAFANA_USER", "admin")
PASSWORD = os.environ.get("GRAFANA_PASSWORD", "admin123")
DASH = Path(__file__).with_name("qwen36-3p1d-dashboard.json")


def api(method: str, path: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    req = urllib.request.Request(
        GRAFANA + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Basic " + token,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def main() -> None:
    dash = json.loads(DASH.read_text(encoding="utf-8"))
    org = api("GET", "/api/org")
    print("org", org.get("name"), org.get("id"))
    pack = api("GET", f"/api/dashboards/uid/{dash['uid']}")
    folder = (pack.get("meta") or {}).get("folderUid") or ""
    dash["id"] = (pack.get("dashboard") or {}).get("id")
    payload = {"dashboard": dash, "overwrite": True}
    if folder:
        payload["folderUid"] = folder
    resp = api("POST", "/api/dashboards/db", payload)
    print("saved", resp)


if __name__ == "__main__":
    main()
