import builtins
import zipfile
from pathlib import Path

import pytest
from hyperrag.parser import MinerUParser


@pytest.mark.unit
def test_missing_pptx_import_names_the_required_distribution(monkeypatch):
    real_import = builtins.__import__

    def import_without_pptx(name, *args, **kwargs):
        if name == "pptx":
            raise ImportError("pptx unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_pptx)

    with pytest.raises(RuntimeError, match="pypptx-with-oxml"):
        MinerUParser()._collect_slide_notes_map(Path("demo.pptx"))


@pytest.mark.unit
def test_merge_speaker_notes_appends_without_duplicates():
    merged = MinerUParser._merge_speaker_notes("existing note", "new transcript")
    assert merged == "existing note\n\nnew transcript"

    merged_again = MinerUParser._merge_speaker_notes(merged, "new transcript")
    assert merged_again == merged


@pytest.mark.unit
def test_compose_visual_caption_with_speaker_notes():
    assert (
        MinerUParser._compose_visual_caption_with_speaker_notes("", "  hello   world  ")
        == "#speaker notes: hello world"
    )
    assert (
        MinerUParser._compose_visual_caption_with_speaker_notes("base", "note")
        == "base\n#speaker notes: note"
    )


@pytest.mark.unit
def test_build_libreoffice_convert_targets_for_pptx_notes_export():
    targets = MinerUParser._build_libreoffice_convert_targets(
        Path("demo.pptx"), export_notes_pages=True
    )
    assert targets[0].startswith("pdf:impress_pdf_Export:")
    assert "pdf" in targets

    no_notes_targets = MinerUParser._build_libreoffice_convert_targets(
        Path("demo.pptx"), export_notes_pages=False
    )
    assert no_notes_targets == ["pdf"]


@pytest.mark.unit
def test_normalize_zip_target_uses_source_part_directory():
    # Target in slide rels should be resolved relative to slide1.xml, not slide1.xml.rels.
    resolved = MinerUParser._normalize_zip_target(
        "ppt/slides/slide1.xml", "../media/media1.wav"
    )
    assert resolved == "ppt/media/media1.wav"


@pytest.mark.unit
def test_extract_pptx_embedded_media_deduplicates_same_media_per_slide(tmp_path):
    pptx_path = tmp_path / "dup_media.pptx"

    rels_xml = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">
  <Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio\" Target=\"../media/media1.wav\"/>
  <Relationship Id=\"rId2\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/audio\" Target=\"../media/media1.wav\"/>
</Relationships>
"""

    with zipfile.ZipFile(pptx_path, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", "<p:sld xmlns:p='p'></p:sld>")
        zf.writestr("ppt/slides/_rels/slide1.xml.rels", rels_xml)
        zf.writestr("ppt/media/media1.wav", b"fake-wav")

    extract_dir = tmp_path / "extract"
    result = MinerUParser._extract_pptx_embedded_media(pptx_path, extract_dir)

    assert 0 in result
    assert len(result[0]) == 1
    assert result[0][0].exists()


@pytest.mark.unit
def test_resolve_whisper_client_kwargs_prefers_runtime_then_injected():
    parser = MinerUParser(
        asr_model_func={"base_url": "http://from-handler/v1", "api_key": "from-handler-key"}
    )

    kwargs = parser._resolve_whisper_client_kwargs({})
    assert kwargs["base_url"] == "http://from-handler/v1"
    assert kwargs["api_key"] == "from-handler-key"

    kwargs2 = parser._resolve_whisper_client_kwargs(
        {"asr_base_url": "http://from-runtime/v1", "asr_api_key": "from-runtime-key"}
    )
    assert kwargs2["base_url"] == "http://from-runtime/v1"
    assert kwargs2["api_key"] == "from-runtime-key"


@pytest.mark.unit
def test_prepare_pptx_media_transcript_notes_writes_augmented_notes(tmp_path, monkeypatch):
    pptx = pytest.importorskip("pptx")

    prs = pptx.Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    src_pptx = tmp_path / "demo.pptx"
    prs.save(src_pptx)

    parser = MinerUParser()
    fake_media = tmp_path / "media.mp3"
    fake_media.write_bytes(b"fake")

    monkeypatch.setattr(
        parser,
        "_extract_pptx_embedded_media",
        lambda *_args, **_kwargs: {0: [fake_media]},
    )

    def _fake_convert(media_path: Path, output_audio_path: Path) -> None:
        output_audio_path.parent.mkdir(parents=True, exist_ok=True)
        output_audio_path.write_bytes(b"fake wav")

    monkeypatch.setattr(parser, "_convert_media_to_asr_audio", _fake_convert)
    monkeypatch.setattr(
        parser,
        "_transcribe_audio_file_with_whisper_api",
        lambda **_kwargs: "transcript from embedded media",
    )

    context = parser._prepare_pptx_media_transcript_notes(
        doc_id="doc-1",
        doc_path=src_pptx,
        mineru_dir=tmp_path / "mineru_cache",
        parser_params={"ppt_media_asr_enabled": True, "lang": "zh"},
    )

    assert context["augmented_pptx_path"]
    assert Path(context["augmented_pptx_path"]).exists()
    assert context["slide_notes_map"][0].startswith("transcript from embedded media")

    updated_prs = pptx.Presentation(context["augmented_pptx_path"])
    notes_text = updated_prs.slides[0].notes_slide.notes_text_frame.text
    assert "transcript from embedded media" in notes_text


@pytest.mark.unit
def test_convert_media_to_asr_audio_uses_first_audio_track(monkeypatch, tmp_path):
    media = tmp_path / "demo.mp4"
    media.write_bytes(b"fake")
    out = tmp_path / "out.wav"

    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("hyperrag.parser.subprocess.run", _fake_run)
    MinerUParser._convert_media_to_asr_audio(media, out)

    cmd = captured["cmd"]
    assert "-map" in cmd
    idx = cmd.index("-map")
    assert cmd[idx + 1] == "0:a:0"


@pytest.mark.unit
def test_inject_speaker_notes_textboxes_for_pdf(tmp_path):
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    src = tmp_path / "src.pptx"
    dst = tmp_path / "dst.pptx"
    prs.save(src)

    injected = MinerUParser._inject_speaker_notes_textboxes_for_pdf(
        src_pptx_path=src,
        output_pptx_path=dst,
        slide_notes_map={0: "hello speaker notes"},
    )
    assert injected is True
    assert dst.exists()

    prs2 = pptx.Presentation(str(dst))
    texts = []
    for shp in prs2.slides[0].shapes:
        if hasattr(shp, "text"):
            texts.append(str(getattr(shp, "text", "") or ""))
    all_text = "\n".join(texts)
    assert "#speaker notes: hello speaker notes" in all_text


@pytest.mark.unit
def test_run_mineru_on_document_passes_notes_export_flag_for_ppt(monkeypatch, tmp_path):
    parser = MinerUParser()
    pptx_path = tmp_path / "demo.pptx"
    pptx_path.write_bytes(b"fake pptx")

    captured = {}

    def _fake_convert(
        doc_path: Path,
        output_dir: Path,
        export_notes_pages: bool = False,
        slide_notes_map=None,
    ) -> Path:
        captured["export_notes_pages"] = export_notes_pages
        captured["slide_notes_map"] = slide_notes_map
        pdf_path = output_dir / "demo.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"pdf")
        return pdf_path

    monkeypatch.setattr(parser, "_convert_office_to_pdf", _fake_convert)
    monkeypatch.setattr(parser, "_run_mineru_command", lambda **_kwargs: None)

    parser._run_mineru_on_document(
        doc_path=pptx_path,
        output_dir=tmp_path,
        method="auto",
        lang="ch",
        export_notes_pages=True,
    )

    assert captured["export_notes_pages"] is True
    assert captured["slide_notes_map"] is None


@pytest.mark.unit
def test_convert_office_to_pdf_accepts_overlay_generated_pdf_name(monkeypatch, tmp_path):
    doc = tmp_path / "demo.pptx"
    doc.write_bytes(b"pptx")
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_workdir = tmp_path / "temp_work"
    temp_workdir.mkdir(parents=True, exist_ok=True)

    class _FakeTempDir:
        def __enter__(self):
            return str(temp_workdir)

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("hyperrag.parser.tempfile.TemporaryDirectory", _FakeTempDir)

    def _fake_inject(src_pptx_path, output_pptx_path, slide_notes_map):
        output_pptx_path.parent.mkdir(parents=True, exist_ok=True)
        output_pptx_path.write_bytes(b"overlay")
        return True

    monkeypatch.setattr(
        MinerUParser,
        "_inject_speaker_notes_textboxes_for_pdf",
        staticmethod(_fake_inject),
    )

    def _fake_run(cmd, **kwargs):
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        src = Path(cmd[-1])
        (outdir / f"{src.stem}.pdf").write_bytes(b"pdf")

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr("hyperrag.parser.subprocess.run", _fake_run)

    final_pdf = MinerUParser._convert_office_to_pdf(
        doc_path=doc,
        output_dir=output_dir,
        export_notes_pages=True,
        slide_notes_map={0: "notes"},
    )

    assert final_pdf == output_dir / "demo.pdf"
    assert final_pdf.exists()


@pytest.mark.unit
def test_convert_office_to_pdf_normalizes_gbk_text_before_libreoffice(monkeypatch, tmp_path):
    doc = tmp_path / "demo.txt"
    doc.write_bytes("中文内容\n第二行".encode("gbk"))
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_workdir = tmp_path / "temp_work"
    temp_workdir.mkdir(parents=True, exist_ok=True)

    class _FakeTempDir:
        def __enter__(self):
            return str(temp_workdir)

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("hyperrag.parser.tempfile.TemporaryDirectory", _FakeTempDir)

    def _fake_run(cmd, **kwargs):
        convert_input = Path(cmd[-1])
        assert convert_input != doc
        assert convert_input.read_text(encoding="utf-8") == "中文内容\n第二行"

        outdir = Path(cmd[cmd.index("--outdir") + 1])
        (outdir / f"{convert_input.stem}.pdf").write_bytes(b"pdf")

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr("hyperrag.parser.subprocess.run", _fake_run)

    final_pdf = MinerUParser._convert_office_to_pdf(doc_path=doc, output_dir=output_dir)

    assert final_pdf == output_dir / "demo.pdf"
    assert final_pdf.exists()


@pytest.mark.unit
def test_convert_office_to_pdf_replaces_readonly_existing_target(monkeypatch, tmp_path):
    doc = tmp_path / "demo.pptx"
    doc.write_bytes(b"pptx")
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_target = output_dir / "demo.pdf"
    existing_target.write_bytes(b"old")
    existing_target.chmod(0o444)
    temp_workdir = tmp_path / "temp_work"
    temp_workdir.mkdir(parents=True, exist_ok=True)

    class _FakeTempDir:
        def __enter__(self):
            return str(temp_workdir)

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("hyperrag.parser.tempfile.TemporaryDirectory", _FakeTempDir)

    def _fake_run(cmd, **kwargs):
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        src = Path(cmd[-1])
        (outdir / f"{src.stem}.pdf").write_bytes(b"new")

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr("hyperrag.parser.subprocess.run", _fake_run)

    final_pdf = MinerUParser._convert_office_to_pdf(doc_path=doc, output_dir=output_dir)

    assert final_pdf == existing_target
    assert final_pdf.read_bytes() == b"new"
