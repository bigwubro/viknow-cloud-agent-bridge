"""
Test the MinerUParser implementation.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from hyperrag.config_loader import HyperRAGConfig
from hyperrag.parser import MinerUParser
from hyperrag.schema import Asset, Segment


def test_parser_initialization():
    """Test that MinerUParser can be initialized without errors."""
    parser = MinerUParser()
    print("✅ MinerUParser initialization successful")

    # Test installation check
    try:
        is_installed = parser.check_installation()
        status = "✅ Installed" if is_installed else "❌ Not installed"
        print(f"📦 MinerU installation status: {status}")
    except Exception as e:
        print(f"⚠️ Error checking MinerU installation: {e}")


def test_parser_methods():
    """Test various parser methods."""
    parser = MinerUParser()

    # Test file format constants
    print(f"📄 Supported OFFICE_FORMATS: {parser.OFFICE_FORMATS}")
    print(f"🖼️ Supported IMAGE_FORMATS: {parser.IMAGE_FORMATS}")
    print(f"📝 Supported TEXT_FORMATS: {parser.TEXT_FORMATS}")

    # Test helper methods with dummy data
    test_content_items = [
        {"type": "text", "text_level": 1, "text": "Test Document Title"},
        {"type": "text", "text": "Some regular text content"},
        {"type": "image", "img_path": "/path/to/image.png", "img_caption": ["Test image"]},
    ]

    # Test title extraction
    title = parser._extract_title(test_content_items)
    print(f"📋 Extracted title: '{title}'")

    # Test language detection
    language = parser._detect_language(test_content_items)
    print(f"🌐 Detected language: '{language}'")

    print("✅ Parser methods test successful")


def test_file_type_detection():
    """Test file type detection logic."""
    parser = MinerUParser()

    # Test file extensions
    test_files = [
        "document.pdf",
        "presentation.pptx",
        "spreadsheet.xlsx",
        "image.png",
        "text.txt",
        "markdown.md",
        "unknown.xyz",
    ]

    for filename in test_files:
        ext = Path(filename).suffix.lower()

        if ext == ".pdf":
            file_type = "PDF"
        elif ext in parser.OFFICE_FORMATS:
            file_type = "Office Document"
        elif ext in parser.IMAGE_FORMATS:
            file_type = "Image"
        elif ext in parser.TEXT_FORMATS:
            file_type = "Text"
        else:
            file_type = "Unknown (will try as PDF)"

        print(f"📎 {filename} → {file_type}")

    print("✅ File type detection test successful")


def test_mineru_command_uses_configured_backend_when_override_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(HyperRAGConfig, "mineru_backend", "vlm-http-client")
    process = MagicMock(returncode=0)

    with patch("hyperrag.parser.subprocess.Popen", return_value=process) as popen:
        MinerUParser._run_mineru_command(
            input_path=tmp_path / "document.pdf",
            output_dir=tmp_path / "output",
            backend=None,
        )

    command = popen.call_args.args[0]
    assert command[command.index("-b") + 1] == "vlm-http-client"


def test_code_segment_supports_mineru_vlm_code_body():
    parser = MinerUParser()
    segment = parser._create_code_segment(
        {
            "type": "code",
            "code_caption": ["测试代码 "],
            "code_body": """{
    "test": "content"
} """,
            "guess_lang": "json",
            "page_idx": 0,
        },
        "doc_seg_0000",
        "doc",
        0,
        [55, 72, 183, 106],
    )

    assert segment.type == "text"
    assert segment.text.startswith("""测试代码

```json""")
    assert '"test": "content"' in segment.text
    assert segment.text.endswith("```")
    assert segment.location["bbox"] == [55, 72, 183, 106]
    assert segment.extra["original_type"] == "code"
    assert segment.extra["code_language"] == "json"
    assert segment.extra["code_caption"] == "测试代码"
    assert (
        segment.extra["code_body"]
        == """{
    "test": "content"
}"""
    )


def test_code_segment_supports_mineru_v2_nested_content():
    parser = MinerUParser()
    segment = parser._create_code_segment(
        {
            "type": "code",
            "content": {
                "code_caption": [{"type": "text", "content": "示例"}],
                "code_content": [{"type": "text", "content": "print(1) "}],
                "code_language": "python",
            },
            "page_idx": 1,
        },
        "doc_seg_0001",
        "doc",
        1,
    )

    assert (
        segment.text
        == """示例

```python
print(1)
```"""
    )
    assert segment.location == {"page": 1}
    assert segment.extra["code_language"] == "python"
    assert segment.extra["code_caption"] == "示例"
    assert segment.extra["code_body"] == "print(1)"


def test_structured_caption_normalization_preserves_provenance():
    segment = Segment(
        segment_id="table-1",
        doc_id="doc-1",
        order=0,
        type="table",
        text="[doc_title]文档[para_title]时序参数[caption]时序表",
        location={"page": 3, "bbox": [1, 2, 3, 4]},
        extra={
            "caption": "[doc_title]文档[para_title]时序参数[caption]时序表",
            "caption_location": {"page": 3, "bbox": [1, 2, 3, 4]},
            "source": "minerU",
        },
    )

    MinerUParser._normalize_structured_caption_segments([segment])

    assert segment.text == "时序参数\n\n时序表"
    assert segment.extra["caption"] == "时序参数\n时序表"
    assert segment.extra["structured_caption"].startswith("[doc_title]文档")
    assert segment.extra["caption_normalized"] is True
    assert segment.location == {"page": 3, "bbox": [1, 2, 3, 4]}
    assert segment.extra["caption_location"]["page"] == 3


def test_parser_drops_placeholder_and_repeated_page_furniture():
    parser = MinerUParser()
    segments = parser._create_segments_from_content(
        [
            {"type": "text", "text": "摘要", "page_idx": 0},
            {"type": "text", "text": "[空白文本]", "page_idx": 0},
            {"type": "header", "text": "第 1 页 产品手册", "page_idx": 0},
            {"type": "footer", "text": "内部资料", "page_idx": 0},
            {"type": "page_number", "text": "1", "page_idx": 0},
            {"type": "text", "text": "正文", "page_idx": 1},
            {"type": "header", "text": "第 2 页 产品手册", "page_idx": 1},
            {"type": "footer", "text": "内部资料", "page_idx": 1},
            {"type": "page_number", "text": "2", "page_idx": 1},
            {"type": "header", "text": "附录", "page_idx": 2},
        ],
        {"pdf_info": []},
        "doc",
    )

    assert [segment.text for segment in segments] == ["摘要", "正文", "附录"]
    assert [
        (segment.segment_id, segment.order, segment.location["page"]) for segment in segments
    ] == [
        ("doc_seg_0000", 0, 0),
        ("doc_seg_0005", 5, 1),
        ("doc_seg_0009", 9, 2),
    ]
    assert parser.validate_parsing(
        Asset(
            doc_id="doc",
            source_uri="test.pdf",
            modality="document",
            file_hash="hash",
            created_at=0,
            segments_count=len(segments),
        ),
        segments,
    )


if __name__ == "__main__":
    print("🧪 Testing MinerUParser Integration...")
    print("=" * 50)

    try:
        test_parser_initialization()
        print()

        test_parser_methods()
        print()

        test_file_type_detection()
        print()

        print("🎉 All tests completed successfully!")

    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback

        traceback.print_exc()
