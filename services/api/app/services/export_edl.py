"""Timeline export to NLE interchange formats (Phase 6, P2, ADR-0010).

Builds FCP7 XML (xmeml v4, readable by Premiere Pro / DaVinci Resolve) and
CMX3600 EDL from a timeline's cut list. Pure functions: deterministic —
the same timeline always yields byte-identical output. No jobs, no cost.

Source media comes from `timeline_clips -> videos.output_path` (video) and
`timeline_audio_clips -> audio_generations.output_path` (audio). All stored
paths are project-relative; the export references them as-is so the NLE
can relink against the project tree (see files/ serving rules, design §11).
"""
from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.dom import minidom

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.models.audio import AudioGeneration, TimelineAudioClip
from app.models.generation import Video
from app.models.system import ExportPreset
from app.models.timeline import Timeline, TimelineClip


@dataclass(frozen=True)
class _VideoEvent:
    index: int  # 0-based order on the timeline
    name: str  # clip name for EDL comments / XML
    rel_path: str  # project-relative source path
    src_in_sec: float
    src_out_sec: float
    tl_start_sec: float  # timeline position of the clip start


@dataclass(frozen=True)
class _AudioEvent:
    index: int
    name: str
    rel_path: str
    tl_start_sec: float
    tl_end_sec: float


@dataclass(frozen=True)
class TimelineExport:
    timeline_id: uuid.UUID
    timeline_name: str
    fps: int
    video_events: tuple[_VideoEvent, ...] = ()
    audio_events: tuple[_AudioEvent, ...] = ()
    skipped: tuple[str, ...] = field(default_factory=tuple)


def _resolve_fps(db: Session, timeline: Timeline, clips: list[TimelineClip]) -> int:
    """Preset fps wins, then the first clip's source fps, else 24."""
    if timeline.output_preset_id is not None:
        preset = db.get(ExportPreset, timeline.output_preset_id)
        if preset is not None and preset.fps:
            return int(preset.fps)
    if clips:
        video = db.get(Video, clips[0].video_id)
        if video is not None and video.fps:
            return int(video.fps)
    return 24


def _clip_name(rel_path: str) -> str:
    stem = rel_path.rsplit("/", 1)[-1]
    return stem.rsplit(".", 1)[0] if "." in stem else stem


def gather_timeline_media(db: Session, timeline_id: uuid.UUID) -> TimelineExport:
    """Collect ordered video/audio events for a timeline (deterministic)."""
    timeline = db.get(Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, f"Timeline not found: {timeline_id}.")

    clips = (
        db.query(TimelineClip)
        .filter(TimelineClip.timeline_id == timeline_id)
        .order_by(TimelineClip.position)
        .all()
    )
    fps = _resolve_fps(db, timeline, clips)

    video_events: list[_VideoEvent] = []
    skipped: list[str] = []
    tl_cursor = 0.0
    for idx, clip in enumerate(clips):
        video = db.get(Video, clip.video_id)
        if video is None or not video.output_path:
            skipped.append(f"clip position {clip.position}: source video unavailable")
            continue
        dur = max(0.0, float(clip.out_sec) - float(clip.in_sec))
        video_events.append(
            _VideoEvent(
                index=idx,
                name=_clip_name(video.output_path),
                rel_path=video.output_path,
                src_in_sec=float(clip.in_sec),
                src_out_sec=float(clip.out_sec),
                tl_start_sec=tl_cursor,
            )
        )
        tl_cursor += dur

    audio_clips = (
        db.query(TimelineAudioClip)
        .filter(TimelineAudioClip.timeline_id == timeline_id)
        .order_by(TimelineAudioClip.start_sec)
        .all()
    )
    audio_events: list[_AudioEvent] = []
    for idx, acl in enumerate(audio_clips):
        audio = db.get(AudioGeneration, acl.audio_id)
        if audio is None or not audio.output_path:
            skipped.append(f"audio clip {acl.id}: source audio unavailable")
            continue
        dur = acl.duration_sec if acl.duration_sec is not None else audio.duration_sec
        if dur is None or dur <= 0:
            skipped.append(f"audio clip {acl.id}: unknown duration")
            continue
        audio_events.append(
            _AudioEvent(
                index=idx,
                name=_clip_name(audio.output_path),
                rel_path=audio.output_path,
                tl_start_sec=float(acl.start_sec),
                tl_end_sec=float(acl.start_sec) + float(dur),
            )
        )

    if not video_events and not audio_events:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Timeline has no exportable clips.",
            details={"timeline_id": str(timeline_id), "skipped": skipped},
        )

    return TimelineExport(
        timeline_id=timeline.id,
        timeline_name=timeline.name,
        fps=fps,
        video_events=tuple(video_events),
        audio_events=tuple(audio_events),
        skipped=tuple(skipped),
    )


