from pathlib import Path

p = Path("/opt/hyperrag/.venv/lib/python3.12/site-packages/actio/tasks/segment.py")
lines = p.read_text(encoding="utf-8").splitlines()
out: list[str] = []
i = 0
while i < len(lines):
    line = lines[i]
    if line.strip() == 'if parsed.scheme not in {"http", "https"}:' and i + 1 < len(lines) and "upload_mode" in lines[i + 1]:
        out.append(line)
        i += 1
        while i < len(lines):
            inner = lines[i]
            if inner.lstrip().startswith("logger.info(") and "Submitting semantic" in inner:
                break
            out.append("    " + inner if inner.strip() else inner)
            i += 1
        continue
    out.append(line)
    i += 1
p.write_text("\n".join(out) + "\n", encoding="utf-8")
print("indented upload block")
