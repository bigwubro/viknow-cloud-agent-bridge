#!/usr/bin/env python3
"""Fix Content-Disposition for non-ASCII filenames in viknow-storage (run on 236)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/cursor-agent/src/viknow-storage")
app = ROOT / "src/viknow_storage/app.py"
text = app.read_text()

start = text.find("\ndef _content_disposition")
if start != -1:
    end = text.find("\ndef _only(body: dict", start)
    if end != -1:
        text = text[:start] + text[end:]

if "from urllib.parse import quote" not in text:
    text = text.replace("import time\n", "import time\nfrom urllib.parse import quote\n")

helper = '''

def _content_disposition(disposition: str, filename: str) -> str:
    name = filename or "download"
    if name.isascii():
        safe = name.replace("\\\\", "\\\\\\\\").replace(\'"\', \'\\\\"\')
        return f\'{disposition}; filename="{safe}"\'
    encoded = quote(name, safe="")
    return f"{disposition}; filename=\\"download\\"; filename*=UTF-8''{encoded}"

'''
text = text.replace("\ndef _only(body: dict", helper + "\ndef _only(body: dict")
text = re.sub(
    r'"Content-Disposition":[^\n]+',
    '"Content-Disposition": _content_disposition(disposition, row.filename or asset_id),',
    text,
    count=1,
)
compile(text, str(app), "exec")
app.write_text(text)
print("fixed", app)
