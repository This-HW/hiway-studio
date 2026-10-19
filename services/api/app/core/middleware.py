"""HTTP plumbing: uniform §8.4 error responses. No business logic here."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.errors import ERROR_HTTP_STATUS, ErrorCode, StudioError, error_body

log = logging.getLogger("ai_studio.errors")


def register_middleware(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # Log the code only; request details may contain user content.
        log.warning("VALIDATION_ERROR path=%s", request.url.path)
        return JSONResponse(
            status_code=400,
            content=error_body(ErrorCode.VALIDATION_ERROR, "Request validation failed."),
        )

    @app.exception_handler(StudioError)
    async def _studio_handler(request: Request, exc: StudioError):
        log.warning("%s path=%s", exc.code, request.url.path)
        return JSONResponse(
            status_code=ERROR_HTTP_STATUS[exc.code],
            content=error_body(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                retry_after_sec=exc.retry_after_sec,
                details=exc.details,
            ),
        )
