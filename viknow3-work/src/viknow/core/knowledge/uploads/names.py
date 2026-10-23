"""Display-name allocation for knowledge uploads."""

from __future__ import annotations

from pathlib import Path


def allocate_display_name(filename: str, existing_names: set[str]) -> str:
    """Return filename, or ``stem（n）.ext`` when conflicting with existing names."""

    path = Path(filename)
    stem = path.stem or "file"
    suffix = path.suffix
    candidate = f"{stem}{suffix}"
    if candidate not in existing_names:
        return candidate
    index = 1
    while True:
        candidate = f"{stem}（{index}）{suffix}"
        if candidate not in existing_names:
            return candidate
        index += 1


def normalize_extension(filename: str) -> str:
    return Path(filename).suffix.lower()
