"""Bind parsed table cells to their original, identity-matched evidence location."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from typing import Any


class _TableCells(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw_rows: list[list[tuple[str, int, int]]] = []
        self.cells: list[tuple[str, int, int]] = []
        self.cell: list[str] | None = None
        self.rowspan = 1
        self.colspan = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"td", "th"}:
            self.cell = []
            attributes = dict(attrs)
            self.rowspan = int(attributes.get("rowspan") or 1)
            self.colspan = int(attributes.get("colspan") or 1)
            if self.rowspan < 1 or not 1 <= self.colspan <= 1000:
                raise ValueError("invalid parsed table cell span")
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None:
            self.cells.append((" ".join("".join(self.cell).split()), self.rowspan, self.colspan))
            self.cell = None
        elif tag == "tr":
            self.raw_rows.append(self.cells)
            self.cells = []

    @property
    def rows(self) -> list[str]:
        grid: dict[tuple[int, int], str] = {}
        result = []
        for row_index, cells in enumerate(self.raw_rows):
            column = 0
            for text, rowspan, colspan in cells:
                while (row_index, column) in grid:
                    column += 1
                for row in range(row_index, min(row_index + rowspan, len(self.raw_rows))):
                    for col in range(column, column + colspan):
                        if (row, col) in grid:
                            raise ValueError("overlapping parsed table cells")
                        grid[row, col] = text
                column += colspan
            width = max((col for row, col in grid if row == row_index), default=-1) + 1
            result.append(" | ".join(grid.get((row_index, col), "") for col in range(width)))
        return result


def bind_table_sources(
    chunk: dict[str, Any], snapshot: dict[str, Any], *, snapshot_directory: Path
) -> dict[str, Any]:
    """Use parsed cells, never retrieval summaries, for matching table locations.

    Persisted locations can contain old visual descriptions while the search
    index contains recovered table cells. Resolve original cells on read without
    rewriting shared storage or attributing a whole chunk to an arbitrary box.
    """
    metadata = chunk.get("metadata") or {}
    asset_id = metadata.get("asset_id") or chunk.get("full_doc_id")
    if metadata.get("edited") or (snapshot.get("asset") or {}).get("doc_id") != asset_id:
        return chunk
    image_url = str(metadata.get("image_url") or "")
    if not image_url:
        return chunk
    snapshot_directory = snapshot_directory.resolve()
    image_path = (snapshot_directory / image_url).resolve()
    if not image_path.is_relative_to(snapshot_directory):
        return chunk
    locations = []
    for location in metadata.get("locations") or []:
        segment = (snapshot.get("seg_map") or {}).get(location.get("segment_id")) or {}
        source_location = segment.get("location") or {}
        extra = segment.get("extra") or {}
        body = extra.get("table_body")
        matches = (
            segment.get("type") == "table"
            and segment.get("segment_id") == location.get("segment_id")
            and segment.get("doc_id") == asset_id
            and location.get("page") is not None
            and location.get("bbox")
            and source_location.get("page") == location["page"]
            and source_location.get("bbox") == location["bbox"]
            and extra.get("img_path")
            and (snapshot_directory / extra["img_path"]).resolve() == image_path
        )
        if matches and isinstance(body, str) and body.strip():
            parser = _TableCells()
            parser.feed(body)
            parser.close()
            if parser.rows:
                location = {
                    **location,
                    "text": "\n".join(parser.rows),
                    "text_source": "parsed_table_body",
                }
        locations.append(location)
    return {**chunk, "metadata": {**metadata, "locations": locations}}
