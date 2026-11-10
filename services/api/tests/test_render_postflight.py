"""Render execution, postflight and publish (B4b, plan D4/D8, G8 H4/RH3/F13).

Proves:
  * request_render is an atomic claim (two concurrent requests -> one owner,
    one dispatch, the other RENDER_LOCKED);
  * a render failure (StudioError or crash) is COMMITTED as ``failed`` — a new
    session sees it — and the previously published output_path is kept;
  * postflight gates publishing: a preset/encoding mismatch or a transparent /
    missing burn-in is never published (real ffmpeg, OCR where legible);
  * publish = conditional UPDATE on id + exec_token + 'rendering' to an
    immutable per-attempt file; a stale execution (RH3 ABA) publishes nothing
    and its file stays an orphan;
  * subtitle cue/style/track edits are locked while the timeline renders;
  * reconcile fails a started render whose lease expired and re-dispatches a
    render that never started.

Runs on a per-test SQLite file by default; when TEST_DATABASE_URL points at
a dedicated Postgres test database (name ending in "test") the same tests run
there (schema reset per test, guarded by conftest.reset_test_postgres). The
service DATABASE_URL is never used (R2-C1). Media tests use
the conftest markers (skipped on hosts without ffmpeg; hard-fail with
REQUIRE_MEDIA_TOOLS=1).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

import app.core.celery_app as celery_mod
import app.db.session as sess_mod
import app.main  # noqa: F401  (imports every model module for create_all)
from app.core import defaults
from app.core.config import get_settings
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.media import ocr
from app.models.generation import Video
from app.models.production import Scene, Shot
from app.models.project import Project
from app.models.system import ExportPreset
from app.models.timeline import Timeline, TimelineClip
from app.services import qc_orchestrator
from app.services import render as render_service
from app.services import subtitles as subtitle_service
from app.tasks import tasks_post  # noqa: F401  (registers q.post dispatchers)
from tests.conftest import isolated_engine, postgres_tests_enabled

_W, _H, _FPS = 640, 360, 24
_OUTPUT_RE = re.compile(
    r"^projects/[0-9a-f-]+/timelines/[0-9a-f-]+/output_[0-9a-f]{8}\.mp4$"
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # Temp SQLite, or the verified dedicated TEST_DATABASE_URL (R2-C1): the
    # service DATABASE_URL is never reset.
    url, engine = isolated_engine(tmp_path, "render.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("SSE_BACKEND", "memory")
    get_settings.cache_clear()
    reset_bridge()
    monkeypatch.setattr(sess_mod, "get_engine", lambda: engine)
    reset_engine()
    sess_mod._SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)

    sent: list[tuple[str, tuple]] = []
    for name in (
        "q.post.render_timeline",
        "q.image.run_generation",
        "q.p1.run_multishot",
    ):
        monkeypatch.setitem(
            celery_mod._JOB_DISPATCHERS, name, lambda *a, _n=name: sent.append((_n, a))
        )
    # The QC preflight gate is covered by test_phase4; here every render is
    # allowed so the tests isolate execution / postflight / publish.
    monkeypatch.setattr(qc_orchestrator, "assert_render_allowed", lambda *a, **k: None)

    with session_scope() as db:
        project = Project(title="render")
        db.add(project)
        db.flush()
        scene = Scene(project_id=project.id, order_index=0)
        db.add(scene)
        db.flush()
        shot = Shot(scene_id=scene.id, order_index=0, status="video_done")
        db.add(shot)
        db.flush()
        pid, shot_id = project.id, shot.id
    yield {"sent": sent, "project_id": pid, "shot_id": shot_id, "tmp": tmp_path}
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()
    engine.dispose()


# --- helpers -----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ffmpeg(*args: str) -> None:
    from app.media import ffmpeg_ops

    subprocess.run(
        [ffmpeg_ops.ffmpeg_bin(), "-y", "-loglevel", "error", *args],
        check=True,
        timeout=300,
    )


def _source_clip(env, *, real: bool) -> Path:
    """A 2.5s 640x360 clip with silent audio (real ffmpeg) or a dummy file."""
    rel = f"projects/{env['project_id']}/videos/{uuid.uuid4().hex}.mp4"
    abs_path = env["tmp"] / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    if real:
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x404040:s={_W}x{_H}:r={_FPS}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "2.5",
            "-r",
            str(_FPS),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(abs_path),
        )
    else:
        abs_path.write_bytes(b"not-a-video")
    return abs_path


def _preset(*, burn: bool = False, resolution: str = f"{_W}x{_H}") -> uuid.UUID:
    with session_scope() as db:
        preset = ExportPreset(
            name=f"test {resolution}",
            platform="custom",
            aspect_ratio="16:9",
            resolution=resolution,
            fps=_FPS,
            codec="h264",
            audio_codec="aac",
            burn_subtitles=burn,
        )
        db.add(preset)
        db.flush()
        return preset.id


def _timeline(
    env,
    *,
    real: bool,
    track_id: uuid.UUID | None = None,
    status: str = "draft",
    output_path: str | None = None,
    track_ids: list[uuid.UUID] | None = None,
) -> str:
    if track_ids is None and track_id is not None:
        track_ids = [track_id]
    if track_ids:
        mode = "dual" if len(track_ids) > 1 else "single"
    else:
        mode = "none"
    clip = _source_clip(env, real=real)
    with session_scope() as db:
        video = Video(
            shot_id=env["shot_id"],
            mode="i2v",
            duration_sec=2.5,
            fps=_FPS,
            resolution=f"{_W}x{_H}",
            model_id="stub",
            status="done",
            output_path=str(clip.relative_to(env["tmp"])),
        )
        db.add(video)
        db.flush()
        tl = Timeline(
            project_id=env["project_id"],
            status=status,
            output_path=output_path,
            subtitle_mode=mode,
            subtitle_track_ids=[str(t) for t in track_ids] if track_ids else None,
        )
        db.add(tl)
        db.flush()
        db.add(
            TimelineClip(
                timeline_id=tl.id,
                position=0,
                shot_id=env["shot_id"],
                video_id=video.id,
                in_sec=0.0,
                out_sec=2.5,
            )
        )
        return str(tl.id)


def _track(
    env,
    text_: str,
    *,
    font_size_px: int = 96,
    start: float = 0.5,
    end: float = 2.0,
) -> uuid.UUID:
    with session_scope() as db:
        track = subtitle_service.create_track(db, env["project_id"], language_code="en")
        subtitle_service.save_cues(
            db, track.id, [{"start_sec": start, "end_sec": end, "text": text_}]
        )
        subtitle_service.save_style(
            db,
            env["project_id"],
            {
                "font_family": "Noto Sans KR",
                "font_size_px": font_size_px,
                "color": "#FFFFFF",
                "position": "bottom",
                "background_color": None,
                "safe_area_margin_pct": 5.0,
            },
        )
        return track.id


def _get(tid: str) -> Timeline:
    with session_scope() as db:
        row = db.get(Timeline, uuid.UUID(tid))
        db.expunge_all()
        return row


def _request(tid: str, preset_id: uuid.UUID) -> dict:
    with session_scope() as db:
        return render_service.request_render(
            db, uuid.UUID(tid), export_preset_id=preset_id
        )


def _render(tid: str):
    return tasks_post.render_timeline.apply(args=[tid])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _render_dispatches(env) -> int:
    return sum(1 for name, _ in env["sent"] if name == "q.post.render_timeline")


def _legacy_output(env, tid: str) -> str:
    """A previously published output in the old fixed-name layout."""
    rel = f"projects/{env['project_id']}/timelines/{tid}/output.mp4"
    (env["tmp"] / rel).parent.mkdir(parents=True, exist_ok=True)
    (env["tmp"] / rel).write_bytes(b"previous-render")
    return rel


# --- atomic claim -------------------------------------------------------------


def test_concurrent_request_render_claims_once(env, monkeypatch):
    tid = _timeline(env, real=False)
    preset_id = _preset()
    barrier = threading.Barrier(2)
    # Both requests pass every read-side check before either claims.
    monkeypatch.setattr(
        qc_orchestrator,
        "assert_render_allowed",
        lambda *a, **k: barrier.wait(timeout=10),
    )
    outcomes: list[str] = []
    lock = threading.Lock()

    def _go():
        try:
            _request(tid, preset_id)
            result = "ok"
        except StudioError as exc:
            result = exc.code
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["RENDER_LOCKED", "ok"], outcomes
    assert _render_dispatches(env) == 1
    tl = _get(tid)
    assert tl.status == "rendering" and tl.exec_token is not None


def test_request_render_while_rendering_is_locked(env):
    tid = _timeline(env, real=False)
    preset_id = _preset()
    _request(tid, preset_id)
    with pytest.raises(StudioError) as err:
        _request(tid, preset_id)
    assert err.value.code == ErrorCode.RENDER_LOCKED


# --- failure persistence (F02/F03 for renders) --------------------------------


def test_studio_error_during_render_is_committed_and_keeps_output(env):
    tid = _timeline(env, real=False, status="done")
    prev = _legacy_output(env, tid)
    with session_scope() as db:
        db.get(Timeline, uuid.UUID(tid)).output_path = prev
    preset_id = _preset()
    _request(tid, preset_id)
    # The clip file disappears after the request: the worker's validation
    # raises a StudioError (VALIDATION_ERROR) mid-render.
    with session_scope() as db:
        video = db.query(Video).first()
        (env["tmp"] / video.output_path).unlink()

    result = _render(tid)
    assert result.failed()

    tl = _get(tid)  # NEW session
    assert tl.status == "failed"
    assert tl.error_code == ErrorCode.VALIDATION_ERROR
    assert tl.output_path == prev
    assert (env["tmp"] / prev).read_bytes() == b"previous-render"


def test_unexpected_error_during_render_is_committed(env, monkeypatch):
    tid = _timeline(env, real=False)
    _request(tid, _preset())

    def _boom(*a, **k):
        raise OSError("disk exploded")

    monkeypatch.setattr(render_service, "_validate_clips", _boom)
    assert _render(tid).failed()
    tl = _get(tid)
    assert tl.status == "failed"
    assert tl.error_code == ErrorCode.PROVIDER_ERROR
    assert tl.output_path is None


def test_redelivery_of_started_render_is_noop(env, monkeypatch):
    tid = _timeline(env, real=False)
    _request(tid, _preset())
    ex = render_service.begin_execution(uuid.UUID(tid))
    assert ex is not None
    calls = []
    monkeypatch.setattr(
        render_service, "_validate_clips", lambda *a, **k: calls.append(1) or []
    )
    summary = render_service.render_timeline(tid)  # duplicate delivery
    assert summary.get("skipped") is True
    assert calls == []
    assert _get(tid).status == "rendering"


# --- subtitle edit guard (F13) ------------------------------------------------


def test_subtitle_edits_locked_while_rendering(env):
    track_id = _track(env, "HELLO")
    tid = _timeline(env, real=False, track_id=track_id)
    with session_scope() as db:
        other = subtitle_service.create_track(db, env["project_id"], language_code="ko")
        other_id = other.id
    _request(tid, _preset(burn=True))
    before = _get(tid).content_version

    def _locked(fn):
        with pytest.raises(StudioError) as err:
            with session_scope() as db:
                fn(db)
        assert err.value.code == ErrorCode.RENDER_LOCKED, err.value

    _locked(
        lambda db: subtitle_service.save_cues(
            db, track_id, [{"start_sec": 0.5, "end_sec": 1.5, "text": "changed"}]
        )
    )
    _locked(
        lambda db: subtitle_service.save_style(
            db, env["project_id"], {"font_size_px": 30}, track_id=track_id
        )
    )
    _locked(
        lambda db: subtitle_service.save_style(
            db, env["project_id"], {"font_size_px": 30}
        )
    )  # project default style
    _locked(lambda db: subtitle_service.delete_track(db, track_id))
    _locked(lambda db: subtitle_service.generate_cues_from_dialogue(db, track_id))

    # A track the rendering timeline does not burn stays editable.
    with session_scope() as db:
        subtitle_service.save_cues(
            db, other_id, [{"start_sec": 0.5, "end_sec": 1.5, "text": "free"}]
        )
    with session_scope() as db:
        cues = subtitle_service.get_cues(db, track_id)
        assert [c.text for c in cues] == ["HELLO"]
    assert _get(tid).content_version == before


# --- reconcile ------------------------------------------------------------------


def test_reconcile_fails_started_render_with_expired_lease(env):
    from app.services import job_recovery

    tid = _timeline(env, real=False, status="done")
    prev = _legacy_output(env, tid)
    with session_scope() as db:
        db.get(Timeline, uuid.UUID(tid)).output_path = prev
    _request(tid, _preset())
    assert render_service.begin_execution(uuid.UUID(tid)) is not None

    summary = job_recovery.reconcile_once(_now())  # lease still valid
    assert _get(tid).status == "rendering"

    later = _now() + timedelta(seconds=defaults.LEASE_TTL_SEC + 5)
    summary = job_recovery.reconcile_once(later)
    assert summary["renders_failed"] == 1
    tl = _get(tid)
    assert tl.status == "failed" and tl.error_code == ErrorCode.PROVIDER_ERROR
    assert tl.output_path == prev


def test_reconcile_redispatches_render_that_never_started(env):
    from app.services import job_recovery

    tid = _timeline(env, real=False)
    _request(tid, _preset())
    assert _render_dispatches(env) == 1
    later = _now() + timedelta(seconds=defaults.LEASE_TTL_SEC + 5)
    summary = job_recovery.reconcile_once(later)
    assert summary["renders_redispatched"] == 1
    assert _render_dispatches(env) == 2
    assert _get(tid).status == "rendering"
    # The re-sent message still runs exactly once.
    assert render_service.begin_execution(uuid.UUID(tid)) is not None
    assert render_service.begin_execution(uuid.UUID(tid)) is None


# --- real renders (ffmpeg) ------------------------------------------------------


@pytest.mark.needs_ffmpeg
def test_render_publishes_attempt_file_with_postflight_metadata(env):
    tid = _timeline(env, real=True, status="done")
    prev = _legacy_output(env, tid)
    with session_scope() as db:
        db.get(Timeline, uuid.UUID(tid)).output_path = prev
    preset_id = _preset()
    _request(tid, preset_id)
    result = _render(tid)
    assert result.successful(), result.result

    tl = _get(tid)
    assert tl.status == "done", (tl.error_code, tl.error_message)
    assert _OUTPUT_RE.match(tl.output_path), tl.output_path
    assert tl.output_path != prev
    assert (env["tmp"] / tl.output_path).is_file()
    assert (env["tmp"] / prev).read_bytes() == b"previous-render"  # never touched
    meta = tl.render_meta
    assert meta["exec_token"] == str(tl.exec_token)
    assert meta["output_path"] == tl.output_path
    assert meta["postflight"]["encoding_match"]["status"] == "pass"
    assert meta["unverified"] == []
    # Per-attempt scratch is cleaned; the published file stays.
    attempts = env["tmp"] / f"projects/{env['project_id']}/timelines/{tid}/attempts"
    assert not attempts.exists() or not any(attempts.iterdir())


@pytest.mark.needs_ffmpeg
def test_postflight_encoding_mismatch_is_not_published(env, monkeypatch):
    from app.media import ffmpeg_ops

    tid = _timeline(env, real=True)
    preset_id = _preset()
    _request(tid, preset_id)
    assert _render(tid).successful()
    good = _get(tid).output_path
    good_hash = _sha(env["tmp"] / good)

    real_extract = ffmpeg_ops.extract_segment

    def _half_size(src, dst, in_sec, out_sec, *, width, height, **kw):
        return real_extract(
            src, dst, in_sec, out_sec, width=width // 2, height=height // 2, **kw
        )

    monkeypatch.setattr(ffmpeg_ops, "extract_segment", _half_size)
    _request(tid, preset_id)
    assert _render(tid).failed()
    tl = _get(tid)
    assert tl.status == "failed"
    assert tl.error_code == ErrorCode.QC_FAILED
    assert "encoding_match" in (tl.error_message or "")
    assert tl.output_path == good
    assert _sha(env["tmp"] / good) == good_hash


def _transparent_ass(monkeypatch):
    """Burn-in writes a fully transparent subtitle (alpha FF fill/outline)."""
    from app.media import subtitle_render

    real = subtitle_render.write_ass_sections

    def _clear(*a, **k):
        path = real(*a, **k)
        path.write_text(
            path.read_text(encoding="utf-8").replace("{\\c", "{\\alpha&HFF&\\c"),
            encoding="utf-8",
        )
        return path

    monkeypatch.setattr(subtitle_render, "write_ass_sections", _clear)


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_cjk_font
def test_transparent_burnin_is_not_published_ocr(env, monkeypatch):
    if not ocr.AVAILABLE:
        if os.environ.get("REQUIRE_MEDIA_TOOLS", "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            pytest.fail("OCR engine not installed (REQUIRE_MEDIA_TOOLS=1)")
        pytest.skip("OCR engine not installed")
    track_id = _track(env, "DO NOT GO", font_size_px=96)
    tid = _timeline(env, real=True, track_id=track_id)
    preset_id = _preset(burn=True)

    _request(tid, preset_id)
    assert _render(tid).successful()
    tl = _get(tid)
    assert tl.status == "done", tl.error_message
    good = tl.output_path
    ocr_results = tl.render_meta["postflight"]["burnin_ocr"]
    assert [r["status"] for r in ocr_results] == ["pass"], ocr_results
    assert tl.render_meta["unverified"] == []

    _transparent_ass(monkeypatch)
    _request(tid, preset_id)
    assert _render(tid).failed()
    tl = _get(tid)
    assert tl.status == "failed" and tl.error_code == ErrorCode.QC_FAILED
    assert tl.output_path == good


@pytest.mark.needs_ffmpeg
def test_transparent_small_burnin_fails_pixel_presence(env, monkeypatch):
    """Glyphs too small for OCR still need visible subtitle pixels."""
    track_id = _track(env, "tiny cue", font_size_px=24)  # 8px at 360p
    tid = _timeline(env, real=True, track_id=track_id)
    preset_id = _preset(burn=True)

    _request(tid, preset_id)
    assert _render(tid).successful()
    tl = _get(tid)
    assert tl.status == "done", tl.error_message
    presence = tl.render_meta["postflight"]["burnin_presence"]
    assert [p["status"] for p in presence] == ["pass"], presence
    assert any("too small for OCR" in u for u in tl.render_meta["unverified"])
    good = tl.output_path

    _transparent_ass(monkeypatch)
    _request(tid, preset_id)
    assert _render(tid).failed()
    tl = _get(tid)
    assert tl.status == "failed" and tl.error_code == ErrorCode.QC_FAILED
    assert "burnin_presence" in tl.error_message
    assert tl.output_path == good


@pytest.mark.needs_ffmpeg
def test_aba_stale_execution_cannot_publish(env, monkeypatch):
    """RH3: A passes postflight and pauses; its lease expires (reconcile fails
    it); B renders and publishes; A resumes -> 0 rows, B's file stays."""
    from app.services import job_recovery

    tid = _timeline(env, real=True)
    preset_id = _preset()
    real_publish = render_service.publish_output
    seen: dict[str, object] = {}

    def _publish(ex, rel_path, meta):
        if "a" in seen:  # B (nested) publishes normally
            seen["b"] = (ex.token, rel_path)
            return real_publish(ex, rel_path, meta)
        seen["a"] = (ex.token, rel_path)
        later = _now() + timedelta(seconds=defaults.LEASE_TTL_SEC + 5)
        assert job_recovery.reconcile_once(later)["renders_failed"] == 1
        assert _get(tid).status == "failed"
        _request(tid, preset_id)
        assert _render(tid).successful()  # B, start to finish
        assert _get(tid).status == "done"
        # B's published bytes, captured BEFORE A resumes (B4B-M4).
        seen["b_hash"] = _sha(env["tmp"] / _get(tid).output_path)
        ok = real_publish(ex, rel_path, meta)  # A resumes
        seen["a_published"] = ok
        return ok

    monkeypatch.setattr(render_service, "publish_output", _publish)
    _request(tid, preset_id)
    summary = _render(tid).result  # A

    assert seen["a_published"] is False
    assert summary["status"] == "superseded"
    a_token, a_rel = seen["a"]
    b_token, b_rel = seen["b"]
    assert a_rel != b_rel
    tl = _get(tid)
    assert tl.status == "done"
    assert tl.exec_token == b_token
    assert tl.output_path == b_rel
    assert tl.render_meta["exec_token"] == str(b_token)
    assert _sha(env["tmp"] / tl.output_path) == seen["b_hash"]
    assert (env["tmp"] / a_rel).is_file()  # A's candidate: orphan, unreferenced


