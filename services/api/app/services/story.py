"""Story service: synopsis/beats CRUD, stub LLM generation, script import.

Text-LLM calls (G-12) are synchronous here — the API returns 201 with the
result. Real text providers arrive as adapters; the stub produces deterministic
canned output so Phase 1 works without API keys.

Budget (plan D1, B5): before the external call a pending llm_calls row is
committed under the project budget lock (403 BUDGET_EXCEEDED when over
budget); the lock is released before the call. The row then settles as
succeeded (spent), failed (nothing sent / provider refused: released) or
uncertain (sent, outcome unknown: stays reserved). Each step commits the
request session, so a settled row survives a later error in the request.
"""
from __future__ import annotations

import json
import re
import uuid

from sqlalchemy.orm import Session

from app.core.contracts import check_enum
from app.core.errors import ErrorCode, StudioError
from app.models.production import Scene
from app.models.project import Project
from app.models.story import Beat, Story
from app.models.system import LLMCall
from app.models_lib import registry, router as model_router
from app.models_lib.adapters.http_polling import poll_until_done
from app.services import budget_guard, cost_estimator, job_execution

SYNOPSIS_TEMPLATE = "synopsis/v1"
BEATS_TEMPLATE = "beats/v1"
TEMPLATE_VERSION = "1.0"


def _project_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


def _story_for_project(db: Session, project_id: uuid.UUID) -> Story:
    _project_or_404(db, project_id)
    story = db.query(Story).filter(Story.project_id == project_id).one_or_none()
    if story is None:
        story = Story(id=uuid.uuid4(), project_id=project_id)
        db.add(story)
        db.flush()
    return story


def get_story(db: Session, project_id: uuid.UUID) -> Story:
    return _story_for_project(db, project_id)


def update_story(
    db: Session, project_id: uuid.UUID, logline: str | None, synopsis: str | None
) -> Story:
    story = _story_for_project(db, project_id)
    if logline is not None:
        story.logline = logline
    if synopsis is not None:
        story.synopsis = synopsis
    db.flush()
    return story


def _call_text_llm(
    db: Session,
    project_id: uuid.UUID,
    purpose: str,
    template: str,
    input_text: str,
    model_id: str | None,
) -> LLMCall:
    """Run one text-LLM call synchronously with a budget reservation (D1)."""
    check_enum("llm_purpose", purpose)
    routed = model_router.route(
        task="text_llm",
        modality="text",
        explicit_model_id=model_id,
        db=db,
    )
    routed_model = registry.get_model(routed["model_id"], db)
    if routed_model.get("modality") != "text":
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"Model {routed['model_id']} is not a text LLM.",
        )
    adapter = registry.get_adapter(routed["adapter"])
    # Prompt size as the token estimate (~4 chars/token); the call commits
    # its reservation before anything is sent (403 when over budget).
    estimated = cost_estimator.estimate(
        db, routed["model_id"], chars=len(input_text), tokens=-(-len(input_text) // 4)
    )
    call = budget_guard.reserve_llm_call(
        db,
        project_id=project_id,
        purpose=purpose,
        model_id=routed["model_id"],
        template=template,
        template_version=TEMPLATE_VERSION,
        input_text=input_text,
        estimated_cost_usd=estimated,
    )
    try:
        provider_job_id = adapter.submit(routed["model_id"], {"task": purpose, "prompt": input_text})
    except Exception as exc:
        sent = not job_execution.submit_not_sent(job_execution.as_studio_error(exc))
        budget_guard.settle_llm_call(
            db, call, budget_guard.LLM_UNCERTAIN if sent else budget_guard.LLM_FAILED
        )
        raise
    try:
        final = poll_until_done(adapter, provider_job_id)
    except Exception:
        budget_guard.settle_llm_call(db, call, budget_guard.LLM_UNCERTAIN)
        raise
    if final.state != "succeeded":
        budget_guard.settle_llm_call(db, call, budget_guard.LLM_FAILED)
        raise StudioError(
            final.error_code or ErrorCode.PROVIDER_ERROR,
            final.error_message or "Text LLM call failed.",
        )
    try:
        output = adapter.fetch_result(provider_job_id).decode("utf-8")
    except Exception:
        budget_guard.settle_llm_call(db, call, budget_guard.LLM_UNCERTAIN)
        raise
    budget_guard.settle_llm_call(db, call, budget_guard.LLM_SUCCEEDED, output_text=output)
    return call


def generate_synopsis(
    db: Session, project_id: uuid.UUID, logline: str, model_id: str | None
) -> tuple[uuid.UUID, str]:
    """Sync stub LLM synopsis generation. Returns (llm_call_id, synopsis)."""
    _project_or_404(db, project_id)
    prompt = f"Write a one-paragraph story synopsis for this logline:\n{logline}"
    call = _call_text_llm(db, project_id, "synopsis", SYNOPSIS_TEMPLATE, prompt, model_id)
    story = _story_for_project(db, project_id)
    story.logline = logline
    story.synopsis = call.output_text
    db.flush()
    return call.id, call.output_text


def generate_beats(
    db: Session, project_id: uuid.UUID, act_count: int | None, model_id: str | None
) -> tuple[uuid.UUID, list[Beat]]:
    """Sync stub LLM beat generation; replaces existing beats. Returns (llm_call_id, beats)."""
    story = _story_for_project(db, project_id)
    if not story.synopsis:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            "Generate a synopsis first — beats are generated from it.",
        )
    acts = f" in {act_count} acts" if act_count else ""
    prompt = f"Break this synopsis into story beats{acts}:\n{story.synopsis}"
    call = _call_text_llm(db, project_id, "beats", BEATS_TEMPLATE, prompt, model_id)
    try:
        payload = json.loads(call.output_text)
        items = payload.get("beats", [])
    except (json.JSONDecodeError, AttributeError):
        items = []
    if not items:
        raise StudioError(ErrorCode.PROVIDER_ERROR, "LLM returned no beats.")

    db.query(Beat).filter(Beat.story_id == story.id).delete()
    beats: list[Beat] = []
    for i, item in enumerate(items):
        beat = Beat(
            id=uuid.uuid4(),
            story_id=story.id,
            order_index=i,
            act=item.get("act"),
            title=item.get("title"),
            summary=item.get("summary"),
        )
        db.add(beat)
        beats.append(beat)
    db.flush()
    return call.id, beats


