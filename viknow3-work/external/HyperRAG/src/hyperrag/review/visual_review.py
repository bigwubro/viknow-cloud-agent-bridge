"""Sample and describe original PDF/PPT pages for retention review."""

import asyncio
import inspect
import logging
import math
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import SourceInformationUnit

logger = logging.getLogger(__name__)


class VisualSourceEnricher:
    def __init__(
        self,
        vision_func: Callable[..., Any] | None,
        *,
        sample_ratio: float = 0.25,
        max_concurrent: int = 4,
    ):
        self.vision_func = vision_func
        self.sample_ratio = max(0.0, min(1.0, sample_ratio))
        self.max_concurrent = max(1, max_concurrent)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    async def enrich(
        self, source_path: Path, units: list[SourceInformationUnit]
    ) -> list[SourceInformationUnit]:
        visual_units = [unit for unit in units if unit.kind in {"visual_page", "visual_slide"}]
        if not visual_units or self.vision_func is None or self.sample_ratio <= 0:
            return units

        sampled_ids = self._sample_ids(visual_units)
        with tempfile.TemporaryDirectory(prefix="hyperrag-review-") as temp_dir:
            try:
                pdf_path = await self._to_pdf(source_path, Path(temp_dir))
                page_images = await asyncio.to_thread(
                    self._render_pdf, pdf_path, Path(temp_dir) / "pages"
                )
            except Exception as exc:
                logger.warning(f"Visual source rendering failed for {source_path}: {exc}")
                return units

            await asyncio.gather(
                *(
                    self._enrich_unit(unit, page_images)
                    for unit in visual_units
                    if unit.unit_id in sampled_ids
                )
            )
        return units

    def _sample_ids(self, units: list[SourceInformationUnit]) -> set[str]:
        sample_count = max(1, math.ceil(len(units) * self.sample_ratio))
        prioritized = sorted(
            units,
            key=lambda unit: (
                not any(
                    risk in {"connector_semantics", "animation_semantics"}
                    for risk in unit.risk_codes
                ),
                unit.unit_id,
            ),
        )
        return {unit.unit_id for unit in prioritized[:sample_count]}

    async def _enrich_unit(self, unit: SourceInformationUnit, page_images: dict[int, Path]) -> None:
        page = unit.locator.get("page")
        if page is None and isinstance(unit.locator.get("slide"), int):
            page = int(unit.locator["slide"]) - 1
        image_path = page_images.get(int(page)) if page is not None else None
        if image_path is None:
            unit.risk_codes.append("visual_render_missing")
            return
        try:
            async with self._semaphore:
                result = self.vision_func(str(image_path), self._prompt())
                if inspect.isawaitable(result):
                    result = await result
            claims = str(result or "").strip()
            if not claims:
                # Retry once for transient empty responses from remote multimodal services.
                async with self._semaphore:
                    retry_result = self.vision_func(str(image_path), self._prompt())
                    if inspect.isawaitable(retry_result):
                        retry_result = await retry_result
                claims = str(retry_result or "").strip()
            if not claims:
                raise ValueError("vision judge returned empty claims")
            unit.content = claims
            unit.reviewable = True
            unit.risk_codes = [
                risk
                for risk in unit.risk_codes
                if risk not in {"visual_page_requires_review", "visual_slide_requires_review"}
            ]
        except Exception as exc:
            detail = str(exc).strip().replace("\n", " ")[:160]
            if detail:
                unit.risk_codes.append(f"visual_judge_failed:{type(exc).__name__}:{detail}")
            else:
                unit.risk_codes.append(f"visual_judge_failed:{type(exc).__name__}")

    @staticmethod
    async def _to_pdf(source_path: Path, temp_dir: Path) -> Path:
        if source_path.suffix.lower() == ".pdf":
            return source_path
        if source_path.suffix.lower() != ".pptx":
            raise ValueError(f"visual review does not support {source_path.suffix}")
        from hyperrag.parser import MinerUParser

        return await asyncio.to_thread(MinerUParser._convert_office_to_pdf, source_path, temp_dir)

    @staticmethod
    def _render_pdf(pdf_path: Path, output_dir: Path) -> dict[int, Path]:
        import pypdfium2 as pdfium

        output_dir.mkdir(parents=True, exist_ok=True)
        document = pdfium.PdfDocument(str(pdf_path))
        images: dict[int, Path] = {}
        try:
            for page_index in range(len(document)):
                page = document[page_index]
                bitmap = page.render(scale=1.5)
                try:
                    image_path = output_dir / f"page-{page_index}.jpg"
                    bitmap.to_pil().convert("RGB").save(image_path, format="JPEG")
                    images[page_index] = image_path
                finally:
                    bitmap.close()
                    page.close()
        finally:
            document.close()
        return images

    @staticmethod
    def _prompt() -> str:
        return (
            "提取这页原始文档中可验证的视觉语义信息，重点关注箭头或连接关系、流程顺序、"
            "形状编码、图表趋势、表格结构和文本。不要推测不可见内容。"
            "用简洁的事实列表输出，每条事实应可与文档解析结果逐项对照。"
        )
