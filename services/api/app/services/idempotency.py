"""Idempotency keys for job-creating endpoints (D-13.2, plan D2).

The `Idempotency-Key` request header de-duplicates double submissions
(double-clicks, client retries, a response lost in transit) for
`IDEMPOTENCY_TTL_SEC`.

Stored in the DB table `idempotency_records`, UNIQUE(method, actual request
path, key): the same key on two different shots never collides, and a
replay works after an API restart. `run()` is THE one helper every
job-creating router uses:

1. An unexpired record with the same request hash -> replay the stored
   status + body (no new job, no budget re-check — plan ambiguity #2).
   A different hash -> 409 IDEMPOTENCY_CONFLICT.
2. An expired record is deleted in this transaction.
3. Otherwise `create()` runs and the record is INSERTed in the SAME
   transaction as the job rows and budget reservations (only 2xx responses
   are recorded). A concurrent request with the same key loses on the
   unique constraint (or fails inside `create()` because the winner's jobs
   now exist, e.g. 403 budget): its transaction is rolled back — its job
   rows and after-commit dispatches vanish with it — and the winner's record
   is replayed (or 409 on a different body).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import defaults
from app.core.errors import ErrorCode, StudioError


def request_hash(
    body: BaseModel | dict | None, extra: dict[str, Any] | None = None
) -> str:
    """sha256 of the canonical (sorted-key) JSON of the validated body plus
    any query parameters that affect the job."""
    if isinstance(body, BaseModel):
        body_data: Any = body.model_dump(mode="json")
    else:
        body_data = body
    canonical = json.dumps(
        {"body": body_data, "query": extra or {}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes for DateTime(timezone=True): stored as UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _normalize_key(raw_key: str | None) -> str | None:
    if raw_key is None:
        return None
    key = raw_key.strip()
    if not key:
        return None
    if len(key) > defaults.IDEMPOTENCY_KEY_MAX_LEN:
        raise StudioError(
            ErrorCode.VALIDATION_ERROR,
            f"{defaults.IDEMPOTENCY_HEADER} is longer than "
            f"{defaults.IDEMPOTENCY_KEY_MAX_LEN} characters.",
        )
    return key


def _find(db: Session, method: str, path: str, key: str):
    from app.models.system import IdempotencyRecord

    return (
        db.query(IdempotencyRecord)
        .filter(
            IdempotencyRecord.method == method,
            IdempotencyRecord.path == path,
            IdempotencyRecord.key == key,
        )
        .populate_existing()
        .one_or_none()
    )


def _replay(record, req_hash: str) -> JSONResponse:
    if record.request_hash != req_hash:
        raise StudioError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            f"This {defaults.IDEMPOTENCY_HEADER} was already used for a different "
            "request body; send a new key for a new request.",
            details={"job_id": str(record.job_id) if record.job_id else None},
        )
    return JSONResponse(status_code=record.status_code, content=record.response_json)


def _job_id_of(body: dict[str, Any]) -> uuid.UUID | None:
    value = body.get("job_id")
    try:
        return uuid.UUID(str(value)) if value is not None else None
    except ValueError:
        return None


def run(
    db: Session,
    request: Request,
    raw_key: str | None,
    *,
    body: BaseModel | dict | None,
    create: Callable[[], Any],
    response_model: type[BaseModel],
    status_code: int = 202,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Run `create()` at most once per (method, actual path, key) within the
    TTL. `request` supplies the method and the ACTUAL path (not the route
    template), e.g. /api/v1/shots/<uuid>/frame.

    `create()` returns the service result; it is validated into
    `response_model`. The response body is the same JSON on the first call
    and on every replay.
    """
    from app.models.system import IdempotencyRecord

    key = _normalize_key(raw_key)
    if key is None:
        result = response_model.model_validate(create())
        return JSONResponse(
            status_code=status_code, content=result.model_dump(mode="json")
        )

    method = request.method.upper()
    path = request.url.path
    req_hash = request_hash(body, extra)
    now = datetime.now(timezone.utc)
    record = _find(db, method, path, key)
    if record is not None:
        if _aware(record.expires_at) > now:
            return _replay(record, req_hash)
        db.delete(record)  # expired: this request starts a new window
        db.flush()

    try:
        result = response_model.model_validate(create())
        content = result.model_dump(mode="json")
        db.add(
            IdempotencyRecord(
                method=method,
                path=path,
                key=key,
                request_hash=req_hash,
                status_code=status_code,
                response_json=content,
                job_id=_job_id_of(content),
                created_at=now,
                expires_at=now + timedelta(seconds=defaults.IDEMPOTENCY_TTL_SEC),
            )
        )
        db.flush()  # a concurrent winner's row -> IntegrityError here
    except (IntegrityError, StudioError):
        # Same key in flight elsewhere: drop this attempt (jobs, reservations
        # and after-commit dispatches roll back) and answer with the winner.
        db.rollback()
        winner = _find(db, method, path, key)
        if winner is not None and _aware(winner.expires_at) > now:
            return _replay(winner, req_hash)
        raise
    return JSONResponse(status_code=status_code, content=content)
