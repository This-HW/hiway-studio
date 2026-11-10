"""Timeline render: claim, execution, postflight and publish (F-15, P0-18,
ADR-0013, plan D4/D8).

Request (API, `request_render`): validates the cut list and the QC gate
(ADR-0006: a fresh pass/warn report is required), then CLAIMS the timeline
with one conditional UPDATE (`status != 'rendering'` -> 'rendering', new
`exec_token`, lease). Zero rows -> 409 RENDER_LOCKED. While `rendering` the
timeline is read-only: clip/audio/subtitle edits raise RENDER_LOCKED.

Execution (worker, `render_timeline`): the delivery marks the start
(`heartbeat_at`, only while it is NULL — a duplicate delivery is a no-op),
renders in its own scratch dir `timelines/<id>/attempts/<token>/` (no
shared file names), keeps the subtitle-free intermediate as the postflight
reference and links the final file to an immutable per-attempt name
`timelines/<id>/output_<token8>.mp4`. A background heartbeat extends the
lease; no DB transaction is held across ffmpeg.

Postflight (D5/D8): `postflight_encoding` must pass (its probe also gives
the output duration; unknown/zero duration -> not published). Cues are
clipped to the part of the output that shows them. Every burned-in track
must change the pixels of ITS OWN glyph footprint against the reference
(track-specific pixel presence, any glyph size) and, when glyphs are large
enough for OCR, `postflight_burnin` must pass. OCR engine missing / glyphs
too small / symbol-only cues (no letters or digits to OCR) -> published,
recorded as `unverified` in `render_meta`. Any other non-pass -> not
published.

Publish = conditional UPDATE `output_path, status='done', render_meta`
WHERE id + exec_token + status='rendering'. Zero rows (the execution was
reconciled/superseded, RH3 ABA) -> nothing changes and the candidate stays
an unreferenced orphan. Failures (StudioError or crash) are committed as
`failed` by the same token-guarded UPDATE before re-raising; the previous
`output_path` is never touched. SSE `render.completed` is sent only after
the commit.
"""

from __future__ import annotations

import errno
import logging
import math
import os
import shutil
import tempfile
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image, ImageChops, ImageFilter
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core import defaults
from app.core.celery_app import dispatch_job
from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge
from app.db.session import dispatch_after_commit, session_scope
from app.files import immutability, paths
from app.media import ffmpeg_ops, subtitle_render
from app.models.timeline import Timeline

log = logging.getLogger("ai_studio.services.render")

RENDER_TASK = "q.post.render_timeline"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_until(now: datetime) -> datetime:
    return now + timedelta(seconds=defaults.LEASE_TTL_SEC)


def _get_timeline(db: Session, timeline_id: uuid.UUID):
    timeline = db.get(Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found.")
    return timeline


def _get_preset(db: Session, preset_id: uuid.UUID):
    from app.models.system import ExportPreset

    preset = db.get(ExportPreset, preset_id) if preset_id is not None else None
    if preset is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Export preset not found.")
    return preset


def render_locked(timeline_id: Any) -> StudioError:
    return StudioError(
        ErrorCode.RENDER_LOCKED,
        "Timeline is rendering; edits are locked until it finishes.",
        retryable=True,
        details={"timeline_id": str(timeline_id)},
    )


def _require_editable(timeline) -> None:
    if timeline.status == "rendering":
        raise render_locked(timeline.id)


def _validate_clips(db: Session, timeline) -> list[dict[str, Any]]:
    """Return the resolved clip list; raise when a clip is not renderable.

    The cut list is read from `timeline_clips` (ADR-0007) in position order.
    """
    from app.models.generation import Video
    from app.services.timelines import get_clip_order

    clips = get_clip_order(db, timeline.id)
    if not clips:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Timeline has no clips; add clips before rendering.",
        )
    resolved = []
    root = paths.projects_root()
    for i, clip in enumerate(clips):
        video_id = clip.get("video_id")
        in_sec = float(clip.get("in_sec", 0.0))
        out_sec = float(clip.get("out_sec", 0.0))
        if out_sec <= in_sec:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Clip #{i} has an empty range (in_sec >= out_sec).",
                details={"clip_index": i},
            )
        video = db.get(Video, uuid.UUID(str(video_id))) if video_id else None
        if video is None or video.status != "done" or not video.output_path:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Clip #{i} does not reference a finished video.",
                details={"clip_index": i, "video_id": str(video_id)},
            )
        abs_path = root / video.output_path
        if not abs_path.is_file():
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Clip #{i} file is missing on disk.",
                details={"clip_index": i, "output_path": video.output_path},
            )
        resolved.append(
            {
                "index": i,
                "shot_id": clip.get("shot_id"),
                "video_id": str(video.id),
                "src": abs_path,
                "in_sec": in_sec,
                "out_sec": out_sec,
            }
        )
    return resolved


