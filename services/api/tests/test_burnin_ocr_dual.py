"""Burn-in OCR with dual subtitle tracks whose OCR bands overlap (B9, ADR-0013).

`postflight_burnin(..., foreign_mask=)` replaces the other track's glyph
footprint with the subtitle-free reference pixels before OCR'ing this track's
band. Proves, without ffmpeg/libass/OCR engine (synthetic frames, a fake OCR
that reads the glyph "ink" present in the cropped band):
  * overlapping bands no longer mix the other track's text into this track's
    OCR text (the old false fail);
  * a transparent / missing track still fails, also when the other track
    shows the very same text (the other track's text never satisfies it).
The real ffmpeg + libass + rapidocr path is
test_render_postflight.test_dual_track_ocr_ignores_other_track.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from app.media import ocr
from app.services import qc_orchestrator

_W, _H = 640, 360
_GREY = (64, 64, 64)
# Middle track A and bottom track B, both 30% safe-area margin, 32px glyphs:
# A's band is y 70..290 and B's is y 140..360 — each contains the other's line.
_STYLE_A = {"position": "middle", "safe_area_margin_pct": 30.0, "font_size_px": 96}
_STYLE_B = {"position": "bottom", "safe_area_margin_pct": 30.0, "font_size_px": 96}
_BOX_A = (200, 164, 440, 196)  # centred line
_BOX_B = (160, 220, 480, 252)  # baseline at 70% of the height
_INK_A = (255, 255, 0)  # each track's glyph pixels carry their own colour so
_INK_B = (0, 255, 255)  # the fake OCR can tell which line it is "reading"


def _cue(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, start_sec=0.5, end_sec=2.0, style_overrides=None)


def _frame(
    *boxes: tuple[tuple[int, int, int, int], tuple[int, int, int]],
) -> Image.Image:
    img = Image.new("RGB", (_W, _H), _GREY)
    for box, ink in boxes:
        img.paste(ink, box)
    return img


def _mask(box: tuple[int, int, int, int], pad: int = 4) -> Image.Image:
    m = Image.new("L", (_W, _H), 0)
    m.paste(255, (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad))
    return m


def _setup(monkeypatch, tmp_path, *, output: Image.Image, texts: tuple[str, str]):
    """Videos are stand-in files; frames come from `output` / a blank
    reference. OCR returns, top to bottom, the text of every ink colour that
    covers at least 50 px of the cropped band."""
    monkeypatch.setattr(ocr, "AVAILABLE", True)
    out = tmp_path / "out.mp4"
    ref = tmp_path / "ref.mp4"
    out.write_bytes(b"x")
    ref.write_bytes(b"x")
    frames = {str(out): output, str(ref): _frame()}

    def fake_extract(src, at, dst):
        frames[str(src)].save(dst)

    def fake_recognize(path, **kw):
        with Image.open(path) as img:
            band = img.convert("RGB")
        hits = []
        for ink, text in ((_INK_A, texts[0]), (_INK_B, texts[1])):
            rows = [
                y
                for y in range(band.height)
                for x in range(0, band.width, 4)
                if band.getpixel((x, y)) == ink
            ]
            if len(rows) >= 50:
                hits.append((min(rows), text))
        return [{"text": t, "confidence": 0.95, "at_sec": 0.0} for _, t in sorted(hits)]

    monkeypatch.setattr(qc_orchestrator, "_extract_frame_file", fake_extract)
    monkeypatch.setattr(ocr, "recognize", fake_recognize)
    return out, ref


def _check(
    out, ref, text: str, style: dict, foreign_box
) -> qc_orchestrator.CheckResult:
    return qc_orchestrator.postflight_burnin(
        out,
        [_cue(text)],
        reference_path=ref,
        style=style,
        foreign_mask=lambda at, size: _mask(foreign_box),
    )


def test_bands_overlap_in_this_layout():
    a = qc_orchestrator.subtitle_band(_W, _H, _STYLE_A)
    b = qc_orchestrator.subtitle_band(_W, _H, _STYLE_B)
    assert a[1] <= _BOX_B[1] and _BOX_B[3] <= a[3], a
    assert b[1] <= _BOX_A[1] and _BOX_A[3] <= b[3], b


def test_overlapping_bands_both_tracks_present_pass(monkeypatch, tmp_path):
    texts = ("MISSING", "VISIBLE OTHER TRACK")
    out, ref = _setup(
        monkeypatch,
        tmp_path,
        output=_frame((_BOX_A, _INK_A), (_BOX_B, _INK_B)),
        texts=texts,
    )
    a = _check(out, ref, texts[0], _STYLE_A, _BOX_B)
    b = _check(out, ref, texts[1], _STYLE_B, _BOX_A)
    assert a.status == "pass", a.details
    assert b.status == "pass", b.details
    assert a.measured[0]["similarity"] == 1.0
    assert a.measured[0]["scrubbed_px"] > 0


def test_overlapping_bands_without_scrub_is_the_old_false_fail(monkeypatch, tmp_path):
    """Documents the defect: the band of A also reads B's line."""
    texts = ("MISSING", "VISIBLE OTHER TRACK")
    out, ref = _setup(
        monkeypatch,
        tmp_path,
        output=_frame((_BOX_A, _INK_A), (_BOX_B, _INK_B)),
        texts=texts,
    )
    r = qc_orchestrator.postflight_burnin(
        out, [_cue(texts[0])], reference_path=ref, style=_STYLE_A
    )
    assert r.status == "fail"
    assert "scrubbed_px" not in r.measured[0]