@pytest.mark.needs_ffmpeg
def test_symbol_only_burnin_cue_is_published_as_unverified(env):
    """WA-R2-M1: a cue with no letters/digits cannot be OCR-verified; like a
    glyph too small for OCR it is published and recorded as unverified (pixel
    presence still has to pass) instead of blocking every render."""
    track_id = _track(env, "!?! ... ?!?", font_size_px=96)
    tid = _timeline(env, real=True, track_id=track_id)
    preset_id = _preset(burn=True)

    _request(tid, preset_id)
    assert _render(tid).successful()
    tl = _get(tid)
    assert tl.status == "done", tl.error_message
    presence = tl.render_meta["postflight"]["burnin_presence"]
    assert [p["status"] for p in presence] == ["pass"], presence
    ocr_results = tl.render_meta["postflight"]["burnin_ocr"]
    assert [r["status"] for r in ocr_results] == ["warn"], ocr_results
    assert ocr_results[0]["applicable"] is True
    assert any("symbol" in u for u in tl.render_meta["unverified"]), tl.render_meta


# --- B4b-fix: postflight cue range, track-specific presence, probe -------------------


_PG = postgres_tests_enabled()


@pytest.mark.needs_ffmpeg
@pytest.mark.parametrize("font_size_px", [24, 96])
def test_cue_overlapping_output_end_is_verified(env, monkeypatch, font_size_px):
    """B4B-H1: a cue that starts inside the output and ends after it (2.5s
    clip, cue [0.1, 6]) is visible for ~2.4s; it must be verified (presence
    and OCR on the clipped range), and its transparent version not published."""
    track_id = _track(env, "TAIL CUE", font_size_px=font_size_px, start=0.1, end=6.0)
    tid = _timeline(env, real=True, track_id=track_id)
    preset_id = _preset(burn=True)

    _request(tid, preset_id)
    assert _render(tid).successful()
    tl = _get(tid)
    assert tl.status == "done", tl.error_message
    presence = tl.render_meta["postflight"]["burnin_presence"]
    assert [(p["status"], p["applicable"]) for p in presence] == [("pass", True)]
    assert all(m["at_sec"] < 2.5 for m in presence[0]["measured"]), presence
    assert not any("output end" in u for u in tl.render_meta["unverified"])
    if font_size_px >= defaults.RENDER_BURNIN_OCR_MIN_GLYPH_PX * 1080 / _H and ocr.AVAILABLE:
        ocr_results = tl.render_meta["postflight"]["burnin_ocr"]
        assert [r["status"] for r in ocr_results] == ["pass"], ocr_results
    good = tl.output_path

    _transparent_ass(monkeypatch)
    _request(tid, preset_id)
    assert _render(tid).failed()
    tl = _get(tid)
    assert tl.status == "failed" and tl.error_code == ErrorCode.QC_FAILED
    assert "burnin_presence" in tl.error_message
    assert tl.output_path == good


