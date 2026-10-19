"""Generic OpenAI-compatible vision client (QC content checks, P2 advisory).

Speaks ``POST {base_url}/chat/completions`` with vision content parts, so any
OpenAI-compatible endpoint works: OpenAI itself, Ollama (``/v1``), vLLM,
LM Studio, llama.cpp server, ... No vendor SDK — plain httpx.

Config (all env; read live on every call so tests can monkeypatch):
- ``QC_VISION_BASE_URL``  e.g. ``http://127.0.0.1:11434/v1`` (Ollama)
- ``QC_VISION_API_KEY``   optional Bearer token; empty = no Authorization header
- ``QC_VISION_MODEL``     e.g. ``qwen2.5vl:7b`` — required together with BASE_URL
- ``QC_VISION_TIMEOUT_SEC`` per-request timeout (default: defaults.QC_VISION_TIMEOUT_SEC)

``available()`` is True only when base_url AND model are both set; ``AVAILABLE``
is the import-time snapshot. Unconfigured -> the QC content checks keep their
existing stub behaviour (warn + stub=true).

Images travel as base64 data URLs: a remote model endpoint cannot read local
files, so the bytes go inside the request. Callers are expected to downscale
frames before handing them over (see qc_content._shot_frames).

Failure contract: transport / HTTP / malformed-response problems raise
``VisionError`` (a RuntimeError subclass) with a short human-readable message;
raw third-party tracebacks never escape. Advisory callers degrade a VisionError
to warn — the render gate is never blocked.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class VisionError(RuntimeError):
    """Predictable vision-call failure (transport/HTTP/parse problem)."""


@dataclass(frozen=True)
class VisionConfig:
    base_url: str
    api_key: str
    model: str
    timeout_sec: float


@dataclass(frozen=True)
class VisionScore:
    """Result of score_shot: integer score in [0, 100] plus short notes."""

    score: int
    notes: str


def _config() -> VisionConfig:
    # Deferred import: core imports nothing, so this is safe at call time and
    # keeps the numeric default in defaults.py (no magic numbers in code).
    from app.core import defaults

    try:
        timeout = float(
            os.environ.get("QC_VISION_TIMEOUT_SEC", str(defaults.QC_VISION_TIMEOUT_SEC))
        )
    except ValueError:
        timeout = defaults.QC_VISION_TIMEOUT_SEC
    if timeout <= 0:
        timeout = defaults.QC_VISION_TIMEOUT_SEC
    return VisionConfig(
        base_url=os.environ.get("QC_VISION_BASE_URL", "").strip().rstrip("/"),
        api_key=os.environ.get("QC_VISION_API_KEY", "").strip(),
        model=os.environ.get("QC_VISION_MODEL", "").strip(),
        timeout_sec=timeout,
    )


def available() -> bool:
    """True only when a vision endpoint is fully configured (live check)."""
    cfg = _config()
    return bool(cfg.base_url and cfg.model)


AVAILABLE: bool = available()


def _client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    # Same defensive pattern as the http_polling adapter: an unparseable
    # ambient proxy config (e.g. bracketed IPv6 in NO_PROXY) must not break
    # client construction; fall back to ignoring the proxy env instead.
    try:
        return httpx.Client(transport=transport, timeout=60.0, follow_redirects=True)
    except Exception as e:  # noqa: BLE001 (defensive: see above)
        log.warning(
            "vision client: ignoring unparseable ambient proxy config (%s: %s).",
            type(e).__name__,
            e,
        )
        return httpx.Client(
            transport=transport, timeout=60.0, follow_redirects=True, trust_env=False
        )


def _data_url(path: str | Path) -> str:
    p = Path(path)
    mime = _MIME_BY_SUFFIX.get(p.suffix.lower())
    if mime is None:
        raise VisionError(f"Unsupported image type for vision scoring: {p.suffix or p}")
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise VisionError(f"Cannot read image for vision scoring: {p} ({e})") from e
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _extract_json(text: str) -> dict:
    """Parse the model's reply into a dict; tolerates surrounding prose."""
    text = (text or "").strip()
    if not text:
        raise VisionError("Vision model returned an empty reply.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            raise VisionError(
                f"Vision model reply was not JSON: {text[:120]!r}"
            ) from None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise VisionError(
                f"Vision model reply was not parseable JSON: {text[:120]!r}"
            ) from e
    if not isinstance(data, dict):
        raise VisionError(f"Vision model reply was not a JSON object: {text[:120]!r}")
    return data


def score_shot(
    image_paths: list[str | Path],
    *,
    system_prompt: str,
    user_text: str,
    max_tokens: int = 512,
    transport: httpx.BaseTransport | None = None,
) -> VisionScore:
    """Ask the vision model to score image(s); returns VisionScore(0..100, notes).

    ``transport`` is a test seam (httpx.MockTransport); production callers
    leave it None. Raises VisionError on any transport / HTTP / parse problem.
    """
    if not image_paths:
        raise VisionError("score_shot needs at least one image.")
    cfg = _config()
    if not cfg.base_url or not cfg.model:
        raise VisionError(
            "Vision model not configured "
            "(set QC_VISION_BASE_URL and QC_VISION_MODEL)."
        )

    parts: list[dict] = [{"type": "text", "text": user_text}]
    for p in image_paths:
        parts.append(
            {"type": "image_url", "image_url": {"url": _data_url(p)}}
        )
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": parts},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    url = f"{cfg.base_url}/chat/completions"
    try:
        with _client(transport) as client:
            resp = client.post(url, json=body, headers=headers, timeout=cfg.timeout_sec)
    except httpx.HTTPError as e:
        raise VisionError(f"Vision request failed: {e}") from e
    if resp.status_code >= 400:
        raise VisionError(
            f"Vision endpoint returned HTTP {resp.status_code}: "
            f"{resp.text[:200].strip()}"
        )
    try:
        data = resp.json()
    except ValueError as e:
        raise VisionError(f"Vision endpoint returned non-JSON: {resp.text[:120]!r}") from e
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise VisionError(
            f"Malformed vision response (no choices[0].message.content): "
            f"{str(data)[:160]!r}"
        ) from e

    parsed = _extract_json(content if isinstance(content, str) else str(content))
    try:
        score = int(parsed["score"])
    except (KeyError, TypeError, ValueError) as e:
        raise VisionError(
            f"Vision reply has no integer 'score': {str(parsed)[:120]!r}"
        ) from e
    score = max(0, min(100, score))
    notes = parsed.get("notes", "")
    notes = notes if isinstance(notes, str) else str(notes)
    return VisionScore(score=score, notes=notes.strip()[:500])