# --- request (API) ----------------------------------------------------------------


def request_render(
    db: Session,
    timeline_id: uuid.UUID,
    *,
    export_preset_id: uuid.UUID,
    ignore_warnings: bool = False,
) -> dict[str, Any]:
    """Validate, claim and enqueue a render. Returns {"job_id", "estimated_cost_usd"}.

    `job_id` is the timeline id: renders are tracked as timeline state, not
    as generation rows (the render SSE events carry `timeline_id`).
    The claim is one conditional UPDATE so two concurrent requests cannot
    both win (Postgres re-evaluates the WHERE after the row lock; SQLite
    serializes writers). It also pins the content_version the QC gate
    approved: an edit that slipped in between fails the claim as stale.
    """
    timeline = _get_timeline(db, timeline_id)
    tid = timeline.id
    _require_editable(timeline)
    preset = _get_preset(db, export_preset_id)
    _validate_clips(db, timeline)  # fail fast before locking

    from app.services import qc_orchestrator

    qc_orchestrator.assert_render_allowed(  # ADR-0006: 409 QC_FAILED
        db, timeline, ignore_warnings=ignore_warnings
    )
    gated_version = int(timeline.content_version or 0)

    check_enum("render_status", "rendering")
    now = _now()
    result = db.execute(
        update(Timeline)
        .where(
            Timeline.id == tid,
            Timeline.status != "rendering",
            func.coalesce(Timeline.content_version, 0) == gated_version,
        )
        .values(
            status="rendering",
            exec_token=uuid.uuid4(),
            # Not started yet: reconcile re-dispatches after this deadline.
            lease_expires_at=_lease_until(now),
            heartbeat_at=None,
            output_preset_id=preset.id,
            error_code=None,
            error_message=None,
        )
        .execution_options(synchronize_session=False)
    )
    db.expire(timeline)
    if result.rowcount != 1:
        if timeline.status == "rendering":
            raise render_locked(tid)
        raise StudioError(
            ErrorCode.QC_FAILED,
            "QC report is stale: the timeline changed after the QC run.",
            details={"reason": "stale", "timeline_id": str(tid)},
        )

    dispatch_after_commit(db, lambda: dispatch_job(RENDER_TASK, str(tid)))
    return {"job_id": tid, "estimated_cost_usd": Decimal("0")}


# --- execution ownership (worker) --------------------------------------------------


class RenderSuperseded(Exception):
    """The execution no longer owns the timeline (0-row guarded write)."""


@dataclass
class RenderExecution:
    timeline_id: uuid.UUID
    token: uuid.UUID
    lost: threading.Event = field(default_factory=threading.Event)


def _owned(ex: RenderExecution) -> tuple:
    return (
        Timeline.id == ex.timeline_id,
        Timeline.exec_token == ex.token,
        Timeline.status == "rendering",
    )