def _dual_tracks(
    env,
    *,
    font_size_px: int,
    texts: tuple[str, str] = ("MISSING", "VISIBLE OTHER TRACK"),
    times: tuple[tuple[float, float], tuple[float, float]] = ((0.5, 2.0), (0.5, 2.0)),
) -> list[uuid.UUID]:
    """Track A (middle) and B (bottom), both with a 30% safe-area margin (the
    reviewer's P08 layout: the generous bands of A and B overlap)."""
    ids = []
    with session_scope() as db:
        for text_, position, (start, end) in zip(
            texts, ("middle", "bottom"), times, strict=True
        ):
            track = subtitle_service.create_track(
                db, env["project_id"], language_code="en"
            )
            subtitle_service.save_cues(
                db, track.id, [{"start_sec": start, "end_sec": end, "text": text_}]
            )
            subtitle_service.save_style(
                db,
                env["project_id"],
                {
                    "font_family": "Noto Sans KR",
                    "font_size_px": font_size_px,
                    "color": "#FFFFFF",
                    "position": position,
                    "background_color": None,
                    "safe_area_margin_pct": 30.0,
                },
                track_id=track.id,
            )
            ids.append(track.id)
    return ids


def _break_section(monkeypatch, name: str, how: str) -> None:
    """Burn-in writes section `name` transparent (alpha FF) or drops it."""
    from app.media import subtitle_render

    real = subtitle_render.write_ass_sections

    def _broken(*a, **k):
        path = real(*a, **k)
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Dialogue:") and f",{name},," in line:
                if how == "missing":
                    continue
                line = line.replace("{\\c", "{\\alpha&HFF&\\c", 1)
            out.append(line)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return path

    monkeypatch.setattr(subtitle_render, "write_ass_sections", _broken)


