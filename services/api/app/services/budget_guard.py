"""Budget guard + cost summary (F-15, design §9; reservations: plan D1/D7, B5).

- `check_budget`: called before any new cost-bearing job is created, in the
  SAME transaction that inserts the job rows. It first row-locks the project
  (`lock_project`), so two requests for one project serialize: the second
  waits for the first commit and then sees its queued rows as reservations.
  Blocks with 403 BUDGET_EXCEEDED when
  (committed + new) / budget >= BUDGET_BLOCK_RATIO and publishes `cost.alert`
  once when the request crosses BUDGET_WARN_RATIO (before < warn <= after).
- committed = spent + reserved for the current UTC month, by each row's
  budget period: generations/audio by `reserved_at` (creation, reset by a
  manual retry; multishot leaves inherit their set's period), budget holds
  by `period_at` (the held attempt's period), queued multishot sets and LLM
  calls by `created_at`:
  spent    = succeeded leaf generations + done audio (actual else estimated)
             + completed LLM calls (status NULL (legacy) or 'succeeded');
  reserved = queued/running leaf generations and audio (estimate);
             failed rows with PROVIDER_UNCERTAIN (an external charge may
             exist, D7); pending/uncertain LLM calls; queued multishot sets
             that have not fanned out yet (their frame jobs do not exist
             yet); budget_holds (an uncertain attempt that a manual retry
             put back in the queue).
  Batch parents never count (their children carry the estimates). A row is
  in exactly one bucket, chosen by its status; failed (other than
  PROVIDER_UNCERTAIN) and canceled rows leave the buckets, i.e. release.
- Lock order (ADR-0013 B5): Project (budget) -> generation -> batch parent
  -> Video -> Shot. Callers take the budget lock before they write any job,
  shot or timeline row in the transaction.
- `reserve_llm_call` / `settle_llm_call`: a text-LLM call commits a pending
  llm_calls row (lock + budget check) before the external call and settles
  it afterwards, each in its own short transaction.
- `spent_usd` / `summary`: aggregate actual spend (design §9.4).
- `set_budget`: PUT /projects/{id}/budget.

Period: the current UTC calendar month (documented; single-user P0).
"""

from __future__ import annotations

import calendar
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core import defaults
from app.core.errors import ErrorCode, StudioError
from app.core.sse_bridge import CHANNEL, get_bridge

log = logging.getLogger("ai_studio.budget")

# llm_calls.status (plan D1). NULL = legacy completed call.
LLM_PENDING = "pending"
LLM_SUCCEEDED = "succeeded"
LLM_FAILED = "failed"
LLM_UNCERTAIN = "uncertain"
LLM_SPENT = (LLM_SUCCEEDED,)  # plus NULL
LLM_RESERVED = (LLM_PENDING, LLM_UNCERTAIN)

JOB_RESERVED = ("queued", "running")
HOLD_REASON_UNCERTAIN = "provider_uncertain"

_JOB_KIND = {
    "text_to_image": "image",
    "image_to_video": "video",
    "text_to_video": "video",
    "inpaint": "inpaint",
    "upscale": "upscale",
    "outpaint": "outpaint",
}


