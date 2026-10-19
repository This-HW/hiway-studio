"""Narration-based B-roll auto-suggest (P1-5, design §4.14).

POST /projects/{id}/broll-suggest {timeline_id}: takes the timeline's
subtitle/narration text, extracts keywords with a lightweight tokenizer,
and matches them against the asset library's background/location assets.
Simple keyword scoring is enough for P1-5 (LLM-based semantic matching is
a later refinement, not a P1 requirement).
"""
from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError

#: Asset kinds eligible as B-roll backdrops.
BROLL_ASSET_KINDS = ("background", "location")

#: Very small stopword lists; this is a heuristic, not NLP.
_STOPWORDS = frozenset(
    """
    이 그 저 것 수 등 때 및 에 의 를 을 가 은 는 이 도 로 으로 와 과 한
    있다 없다 하다 되다 있다 그리고 그러나 그래서 또한 너무 매우 정말
    the a an and or of to in on for with is are was were be been this that
    these those it its as at by from into over after before
    """.split()
)

_TOKEN_RE = re.compile(r"[0-9a-zA-Z가-힣]+")
_MAX_KEYWORDS = 12


def extract_keywords(text: str, *, limit: int = _MAX_KEYWORDS) -> list[str]:
    """Naive keyword extraction: tokenize, drop stopwords, rank by frequency."""
    tokens = _TOKEN_RE.findall(text.lower())
    kept = [t for t in tokens if len(t) >= 2 and t not in _STOPWORDS]
    counts = Counter(kept)
    return [w for w, _ in counts.most_common(limit)]


def _asset_text(asset) -> tuple[str, str, list[str]]:
    name = (asset.name or "").lower()
    prompt = (asset.prompt_fragment or "").lower()
    tags = [str(t).lower() for t in (asset.tags or [])]
    return name, prompt, tags


def _score_asset(asset, keywords: list[str]) -> tuple[float, str | None]:
    """Score one asset against keywords; return (score, best_keyword)."""
    name, prompt, tags = _asset_text(asset)
    best_kw: str | None = None
    best_kw_score = 0.0
    total = 0.0
    for kw in keywords:
        s = 0.0
        if any(kw in t or t in kw for t in tags):
            s += 3.0
        if kw in name:
            s += 2.0
        if kw in prompt:
            s += 1.0
        total += s
        if s > best_kw_score:
            best_kw_score = s
            best_kw = kw
    return total, best_kw


def _narration_text(db: Session, project_id: uuid.UUID, timeline) -> str:
    """Narration text for a timeline: its subtitle tracks' cues, in order."""
    from app.models.subtitle import SubtitleCue, SubtitleTrack

    track_ids: list[str] = list(timeline.subtitle_track_ids or [])
    tracks: list[Any] = []
    if track_ids:
        tracks = (
            db.query(SubtitleTrack)
            .filter(SubtitleTrack.id.in_(
                [uuid.UUID(t) for t in track_ids if _is_uuid(t)]),
                    SubtitleTrack.project_id == project_id)
            .order_by(SubtitleTrack.display_order)
            .all()
        )
    if not tracks:
        # Fallback: the project's default (or first) track.
        tracks = (
            db.query(SubtitleTrack)
            .filter(SubtitleTrack.project_id == project_id)
            .order_by(SubtitleTrack.is_default.desc(),
                      SubtitleTrack.display_order)
            .limit(1)
            .all()
        )
    parts: list[str] = []
    for track in tracks:
        cues = (
            db.query(SubtitleCue)
            .filter(SubtitleCue.track_id == track.id)
            .order_by(SubtitleCue.start_sec)
            .all()
        )
        parts.extend(c.text for c in cues if c.text)
    return "\n".join(parts)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def suggest_broll(
    db: Session,
    project_id: uuid.UUID,
    timeline_id: uuid.UUID,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    """Return B-roll asset suggestions for the timeline's narration."""
    from app.models.asset import Asset
    from app.models.timeline import Timeline

    timeline = db.get(Timeline, timeline_id)
    if timeline is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Timeline not found.")
    if timeline.project_id != project_id:
        raise StudioError(
            ErrorCode.NOT_FOUND, "Timeline not found in this project.")

    narration = _narration_text(db, project_id, timeline)
    keywords = extract_keywords(narration)

    candidates = (
        db.query(Asset)
        .filter(
            Asset.kind.in_(BROLL_ASSET_KINDS),
            (Asset.project_id == project_id) | (Asset.project_id.is_(None)),
        )
        .all()
    )
    scored: list[dict[str, Any]] = []
    for asset in candidates:
        score, best_kw = _score_asset(asset, keywords)
        if score > 0 and best_kw is not None:
            scored.append({
                "asset_id": asset.id,
                "asset_name": asset.name,
                "keyword": best_kw,
                "score": round(score, 2),
            })
    scored.sort(key=lambda s: (-s["score"], s["asset_name"]))
    return {
        "suggestions": scored[: max(1, limit)],
        "keywords": keywords,
        "narration_chars": len(narration),
    }