@pytest.mark.needs_ffmpeg
@pytest.mark.parametrize("mode", ["small", "no_ocr"])
@pytest.mark.parametrize("broken", [None, "transparent", "missing"])
def test_dual_track_presence_is_track_specific(env, monkeypatch, mode, broken):
    """B4B-H2: another track's glyphs must never prove a transparent/missing
    track present (small glyphs = pixel path only; large glyphs with the OCR
    engine absent = pixel presence is the only real gate)."""
    if mode == "no_ocr":
        monkeypatch.setattr(ocr, "AVAILABLE", False)
        size = 96  # 32px at 360p: OCR-sized, but no engine
    else:
        size = 24  # 8px at 360p: below the OCR floor
    a, b = _dual_tracks(env, font_size_px=size)
    tid = _timeline(env, real=True, track_ids=[a, b])
    preset_id = _preset(burn=True)
    if broken:
        _break_section(monkeypatch, "S0", broken)

    _request(tid, preset_id)
    result = _render(tid)
    tl = _get(tid)
    if broken is None:
        assert result.successful(), tl.error_message
        assert tl.status == "done", tl.error_message
        presence = tl.render_meta["postflight"]["burnin_presence"]
        assert [p["status"] for p in presence] == ["pass", "pass"], presence
        return
    assert result.failed()
    assert tl.status == "failed" and tl.error_code == ErrorCode.QC_FAILED
    assert "burnin_presence (track 0)" in tl.error_message, tl.error_message
    assert "burnin_presence (track 1)" not in tl.error_message, tl.error_message
    assert tl.output_path is None


