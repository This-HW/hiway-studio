"""Unit B: OCR engine integration (QC text_ocr_match / text_artifact).

Real-engine tests use rapidocr_onnxruntime with the Korean PP-OCRv5 mobile
recognizer (installed in the project venv). Synthetic fixtures are generated
with PIL at test time. Check-logic edge cases (low-confidence detections,
engine errors) use monkeypatched recognize() for determinism.
"""
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.media import ocr
from app.services.qc_orchestrator import (
    QCContext,
    _check_text_artifact,
    _check_text_ocr_match,
)

CJK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
needs_cjk = pytest.mark.skipif(
    not Path(CJK_FONT).exists(), reason="Noto Sans CJK font not available"
)
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not available"
)
needs_engine = pytest.mark.skipif(not ocr.AVAILABLE, reason="OCR engine not installed")


def _text_image(path: Path, lines: list[str], font_size: int = 48) -> Path:
    font = (
        ImageFont.truetype(CJK_FONT, font_size)
        if Path(CJK_FONT).exists()
        else ImageFont.load_default()
    )
    img = Image.new("RGB", (900, 120 * len(lines) + 60), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((40, 30 + i * 120), line, font=font, fill="black")
    img.save(path)
    return path


@pytest.fixture(scope="module")
def en_image(tmp_path_factory):
    return _text_image(tmp_path_factory.mktemp("ocr") / "en.png", ["Hello World"])


@pytest.fixture(scope="module")
def ko_image(tmp_path_factory):
    return _text_image(
        tmp_path_factory.mktemp("ocr") / "ko.png", ["안녕하세요 자막 테스트"]
    )


@pytest.fixture(scope="module")
def blank_image(tmp_path_factory):
    p = tmp_path_factory.mktemp("ocr") / "blank.png"
    Image.new("RGB", (400, 200), "white").save(p)
    return p


def _shot(intended_text=None):
    return SimpleNamespace(id=uuid.uuid4(), intended_text=intended_text)


def _ctx(shots, media_pairs):
    return QCContext(
        timeline=None,
        preset=None,
        shots=shots,
        shot_media={s.id: str(p) for s, p in media_pairs},
    )


# ---------------------------------------------------------------------------
# recognize()
# ---------------------------------------------------------------------------


@needs_engine
def test_recognize_result_schema(en_image):
    hits = ocr.recognize(str(en_image))
    assert hits, "expected detections on text image"
    for h in hits:
        assert set(h) == {"text", "confidence", "at_sec"}
        assert isinstance(h["text"], str) and h["text"].strip()
        assert 0.0 <= h["confidence"] <= 1.0
        assert h["at_sec"] == 0.0


@needs_engine
def test_recognize_english(en_image):
    texts = [h["text"] for h in ocr.recognize(str(en_image))]
    assert any("Hello World" in t for t in texts)


@needs_engine
@needs_cjk
def test_recognize_korean(ko_image):
    hits = ocr.recognize(str(ko_image))
    texts = " ".join(h["text"] for h in hits)
    assert "안녕하세요" in texts
    assert any(h["confidence"] >= 0.9 for h in hits if "안녕하세요" in h["text"])


@needs_engine
def test_recognize_blank_image(blank_image):
    assert ocr.recognize(str(blank_image)) == []


@needs_engine
def test_recognize_missing_file_raises_predictable_error(tmp_path):
    with pytest.raises(ocr.OCRError):
        ocr.recognize(str(tmp_path / "nope.png"))


@needs_engine
@needs_ffmpeg
@needs_cjk
def test_recognize_video_samples_per_interval(ko_image, tmp_path):
    mp4 = tmp_path / "ko.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(ko_image),
         "-t", "3", "-pix_fmt", "yuv420p", str(mp4)],
        check=True,
    )
    hits = ocr.recognize(str(mp4), sample_interval_sec=1.0)
    at_secs = sorted({h["at_sec"] for h in hits})
    assert at_secs == [0.0, 1.0, 2.0]
    assert any("안녕하세요" in h["text"] for h in hits)


