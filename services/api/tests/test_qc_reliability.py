"""B3 — QC gate reliability (plan 2026-11-09 audit roadmap, D5 / G8 C16, H5, C10).

- lipsync_drift with a dialogue clip no longer crashes (C16).
- Per-check isolation: a raising required check -> fail, a raising advisory
  check -> warn; an errored check never yields pass (H5).
- A failure outside the checks persists the report as overall='fail' in a
  committed transaction (a NEW session sees it) and qc.completed is
  published only after that commit.
- Continuity checks: real SSIM; missing inputs -> warn, never pass (C10).
- text_ocr_match with intended_text but no media -> warn (C10).
- Pre-render encoding_match is "not applicable" and is not counted as pass;
  postflight helpers verify the rendered output (encoding / burn-in).
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db.session as sess_mod
import app.main  # noqa: F401  (registers every model on Base.metadata)
from app.core import defaults
from app.core.config import get_settings
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.media import ocr
from app.services import qc_content, qc_orchestrator
from app.services.qc_orchestrator import CheckResult, QCContext

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/qc.db")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("SSE_BACKEND", "memory")
    get_settings.cache_clear()
    reset_bridge()
    engine = create_engine(f"sqlite:///{tmp_path}/qc.db")
    monkeypatch.setattr(sess_mod, "get_engine", lambda: engine)
    reset_engine()
    sess_mod._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    yield tmp_path
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def _new_session():
    """A brand-new session (fresh connection state) for read-back checks."""
    return sess_mod.get_session_factory()()


def _make_report(*, dialogue_audio: bool = False) -> str:
    from app.models.audio import AudioGeneration, TimelineAudioClip
    from app.models.project import Project
    from app.models.qc import QCReport
    from app.models.timeline import Timeline

    with session_scope() as db:
        project = Project(title="QC reliability")
        db.add(project)
        db.flush()
        timeline = Timeline(project_id=project.id, name="T")
        db.add(timeline)
        db.flush()
        if dialogue_audio:
            audio = AudioGeneration(
                project_id=project.id,
                kind="dialogue",
                text="안녕",
                duration_sec=2.0,
                status="done",
            )
            db.add(audio)
            db.flush()
            db.add(
                TimelineAudioClip(
                    timeline_id=timeline.id,
                    audio_id=audio.id,
                    kind="dialogue",
                    start_sec=0.0,
                    duration_sec=2.0,
                )
            )
        report = QCReport(
            project_id=project.id,
            target_type="timeline",
            target_id=timeline.id,
            overall="pending",
            checks=[],
            timeline_content_version=0,
        )
        db.add(report)
        db.flush()
        return str(report.id)


def _read_report(report_id: str):
    from app.models.qc import QCReport

    s = _new_session()
    try:
        r = s.get(QCReport, uuid.UUID(report_id))
        return SimpleNamespace(
            overall=r.overall,
            checks=list(r.checks or []),
            error_message=r.error_message,
        )
    finally:
        s.close()


def _pass_fn(code):
    return lambda ctx: CheckResult(code, "pass", "ok")


def _all_pass_checks():
    return [(code, _pass_fn(code)) for code, _ in qc_orchestrator.CHECKS]


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *args], check=True, timeout=300
    )


def _pattern_image(path: Path, kind: str, size=(320, 240)) -> Path:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    if kind == "checker":
        step = 20
        for y in range(0, size[1], step):
            for x in range(0, size[0], step):
                if (x // step + y // step) % 2 == 0:
                    d.rectangle([x, y, x + step - 1, y + step - 1], fill="black")
    elif kind == "stripes":
        for x in range(0, size[0], 40):
            d.rectangle([x, 0, x + 19, size[1]], fill=(30, 30, 30))
        d.ellipse([100, 60, 220, 180], fill=(200, 40, 40))
    elif kind == "gradient":
        for x in range(size[0]):
            v = int(255 * x / size[0])
            d.line([(x, 0), (x, size[1])], fill=(v, v, v))
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# C16 — lipsync with a dialogue clip
# ---------------------------------------------------------------------------


def test_lipsync_dialogue_clip_zero_drift_does_not_crash():
    audio = SimpleNamespace(id=uuid.uuid4(), kind="dialogue", duration_sec=2.0)
    clip = SimpleNamespace(duration_sec=2.0)
    ctx = QCContext(
        timeline=None,
        preset=None,
        audio_clips=[{"clip": clip, "audio": audio, "abs": None}],
    )
    r = qc_orchestrator._check_lipsync_drift(ctx)
    assert r.status == "pass", r
    assert r.threshold in (None, defaults.LIPSYNC_WARN_DIFF_SEC)


def test_lipsync_dialogue_clip_drift_warns_with_defaults_threshold():
    audio = SimpleNamespace(id=uuid.uuid4(), kind="dialogue", duration_sec=3.0)
    clip = SimpleNamespace(duration_sec=2.0)
    ctx = QCContext(
        timeline=None,
        preset=None,
        audio_clips=[{"clip": clip, "audio": audio, "abs": None}],
    )
    r = qc_orchestrator._check_lipsync_drift(ctx)
    assert r.status == "warn", r
    assert r.threshold == defaults.LIPSYNC_WARN_DIFF_SEC


def test_run_qc_with_dialogue_clip_reaches_terminal_state(db_env):
    report_id = _make_report(dialogue_audio=True)
    try:
        qc_orchestrator.run_qc(report_id)
    except Exception:  # noqa: BLE001 — the assertion below is the contract
        pass
    report = _read_report(report_id)
    assert report.overall in ("pass", "warn", "fail"), report.overall
    by_code = {c["code"]: c for c in report.checks}
    assert by_code["lipsync_drift"]["status"] == "pass", by_code["lipsync_drift"]


# ---------------------------------------------------------------------------
# H5 — per-check isolation
# ---------------------------------------------------------------------------


def test_required_check_exception_becomes_fail(db_env, monkeypatch):
    def boom(ctx):
        raise RuntimeError("synthetic required-check crash")

    checks = _all_pass_checks()
    checks = [(c, boom if c == "subtitle_timing" else fn) for c, fn in checks]
    monkeypatch.setattr(qc_orchestrator, "CHECKS", checks)
    report_id = _make_report()
    summary = qc_orchestrator.run_qc(report_id)
    assert summary["overall"] == "fail"
    report = _read_report(report_id)
    assert report.overall == "fail"
    assert len(report.checks) == len(checks)  # the other checks still ran
    by_code = {c["code"]: c for c in report.checks}
    st = by_code["subtitle_timing"]
    assert st["status"] == "fail"
    assert st["details"].startswith("CHECK_ERROR: RuntimeError:"), st["details"]
    assert "synthetic required-check crash" in st["details"]


def test_advisory_check_exception_becomes_warn_never_pass(db_env, monkeypatch):
    def boom(ctx):
        raise ValueError("synthetic advisory crash")

    checks = [
        (c, boom if c == "story_fidelity" else fn) for c, fn in _all_pass_checks()
    ]
    monkeypatch.setattr(qc_orchestrator, "CHECKS", checks)
    report_id = _make_report()
    summary = qc_orchestrator.run_qc(report_id)
    assert summary["overall"] == "warn"  # not pass, and advisory never fails
    report = _read_report(report_id)
    assert report.overall == "warn"
    sf = {c["code"]: c for c in report.checks}["story_fidelity"]
    assert sf["status"] == "warn"
    assert sf["details"].startswith("CHECK_ERROR: ValueError:"), sf["details"]


def test_advisory_fail_result_is_downgraded_to_warn():
    # _run_check (the run_qc path) normalizes an advisory "fail" to warn, so
    # the verdict cannot silently become pass (raw _overall semantics for an
    # advisory fail stay as locked by test_phase6b_qc_content).
    r = qc_orchestrator._run_check(
        "story_fidelity", lambda ctx: CheckResult("story_fidelity", "fail", "x"), None
    )
    assert r.status == "warn"
    assert qc_orchestrator._overall([r, CheckResult("audio_peak", "pass")]) == "warn"


# ---------------------------------------------------------------------------
# Outer failure -> committed fail, SSE after commit
# ---------------------------------------------------------------------------


def test_outer_failure_persists_fail_seen_from_new_session(db_env, monkeypatch):
    seen_at_publish: list[str] = []

    class Bridge:
        def publish(self, channel, event, data):
            # Runs at publish time: the fail must already be committed.
            seen_at_publish.append(_read_report(data["qc_report_id"]).overall)

    monkeypatch.setattr(qc_orchestrator, "get_bridge", lambda: Bridge())

    def broken_gather(db, timeline_id):
        raise RuntimeError("context build exploded")

    monkeypatch.setattr(qc_orchestrator, "_gather", broken_gather)
    report_id = _make_report()
    with pytest.raises(RuntimeError):
        qc_orchestrator.run_qc(report_id)
    report = _read_report(report_id)
    assert report.overall == "fail", report.overall
    assert report.error_message and "RuntimeError" in report.error_message
    assert "context build exploded" in report.error_message
    assert seen_at_publish == ["fail"]


def test_completion_event_published_after_commit(db_env, monkeypatch):
    seen_at_publish: list[str] = []

    class Bridge:
        def publish(self, channel, event, data):
            assert event == "qc.completed"
            seen_at_publish.append(_read_report(data["qc_report_id"]).overall)

    monkeypatch.setattr(qc_orchestrator, "get_bridge", lambda: Bridge())
    monkeypatch.setattr(qc_orchestrator, "CHECKS", _all_pass_checks())
    report_id = _make_report()
    qc_orchestrator.run_qc(report_id)
    assert seen_at_publish == ["pass"]


def test_outer_failure_does_not_overwrite_completed_report(db_env, monkeypatch):
    monkeypatch.setattr(qc_orchestrator, "CHECKS", _all_pass_checks())
    report_id = _make_report()
    qc_orchestrator.run_qc(report_id)
    assert (
        qc_orchestrator.fail_report_unexpected(report_id, RuntimeError("late")) is False
    )
    assert _read_report(report_id).overall == "pass"


# ---------------------------------------------------------------------------
# C10 — continuity SSIM
# ---------------------------------------------------------------------------


def test_ssim_identical_and_different(tmp_path):
    a = Image.open(_pattern_image(tmp_path / "a.png", "checker"))
    b = Image.open(_pattern_image(tmp_path / "b.png", "checker"))
    c = Image.open(_pattern_image(tmp_path / "c.png", "gradient"))
    assert qc_orchestrator.ssim(a, b) > 0.99
    assert qc_orchestrator.ssim(a, c) < 0.5


def _cont_ctx(
    tmp_path,
    *,
    still_kind="checker",
    clip_kinds=("checker", "checker"),
    chained=True,
    files_exist=True,
):
    shots, clips, stills, links = [], [], {}, {}
    scene_id = uuid.uuid4()
    for i, kind in enumerate(clip_kinds):
        shot = SimpleNamespace(id=uuid.uuid4(), scene_id=scene_id, intended_text=None)
        shots.append(shot)
        clip_path = tmp_path / f"clip_{i}_{kind}.mp4"
        if files_exist:
            clip_path.write_bytes(b"fake")  # frames are served by the patched extractor
        clips.append(
            {
                "video": SimpleNamespace(id=uuid.uuid4(), shot_id=shot.id, mode="i2v"),
                "abs": clip_path,
                "in_sec": 0.0,
                "out_sec": 2.0,
            }
        )
        still = tmp_path / f"still_{i}.png"
        if files_exist:
            _pattern_image(still, still_kind)
        stills[shot.id] = still
        if chained and i > 0:
            links[shot.id] = shots[i - 1].id
    return QCContext(
        timeline=None,
        preset=None,
        clips=clips,
        shots=shots,
        shot_stills=stills,
        continuity_links=links,
    )


def _patch_frames(monkeypatch, tmp_path):
    def fake_frame(path, at_sec):
        kind = Path(path).stem.split("_")[-1]
        return Image.open(_pattern_image(tmp_path / f"frame_{kind}.png", kind))

    monkeypatch.setattr(qc_orchestrator, "_clip_frame", fake_frame)


def test_continuity_missing_inputs_warn_not_pass(tmp_path):
    ctx = _cont_ctx(tmp_path, files_exist=False)
    for fn in (
        qc_orchestrator._check_continuity_first_frame,
        qc_orchestrator._check_continuity_between_shots,
    ):
        r = fn(ctx)
        assert r.status == "warn", (r.code, r)
        assert r.applicable is True


def test_continuity_without_ffmpeg_warns(tmp_path, monkeypatch):
    from app.media import ffmpeg_ops

    monkeypatch.setenv("FFMPEG_BIN", str(tmp_path / "no-such-ffmpeg"))
    ctx = _cont_ctx(tmp_path)
    for fn in (
        qc_orchestrator._check_continuity_first_frame,
        qc_orchestrator._check_continuity_between_shots,
    ):
        r = fn(ctx)
        assert r.status == "warn", (r.code, r)
    assert ffmpeg_ops.ffmpeg_bin().endswith("no-such-ffmpeg")


def test_continuity_identical_frames_pass(tmp_path, monkeypatch):
    _patch_frames(monkeypatch, tmp_path)
    ctx = _cont_ctx(tmp_path)
    r = qc_orchestrator._check_continuity_first_frame(ctx)
    assert r.status == "pass" and r.stub is False, r
    assert r.threshold == defaults.QC_SSIM_FIRST_FRAME
    r = qc_orchestrator._check_continuity_between_shots(ctx)
    assert r.status == "pass" and r.stub is False, r
    assert r.threshold == defaults.QC_SSIM_BETWEEN_SHOTS


def test_continuity_very_different_frames_warn(tmp_path, monkeypatch):
    _patch_frames(monkeypatch, tmp_path)
    ctx = _cont_ctx(tmp_path, still_kind="gradient", clip_kinds=("checker", "stripes"))
    r = qc_orchestrator._check_continuity_first_frame(ctx)
    assert r.status == "warn" and r.stub is False, r
    r = qc_orchestrator._check_continuity_between_shots(ctx)
    assert r.status == "warn" and r.stub is False, r


def test_continuity_between_shots_unchained_is_not_applicable(tmp_path, monkeypatch):
    _patch_frames(monkeypatch, tmp_path)
    ctx = _cont_ctx(tmp_path, clip_kinds=("checker", "stripes"), chained=False)
    r = qc_orchestrator._check_continuity_between_shots(ctx)
    assert r.applicable is False, r  # nothing chained -> not counted as pass
    assert qc_orchestrator._overall([r]) == "pass"  # ...and does not force warn


@pytest.mark.needs_ffmpeg
def test_continuity_real_frames_ffmpeg(tmp_path):
    checker = _pattern_image(tmp_path / "checker.png", "checker")
    stripes = _pattern_image(tmp_path / "stripes.png", "stripes")
    clips = {}
    for name, img in (("checker", checker), ("stripes", stripes)):
        dst = tmp_path / f"{name}.mp4"
        _ffmpeg(
            "-loop",
            "1",
            "-i",
            str(img),
            "-t",
            "2",
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(dst),
        )
        clips[name] = dst
    scene = uuid.uuid4()
    s0 = SimpleNamespace(id=uuid.uuid4(), scene_id=scene, intended_text=None)
    s1 = SimpleNamespace(id=uuid.uuid4(), scene_id=scene, intended_text=None)

    def ctx_for(clip0, clip1, still0):
        return QCContext(
            timeline=None,
            preset=None,
            shots=[s0, s1],
            clips=[
                {
                    "video": SimpleNamespace(
                        id=uuid.uuid4(), shot_id=s0.id, mode="i2v"
                    ),
                    "abs": clip0,
                    "in_sec": 0.0,
                    "out_sec": 2.0,
                },
                {
                    "video": SimpleNamespace(
                        id=uuid.uuid4(), shot_id=s1.id, mode="i2v"
                    ),
                    "abs": clip1,
                    "in_sec": 0.0,
                    "out_sec": 2.0,
                },
            ],
            shot_stills={s0.id: still0, s1.id: checker},
            continuity_links={s1.id: s0.id},
        )

    same = ctx_for(clips["checker"], clips["checker"], checker)
    assert qc_orchestrator._check_continuity_first_frame(same).status == "pass"
    assert qc_orchestrator._check_continuity_between_shots(same).status == "pass"
    diff = ctx_for(clips["checker"], clips["stripes"], stripes)
    assert qc_orchestrator._check_continuity_first_frame(diff).status == "warn"
    assert qc_orchestrator._check_continuity_between_shots(diff).status == "warn"


# ---------------------------------------------------------------------------
# C10 — text_ocr_match without media
# ---------------------------------------------------------------------------


def test_text_ocr_match_intended_text_without_media_warns(monkeypatch):
    monkeypatch.setattr(ocr, "AVAILABLE", True)
    shot = SimpleNamespace(id=uuid.uuid4(), intended_text="오프닝 타이틀")
    ctx = QCContext(timeline=None, preset=None, shots=[shot], shot_media={})
    r = qc_orchestrator._check_text_ocr_match(ctx)
    assert r.status == "warn", r
    assert "unverified: no media" in r.details


# ---------------------------------------------------------------------------
# encoding_match pre-render / postflight helpers
# ---------------------------------------------------------------------------


def test_pre_render_encoding_match_not_counted_as_pass():
    ctx = QCContext(
        timeline=None,
        preset=SimpleNamespace(resolution="320x240", fps=24, codec="h264"),
    )
    r = qc_orchestrator._check_encoding_match(ctx)
    assert r.applicable is False, r
    assert "verified at render postflight" in r.details
    counts = qc_orchestrator.count_results([r, CheckResult("audio_peak", "warn")])
    assert counts["pass"] == 0 and counts["warn"] == 1 and counts["not_applicable"] == 1
    # a non-applicable check never moves the verdict
    assert (
        qc_orchestrator._overall([CheckResult("x", "fail", applicable=False)]) == "pass"
    )


def test_run_qc_counts_exclude_not_applicable(db_env, monkeypatch):
    checks = [
        (c, qc_orchestrator._check_encoding_match if c == "encoding_match" else fn)
        for c, fn in _all_pass_checks()
    ]
    monkeypatch.setattr(qc_orchestrator, "CHECKS", checks)
    report_id = _make_report()
    summary = qc_orchestrator.run_qc(report_id)
    assert summary["not_applicable_count"] == 1
    assert summary["pass_count"] == len(checks) - 1
    enc = {c["code"]: c for c in _read_report(report_id).checks}["encoding_match"]
    assert enc["applicable"] is False


def test_postflight_encoding_mismatch_fails_with_probe(monkeypatch, tmp_path):
    from app.media import ffmpeg_ops

    out = tmp_path / "out.mp4"
    out.write_bytes(b"x")
    monkeypatch.setattr(
        ffmpeg_ops,
        "probe",
        lambda p: {"width": 160, "height": 120, "fps": 30.0, "video_codec": "hevc"},
    )
    preset = SimpleNamespace(resolution="320x240", fps=24, codec="h264")
    r = qc_orchestrator.postflight_encoding(out, preset)
    assert r.status == "fail" and r.code == "encoding_match", r
    assert "resolution" in r.details and "fps" in r.details and "codec" in r.details


@pytest.mark.needs_ffmpeg
def test_postflight_encoding_real_output(tmp_path):
    out = tmp_path / "out.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=24:duration=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(out),
    )
    ok = SimpleNamespace(resolution="160x120", fps=24, codec="h264")
    bad = SimpleNamespace(resolution="320x240", fps=24, codec="h264")
    assert qc_orchestrator.postflight_encoding(out, ok).status == "pass"
    assert qc_orchestrator.postflight_encoding(out, bad).status == "fail"


def _cue(text, start=0.5, end=2.5):
    return SimpleNamespace(text=text, start_sec=start, end_sec=end, style_overrides=None)


# Review WA-H2: postflight_burnin OCRs only the subtitle band (style position
# + safe-area margin), compares with an ORDER-PRESERVING similarity, and with
# a subtitle-free reference requires the cue text to be ADDED by the burn-in
# (absent from the reference band). Without a reference it never passes.

_BOTTOM_STYLE = {"position": "bottom", "safe_area_margin_pct": 5.0, "font_size_px": 96}


def _patch_burnin_ocr(monkeypatch, tmp_path, *, out_text, ref_text=""):
    """Frames are real (blank) PNGs; OCR answers by which video they came
    from (frame names are out_* / ref_*)."""
    monkeypatch.setattr(ocr, "AVAILABLE", True)

    def fake_extract(src, at, dst):
        Image.new("RGB", (640, 360), "black").save(dst)

    def fake_recognize(path, **kw):
        text = ref_text if Path(path).name.startswith("ref_") else out_text
        if isinstance(text, Exception):
            raise text
        return [{"text": text, "confidence": 0.95, "at_sec": 0.0}] if text else []

    monkeypatch.setattr(qc_orchestrator, "_extract_frame_file", fake_extract)
    monkeypatch.setattr(ocr, "recognize", fake_recognize)
    out = tmp_path / "out.mp4"
    ref = tmp_path / "ref.mp4"
    out.write_bytes(b"x")
    ref.write_bytes(b"x")
    return out, ref


def test_postflight_burnin_logic_pass_with_reference(monkeypatch, tmp_path):
    out, ref = _patch_burnin_ocr(monkeypatch, tmp_path, out_text="DO NOT GO")
    r = qc_orchestrator.postflight_burnin(
        out, [_cue("DO NOT GO")], reference_path=ref, style=_BOTTOM_STYLE)
    assert r.status == "pass", r
    assert r.code == "burnin_ocr"
    assert r.threshold == defaults.RENDER_BURNIN_OCR_MIN_MATCH


def test_postflight_burnin_logic_missing_subtitle_fails(monkeypatch, tmp_path):
    out, ref = _patch_burnin_ocr(monkeypatch, tmp_path, out_text="")
    r = qc_orchestrator.postflight_burnin(out, [_cue("테스트 자막")], reference_path=ref)
    assert r.status == "fail", r


def test_postflight_burnin_logic_reversed_order_fails(monkeypatch, tmp_path):
    out, ref = _patch_burnin_ocr(monkeypatch, tmp_path, out_text="GO DO NOT")
    r = qc_orchestrator.postflight_burnin(out, [_cue("DO NOT GO")], reference_path=ref)
    assert r.status == "fail", r


def test_postflight_burnin_logic_text_already_in_reference_fails(monkeypatch, tmp_path):
    out, ref = _patch_burnin_ocr(
        monkeypatch, tmp_path, out_text="DO NOT GO", ref_text="DO NOT GO")
    r = qc_orchestrator.postflight_burnin(out, [_cue("DO NOT GO")], reference_path=ref)
    assert r.status == "fail", r
    assert "reference" in r.details


def test_postflight_burnin_logic_no_reference_never_passes(monkeypatch, tmp_path):
    out, _ = _patch_burnin_ocr(monkeypatch, tmp_path, out_text="DO NOT GO")
    r = qc_orchestrator.postflight_burnin(out, [_cue("DO NOT GO")])
    assert r.status == "warn", r
    assert "unverified: no reference" in r.details


def test_postflight_burnin_logic_ocr_error_fails(monkeypatch, tmp_path):
    out, ref = _patch_burnin_ocr(
        monkeypatch, tmp_path, out_text=ocr.OCRError("synthetic OCR failure"))
    r = qc_orchestrator.postflight_burnin(out, [_cue("DO NOT GO")], reference_path=ref)
    assert r.status == "fail", r
    assert "synthetic OCR failure" in r.details


def test_postflight_burnin_logic_engine_missing_warns(monkeypatch, tmp_path):
    out, ref = _patch_burnin_ocr(monkeypatch, tmp_path, out_text="DO NOT GO")
    monkeypatch.setattr(ocr, "AVAILABLE", False)
    r = qc_orchestrator.postflight_burnin(out, [_cue("DO NOT GO")], reference_path=ref)
    assert r.status == "warn" and r.stub is True, r


# Review WA-R2-M1: a cue with non-blank text that has no letters/digits
# ("...", "…", "♪") is still a burned-in cue: never N/A, never pass.


@pytest.mark.parametrize("text", ["...", "\u2026", "\u266a"])
def test_postflight_burnin_symbol_only_cue_is_not_not_applicable(monkeypatch, tmp_path, text):
    missing_out = tmp_path / "missing_out.mp4"
    missing_ref = tmp_path / "missing_ref.mp4"
    r = qc_orchestrator.postflight_burnin(missing_out, [_cue(text)], reference_path=missing_ref)
    assert r.applicable is True, r
    assert r.status != "pass", r

    out, ref = _patch_burnin_ocr(monkeypatch, tmp_path, out_text="")
    r = qc_orchestrator.postflight_burnin(out, [_cue(text)], reference_path=ref)
    assert r.applicable is True and r.status == "warn", r
    assert "symbol" in r.details
    assert qc_orchestrator.burnin_symbol_only_unverified(r)


def test_postflight_burnin_symbol_cue_next_to_verified_cue_warns(monkeypatch, tmp_path):
    out, ref = _patch_burnin_ocr(monkeypatch, tmp_path, out_text="DO NOT GO")
    r = qc_orchestrator.postflight_burnin(
        out, [_cue("DO NOT GO"), _cue("\u266a", 3.0, 4.0)], reference_path=ref)
    assert r.status == "warn", r
    assert qc_orchestrator.burnin_symbol_only_unverified(r)
    # Without a reference the warn is the "no reference" kind, not this one.
    r = qc_orchestrator.postflight_burnin(out, [_cue("DO NOT GO"), _cue("...", 3.0, 4.0)])
    assert r.status == "warn" and not qc_orchestrator.burnin_symbol_only_unverified(r), r


def test_postflight_burnin_blank_cues_are_not_applicable(tmp_path):
    r = qc_orchestrator.postflight_burnin(
        tmp_path / "missing.mp4", [_cue("   "), _cue("")], reference_path=None)
    assert r.applicable is False, r


def test_postflight_burnin_band_follows_style_position():
    bottom = qc_orchestrator.subtitle_band(1280, 720, {"position": "bottom"})
    top = qc_orchestrator.subtitle_band(1280, 720, {"position": "top"})
    middle = qc_orchestrator.subtitle_band(1280, 720, {"position": "middle"})
    assert bottom[3] == 720 and bottom[1] > 360
    assert top[1] == 0 and top[3] < 360
    assert middle[1] < 360 < middle[3]
    assert qc_orchestrator.subtitle_band(1280, 720, None) == bottom


# --- fixed-input media tests through the real burn-in path ------------------

_W, _H, _FPS = 640, 360, 24


def _src_video(dst: Path, background: Path | None = None) -> Path:
    """2.5s video + silent audio (finalize_render maps 0:a)."""
    from app.media import ffmpeg_ops

    video_in = (
        ["-loop", "1", "-i", str(background)]
        if background is not None
        else ["-f", "lavfi", "-i", f"color=c=0x404040:s={_W}x{_H}:r={_FPS}"]
    )
    subprocess.run(
        [ffmpeg_ops.ffmpeg_bin(), "-y", "-loglevel", "error", *video_in,
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2.5",
         "-r", str(_FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(dst)],
        check=True, timeout=300,
    )
    return dst


def _burn(src: Path, dst: Path, text: str, style: dict, *, transparent=False) -> Path:
    """Burn one cue in with the project's real path: subtitle_render.write_ass
    -> subtitles= filter -> ffmpeg_ops.finalize_render (as services/render)."""
    from app.media import ffmpeg_ops, subtitle_render

    ass = subtitle_render.write_ass(
        [_cue(text)], style, dst.with_suffix(".ass"), width=_W, height=_H)
    if transparent:  # fully transparent fill/outline/shadow
        ass.write_text(ass.read_text(encoding="utf-8").replace(
            "{\\c", "{\\alpha&HFF&\\c"), encoding="utf-8")
    esc = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    ffmpeg_ops.finalize_render(
        [src], dst, filter_complex=f"[0:v]subtitles='{esc}'[vout]",
        vmap="[vout]", amap="0:a", fps=_FPS)
    return dst


def _background_with_text(dst: Path, text: str, y: int) -> Path:
    from PIL import ImageFont

    from tests.conftest import find_cjk_font

    img = Image.new("RGB", (_W, _H), (64, 64, 64))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(find_cjk_font(), 36)
    w = d.textbbox((0, 0), text, font=font)[2]
    d.text(((_W - w) // 2, y), text, font=font, fill="white",
           stroke_width=2, stroke_fill="black")
    img.save(dst)
    return dst


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_cjk_font
@pytest.mark.skipif(not ocr.AVAILABLE, reason="OCR engine not installed")
def test_postflight_burnin_real_output(tmp_path):
    style = {"font_family": "Noto Sans KR", "font_size_px": 96, "color": "#FFFFFF",
             "background_color": None, "position": "bottom",
             "safe_area_margin_pct": 5.0}
    cues = [_cue("DO NOT GO")]
    ref = _src_video(tmp_path / "ref.mp4")

    good = _burn(ref, tmp_path / "good.mp4", "DO NOT GO", style)
    r = qc_orchestrator.postflight_burnin(good, cues, reference_path=ref, style=style)
    assert r.status == "pass", r

    ko_cues = [_cue("테스트 자막")]
    ko = _burn(ref, tmp_path / "ko.mp4", "테스트 자막", style)
    r = qc_orchestrator.postflight_burnin(ko, ko_cues, reference_path=ref, style=style)
    assert r.status == "pass", r

    clear = _burn(ref, tmp_path / "clear.mp4", "DO NOT GO", style, transparent=True)
    r = qc_orchestrator.postflight_burnin(clear, cues, reference_path=ref, style=style)
    assert r.status == "fail", r

    r = qc_orchestrator.postflight_burnin(ref, cues, reference_path=ref, style=style)
    assert r.status == "fail", r  # missing: output identical to the reference

    reversed_ = _burn(ref, tmp_path / "rev.mp4", "GO DO NOT", style)
    r = qc_orchestrator.postflight_burnin(reversed_, cues, reference_path=ref, style=style)
    assert r.status == "fail", r

    # The cue words exist only in the BACKGROUND, inside the subtitle band,
    # in both the reference and the output: no subtitle was added -> fail.
    bg = _background_with_text(tmp_path / "bg.png", "DO NOT GO", y=_H - 80)
    bg_ref = _src_video(tmp_path / "bg_ref.mp4", background=bg)
    bg_out = _burn(bg_ref, tmp_path / "bg_out.mp4", " ", style)  # empty cue
    r = qc_orchestrator.postflight_burnin(bg_out, cues, reference_path=bg_ref, style=style)
    assert r.status == "fail", r
    assert r.measured[0]["reference_similarity"] >= defaults.RENDER_BURNIN_OCR_MIN_MATCH, r

    # Same words only in the background OUTSIDE the band (top of frame) -> fail.
    top = _background_with_text(tmp_path / "top.png", "DO NOT GO", y=20)
    top_out = _src_video(tmp_path / "top_out.mp4", background=top)
    r = qc_orchestrator.postflight_burnin(top_out, cues, reference_path=ref, style=style)
    assert r.status == "fail", r

    # A correct burn-in without a reference is never pass.
    r = qc_orchestrator.postflight_burnin(good, cues, style=style)
    assert r.status == "warn", r
    assert "unverified: no reference" in r.details


def test_advisory_set_unchanged():
    assert qc_orchestrator.ADVISORY_CHECKS == {"story_fidelity", "performance_quality"}
    assert qc_content.check_story_fidelity in [fn for _, fn in qc_orchestrator.CHECKS]