def _require_ocr() -> None:
    if ocr.AVAILABLE:
        return
    if os.environ.get("REQUIRE_MEDIA_TOOLS", "").strip().lower() in ("1", "true", "yes"):
        pytest.fail("OCR engine not installed (REQUIRE_MEDIA_TOOLS=1)")
    pytest.skip("OCR engine not installed")


def _capture_postflight(monkeypatch) -> dict:
    """Keep the postflight results of the last render (also when it fails)."""
    real = render_service._postflight
    seen: dict = {}

    def _wrapped(*a, **k):
        seen["results"], seen["problems"], seen["unverified"] = real(*a, **k)
        return seen["results"], seen["problems"], seen["unverified"]

    monkeypatch.setattr(render_service, "_postflight", _wrapped)
    return seen


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_cjk_font
@pytest.mark.parametrize(
    "texts",
    [("MISSING", "VISIBLE OTHER TRACK"), ("SAME TEXT", "SAME TEXT")],
    ids=["distinct", "same"],
)
@pytest.mark.parametrize("broken", [None, "transparent", "missing"])
def test_dual_track_ocr_ignores_other_track(env, monkeypatch, texts, broken):
    """B9: overlapping OCR bands (middle + 30%-margin bottom, 32px glyphs, OCR
    engine present). A correct dual render is published — the other track's
    glyphs no longer leak into a track's OCR text (false fail) — and a
    transparent / missing track 0 still fails ITS OWN OCR check even when
    track 1 shows the very same text (no false pass)."""
    _require_ocr()
    a, b = _dual_tracks(env, font_size_px=96, texts=texts)
    tid = _timeline(env, real=True, track_ids=[a, b])
    preset_id = _preset(burn=True)
    if broken:
        _break_section(monkeypatch, "S0", broken)
    seen = _capture_postflight(monkeypatch)

    _request(tid, preset_id)
    result = _render(tid)
    tl = _get(tid)
    ocr_results = seen["results"]["burnin_ocr"]
    statuses = [(r["track"], r["status"]) for r in ocr_results]
    if broken is None:
        assert result.successful(), tl.error_message
        assert tl.status == "done", tl.error_message
        assert statuses == [(0, "pass"), (1, "pass")], ocr_results
        assert tl.render_meta["unverified"] == []
        scrubbed = [m.get("scrubbed_px", 0) for r in ocr_results for m in r["measured"]]
        assert all(px > 0 for px in scrubbed), ocr_results
        return
    assert result.failed()
    assert tl.status == "failed" and tl.error_code == ErrorCode.QC_FAILED
    assert tl.output_path is None
    # Track 0's own OCR check fails (not only pixel presence); track 1 passes.
    assert statuses == [(0, "fail"), (1, "pass")], ocr_results
    assert not any("(track 1)" in p for p in seen["problems"]), seen["problems"]


# --- B9b-r2: which cue is on screen = ASS centisecond times + real frame (B9-H1) ----


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_cjk_font
@pytest.mark.parametrize(
    "resolution,font_size_px",
    [("640x360", 240), ("1920x1080", 216)],
    ids=["360p", "1080p"],
)
def test_dual_missing_track_at_other_cue_ass_boundary_is_not_published(
    env, monkeypatch, resolution, font_size_px
):
    """B9-H1 (reviewer P07): A = middle [1.201, 1.301], B = bottom [1.252,
    2.4], both "SAME TEXT". The midpoint of A (1.251s) is before B's raw
    start, but B's ASS start is 1.25s and the extracted frame is a real frame
    — B is on screen there. A missing / transparent A must not be proven
    present by B's pixels or B's OCR text: the render fails, nothing is
    published (the old raw-float decision published it). A TRANSPARENT A is
    not this case: libass still lays the invisible event out and its
    collision handling moves B away for B's whole lifetime."""
    _require_ocr()
    a, b = _dual_tracks(
        env,
        font_size_px=font_size_px,
        texts=("SAME TEXT", "SAME TEXT"),
        times=((1.201, 1.301), (1.252, 2.4)),
    )
    tid = _timeline(env, real=True, track_ids=[a, b])
    preset_id = _preset(burn=True, resolution=resolution)
    _break_section(monkeypatch, "S0", "missing")
    seen = _capture_postflight(monkeypatch)

    _request(tid, preset_id)
    result = _render(tid)
    tl = _get(tid)
    assert result.failed(), seen.get("results")
    assert tl.status == "failed" and tl.error_code == ErrorCode.QC_FAILED
    assert tl.output_path is None
    assert any("(track 0)" in p for p in seen["problems"]), seen["problems"]
    assert not any("(track 1)" in p for p in seen["problems"]), seen["problems"]