def begin_execution(timeline_id: uuid.UUID) -> RenderExecution | None:
    """Mark the claimed render as started by this delivery (committed).

    Only while `heartbeat_at IS NULL`: a duplicate or late delivery of the
    same request, a terminal timeline, or one without a token -> None (no-op).
    """
    now = _now()
    with session_scope() as db:
        token = db.execute(
            select(Timeline.exec_token).where(
                Timeline.id == timeline_id,
                Timeline.status == "rendering",
                Timeline.heartbeat_at.is_(None),
            )
        ).scalar_one_or_none()
        if token is None:
            return None
        result = db.execute(
            update(Timeline)
            .where(
                Timeline.id == timeline_id,
                Timeline.exec_token == token,
                Timeline.status == "rendering",
                Timeline.heartbeat_at.is_(None),
            )
            .values(heartbeat_at=now, lease_expires_at=_lease_until(now))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
    return RenderExecution(timeline_id=timeline_id, token=token)


def _guarded(ex: RenderExecution, values: dict[str, Any]) -> bool:
    """UPDATE ... WHERE id + exec_token + status='rendering', committed."""
    with session_scope() as db:
        result = db.execute(
            update(Timeline)
            .where(*_owned(ex))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        ok = result.rowcount == 1
    if not ok:
        ex.lost.set()
    return ok


def heartbeat(ex: RenderExecution) -> bool:
    now = _now()
    return _guarded(ex, {"heartbeat_at": now, "lease_expires_at": _lease_until(now)})


@contextmanager
def _keepalive(ex: RenderExecution) -> Iterator[RenderExecution]:
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(float(defaults.HEARTBEAT_INTERVAL_SEC)):
            try:
                if not heartbeat(ex):
                    return
            except Exception:  # noqa: BLE001 - heartbeat must never kill the render
                log.exception("render heartbeat failed timeline=%s", ex.timeline_id)

    thread = threading.Thread(
        target=_loop, name=f"render-heartbeat-{ex.timeline_id}", daemon=True
    )
    thread.start()
    try:
        yield ex
    finally:
        stop.set()
        thread.join(timeout=5)


def _ensure_owner(ex: RenderExecution) -> None:
    if ex.lost.is_set():
        raise RenderSuperseded(str(ex.timeline_id))


def publish_output(ex: RenderExecution, rel_path: str, meta: dict[str, Any]) -> bool:
    """THE publish act (D8): point the timeline at the verified attempt file.

    Returns False when this execution no longer owns the timeline; then
    nothing changed and the attempt file is an orphan.
    """
    check_enum("render_status", "done")
    return _guarded(
        ex,
        {
            "status": "done",
            "output_path": paths.assert_relative(rel_path),
            "render_meta": meta,
            "lease_expires_at": None,
            "heartbeat_at": _now(),
            "error_code": None,
            "error_message": None,
        },
    )


def record_render_failure(ex: RenderExecution, code: str, message: str) -> bool:
    """Commit `failed` for this execution (output_path untouched)."""
    check_enum("render_status", "failed")
    return _guarded(
        ex,
        {
            "status": "failed",
            "error_code": code,
            "error_message": message[:2000],
            "lease_expires_at": None,
            "heartbeat_at": _now(),
        },
    )


def _sse(event: str, payload: dict[str, Any]) -> None:
    try:
        get_bridge().publish(CHANNEL, event, payload)
    except Exception:  # noqa: BLE001 - SSE is best effort; the row is committed
        log.warning("render SSE %s skipped", event)


def _publish_completed(timeline_id: str, output_path: str, status: str) -> None:
    _sse(
        "render.completed",
        {"timeline_id": timeline_id, "output_path": output_path, "status": status},
    )


def _progress(timeline_id: str, pct: float, stage: str) -> None:
    _sse(
        "job.progress",
        {
            "job_id": timeline_id,
            "job_type": "render",
            "status": "running",
            "progress_pct": pct,
            "stage": stage,
        },
    )


# --- render plan (one short read transaction) ----------------------------------


@dataclass
class _RenderPlan:
    timeline_id: str
    project_id: str
    content_version: int
    preset: SimpleNamespace
    width: int
    height: int
    fps: float
    sample_rate: int
    clips: list[dict[str, Any]]
    # (cues, effective style) per burned track, positions already separated
    # exactly like subtitle_render.write_ass_sections does for dual mode.
    sections: list[tuple[list, dict]]
    audio_inputs: list[tuple]  # (abs_path, start_sec, duration_sec|None, gain_db)

    @property
    def out_rel_dir(self) -> str:
        return f"projects/{self.project_id}/timelines/{self.timeline_id}"


def _burn_sections(db: Session, timeline, preset) -> list[tuple[list, dict]]:
    from app.models.subtitle import SubtitleCue, SubtitleTrack
    from app.services import subtitles as subtitle_service

    burn = (timeline.subtitle_mode or "none") != "none" and bool(
        getattr(preset, "burn_subtitles", False)
    )
    if not burn:
        return []
    tracks = []
    for tid in timeline.subtitle_track_ids or []:
        try:
            track = db.get(SubtitleTrack, uuid.UUID(str(tid)))
        except ValueError:
            continue
        if track is not None:
            tracks.append(track)
    tracks.sort(key=lambda t: t.display_order)
    if (timeline.subtitle_mode or "single") == "single":
        tracks = tracks[:1]
    sections: list[tuple[list, dict]] = []
    seen_positions: list[str] = []
    for track in tracks:
        cues = [
            SimpleNamespace(
                start_sec=float(c.start_sec),
                end_sec=float(c.end_sec),
                text=c.text or "",
                style_overrides=c.style_overrides,
            )
            for c in db.query(SubtitleCue)
            .filter(SubtitleCue.track_id == track.id)
            .order_by(SubtitleCue.start_sec)
            .all()
        ]
        if not cues:
            continue
        style = dict(subtitle_service.get_style(db, timeline.project_id, track.id))
        # Dual: a same-position track moves to the opposite position (the
        # rule of write_ass_sections), so postflight checks the right band.
        position = style.get("position") or "bottom"
        if position in seen_positions:
            position = subtitle_render._opposite_position(position)
            style["position"] = position
        seen_positions.append(position)
        sections.append((cues, style))
    return sections


def _audio_inputs(db: Session, timeline) -> list[tuple]:
    from app.models.audio import AudioGeneration, TimelineAudioClip

    root = paths.projects_root()
    inputs: list[tuple] = []
    for clip in (
        db.query(TimelineAudioClip)
        .filter(TimelineAudioClip.timeline_id == timeline.id)
        .order_by(TimelineAudioClip.start_sec)
        .all()
    ):
        audio = db.get(AudioGeneration, clip.audio_id)
        if audio is None or audio.status != "done" or not audio.output_path:
            continue
        abs_path = root / audio.output_path
        if abs_path.is_file():
            inputs.append(
                (
                    abs_path,
                    float(clip.start_sec),
                    float(clip.duration_sec)
                    if clip.duration_sec
                    else (audio.duration_sec or None),
                    float(clip.gain_db or 0.0),
                )
            )
    return inputs


def _load_plan(ex: RenderExecution) -> _RenderPlan:
    with session_scope() as db:
        timeline = _get_timeline(db, ex.timeline_id)
        preset = _get_preset(db, timeline.output_preset_id)
        width, height = ffmpeg_ops.parse_resolution(preset.resolution)
        clips = _validate_clips(db, timeline)
        return _RenderPlan(
            timeline_id=str(timeline.id),
            project_id=str(timeline.project_id),
            content_version=int(timeline.content_version or 0),
            preset=SimpleNamespace(
                id=str(preset.id),
                resolution=preset.resolution,
                fps=preset.fps,
                codec=preset.codec,
                burn_subtitles=bool(preset.burn_subtitles),
            ),
            width=width,
            height=height,
            fps=float(preset.fps or defaults.OUTPUT_FPS),
            sample_rate=int(defaults.OUTPUT_AUDIO_SAMPLE_RATE),
            clips=clips,
            sections=_burn_sections(db, timeline, preset),
            audio_inputs=_audio_inputs(db, timeline),
        )


# --- media -----------------------------------------------------------------------


def _finalize(plan: _RenderPlan, intermediate: Path, scratch: Path) -> Path:
    """Subtitle burn-in + audio mix pass (design §10.2, §4.14).

    Returns `intermediate` unchanged when there is nothing to burn or mix,
    otherwise a new re-encoded file in the attempt scratch dir.
    """
    ass_path = None
    if plan.sections:
        ass_path = scratch / "subs.ass"
        subtitle_render.write_ass_sections(
            plan.sections, ass_path, width=plan.width, height=plan.height
        )
    if ass_path is None and not plan.audio_inputs:
        return intermediate

    inputs = [str(intermediate)] + [str(p) for p, _, _, _ in plan.audio_inputs]
    filters: list[str] = []
    vmap, amap = "0:v", "0:a"
    if ass_path is not None:
        esc = (
            str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        )
        filters.append(f"[0:v]subtitles='{esc}'[vout]")
        vmap = "[vout]"
    if plan.audio_inputs:
        chains = ["[0:a]aresample=48000[a0]"]
        labels = ["[a0]"]
        for i, (_, start, dur, gain) in enumerate(plan.audio_inputs, start=1):
            ms = int(round(start * 1000))
            trim = f"atrim=0:{dur:.3f}," if dur else ""
            chains.append(
                f"[{i}:a]aresample=48000,{trim}adelay={ms}|{ms},volume={gain}dB[a{i}]"
            )
            labels.append(f"[a{i}]")
        chains.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[aout]"
        )
        filters.append(";".join(chains))
        amap = "[aout]"

    final_abs = scratch / "final.mp4"
    ffmpeg_ops.finalize_render(
        inputs,
        final_abs,
        filter_complex=";".join(filters) if filters else None,
        vmap=vmap,
        amap=amap,
        fps=plan.fps,
        sample_rate=plan.sample_rate,
    )
    return final_abs


_NO_HARDLINK_ERRNOS = {errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP}


def _link_new(src: Path, dst: Path) -> None:
    """Expose `src` at `dst` without ever overwriting (immutability §11.3)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return
    except FileExistsError:
        raise immutability.ImmutableViolation(str(dst)) from None
    except OSError as exc:
        if exc.errno not in _NO_HARDLINK_ERRNOS:
            raise
    try:
        out = open(dst, "xb")
    except FileExistsError:
        raise immutability.ImmutableViolation(str(dst)) from None
    try:
        with out, open(src, "rb") as inp:
            shutil.copyfileobj(inp, out)
    except BaseException:
        dst.unlink(missing_ok=True)
        raise


# --- postflight ------------------------------------------------------------------


def _glyph_px(style: dict, height: int) -> int:
    """Rendered font size in px (same scaling as subtitle_render._style_line)."""
    size = int(style.get("font_size_px") or 48)
    return max(8, int(round(size * height / 1080)))


def _frame(video: Path, at_sec: float, dst: Path) -> Image.Image:
    ffmpeg_ops.extract_frame(video, dst, at_sec=at_sec)
    if not dst.is_file() or dst.stat().st_size == 0:
        raise OSError(f"no frame decoded at {at_sec:.2f}s of {video.name}")
    with Image.open(dst) as img:
        return img.convert("L")


def _visible_cues(cues: list, duration: float, fps: float) -> tuple[list, int]:
    """Cues clipped to the part of the output that shows them (B4B-H1).

    The checkable window is [0, duration - 1 frame) (the last frame starts
    there). A cue is checked when it intersects that window for at least one
    frame interval; its range is CLIPPED to the intersection so presence and
    OCR both sample inside what the output really shows (a cue [0.1, 6] on a
    2.5s output is checked on [0.1, ~2.46]). Returns (clipped cues, number
    of cues not visible: after the end, or shown for less than one frame)."""
    frame = 1.0 / fps if fps and fps > 0 else 1.0 / float(defaults.OUTPUT_FPS)
    last = duration - frame
    visible: list = []
    skipped = 0
    for cue in cues:
        start = max(0.0, float(cue.start_sec))
        end = min(float(cue.end_sec), last)
        if end - start < frame:
            skipped += 1
            continue
        visible.append(
            SimpleNamespace(
                start_sec=start,
                end_sec=end,
                text=cue.text,
                style_overrides=getattr(cue, "style_overrides", None),
            )
        )
    return visible, skipped


def _binary(img: Image.Image, threshold: int) -> Image.Image:
    return img.point(lambda v: 255 if v > threshold else 0)


def _count(mask: Image.Image) -> int:
    return mask.histogram()[255]


# Glyph masks kept per postflight (B4BR2-M2): an LRU of at most this many
# CROPPED masks, so memory is bounded by a constant, not cue count x frame size.
MASK_CACHE_MAX_ENTRIES: int = 32


class _MaskCache:
    """Bounded LRU of glyph masks: key -> (cropped binary mask | None, (x, y)).

    Only the glyph bounding box is kept (a blank mask keeps nothing); the
    full-frame mask is rebuilt per use and dropped with the cue. At most
    `max_entries` entries — the least recently used is evicted (re-rendered
    if needed again)."""

    def __init__(self, max_entries: int = MASK_CACHE_MAX_ENTRIES) -> None:
        self.max_entries = max(1, int(max_entries))
        self._items: OrderedDict = OrderedDict()
        self.renders = 0  # masks rendered (cache misses)

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key):
        item = self._items.get(key)
        if item is not None:
            self._items.move_to_end(key)
        return item

    def put(self, key, item) -> None:
        self._items[key] = item
        self._items.move_to_end(key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def nbytes(self) -> int:
        """Pixel payload held (mode L: 1 byte per pixel)."""
        return sum(
            crop.width * crop.height
            for crop, _ in self._items.values()
            if crop is not None
        )


def _glyph_mask(
    text: str,
    style: dict,
    size: tuple[int, int],
    work: Path,
    cache: _MaskCache,
) -> Image.Image:
    """Binary footprint (fill + outline) of `text` drawn alone in `style` at
    the output size — where THIS track's cue must change pixels. Cached
    cropped to its bounding box (B4BR2-M2); returned as a full-frame mask."""
    key = (repr(sorted(style.items())), text, size)
    item = cache.get(key)
    if item is None:
        cache.renders += 1
        dst = work / f"mask_{cache.renders}.png"
        subtitle_render.render_glyph_mask(
            [text], style, dst, width=size[0], height=size[1]
        )
        with Image.open(dst) as img:
            full = _binary(img.convert("L"), defaults.RENDER_BURNIN_MASK_LUMA)
        dst.unlink(missing_ok=True)
        dst.with_suffix(".ass").unlink(missing_ok=True)
        box = full.getbbox()
        item = (full.crop(box), box[:2]) if box else (None, (0, 0))
        cache.put(key, item)
    crop, offset = item
    mask = Image.new("L", size, 0)
    if crop is not None:
        mask.paste(crop, offset)
    return mask


def burnin_presence(
    output: Path,
    reference: Path,
    cues: list,
    style: dict,
    *,
    others: list[tuple[list, dict]] | tuple = (),
    mask_cache: _MaskCache | None = None,
) -> dict[str, Any]:
    """Every cue of THIS track must change the pixels of its own glyphs.

    Track-specific evidence (B4B-H2): at the midpoint of each (clipped) cue
    the cue is drawn alone in this track's style on a blank canvas of the
    output size (`subtitle_render.render_glyph_mask`) — its glyph + outline
    footprint, dilated 1px. Every pixel another track's cue active at that
    time would draw (`others`, dilated 2px) is removed, so another track's
    subtitle can never prove this one present. Inside what remains, pixels
    whose luma differs from the subtitle-free reference by more than
    RENDER_BURNIN_PIXEL_DELTA must number at least
    max(RENDER_BURNIN_MIN_CHANGED_PX, RENDER_BURNIN_MASK_MIN_FRACTION x
    footprint). Catches a missing or transparent burn-in at any glyph size;
    an undecodable frame / mask or a footprint hidden by another track fails.
    """
    targets = [c for c in cues if (c.text or "").strip()]
    if not targets:
        return {
            "code": "burnin_presence",
            "status": "pass",
            "applicable": False,
            "details": "No subtitle cue text to verify.",
            "measured": [],
        }
    cache = mask_cache if mask_cache is not None else _MaskCache()
    minimum = defaults.RENDER_BURNIN_MIN_CHANGED_PX
    fraction = defaults.RENDER_BURNIN_MASK_MIN_FRACTION
    delta = defaults.RENDER_BURNIN_PIXEL_DELTA
    measured: list[dict[str, Any]] = []
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="render_presence_") as d:
        work = Path(d)
        for i, cue in enumerate(targets):
            mid = (float(cue.start_sec) + float(cue.end_sec)) / 2
            entry: dict[str, Any] = {"cue": i, "at_sec": round(mid, 2)}
            measured.append(entry)
            try:
                out = _frame(output, mid, work / f"out_{i}.png")
                ref = _frame(reference, mid, work / f"ref_{i}.png")
                size = out.size
                own = _glyph_mask(cue.text, style, size, work, cache).filter(
                    ImageFilter.MaxFilter(3)
                )
                foreign = Image.new("L", size, 0)
                for other_cues, other_style in others:
                    for other in other_cues:
                        if not (other.text or "").strip():
                            continue
                        if float(other.start_sec) <= mid < float(other.end_sec):
                            foreign = ImageChops.lighter(
                                foreign,
                                _glyph_mask(
                                    other.text, other_style, size, work, cache
                                ).filter(ImageFilter.MaxFilter(5)),
                            )
                footprint = ImageChops.subtract(own, foreign)
                diff = _binary(ImageChops.difference(out, ref), delta)
                changed = _count(ImageChops.multiply(diff, footprint))
                mask_px = _count(footprint)
            except Exception as exc:  # noqa: BLE001 - unverifiable = fail
                entry["error"] = str(exc)[:200]
                problems.append(
                    f"cue {i} @{mid:.2f}s: not verifiable ({str(exc)[:120]})"
                )
                continue
            need = max(minimum, math.ceil(fraction * mask_px))
            entry.update(changed_px=changed, mask_px=mask_px, need_px=need)
            if mask_px < minimum:
                problems.append(
                    f"cue {i} @{mid:.2f}s: glyph footprint {mask_px} px (hidden by "
                    "another track) — presence not verifiable"
                )
            elif changed < need:
                problems.append(
                    f"cue {i} @{mid:.2f}s: {changed} of {mask_px} glyph px changed "
                    f"(min {need}) — burn-in missing or transparent"
                )
    if problems:
        return {
            "code": "burnin_presence",
            "status": "fail",
            "applicable": True,
            "details": "; ".join(problems[:5]),
            "measured": measured,
        }
    return {
        "code": "burnin_presence",
        "status": "pass",
        "applicable": True,
        "details": f"{len(targets)} cue(s) change their own glyph pixels.",
        "measured": measured,
    }


def _postflight(
    plan: _RenderPlan, candidate: Path, reference: Path
) -> tuple[dict[str, Any], list[str], list[str]]:
    """(results, problems, unverified). Any problem blocks publishing."""
    from app.services import qc_orchestrator

    enc = qc_orchestrator.postflight_encoding(candidate, plan.preset)
    results: dict[str, Any] = {
        "encoding_match": enc.to_dict(),
        "burnin_presence": [],
        "burnin_ocr": [],
    }
    problems: list[str] = []
    unverified: list[str] = []
    if enc.status != "pass":
        problems.append(f"encoding_match: {enc.details}")
    if not plan.sections:
        return results, problems, unverified

    # The ONE probe of postflight_encoding supplies the duration (B4B-M1): a
    # failed / zero / unknown duration blocks publishing instead of skipping
    # every cue as "after the output end".
    facts = enc.measured if isinstance(enc.measured, dict) else {}
    try:
        duration = float(facts.get("duration_sec") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0.0:
        if enc.status == "pass":
            problems.append(
                "burnin: output duration unknown or zero; burned-in subtitles "
                "cannot be verified"
            )
        return results, problems, unverified

    visible_sections: list[tuple[list, dict]] = []
    for i, (cues, style) in enumerate(plan.sections):
        visible, skipped = _visible_cues(cues, duration, plan.fps)
        if skipped:
            unverified.append(
                f"track {i}: {skipped} cue(s) not shown before the output end "
                f"({duration:.2f}s) not checked"
            )
        visible_sections.append((visible, style))

    mask_cache = _MaskCache()  # bounded, shared by all tracks (B4BR2-M2)
    for i, (rendered, style) in enumerate(visible_sections):
        presence = burnin_presence(
            candidate,
            reference,
            rendered,
            style,
            others=[s for j, s in enumerate(plan.sections) if j != i],
            mask_cache=mask_cache,
        )
        results["burnin_presence"].append({"track": i, **presence})
        if presence["status"] != "pass":
            problems.append(f"burnin_presence (track {i}): {presence['details']}")
        glyph = _glyph_px(style, plan.height)
        if glyph < defaults.RENDER_BURNIN_OCR_MIN_GLYPH_PX:
            unverified.append(
                f"burnin_ocr (track {i}): glyph {glyph}px too small for OCR "
                f"(< {defaults.RENDER_BURNIN_OCR_MIN_GLYPH_PX}px); pixel presence only"
            )
            continue
        ocr_result = qc_orchestrator.postflight_burnin(
            candidate, rendered, reference_path=reference, style=style
        )
        results["burnin_ocr"].append({"track": i, **ocr_result.to_dict()})
        if ocr_result.status == "pass" or not ocr_result.applicable:
            continue
        if ocr_result.status == "warn" and ocr_result.stub:
            # OCR engine not installed: publish, but say it was not verified.
            unverified.append(f"burnin_ocr (track {i}): {ocr_result.details}")
            continue
        if qc_orchestrator.burnin_symbol_only_unverified(ocr_result):
            # Cues without letters/digits ("...", "♪") cannot be OCR-compared;
            # like too-small glyphs they rely on pixel presence (WA-R2-M1).
            unverified.append(f"burnin_ocr (track {i}): {ocr_result.details}")
            continue
        problems.append(f"burnin_ocr (track {i}): {ocr_result.details}")
    return results, problems, unverified


# --- worker entry ------------------------------------------------------------------


def _produce(
    ex: RenderExecution, plan: _RenderPlan, scratch: Path
) -> tuple[str, dict[str, Any]]:
    """Render + postflight one attempt. Returns (candidate rel path, meta)."""
    tid = plan.timeline_id
    root = paths.projects_root()
    scratch.mkdir(parents=True, exist_ok=True)

    _progress(tid, 5.0, "normalizing")
    segments: list[str] = []
    total = len(plan.clips)
    for n, clip in enumerate(plan.clips):
        _ensure_owner(ex)
        seg = scratch / f"seg_{n:04d}.mp4"
        ffmpeg_ops.extract_segment(
            clip["src"],
            seg,
            clip["in_sec"],
            clip["out_sec"],
            width=plan.width,
            height=plan.height,
            fps=plan.fps,
            sample_rate=plan.sample_rate,
        )
        segments.append(str(seg))
        _progress(tid, 5.0 + 75.0 * (n + 1) / total, "normalizing")

    _ensure_owner(ex)
    _progress(tid, 82.0, "concat")
    # Subtitle-free intermediate: kept as the burn-in postflight reference.
    intermediate = scratch / "intermediate.mp4"
    ffmpeg_ops.concat_mp4(segments, intermediate)
    final = _finalize(plan, intermediate, scratch)

    _ensure_owner(ex)
    rel = paths.assert_relative(f"{plan.out_rel_dir}/output_{ex.token.hex[:8]}.mp4")
    candidate = root / rel
    _link_new(final, candidate)

    _progress(tid, 92.0, "postflight")
    results, problems, unverified = _postflight(plan, candidate, intermediate)
    if problems:
        raise StudioError(
            ErrorCode.QC_FAILED,
            "Render postflight failed; output not published: " + " | ".join(problems),
            details={"postflight": results, "candidate": rel},
        )
    meta = {
        "exec_token": str(ex.token),
        "output_path": rel,
        "preset_id": plan.preset.id,
        "content_version": plan.content_version,
        "rendered_at": _now().isoformat(),
        "postflight": results,
        "unverified": unverified,
    }
    return rel, meta


def _as_studio_error(exc: BaseException) -> StudioError:
    if isinstance(exc, StudioError):
        return exc
    return StudioError(
        ErrorCode.PROVIDER_ERROR,
        f"Unexpected render failure ({exc.__class__.__name__}); see worker logs.",
    )


def render_timeline(timeline_id: str) -> dict[str, Any]:
    """Worker entry (q.post.render_timeline, single arg = timeline id).

    Returns a summary dict. Raises after committing `failed` on any error.
    """
    ex = begin_execution(uuid.UUID(timeline_id))
    if ex is None:
        with session_scope() as db:
            row = db.get(Timeline, uuid.UUID(timeline_id))
            status = row.status if row is not None else "missing"
        log.info(
            "render delivery is a no-op timeline=%s status=%s", timeline_id, status
        )
        return {"timeline_id": timeline_id, "status": status, "skipped": True}

    scratch: Path | None = None
    try:
        try:
            with _keepalive(ex):
                plan = _load_plan(ex)
                scratch = (
                    paths.projects_root() / plan.out_rel_dir / "attempts" / ex.token.hex
                )
                rel, meta = _produce(ex, plan, scratch)
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
        # Publish is inside the same failure boundary (B4B-M2): an exception
        # here commits `failed` through the token CAS below. When the `done`
        # commit itself succeeded and only its acknowledgement was lost, that
        # CAS (status='rendering') matches 0 rows and `done` stays.
        published = publish_output(ex, rel, meta)
    except RenderSuperseded:
        log.warning(
            "render superseded mid-run timeline=%s token=%s", timeline_id, ex.token
        )
        return {"timeline_id": timeline_id, "status": "superseded"}
    except Exception as exc:
        err = _as_studio_error(exc)
        if err is not exc:
            log.exception("render crashed timeline=%s", timeline_id)
        if record_render_failure(ex, err.code, err.message):
            _publish_completed(timeline_id, "", "failed")
        else:
            log.warning(
                "render failure not recorded (superseded or already published) "
                "timeline=%s",
                timeline_id,
            )
        raise

    if not published:
        log.warning(
            "render superseded before publish timeline=%s; %s left as an orphan",
            timeline_id,
            rel,
        )
        return {"timeline_id": timeline_id, "status": "superseded", "orphan_path": rel}
    _publish_completed(timeline_id, rel, "done")
    return {"timeline_id": timeline_id, "status": "done", "output_path": rel}


def fail_timeline_unexpected(timeline_id: str) -> None:
    """Safety net for the task wrapper when render_timeline crashed before it
    could record anything (e.g. before the start was marked).

    Only rows WITHOUT an execution token are touched (legacy rows); token-
    owned renders are failed by their own execution or by reconcile, so
    this can never overwrite a newer render (ABA). Never raises.
    """
    try:
        check_enum("render_status", "failed")
        with session_scope() as db:
            result = db.execute(
                update(Timeline)
                .where(
                    Timeline.id == uuid.UUID(timeline_id),
                    Timeline.status == "rendering",
                    Timeline.exec_token.is_(None),
                )
                .values(
                    status="failed",
                    error_code=ErrorCode.PROVIDER_ERROR,
                    error_message="Unexpected render failure; see worker logs.",
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            failed = result.rowcount == 1
        if failed:
            _publish_completed(timeline_id, "", "failed")
    except Exception:  # noqa: BLE001 - last-resort safety net
        log.exception("fail_timeline_unexpected failed timeline=%s", timeline_id)