def to_timecode(seconds: float, fps: int) -> str:
    """Seconds -> HH:MM:SS:FF at the given fps (non-drop)."""
    total_frames = max(0, int(round(seconds * fps)))
    ff = total_frames % fps
    total_sec = total_frames // fps
    ss = total_sec % 60
    mm = (total_sec // 60) % 60
    hh = total_sec // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def build_cmx3600_edl(export: TimelineExport) -> str:
    """CMX3600 EDL text. Video events first (V track), then audio (A track)."""
    lines = [
        f"TITLE: {export.timeline_name}",
        "FCM: NON-DROP FRAME",
        "",
    ]
    event_no = 0

    def _event(track: str, src_in: float, src_out: float,
               rec_in: float, rec_out: float, name: str) -> None:
        nonlocal event_no
        event_no += 1
        lines.append(
            f"{event_no:03d}  AX       {track}     C        "
            f"{to_timecode(src_in, export.fps)} {to_timecode(src_out, export.fps)} "
            f"{to_timecode(rec_in, export.fps)} {to_timecode(rec_out, export.fps)}"
        )
        lines.append(f"* FROM CLIP NAME: {name}")

    for ev in export.video_events:
        dur = ev.src_out_sec - ev.src_in_sec
        _event("V", ev.src_in_sec, ev.src_out_sec,
               ev.tl_start_sec, ev.tl_start_sec + dur, ev.name)
    for ev in export.audio_events:
        _event("A", 0.0, ev.tl_end_sec - ev.tl_start_sec,
               ev.tl_start_sec, ev.tl_end_sec, ev.name)
    for note in export.skipped:
        lines.append(f"* NOTE: skipped - {note}")
    lines.append("")
    return "\n".join(lines)


def _rate_el(parent: ET.Element, fps: int) -> None:
    rate = ET.SubElement(parent, "rate")
    ET.SubElement(rate, "timebase").text = str(fps)
    ET.SubElement(rate, "ntsc").text = "FALSE"


def _file_el(parent: ET.Element, file_id: str, name: str,
             rel_path: str, duration_frames: int, fps: int, is_audio: bool) -> None:
    file_el = ET.SubElement(parent, "file", {"id": file_id})
    ET.SubElement(file_el, "name").text = name
    # Project-relative source; the NLE relinks against the project tree.
    ET.SubElement(file_el, "pathurl").text = f"file://./{rel_path}"
    _rate_el(file_el, fps)
    ET.SubElement(file_el, "duration").text = str(duration_frames)
    media = ET.SubElement(file_el, "media")
    if is_audio:
        ET.SubElement(media, "audio")
    else:
        video = ET.SubElement(media, "video")
        ET.SubElement(video, "samplecharacteristics")


def build_fcp7_xml(export: TimelineExport) -> bytes:
    """FCP7 xmeml v4 document as UTF-8 bytes (deterministic)."""
    fps = export.fps

    def _frames(sec: float) -> int:
        return max(0, int(round(sec * fps)))

    xmeml = ET.Element("xmeml", {"version": "4"})
    seq = ET.SubElement(xmeml, "sequence", {"name": export.timeline_name})
    ET.SubElement(seq, "uuid").text = str(export.timeline_id)
    total_frames = 0
    for ev in export.video_events:
        total_frames = max(total_frames, _frames(ev.tl_start_sec + (ev.src_out_sec - ev.src_in_sec)))
    for ev in export.audio_events:
        total_frames = max(total_frames, _frames(ev.tl_end_sec))
    ET.SubElement(seq, "duration").text = str(total_frames)
    _rate_el(seq, fps)
    media = ET.SubElement(seq, "media")

    video_el = ET.SubElement(media, "video")
    vtrack = ET.SubElement(video_el, "track")
    for i, ev in enumerate(export.video_events):
        dur = _frames(ev.src_out_sec - ev.src_in_sec)
        start = _frames(ev.tl_start_sec)
        item = ET.SubElement(vtrack, "clipitem", {"id": f"clipitem-{i + 1}"})
        ET.SubElement(item, "name").text = ev.name
        ET.SubElement(item, "duration").text = str(dur)
        _rate_el(item, fps)
        ET.SubElement(item, "start").text = str(start)
        ET.SubElement(item, "end").text = str(start + dur)
        ET.SubElement(item, "in").text = str(_frames(ev.src_in_sec))
        ET.SubElement(item, "out").text = str(_frames(ev.src_out_sec))
        _file_el(item, f"file-v{i + 1}", ev.name, ev.rel_path, dur, fps, is_audio=False)

    audio_el = ET.SubElement(media, "audio")
    atrack = ET.SubElement(audio_el, "track")
    for i, ev in enumerate(export.audio_events):
        dur = _frames(ev.tl_end_sec - ev.tl_start_sec)
        start = _frames(ev.tl_start_sec)
        item = ET.SubElement(atrack, "clipitem", {"id": f"aclipitem-{i + 1}"})
        ET.SubElement(item, "name").text = ev.name
        ET.SubElement(item, "duration").text = str(dur)
        _rate_el(item, fps)
        ET.SubElement(item, "start").text = str(start)
        ET.SubElement(item, "end").text = str(start + dur)
        ET.SubElement(item, "in").text = "0"
        ET.SubElement(item, "out").text = str(dur)
        _file_el(item, f"file-a{i + 1}", ev.name, ev.rel_path, dur, fps, is_audio=True)

    raw = ET.tostring(xmeml, encoding="utf-8")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    return pretty