@pytest.mark.needs_ffmpeg
@pytest.mark.needs_cjk_font
@pytest.mark.parametrize("broken", [None, "missing"])
def test_dual_other_cue_starting_near_midpoint_is_judged_by_ass_time(
    env, monkeypatch, broken
):
    """B9-H1 (reviewer P08): A = "MISSING" [0.5, 2.002], B = "VISIBLE OTHER
    TRACK" [1.252, 2.4], 32px. A's midpoint 1.251s sits on B's ASS start
    (1.25s): the old raw comparison left B unscrubbed in a frame that shows
    it, and a correct render false-failed. A is now sampled at least one
    frame + 1cs away from B's boundaries: the correct render is published,
    and a missing A still fails its own checks."""
    _require_ocr()
    a, b = _dual_tracks(
        env,
        font_size_px=96,
        times=((0.5, 2.002), (1.252, 2.4)),
    )
    tid = _timeline(env, real=True, track_ids=[a, b])
    preset_id = _preset(burn=True)
    if broken:
        _break_section(monkeypatch, "S0", broken)
    seen = _capture_postflight(monkeypatch)

    _request(tid, preset_id)
    result = _render(tid)
    tl = _get(tid)
    if broken is None:
        assert result.successful(), seen.get("problems")
        assert tl.status == "done", tl.error_message
        assert tl.output_path
        ocr_results = seen["results"]["burnin_ocr"]
        assert [(r["track"], r["status"]) for r in ocr_results] == [
            (0, "pass"),
            (1, "pass"),
        ], ocr_results
        presence = seen["results"]["burnin_presence"]
        assert [p["status"] for p in presence] == ["pass", "pass"], presence
        return
    assert result.failed()
    assert tl.status == "failed" and tl.error_code == ErrorCode.QC_FAILED
    assert tl.output_path is None
    assert any("(track 0)" in p for p in seen["problems"]), seen["problems"]
    assert not any("(track 1)" in p for p in seen["problems"]), seen["problems"]


def _probe_candidate(monkeypatch, behaviour) -> None:
    """Wrap ffprobe for the published candidate (output_<token8>.mp4) only."""
    from app.media import ffmpeg_ops

    real = ffmpeg_ops.probe
    calls: dict[str, int] = {}

    def _probe(path):
        key = str(path)
        if "/output_" not in key:
            return real(path)
        calls[key] = calls.get(key, 0) + 1
        return behaviour(calls[key], real(path))

    monkeypatch.setattr(ffmpeg_ops, "probe", _probe)


@pytest.mark.needs_ffmpeg
def test_duration_probe_failure_after_encoding_probe_is_not_published(
    env, monkeypatch
):
    """B4B-M1: a probe error after a successful encoding probe must not turn
    into duration 0 (every cue skipped) and publish a transparent burn-in."""
    from app.media.ffmpeg_ops import FFmpegError

    track_id = _track(env, "tiny cue", font_size_px=24)
    tid = _timeline(env, real=True, track_id=track_id)

    def _second_fails(n, facts):
        if n >= 2:
            raise FFmpegError(ErrorCode.PROVIDER_ERROR, "ffprobe failed (injected)")
        return facts

    _probe_candidate(monkeypatch, _second_fails)
    _transparent_ass(monkeypatch)
    _request(tid, _preset(burn=True))
    assert _render(tid).failed()
    tl = _get(tid)
    assert tl.status == "failed" and tl.error_code == ErrorCode.QC_FAILED
    assert tl.output_path is None


@pytest.mark.needs_ffmpeg
def test_unknown_output_duration_is_not_published(env, monkeypatch):
    """B4B-M1: zero / unknown duration cannot exempt the burn-in checks."""
    track_id = _track(env, "tiny cue", font_size_px=24)
    tid = _timeline(env, real=True, track_id=track_id)
    _probe_candidate(monkeypatch, lambda n, facts: {**facts, "duration_sec": 0.0})
    _request(tid, _preset(burn=True))
    assert _render(tid).failed()
    tl = _get(tid)
    assert tl.status == "failed" and tl.error_code == ErrorCode.QC_FAILED
    assert "duration" in (tl.error_message or ""), tl.error_message
    assert tl.output_path is None


# --- B4b-fix: publish inside the failure boundary (B4B-M2) ---------------------------


def _fake_produce(env, monkeypatch) -> None:
    """Skip ffmpeg: the attempt 'produces' a candidate file and metadata."""

    def _produce(ex, plan, scratch):
        rel = f"{plan.out_rel_dir}/output_{ex.token.hex[:8]}.mp4"
        (env["tmp"] / rel).parent.mkdir(parents=True, exist_ok=True)
        (env["tmp"] / rel).write_bytes(b"candidate")
        return rel, {"exec_token": str(ex.token), "output_path": rel}

    monkeypatch.setattr(render_service, "_produce", _produce)
    monkeypatch.setattr(render_service, "_validate_clips", lambda *a, **k: [])


def test_publish_entry_error_is_committed_as_failed(env, monkeypatch):
    tid = _timeline(env, real=False, status="done")
    prev = _legacy_output(env, tid)
    with session_scope() as db:
        db.get(Timeline, uuid.UUID(tid)).output_path = prev
    _request(tid, _preset())
    _fake_produce(env, monkeypatch)

    def _boom(*a, **k):
        raise OSError("publish entry exploded (injected)")

    monkeypatch.setattr(render_service, "publish_output", _boom)
    assert _render(tid).failed()
    tl = _get(tid)  # NEW session
    assert tl.status == "failed"
    assert tl.error_code == ErrorCode.PROVIDER_ERROR
    assert tl.output_path == prev