def _month_bounds_utc() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = calendar.monthrange(now.year, now.month)[1]
    end = start.replace(day=last, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def _dec(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _add(target: dict[str, Decimal], kind: str, amount: Any) -> None:
    target[kind] = target.get(kind, Decimal(0)) + _dec(amount)


def _uncertain_or(model, statuses):
    """status IN statuses, or failed with PROVIDER_UNCERTAIN (kept, D7)."""
    return or_(
        model.status.in_(statuses),
        and_(
            model.status == "failed", model.error_code == ErrorCode.PROVIDER_UNCERTAIN
        ),
    )


def _ledger(
    db: Session, project_id: uuid.UUID
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """(spent_by_kind, reserved_by_kind) for the current month."""
    from app.models.audio import AudioGeneration
    from app.models.generation import Generation
    from app.models.system import BudgetHold, LLMCall

    start, end = _month_bounds_utc()
    spent: dict[str, Decimal] = {}
    reserved: dict[str, Decimal] = {}

    gens = (
        db.query(Generation)
        .filter(
            Generation.project_id == project_id,
            Generation.is_batch.is_(False),
            _uncertain_or(Generation, ("succeeded",) + JOB_RESERVED),
            Generation.reserved_at >= start,
            Generation.reserved_at <= end,
        )
        .all()
    )
    for g in gens:
        kind = _JOB_KIND.get(g.job_type or "", "other")
        if g.status == "succeeded":
            _add(
                spent,
                kind,
                g.actual_cost_usd
                if g.actual_cost_usd is not None
                else g.estimated_cost_usd,
            )
        else:
            _add(reserved, kind, g.estimated_cost_usd)

    audios = (
        db.query(AudioGeneration)
        .filter(
            AudioGeneration.project_id == project_id,
            _uncertain_or(AudioGeneration, ("done",) + JOB_RESERVED),
            AudioGeneration.reserved_at >= start,
            AudioGeneration.reserved_at <= end,
        )
        .all()
    )
    for a in audios:
        if a.status == "done":
            _add(
                spent,
                "audio",
                a.actual_cost_usd
                if a.actual_cost_usd is not None
                else a.estimated_cost_usd,
            )
        else:
            _add(reserved, "audio", a.estimated_cost_usd)

    calls = (
        db.query(LLMCall.status, LLMCall.estimated_cost_usd)
        .filter(
            LLMCall.project_id == project_id,
            or_(LLMCall.status.is_(None), LLMCall.status.in_(LLM_SPENT + LLM_RESERVED)),
            LLMCall.created_at >= start,
            LLMCall.created_at <= end,
        )
        .all()
    )
    for status, cost in calls:
        _add(reserved if status in LLM_RESERVED else spent, "llm", cost)

    for (amount,) in db.query(BudgetHold.amount_usd).filter(
        BudgetHold.project_id == project_id,
        BudgetHold.period_at >= start,
        BudgetHold.period_at <= end,
    ):
        _add(reserved, "hold", amount)

    _add(reserved, "multishot", _queued_multishot_estimate(db, project_id, start, end))
    return spent, reserved


def _queued_multishot_estimate(
    db: Session, project_id: uuid.UUID, start, end
) -> Decimal:
    """Queued multishot sets have no frame jobs yet (the worker fans out and
    claims the set in one transaction): reserve their coverage estimate."""
    from app.models.multishot import COVERAGE_ANGLES, MultishotSet
    from app.models.production import Scene, Shot
    from app.services import cost_estimator

    total = Decimal(0)
    rows = (
        db.query(MultishotSet.state_snapshot)
        .join(Shot, Shot.id == MultishotSet.shot_id)
        .join(Scene, Scene.id == Shot.scene_id)
        .filter(
            Scene.project_id == project_id,
            MultishotSet.status == "queued",
            MultishotSet.created_at >= start,
            MultishotSet.created_at <= end,
        )
        .all()
    )
    for (snapshot,) in rows:
        model_id = (snapshot or {}).get("model_id")
        if not model_id:
            continue
        try:
            total += cost_estimator.estimate_images(
                db, str(model_id), len(COVERAGE_ANGLES)
            )
        except StudioError:  # model gone from the registry: nothing to price
            log.warning("multishot reservation: no price for model %s", model_id)
    return total


def _spent_by_kind(db: Session, project_id: uuid.UUID) -> dict[str, Decimal]:
    return _ledger(db, project_id)[0]


def spent_usd(db: Session, project_id: uuid.UUID) -> Decimal:
    return sum(_spent_by_kind(db, project_id).values(), Decimal(0))


def committed_usd(db: Session, project_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    """(spent, reserved) for the current month; committed = spent + reserved."""
    spent, reserved = _ledger(db, project_id)
    return sum(spent.values(), Decimal(0)), sum(reserved.values(), Decimal(0))


def get_project(db: Session, project_id: uuid.UUID):
    from app.models.project import Project

    project = db.get(Project, project_id)
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


def lock_project(db: Session, project_id: uuid.UUID):
    """Row-lock + reload the project: THE budget lock (plan D1).

    Postgres: SELECT ... FOR NO KEY UPDATE (conflicts with itself and with
    the subtitle-scope lock, but not with the FK KEY SHARE locks that
    inserts of job rows take). SQLite has no row locks: a no-op write on the
    row takes the database write lock for the rest of the transaction (the
    lock_row approach, B4R4-M2), keeping updated_at unchanged.
    Take it before writing any job/shot/timeline row in the transaction.
    """
    from sqlalchemy import update

    from app.models.project import Project

    db.flush()  # never let populate_existing drop pending changes
    if db.get_bind().dialect.name == "sqlite":
        table = Project.__table__
        db.execute(
            update(table)
            .where(table.c.id == project_id)
            .values(id=table.c.id, updated_at=table.c.updated_at)
        )
    project = db.get(
        Project, project_id, with_for_update={"key_share": True}, populate_existing=True
    )
    if project is None:
        raise StudioError(ErrorCode.NOT_FOUND, "Project not found.")
    return project


def check_budget(
    db: Session, project_id: uuid.UUID, *, estimated_new: Decimal | float = 0
) -> None:
    """Lock the project, then block at BUDGET_BLOCK_RATIO of committed spend
    (spent + reserved + this request); alert once when crossing
    BUDGET_WARN_RATIO. The caller inserts its job rows in the same
    transaction, so they are visible to the next check after commit."""
    project = lock_project(db, project_id)
    if project.monthly_budget_usd is None:
        return
    budget = Decimal(str(project.monthly_budget_usd))
    if budget <= 0:
        return
    spent, reserved = committed_usd(db, project_id)
    committed = spent + reserved
    new = _dec(estimated_new)
    warn = Decimal(str(defaults.BUDGET_WARN_RATIO))
    block = Decimal(str(defaults.BUDGET_BLOCK_RATIO))
    before_pct = committed / budget
    after_pct = (committed + new) / budget
    if after_pct >= block:
        raise StudioError(
            ErrorCode.BUDGET_EXCEEDED,
            "Monthly budget reached: this job would push committed spend "
            f"to {float(block) * 100:.0f}% or more.",
            details={
                "spent_usd": float(spent),
                "reserved_usd": float(reserved),
                "budget_usd": float(budget),
                "estimated_new_usd": float(new),
            },
        )
    if before_pct < warn <= after_pct:
        get_bridge().publish(
            CHANNEL,
            "cost.alert",
            {
                "project_id": str(project_id),
                "usage_pct": round(float(after_pct) * 100, 1),
                "spent_usd": float(committed + new),
                "budget_usd": float(budget),
                "threshold_pct": round(float(warn) * 100),
            },
        )


def add_hold(
    db: Session,
    project_id: uuid.UUID,
    *,
    source_kind: str,
    source_id: uuid.UUID,
    amount_usd: Any,
    period_at: Any,
) -> None:
    """Keep an uncertain attempt's estimate reserved after its row is
    re-queued by a manual retry (D7). Call under the budget lock."""
    from app.models.system import BudgetHold

    db.add(
        BudgetHold(
            project_id=project_id,
            source_kind=source_kind,
            source_id=source_id,
            amount_usd=_dec(amount_usd),
            reason=HOLD_REASON_UNCERTAIN,
            period_at=period_at,
        )
    )
    db.flush()


# --- LLM reservations (plan D1) ------------------------------------------------


def reserve_llm_call(
    db: Session,
    *,
    project_id: uuid.UUID,
    purpose: str,
    model_id: str,
    template: str,
    template_version: str,
    input_text: str,
    estimated_cost_usd: Decimal,
):
    """Commit a pending llm_calls row under the budget lock (403 when over
    budget). The commit ends the transaction, so no DB lock is held during
    the external call; the pending row counts as a reservation meanwhile."""
    from app.models.system import LLMCall

    check_budget(db, project_id, estimated_new=estimated_cost_usd)
    call = LLMCall(
        id=uuid.uuid4(),
        project_id=project_id,
        purpose=purpose,
        model_id=model_id,
        system_prompt_template=template,
        system_prompt_version=template_version,
        input_text=input_text,
        output_text="",
        estimated_cost_usd=estimated_cost_usd,
        status=LLM_PENDING,
    )
    db.add(call)
    db.commit()
    return call


def settle_llm_call(
    db: Session, call, status: str, *, output_text: str | None = None
) -> None:
    """pending -> succeeded | failed | uncertain, committed on its own."""
    call.status = status
    if output_text is not None:
        call.output_text = output_text
    db.commit()


def set_budget(
    db: Session, project_id: uuid.UUID, monthly_budget_usd: float | None
) -> Any:
    project = get_project(db, project_id)
    project.monthly_budget_usd = (
        None if monthly_budget_usd is None else Decimal(str(monthly_budget_usd))
    )
    db.flush()
    return project


def summary(db: Session, project_id: uuid.UUID) -> dict[str, Any]:
    from app.models.generation import Generation

    get_project(db, project_id)
    by_kind_dec = _spent_by_kind(db, project_id)
    total_actual = sum(by_kind_dec.values(), Decimal(0))
    total_estimated = db.query(
        func.coalesce(func.sum(Generation.estimated_cost_usd), 0)
    ).filter(Generation.project_id == project_id).scalar() or Decimal(0)
    project = get_project(db, project_id)
    budget = (
        Decimal(str(project.monthly_budget_usd)) if project.monthly_budget_usd else None
    )
    usage_pct = float(total_actual / budget * 100) if budget and budget > 0 else None
    start, end = _month_bounds_utc()
    return {
        "project_id": project_id,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "by_kind": {k: float(v) for k, v in sorted(by_kind_dec.items())},
        "total_actual_usd": float(total_actual),
        "total_estimated_usd": float(Decimal(str(total_estimated))),
        "budget_usd": float(budget) if budget is not None else None,
        "budget_usage_pct": round(usage_pct, 1) if usage_pct is not None else None,
    }
