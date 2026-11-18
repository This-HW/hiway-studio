"""Secret redaction for provider text quoted in messages (ADR-0014 §4).

Every piece of provider-controlled text that can land in a ``JobStatus`` /
``StudioError`` message (HTTP error snippets, an HTTP 200 body's error field,
state/stage values, CLI stderr) goes through ``redact`` with a length cap:
the message is persisted in ``error_message`` and published over SSE.
Pure module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(
        r"(?i)(\"?(?:api[_-]?key|apikey|access[_-]?token|secret|password|"
        r"authorization|x-api-key|token)\"?\s*[:=]\s*\"?)([^\"\s,&}]+)"
    ),
)
# Bare provider key shapes (OpenAI/Anthropic/Stripe-style "sk-...").
_BARE_KEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}")


def redact(text: str, secrets: Iterable[str] = (), *, limit: int | None = None) -> str:
    """Mask known secret values and secret-looking tokens, then cap length."""
    text = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    text = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _BARE_KEY_RE.sub(REDACTED, text)
    if limit is not None and len(text) > limit:
        text = text[:limit]
    return text
