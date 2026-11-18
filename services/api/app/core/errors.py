"""The canonical error codes (design report §8.4).

Single source of truth for error codes. packages/contracts/errors.json mirrors
this file for TypeScript codegen. Add a new code here first, then mirror it.
"""
from __future__ import annotations

from typing import Any, Optional


class ErrorCode:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    QC_FAILED = "QC_FAILED"
    RENDER_LOCKED = "RENDER_LOCKED"
    STYLE_KIT_LOCKED = "STYLE_KIT_LOCKED"
    SUBTITLE_OVERLAP = "SUBTITLE_OVERLAP"
    PROMPT_TOO_LONG = "PROMPT_TOO_LONG"
    MODERATION_BLOCKED = "MODERATION_BLOCKED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    QUEUE_FULL = "QUEUE_FULL"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    LORA_TOO_FEW_IMAGES = "LORA_TOO_FEW_IMAGES"
    DUPLICATE_KEYFRAME = "DUPLICATE_KEYFRAME"
    # D7 (ADR-0013): the provider submit may or may not have happened (response
    # lost, timeout after send, crash after intent). Never auto-resubmitted.
    PROVIDER_UNCERTAIN = "PROVIDER_UNCERTAIN"
    # D2: an Idempotency-Key was reused with a different request body.
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    ALL = (
        VALIDATION_ERROR, NOT_FOUND, APPROVAL_REQUIRED, QC_FAILED, RENDER_LOCKED,
        STYLE_KIT_LOCKED, SUBTITLE_OVERLAP, PROMPT_TOO_LONG, MODERATION_BLOCKED,
        BUDGET_EXCEEDED, QUEUE_FULL, RATE_LIMITED, PROVIDER_ERROR, MODEL_UNAVAILABLE,
        LORA_TOO_FEW_IMAGES, DUPLICATE_KEYFRAME, PROVIDER_UNCERTAIN,
        IDEMPOTENCY_CONFLICT,
    )


ERROR_HTTP_STATUS: dict[str, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.APPROVAL_REQUIRED: 409,
    ErrorCode.QC_FAILED: 409,
    ErrorCode.RENDER_LOCKED: 409,
    ErrorCode.STYLE_KIT_LOCKED: 409,
    ErrorCode.SUBTITLE_OVERLAP: 422,
    ErrorCode.PROMPT_TOO_LONG: 422,
    ErrorCode.MODERATION_BLOCKED: 422,
    ErrorCode.BUDGET_EXCEEDED: 403,
    ErrorCode.QUEUE_FULL: 429,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PROVIDER_ERROR: 502,
    ErrorCode.MODEL_UNAVAILABLE: 503,
    ErrorCode.LORA_TOO_FEW_IMAGES: 422,
    ErrorCode.DUPLICATE_KEYFRAME: 422,
    ErrorCode.PROVIDER_UNCERTAIN: 502,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
}

# Retryable as defined in §8.4. BUDGET_EXCEEDED becomes retryable only after the
# user raises the budget, so it stays False here.
ERROR_RETRYABLE: dict[str, bool] = {
    ErrorCode.VALIDATION_ERROR: False,
    ErrorCode.NOT_FOUND: False,
    ErrorCode.APPROVAL_REQUIRED: False,
    ErrorCode.QC_FAILED: False,
    ErrorCode.RENDER_LOCKED: True,
    ErrorCode.STYLE_KIT_LOCKED: False,
    ErrorCode.SUBTITLE_OVERLAP: False,
    ErrorCode.PROMPT_TOO_LONG: False,
    ErrorCode.MODERATION_BLOCKED: False,
    ErrorCode.BUDGET_EXCEEDED: False,
    ErrorCode.QUEUE_FULL: True,
    ErrorCode.RATE_LIMITED: True,
    ErrorCode.PROVIDER_ERROR: True,
    ErrorCode.MODEL_UNAVAILABLE: True,
    ErrorCode.LORA_TOO_FEW_IMAGES: False,
    ErrorCode.DUPLICATE_KEYFRAME: False,
    # Not auto-retryable: a manual retry creates a new attempt (D7).
    ErrorCode.PROVIDER_UNCERTAIN: False,
    # Same key, different body: resending cannot succeed (use a new key).
    ErrorCode.IDEMPOTENCY_CONFLICT: False,
}


class StudioError(Exception):
    """Domain exception. Always converted to the §8.4 JSON shape by middleware."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: Optional[bool] = None,
        retry_after_sec: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        if code not in ErrorCode.ALL:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = ERROR_RETRYABLE[code] if retryable is None else retryable
        self.retry_after_sec = retry_after_sec
        # Never put secrets, absolute paths or personal file names in details.
        self.details = details or {}


def error_body(
    code: str,
    message: str,
    *,
    retryable: Optional[bool] = None,
    retry_after_sec: Optional[int] = None,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "retryable": ERROR_RETRYABLE[code] if retryable is None else retryable,
        "retry_after_sec": retry_after_sec,
        "details": details or {},
    }