# ---------------------------------------------------------------------------
# text_ocr_match
# ---------------------------------------------------------------------------


@needs_engine
def test_text_ocr_match_pass(en_image):
    shot = _shot("Hello World")
    r = _check_text_ocr_match(_ctx([shot], [(shot, en_image)]))
    assert r.status == "pass" and not r.stub, r


@needs_engine
def test_text_ocr_match_fail(en_image):
    shot = _shot("Goodbye Moon")
    r = _check_text_ocr_match(_ctx([shot], [(shot, en_image)]))
    assert r.status == "fail", r
    assert "mismatch" in r.details


def test_text_ocr_match_no_intended_text_pass():
    r = _check_text_ocr_match(_ctx([_shot(None)], {}))
    assert r.status == "pass" and not r.stub


def test_text_ocr_match_engine_absent_is_stub(monkeypatch):
    monkeypatch.setattr(ocr, "AVAILABLE", False)
    r = _check_text_ocr_match(_ctx([_shot("Hello")], {}))
    assert r.status == "pass" and r.stub is True


@needs_engine
def test_text_ocr_match_ocr_error_does_not_fail_gate(monkeypatch, en_image):
    def boom(path):
        raise ocr.OCRError("simulated engine failure")

    monkeypatch.setattr(ocr, "recognize", boom)
    shot = _shot("Hello World")
    r = _check_text_ocr_match(_ctx([shot], [(shot, en_image)]))
    assert r.status == "pass", r  # infra failure is not a text mismatch
    assert "no verifiable media" in r.details or "ocr error" in r.details


@needs_engine
def test_text_ocr_match_shot_without_media_is_unverifiable():
    shot = _shot("Hello World")
    r = _check_text_ocr_match(_ctx([shot], []))
    assert r.status == "pass", r
    assert "no verifiable media" in r.details


# ---------------------------------------------------------------------------
# text_artifact
# ---------------------------------------------------------------------------


@needs_engine
def test_text_artifact_pass_on_clean_image(blank_image):
    shot = _shot(None)
    r = _check_text_artifact(_ctx([shot], [(shot, blank_image)]))
    assert r.status == "pass" and not r.stub, r


def test_text_artifact_engine_absent_is_stub(monkeypatch):
    monkeypatch.setattr(ocr, "AVAILABLE", False)
    r = _check_text_artifact(_ctx([_shot(None)], {}))
    assert r.status == "pass" and r.stub is True


def test_text_artifact_warns_on_low_confidence_text(monkeypatch):
    def fake_recognize(path):
        return [{"text": "깨진듯한글자", "confidence": 0.31, "at_sec": 1.0}]

    monkeypatch.setattr(ocr, "recognize", fake_recognize)
    monkeypatch.setattr(ocr, "AVAILABLE", True)
    shot = _shot(None)
    r = _check_text_artifact(_ctx([shot], [(shot, "/tmp/fake.png")]))
    assert r.status == "warn", r
    assert not r.stub


def test_text_artifact_ignores_confident_text(monkeypatch):
    def fake_recognize(path):
        return [{"text": "깨끗한 텍스트", "confidence": 0.95, "at_sec": 0.0}]

    monkeypatch.setattr(ocr, "recognize", fake_recognize)
    monkeypatch.setattr(ocr, "AVAILABLE", True)
    shot = _shot(None)
    r = _check_text_artifact(_ctx([shot], [(shot, "/tmp/fake.png")]))
    assert r.status == "pass", r


def test_text_artifact_skips_shots_with_intended_text(monkeypatch):
    def fake_recognize(path):
        return [{"text": "???", "confidence": 0.2, "at_sec": 0.0}]

    monkeypatch.setattr(ocr, "recognize", fake_recognize)
    monkeypatch.setattr(ocr, "AVAILABLE", True)
    shot = _shot("예상된 자막")
    r = _check_text_artifact(_ctx([shot], [(shot, "/tmp/fake.png")]))
    assert r.status == "pass", r  # expected text is not an artifact