@pytest.mark.parametrize(
    "texts",
    [("MISSING", "VISIBLE OTHER TRACK"), ("SAME TEXT", "SAME TEXT")],
    ids=["distinct", "same"],
)
def test_missing_track_still_fails_when_other_track_renders(
    monkeypatch, tmp_path, texts
):
    """Transparent / missing track A (no ink of its own) next to a correct
    track B — even when B shows A's exact text — must fail A's check."""
    out, ref = _setup(
        monkeypatch, tmp_path, output=_frame((_BOX_B, _INK_B)), texts=texts
    )
    a = _check(out, ref, texts[0], _STYLE_A, _BOX_B)
    b = _check(out, ref, texts[1], _STYLE_B, _BOX_A)
    assert a.status == "fail", a.details
    assert a.measured[0]["similarity"] < 0.6
    assert b.status == "pass", b.details


def test_scrub_fills_from_reference_and_rejects_wrong_size():
    img = _frame((_BOX_B, _INK_B))
    fill = _frame()
    assert qc_orchestrator._scrub_foreign(img, None, fill) == 0
    n = qc_orchestrator._scrub_foreign(img, _mask(_BOX_B), fill)
    assert n == (_BOX_B[2] - _BOX_B[0] + 8) * (_BOX_B[3] - _BOX_B[1] + 8)
    assert _INK_B not in {c for _, c in img.getcolors(maxcolors=_W * _H)}
    with pytest.raises(ValueError):
        qc_orchestrator._scrub_foreign(img, Image.new("L", (10, 10), 255), fill)


# --- B9b-r2: sample timing (B9-H1), no ffmpeg ----------------------------------


def _timed(start: float, end: float, text: str = "X") -> SimpleNamespace:
    return SimpleNamespace(text=text, start_sec=start, end_sec=end, style_overrides=None)


def test_ass_time_matches_written_timestamp():
    from app.media import subtitle_render

    for sec in (0.0, 1.201, 1.252, 1.255, 2.002, 2.4, 3599.999):
        q = subtitle_render.ass_time(sec)
        assert subtitle_render._ass_timestamp(sec) == subtitle_render._ass_timestamp(q)
        assert abs(q - sec) <= subtitle_render.ASS_TIME_QUANTUM_SEC / 2 + 1e-9


def test_sample_time_avoids_other_track_ass_boundary():
    """P08: A [0.5, 2.002] next to B [1.252, 2.4]. A's raw midpoint 1.251 is
    within one frame of B's ASS start 1.25; A is sampled where B is certainly
    off screen, B where A is certainly on screen."""
    from app.services import render

    guard = render._sample_guard(24)
    a, b = _timed(0.5, 2.002), _timed(1.252, 2.4)
    (va,), _ = render._visible_cues([a], 2.5, 24)
    (vb,), _ = render._visible_cues([b], 2.5, 24)
    t_a = render._sample_time(va, [([b], {})], guard)
    t_b = render._sample_time(vb, [([a], {})], guard)
    assert abs(t_a - 1.25) >= guard and 0.5 + guard <= t_a <= 2.0 - guard
    assert not render._maybe_shown(b, t_a, guard)
    assert render._maybe_shown(a, t_b, guard)
    assert abs(t_b - 2.0) >= guard


def test_ambiguous_other_cue_counts_as_shown():
    """P07: A [1.201, 1.301] has no time a frame + 1cs away from B's ASS start
    1.25. The raw comparison said B is off screen at 1.251; it now counts as
    shown (its pixels are excluded / scrubbed, never A's evidence)."""
    from app.services import render

    guard = render._sample_guard(24)
    a, b = _timed(1.201, 1.301), _timed(1.252, 2.4)
    (va,), _ = render._visible_cues([a], 2.5, 24)
    t_a = render._sample_time(va, [([b], {})], guard)
    assert 1.20 <= t_a < 1.30
    assert not (b.start_sec <= 1.251 < b.end_sec)  # the old raw decision
    assert render._maybe_shown(b, t_a, guard)
    # Track B is sampled where A (ASS end 1.30) is certainly gone.
    (vb,), _ = render._visible_cues([b], 2.5, 24)
    t_b = render._sample_time(vb, [([a], {})], guard)
    assert not render._maybe_shown(a, t_b, guard)


def test_ocr_uses_cue_sample_sec(monkeypatch, tmp_path):
    texts = ("MISSING", "VISIBLE OTHER TRACK")
    out, ref = _setup(
        monkeypatch,
        tmp_path,
        output=_frame((_BOX_A, _INK_A), (_BOX_B, _INK_B)),
        texts=texts,
    )
    seen: list[float] = []

    def mask(at, size):
        seen.append(at)
        return _mask(_BOX_B)

    cue = _cue(texts[0])
    cue.sample_sec = 0.875
    r = qc_orchestrator.postflight_burnin(
        out, [cue], reference_path=ref, style=_STYLE_A, foreign_mask=mask
    )
    assert r.status == "pass", r.details
    assert seen == [0.875] and r.measured[0]["at_sec"] == 0.88


def test_foreign_mask_error_is_a_failure(monkeypatch, tmp_path):
    texts = ("MISSING", "VISIBLE OTHER TRACK")
    out, ref = _setup(
        monkeypatch,
        tmp_path,
        output=_frame((_BOX_A, _INK_A), (_BOX_B, _INK_B)),
        texts=texts,
    )

    def boom(at, size):
        raise OSError("mask render failed (injected)")

    r = qc_orchestrator.postflight_burnin(
        out, [_cue(texts[0])], reference_path=ref, style=_STYLE_A, foreign_mask=boom
    )
    assert r.status == "fail"
    assert "not verifiable" in r.details