def test_publish_commit_then_lost_response_keeps_done(env, monkeypatch):
    """The `done` commit succeeded and only the acknowledgement was lost: the
    token CAS of the failure record must not overwrite `done`."""
    tid = _timeline(env, real=False)
    _request(tid, _preset())
    _fake_produce(env, monkeypatch)
    real = render_service.publish_output
    published: dict[str, str] = {}

    def _lost(ex, rel, meta):
        assert real(ex, rel, meta)
        published["rel"] = rel
        raise OSError("connection lost after COMMIT (injected)")

    monkeypatch.setattr(render_service, "publish_output", _lost)
    _render(tid)
    tl = _get(tid)
    assert tl.status == "done"
    assert tl.output_path == published["rel"]


# --- B4b-fix: edit guard vs render claim (B4B-H3/H4) ---------------------------------


@pytest.mark.parametrize("change", ["name", "selection"])
def test_stale_timeline_edit_never_overwrites_render_claim(env, change):
    """B4B-H4: the editor read the timeline as `done` before the render claim
    committed; its later write must not move the claimed timeline away from
    `rendering` (the worker must still start)."""
    from app.services import timelines as timeline_service

    track_id = _track(env, "HELLO")
    with session_scope() as db:
        other = subtitle_service.create_track(db, env["project_id"], language_code="ko")
        other_id = other.id
    tid = _timeline(env, real=False, status="done", track_id=track_id)
    preset_id = _preset()

    editor = sess_mod.get_session_factory()()
    try:
        stale = timeline_service._get_timeline(editor, uuid.UUID(tid))
        assert stale.status == "done"
        _request(tid, preset_id)  # R claims in its own transaction
        try:
            if change == "name":
                timeline_service.update_timeline(editor, uuid.UUID(tid), name="renamed")
            else:
                timeline_service.update_timeline(
                    editor, uuid.UUID(tid), subtitle_track_ids=[str(other_id)]
                )
            editor.commit()
        except StudioError as exc:
            assert exc.code == ErrorCode.RENDER_LOCKED, exc
            editor.rollback()
    finally:
        editor.close()

    tl = _get(tid)
    assert tl.status == "rendering"
    assert tl.exec_token is not None
    assert tl.subtitle_track_ids == [str(track_id)]
    assert render_service.begin_execution(uuid.UUID(tid)) is not None