def list_beats(db: Session, story_id: uuid.UUID) -> list[Beat]:
    story = db.get(Story, story_id)
    if story is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Story not found.")
    return (
        db.query(Beat).filter(Beat.story_id == story_id).order_by(Beat.order_index).all()
    )


def create_beat(
    db: Session,
    story_id: uuid.UUID,
    order_index: int | None,
    act: int | None,
    title: str | None,
    summary: str | None,
) -> Beat:
    story = db.get(Story, story_id)
    if story is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Story not found.")
    existing = db.query(Beat).filter(Beat.story_id == story_id).all()
    if order_index is None:
        order_index = max((b.order_index for b in existing), default=-1) + 1
    for b in existing:
        if b.order_index >= order_index:
            b.order_index += 1
    beat = Beat(
        id=uuid.uuid4(),
        story_id=story_id,
        order_index=order_index,
        act=act,
        title=title,
        summary=summary,
    )
    db.add(beat)
    db.flush()
    return beat


def update_beat(db: Session, beat_id: uuid.UUID, **fields) -> Beat:
    beat = db.get(Beat, beat_id)
    if beat is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Beat not found.")
    if "order_index" in fields and fields["order_index"] is not None:
        _reorder_beat(db, beat, fields["order_index"])
    for key in ("act", "title", "summary"):
        if key in fields and fields[key] is not None:
            setattr(beat, key, fields[key])
    db.flush()
    return beat


def _reorder_beat(db: Session, beat: Beat, new_index: int) -> None:
    siblings = (
        db.query(Beat)
        .filter(Beat.story_id == beat.story_id, Beat.id != beat.id)
        .order_by(Beat.order_index)
        .all()
    )
    old_index = beat.order_index
    new_index = max(0, min(new_index, len(siblings)))
    beat.order_index = new_index
    for i, b in enumerate(siblings):
        target = i if i < new_index else i + 1
        b.order_index = target
    beat.order_index = new_index
    db.flush()


def delete_beat(db: Session, beat_id: uuid.UUID) -> None:
    beat = db.get(Beat, beat_id)
    if beat is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Beat not found.")
    db.delete(beat)
    db.flush()
    siblings = (
        db.query(Beat)
        .filter(Beat.story_id == beat.story_id)
        .order_by(Beat.order_index)
        .all()
    )
    for i, b in enumerate(siblings):
        b.order_index = i
    db.flush()


def import_script(db: Session, project_id: uuid.UUID, content: str, script_format: str = "txt") -> int:
    """Import a screenplay into scenes (shot splitting arrives in Phase 2).

    Formats:
    - txt/markdown: `## Scene: <title>` headings, optional `Location:` /
      `Time:` / `Mood:` metadata lines beneath them.
    - fountain: `.INT./.EXT.` scene headings start a new scene.
    - fdx: Final Draft XML; `<Paragraph Type="Scene Heading">` starts a scene.
    Returns the number of scenes created.
    """
    check_enum("script_format", script_format)
    project = _project_or_404(db, project_id)
    if script_format == "fountain":
        blocks = _split_fountain(content)
    elif script_format == "fdx":
        blocks = _split_fdx(content)
    else:
        blocks = _split_markdown(content)

    order = 0
    for block in blocks:
        db.add(
            Scene(
                id=uuid.uuid4(),
                project_id=project.id,
                order_index=order,
                title=block.get("title"),
                location=block.get("location"),
                time_of_day=block.get("time"),
                mood=block.get("mood"),
            )
        )
        order += 1
    db.flush()
    return len(blocks)


def _split_markdown(content: str) -> list[dict]:
    blocks: list[dict] = []
    current: dict | None = None
    for line in content.splitlines():
        m = re.match(r"^##\s*Scene:\s*(.+)$", line.strip())
        if m:
            current = {"title": m.group(1).strip()}
            blocks.append(current)
            continue
        if current is None:
            continue
        meta = re.match(r"^(Location|Time|Mood):\s*(.+)$", line.strip())
        if meta:
            current[meta.group(1).lower()] = meta.group(2).strip()
    return blocks


def _split_fountain(content: str) -> list[dict]:
    blocks: list[dict] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(".") and not stripped.startswith("..") and len(stripped) > 1:
            blocks.append({"title": stripped[1:].strip()})
        elif re.match(r"^(INT|EXT)\.", stripped, re.IGNORECASE):
            blocks.append({"title": stripped})
    return blocks


def _split_fdx(content: str) -> list[dict]:
    import xml.etree.ElementTree as ET

    blocks: list[dict] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise StudioError(ErrorCode.VALIDATION_ERROR, "Invalid FDX file.", details={"reason": str(exc)})
    for p in root.iter("Paragraph"):
        if p.get("Type") == "Scene Heading":
            text = "".join(p.itertext()).strip()
            if text:
                blocks.append({"title": text})
    return blocks
