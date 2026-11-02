import asyncio

from hyperrag.image_enhancer import ImageEnhancer
from hyperrag.schema import Asset, Segment


async def _fake_vision(_image_path: str, _prompt: str) -> str:
    return "这是视觉描述"


def test_image_enhancer_ppt_visual_moves_speaker_notes_to_tail_once(tmp_path):
    method_dir = tmp_path / "ppt" / "vlm"
    images_dir = method_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "p0.png").write_bytes(b"fake-image")
    (method_dir / "doc1_content_list.json").write_text("[]", encoding="utf-8")

    enhancer = ImageEnhancer(vision_model_func=_fake_vision, max_concurrent_requests=1)

    asset = Asset(
        doc_id="doc1",
        source_uri="/tmp/doc1.pptx",
        modality="document",
        segments_count=1,
        title="doc1",
        language="zh",
        extra={
            "mineru_output_dir": str(method_dir.parent.parent),
            "content_list_file": str(method_dir / "doc1_content_list.json"),
        },
    )
    seg = Segment(
        segment_id="s1",
        doc_id="doc1",
        order=0,
        type="image",
        text="#speaker notes: 备注甲 备注乙",
        location={"page": 0},
        extra={
            "source": "ppt_visual",
            "img_path": "images/p0.png",
            "caption": "#speaker notes: 备注甲 备注乙",
        },
    )

    out = asyncio.run(enhancer.enhance(asset, [seg]))
    assert len(out) == 1
    text = out[0].text

    assert "[视觉要点]\n这是视觉描述" in text
    assert text.count("#speaker notes:") == 1
    assert text.endswith("#speaker notes: 备注甲 备注乙")


def test_image_enhancer_non_visual_keeps_original_merge_order(tmp_path):
    method_dir = tmp_path / "pdf" / "vlm"
    images_dir = method_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "p0.png").write_bytes(b"fake-image")
    (method_dir / "doc2_content_list.json").write_text("[]", encoding="utf-8")

    enhancer = ImageEnhancer(vision_model_func=_fake_vision, max_concurrent_requests=1)

    asset = Asset(
        doc_id="doc2",
        source_uri="/tmp/doc2.pdf",
        modality="document",
        segments_count=1,
        title="doc2",
        language="zh",
        extra={
            "mineru_output_dir": str(method_dir.parent.parent),
            "content_list_file": str(method_dir / "doc2_content_list.json"),
        },
    )
    seg = Segment(
        segment_id="s2",
        doc_id="doc2",
        order=0,
        type="image",
        text="原始内容",
        location={"page": 0},
        extra={"source": "minerU", "img_path": "images/p0.png"},
    )

    out = asyncio.run(enhancer.enhance(asset, [seg]))
    assert len(out) == 1
    assert out[0].text == "原始内容\n[视觉要点]\n这是视觉描述"


def test_image_enhancer_resolves_relative_image_path_from_content_list_dir(tmp_path):
    captured = {}

    async def _capture_vision(image_path: str, _prompt: str) -> str:
        captured["image_path"] = image_path
        return "这是视觉描述"

    method_dir = tmp_path / "mini_lam" / "vlm"
    images_dir = method_dir / "images"
    images_dir.mkdir(parents=True)
    image_path = images_dir / "p0.jpg"
    image_path.write_bytes(b"fake-image")
    content_list_file = method_dir / "mini_lam_content_list.json"
    content_list_file.write_text("[]", encoding="utf-8")

    enhancer = ImageEnhancer(vision_model_func=_capture_vision, max_concurrent_requests=1)
    asset = Asset(
        doc_id="mini_lam_pdf_001",
        source_uri="/tmp/mini_lam.pdf",
        modality="document",
        segments_count=1,
        title="mini_lam",
        language="zh",
        extra={
            "mineru_output_dir": str(tmp_path),
            "content_list_file": str(content_list_file),
        },
    )
    seg = Segment(
        segment_id="s3",
        doc_id="mini_lam_pdf_001",
        order=0,
        type="image",
        text="原始内容",
        location={"page": 0},
        extra={"source": "minerU", "img_path": "images/p0.jpg"},
    )

    out = asyncio.run(enhancer.enhance(asset, [seg]))

    assert len(out) == 1
    assert captured["image_path"] == str(image_path)
    assert out[0].text == "原始内容\n[视觉要点]\n这是视觉描述"