@pytest.mark.skipif(
    not _PG,
    reason="needs real row locks (set TEST_DATABASE_URL to a dedicated *_test Postgres DB)",
)
def test_pg_unselected_track_edit_vs_selection_change_and_render(env, monkeypatch):
    """B4B-H3 (P04): E edits track B while no timeline selects it; before E
    commits, R selects B on the timeline, QCs and claims a render. Either E is
    serialized ahead of R (R's QC sees the new cues) or E is refused — never a
    render whose cues changed under the content_version QC approved."""
    import time

    from app.services import timelines as timeline_service

    a = _track(env, "OLD A")
    with session_scope() as db:
        b = subtitle_service.create_track(db, env["project_id"], language_code="ko").id
        subtitle_service.save_cues(
            db, b, [{"start_sec": 0.5, "end_sec": 2.0, "text": "OLD B"}]
        )
    tid = _timeline(env, real=False, track_id=a)
    preset_id = _preset(burn=True)

    e_written = threading.Event()
    r_started = threading.Event()
    real_bump = subtitle_service._bump_timelines_for_track

    def _slow_bump(db, track):
        real_bump(db, track)
        if threading.current_thread().name == "editor":
            e_written.set()  # cues replaced, not committed
            r_started.wait(timeout=10)
            time.sleep(1.0)  # R slips through here unless it is serialized

    monkeypatch.setattr(subtitle_service, "_bump_timelines_for_track", _slow_bump)
    seen: dict[str, object] = {}

    def _qc(db, timeline, **k):
        seen["version"] = int(timeline.content_version or 0)
        seen["cues"] = [c.text for c in subtitle_service.get_cues(db, b)]

    monkeypatch.setattr(qc_orchestrator, "assert_render_allowed", _qc)
    errors: list[BaseException] = []

    def _editor():
        try:
            with session_scope() as db:
                subtitle_service.save_cues(
                    db, b, [{"start_sec": 0.5, "end_sec": 2.0, "text": "NEW B"}]
                )
        except StudioError as exc:
            seen["edit_error"] = exc.code
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def _renderer():
        try:
            assert e_written.wait(timeout=10)
            r_started.set()
            with session_scope() as db:
                timeline_service.update_timeline(
                    db, uuid.UUID(tid), subtitle_track_ids=[str(b)]
                )
            _request(tid, preset_id)
        except StudioError as exc:
            seen["render_error"] = exc.code
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=_editor, name="editor"),
        threading.Thread(target=_renderer, name="renderer"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    tl = _get(tid)
    with session_scope() as db:
        final = [c.text for c in subtitle_service.get_cues(db, b)]
    if tl.status == "rendering":
        assert (tl.content_version != seen["version"]) or (final == seen["cues"]), (
            seen,
            final,
            tl.content_version,
        )
    assert tl.subtitle_track_ids == [str(b)] or "render_error" in seen, seen


# --- B4b-r2-fix: one lock order for every timeline edit (B4BR2-M1) -----------------


@pytest.mark.skipif(
    not _PG,
    reason="needs real row locks (set TEST_DATABASE_URL to a dedicated *_test Postgres DB)",
)
def test_pg_clip_edit_vs_rename_and_clip_edit_do_not_deadlock(env, monkeypatch):
    """B4BR2-M1: A replaces the cut list only; B renames AND replaces it. A
    is paused right after writing its clip rows (before its version bump)
    and B starts there. Before the fix B's rename took the Timeline row, then
    waited on A's clip rows while A waited on the Timeline -> 40P01. Every
    edit now locks the Timeline before any clip row, so B waits for A's
    commit (it cannot rename meanwhile) and both edits apply in order."""
    import time

    from app.services import timelines as timeline_service

    tid = uuid.UUID(_timeline(env, real=False))
    with session_scope() as db:
        first = db.query(TimelineClip).filter(TimelineClip.timeline_id == tid).one()
        video_a = first.video_id
        clip_b = _source_clip(env, real=False)
        video = Video(
            shot_id=env["shot_id"],
            mode="i2v",
            duration_sec=2.5,
            fps=_FPS,
            resolution=f"{_W}x{_H}",
            model_id="stub",
            status="done",
            output_path=str(clip_b.relative_to(env["tmp"])),
        )
        db.add(video)
        db.flush()
        video_b = video.id
        version0 = int(db.get(Timeline, tid).content_version or 0)

    def _order(video_id, out_sec):
        return [
            {
                "shot_id": str(env["shot_id"]),
                "video_id": str(video_id),
                "in_sec": 0.0,
                "out_sec": out_sec,
            }
        ]

    a_clips_written = threading.Event()
    b_renamed = threading.Event()
    seen: dict[str, object] = {}
    real_set_clips = timeline_service._set_clips
    real_rename = timeline_service._rename

    def _set_clips(db, timeline_id, entries):
        real_set_clips(db, timeline_id, entries)
        if threading.current_thread().name == "clip-only":
            a_clips_written.set()  # A's clip rows written, version not bumped
            # Pre-fix B renames at once and A's bump then deadlocks; now B is
            # blocked on the Timeline lock A holds, so this wait times out.
            seen["b_renamed_while_a_open"] = b_renamed.wait(timeout=2.0)

    def _rename(db, timeline, name):
        real_rename(db, timeline, name)
        if threading.current_thread().name == "name+clip":
            b_renamed.set()
            time.sleep(0.2)

    monkeypatch.setattr(timeline_service, "_set_clips", _set_clips)
    monkeypatch.setattr(timeline_service, "_rename", _rename)
    errors: list[tuple[str, str, object]] = []
    done: list[str] = []

    def _run(label, **kwargs):
        try:
            if label == "name+clip":
                assert a_clips_written.wait(timeout=10)
            with session_scope() as db:
                timeline_service.update_timeline(db, tid, **kwargs)
            done.append(label)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            orig = getattr(exc, "orig", None)
            errors.append(
                (label, type(exc).__name__, getattr(orig, "sqlstate", None))
            )

    threads = [
        threading.Thread(
            target=_run,
            name="clip-only",
            args=("clip-only",),
            kwargs={"clip_order": _order(video_a, 1.5)},
        ),
        threading.Thread(
            target=_run,
            name="name+clip",
            args=("name+clip",),
            kwargs={"name": "renamed", "clip_order": _order(video_b, 2.0)},
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert not errors, errors  # no 40P01 deadlock, no other failure
    assert done == ["clip-only", "name+clip"], done  # serialized, A first
    assert seen["b_renamed_while_a_open"] is False, seen
    tl = _get(str(tid))
    assert tl.name == "renamed"
    assert tl.status == "draft"
    assert int(tl.content_version or 0) == version0 + 2
    with session_scope() as db:
        clips = timeline_service.get_clip_order(db, tid)
    assert [(c["video_id"], c["out_sec"]) for c in clips] == [(str(video_b), 2.0)]


# --- B4b-r2-fix: bounded glyph-mask cache (B4BR2-M2) -------------------------------


def test_glyph_mask_cache_is_bounded_for_many_distinct_cues(tmp_path, monkeypatch):
    """B4BR2-M2: the postflight kept one full-frame mask per distinct cue
    text for the whole run (4K x 500 cues ~ 4 GB). The cache now holds at
    most MASK_CACHE_MAX_ENTRIES glyph bounding boxes, while every returned
    mask still equals the full-frame render. Synthetic masks: no ffmpeg."""
    from PIL import Image, ImageDraw

    from app.media import subtitle_render

    w, h = 1920, 1080
    box_w, box_h = 300, 60

    def _box(text: str) -> tuple[int, int, int, int]:
        n = int(text.split()[-1])
        x, y = (n * 37) % (w - box_w), h - box_h - 20 - (n % 5) * 10
        return x, y, x + box_w, y + box_h

    def _fake_render(texts, style, dst, *, width, height):
        img = Image.new("L", (width, height), 0)
        if texts[0] != "blank 0":
            ImageDraw.Draw(img).rectangle(_box(texts[0]), fill=255)
        img.save(dst)
        return dst

    monkeypatch.setattr(subtitle_render, "render_glyph_mask", _fake_render)
    style = {"font_size_px": 48, "position": "bottom"}
    cap = render_service.MASK_CACHE_MAX_ENTRIES
    cache = render_service._MaskCache()
    frame_px = w * h
    box_px = (box_w + 1) * (box_h + 1)  # PIL rectangle includes the edge
    peak_len = peak_bytes = 0
    n = cap * 4 + 7
    for i in range(n):
        text = f"cue {i}"
        mask = render_service._glyph_mask(text, style, (w, h), tmp_path, cache)
        assert mask.size == (w, h)
        assert mask.getbbox() == tuple(
            v + (1 if k >= 2 else 0) for k, v in enumerate(_box(text))
        )
        assert mask.histogram()[255] == box_px
        peak_len = max(peak_len, len(cache))
        peak_bytes = max(peak_bytes, cache.nbytes())
    assert cache.renders == n
    assert peak_len == cap  # never more entries than the constant cap
    assert peak_bytes <= cap * box_px  # glyph boxes only, never full frames
    assert peak_bytes < frame_px  # all entries together < ONE full frame
    assert not list(tmp_path.glob("mask_*"))  # rendered files are not kept

    # LRU: a recent cue is a hit; the oldest was evicted and re-renders.
    render_service._glyph_mask(f"cue {n - 1}", style, (w, h), tmp_path, cache)
    assert cache.renders == n
    render_service._glyph_mask("cue 0", style, (w, h), tmp_path, cache)
    assert cache.renders == n + 1
    assert len(cache) == cap

    # A blank mask keeps no pixels at all.
    blank = render_service._glyph_mask("blank 0", style, (w, h), tmp_path, cache)
    assert blank.getbbox() is None and blank.size == (w, h)
    assert cache.nbytes() <= cap * box_px
