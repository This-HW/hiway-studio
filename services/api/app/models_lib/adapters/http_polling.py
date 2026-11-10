"""Generic adapter for HTTP submit + polling providers (fal.ai, Replicate,
direct APIs, ComfyUI).

One adapter serves every HTTP-compatible model: adding a model is one seed
row, never a new file (ARCHITECTURE §4.5). Polling starts at 2s and backs off
to 10s max (design report §3.4 [가정]).

The adapter knows ONLY the provider's call protocol. Routing, retries and
fallback live outside (router.py, tasks/) per design report §12. Every
failure — including network errors — is reported as a ``JobStatus`` or a
``StudioError``; raw exceptions never escape (base.py contract).

Seed config schema (model seed JSON, ``capabilities.http``)::

    "capabilities": {
      "http": {
        "timeout_sec": 30,            # optional; submit call timeout (default 30)
        "poll_timeout_sec": 15,       # optional; status poll timeout (default 15)
        "download_timeout_sec": 120,  # optional; artifact download timeout (default 120)
        "upload": {                   # optional; pre-submit file upload
                                      # (e.g. ComfyUI /upload/image for img2img).
                                      # Runs BEFORE submit; skipped when the
                                      # payload has no input file.
          "url": "{base_url}/upload/image",
          "method": "POST",           # optional, default POST
          "headers": {},              # optional; same env: rules as submit
          "file_param": "image",      # multipart field name for the file
          "file_source": "{init_image}",
                                      # submit-payload param holding a LOCAL
                                      # file path. Exactly-one-placeholder +
                                      # missing/empty value -> step SKIPPED.
          "extra_fields": {"overwrite": "false", "type": "input"},
                                      # optional extra form fields, {..}-rendered
          "filename_response_path": "name",
                                      # JSON path of the stored filename
          "subfolder_response_path": "subfolder",
                                      # optional; non-empty value is joined
                                      # as "<subfolder>/<name>"
          "store_as": "uploaded_image",
                                      # ctx key; use {uploaded_image} in the
                                      # submit body_template below
          "timeout_sec": 60.0         # optional, default 60
        },
        "submit": {
          "url": "https://queue.fal.run/fal-ai/flux/dev",
          "method": "POST",           # optional, default POST
          "headers": {"Authorization": "env:FAL_API_KEY"},
          "body_template": {          # JSON body; {placeholders} filled from the
            "prompt": "{prompt}",     # submit payload. A value that is exactly
            "image_size": {           # one placeholder keeps its type
              "width": "{width}",     # (int stays int); otherwise the value is
              "height": "{height}"    # string-interpolated.
            },
            "num_images": 1
          }
        },
        "job_id_path": "request_id",  # JSON path in the submit response
        "status": {
          "url_template": "https://queue.fal.run/fal-ai/flux/requests/{job_id}/status",
          "method": "GET",            # optional, default GET
          "headers": {},              # optional; same env: rules as submit
          "state_path": "status",     # JSON path; may also hold {job_id}.
                                      # A boolean is accepted: true=succeeded,
                                      # false=running. A missing state or a
                                      # value outside state_map is an ERROR
                                      # (ADR-0014) unless the seed opts in:
          "missing_state": "queued",  # optional; e.g. ComfyUI /history is {}
                                      # until the prompt finishes
          "unknown_state": "running", # optional; tolerate unmapped values
          "state_map": {"IN_QUEUE": "queued", "IN_PROGRESS": "running",
                        "COMPLETED": "succeeded", "FAILED": "failed"},
          "progress_path": "progress",  # optional; 0..1 fraction or 0..100
          "error_path": "error"         # optional; failure message
        },
        "result": {
          # After success, ONE of:
          "output_url_path": "images.0.url",  # string, list of strings, or
                                              # list of dicts (needs the template)
          "output_url_template": "...",       # optional; rendered per item when
                                              # the output is a list of dicts
          "output_bytes_path": "image_b64",   # optional; base64 (or data-URI)
                                              # bytes inline instead of a URL
          # Optional separate result fetch (e.g. fal queue: status and result
          # are different endpoints). output_*_path digs into THIS response.
          "url": "https://queue.fal.run/fal-ai/flux/requests/{job_id}",
          "method": "GET"                     # optional, default GET
        },
        "cancel": {                   # optional; absent -> best-effort no-op
          "url_template": ".../requests/{job_id}/cancel",
          "method": "PUT"             # optional, default POST
        }
      }
    }

ComfyUI (local, e.g. http://127.0.0.1:8188) fits the same schema. ``submit``
POSTs the whole workflow JSON as ``body_template`` — placeholders like
``{prompt}`` / ``{seed}`` are substituted into the workflow — ``job_id_path``
is ``"prompt_id"``, and the status block reads ``/history/{job_id}``::

    "submit": {"url": "http://127.0.0.1:8188/prompt", "method": "POST",
               "headers": {},
               "body_template": {"prompt": {
                   "3": {"class_type": "KSampler",
                         "inputs": {"seed": "{seed}", "steps": 20, "cfg": 8.0,
                                    "sampler_name": "euler", "scheduler": "normal",
                                    "denoise": 1.0, "model": ["4", 0],
                                    "positive": ["6", 0], "negative": ["7", 0],
                                    "latent_image": ["5", 0}}},
                   "...": "rest of the workflow JSON as usual"}}},
    "job_id_path": "prompt_id",
    "status": {"url_template": "http://127.0.0.1:8188/history/{job_id}",
               "state_path": "{job_id}.status.completed",
               "state_map": {"success": "succeeded", "error": "failed"}},
    "result": {"output_url_path": "{job_id}.outputs.9.images",
               "output_url_template": "http://127.0.0.1:8188/view"
                                      "?filename={filename}&subfolder={subfolder}&type={type}"}

Secrets are never in code or seeds: any string value in the ``http`` block may
use ``"env:VAR_NAME"`` and is resolved from the process environment (.env) at
call time. A missing variable raises ``StudioError(MODEL_UNAVAILABLE)`` naming
the variable. See ``.env.example`` for the key slots (FAL_API_KEY,
REPLICATE_API_TOKEN, COMFYUI_URL, ...).

Machine moves are one env var: ``{base_url}`` (scheme://host:port, derived
from the submit URL) is available in the ``status`` / ``result`` / ``upload``
URL templates, so pointing the seed at a new machine means changing only the
submit URL (usually an ``env:`` reference). See ``docs/PROVIDERS.md``.

Provider input contract (ADR-0014, models_lib/contract.py): the submit
payload carries ``prompt``, ``negative_prompt`` (default ""), ``seed``,
``width``/``height`` and, when resolved, the LOCAL file paths ``init_image``,
``mask`` and ``reference_image`` (+ ``reference_weight``). A template may use
``{<file key>_data_uri}`` to inline a file as a base64 data URI. Optional
contract keys rendered as an exact placeholder are DROPPED from the body when
absent. A payload editing input (``init_image``/``mask``) that the seed's
templates do not consume is rejected before any HTTP call (never silently
ignored).

Bounds (ADR-0014): one monotonic deadline per provider job
(``defaults.HTTP_JOB_TOTAL_DEADLINE_SEC``, submit -> end of download).
Every request — upload, submit, status, result, download, and the error
body of any of them — is streamed (B7-r2 H2): the deadline is re-checked
per received chunk and, on the real transport, every socket read and every
partial send is clamped to the time left (B7-r3 H1), so a provider that
drips bytes or reads a large request slowly cannot stretch one call past
the deadline. Redirects are followed here (<= 5 hops, redirect bodies never
read) and only identity-encoded bodies are accepted (B7-r3 M2). ``submit``
returns a local job handle (``job_handle``: remote id + model id + nonce,
B7-r3 M5); ``{job_id}`` in URL templates is still the provider's id. Cancel has its own short absolute cap
(``HTTP_CANCEL_TIMEOUT_SEC``). Each body has a byte cap: JSON responses
``HTTP_JSON_MAX_BYTES`` (+ the base64 size of the media cap when the
response carries inline output bytes), error bodies a short snippet, and the
result download a per-media cap (``HTTP_RESULT_MAX_BYTES_*``) — a lying or
missing Content-Length cannot bypass any of them. Inline base64 output is
length-checked BEFORE decoding. Results must pass the Content-Type,
magic-byte and (images) decode checks (models_lib/media_check.py). Every
provider text quoted in a message (error snippets, a 200 body's error field,
state values) is secret-redacted and length-capped (models_lib/redaction.py).

ComfyUI img2img fits the same schema: the ``upload`` block (one dict, or a
list of them for several files, e.g. image + mask) POSTs the input image to
``{base_url}/upload/image`` (multipart field ``image``, form fields
``overwrite=false`` + ``type=input``) and the response's ``name`` (joined
with a non-empty ``subfolder``) lands in ``{uploaded_image}`` for the
workflow's ``LoadImage`` node. The multipart filename is unique per submit
(``hs-<token>-<basename>``, B7-r2 H3) so two jobs whose local files share a
basename never overwrite each other's input on the provider; the workflow
references the name the provider RETURNS (it may rename on collision)::

    "upload": {"url": "{base_url}/upload/image", "file_param": "image",
               "file_source": "{init_image}", "filename_response_path": "name",
               "subfolder_response_path": "subfolder",
               "extra_fields": {"overwrite": "false", "type": "input"},
               "store_as": "uploaded_image"},
    "submit": {"url": "env:COMFYUI_URL", ...,
               "body_template": {"prompt": {
                   "4": {"class_type": "LoadImage",
                         "inputs": {"image": "{uploaded_image}"}},
                   ...}}},
"""
from __future__ import annotations

import base64
import dataclasses
import json
import logging
import math
import mimetypes
import os
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpcore
import httpx

from app.core import defaults
from app.core.errors import ErrorCode, StudioError
from app.models_lib import capabilities, contract, media_check
from app.models_lib.base import EstimatedCost, JobStatus, ProviderAdapter
from app.models_lib.redaction import redact as _redact

try:  # the plain/TLS socket stream of httpcore's sync backend (B7-r3 H1)
    from httpcore._backends.sync import SyncStream as _SyncStream
except ImportError:  # pragma: no cover - httpcore layout changed
    _SyncStream = None

log = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Clock / sleep hooks (tests patch these; never time.* globally).
_monotonic = time.monotonic
_sleep = time.sleep

# Optional contract keys: an exact placeholder whose value is absent is
# dropped from the rendered body instead of failing the submit.
_OPTIONAL_KEYS = frozenset(
    [*contract.FILE_KEYS, contract.KEY_REFERENCE_WEIGHT]
    + [f"{key}_data_uri" for key in contract.FILE_KEYS]
)
_DROP = object()

# env: names treated as secrets (redacted from messages, ADR-0014 §4).
_SECRET_NAME_RE = re.compile(r"(?i)(key|token|secret|password|auth)")
_ERROR_BODY_READ_BYTES = 4096
# JSON envelope allowance on top of an inline result's base64 size.
_INLINE_JSON_SLACK_BYTES = 64 * 1024
# Redirect hops followed per call (each redirect body is closed unread).
_MAX_REDIRECTS = 5
# Write size per clamped call when the socket cannot be driven directly.
_WRITE_PIECE_BYTES = 16 * 1024
# Cap of a whole message built from provider text/metadata (after redaction).
_MESSAGE_MAX_CHARS = 1000
# Cap of a provider job handle quoted in a message.
_ID_QUOTE_CHARS = 120


def _b64_len(n: int) -> int:
    """Length of the base64 text of n bytes (no whitespace, padded)."""
    return 4 * math.ceil(n / 3)


# ---------------------------------------------------------------------------
# Absolute deadline on the real transport (B7-r2 H2)
#
# httpx timeouts apply per socket operation: a provider that sends one byte
# just inside the read timeout, forever, never trips them. The per-chunk
# check in ``_stream`` stops a dripping BODY; this backend wrapper also
# clamps every socket connect/read/write (headers included) to the time
# left before the current call's absolute deadline.
# ---------------------------------------------------------------------------

_call_deadline = threading.local()


def _clamped(timeout: float | None, exc: type[Exception]) -> float | None:
    deadline = getattr(_call_deadline, "value", None)
    if deadline is None:
        return timeout
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise exc("provider call deadline reached")
    return remaining if timeout is None else min(timeout, remaining)


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, inner: httpcore.NetworkStream) -> None:
        self._inner = inner

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, _clamped(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        """Send under the call deadline, re-checked on EVERY partial send.

        httpcore's ``SyncStream.write`` loops ``sock.send`` with one fixed
        timeout, so a slow reader could stretch one large write (a big JSON
        or data-URI body is a single chunk) far past the deadline (B7-r3
        H1). Here each ``send`` gets the time left; at the deadline the
        write fails with WriteTimeout.
        """
        if getattr(_call_deadline, "value", None) is None or not buffer:
            self._inner.write(buffer, timeout)
            return
        sock = (
            self._inner.get_extra_info("socket")
            if _SyncStream is not None and isinstance(self._inner, _SyncStream)
            else None
        )
        view = memoryview(buffer)
        if sock is None:
            # Stream we cannot drive directly (e.g. TLS inside a TLS proxy):
            # bounded pieces, each with the time left.
            for start in range(0, len(view), _WRITE_PIECE_BYTES):
                piece = bytes(view[start : start + _WRITE_PIECE_BYTES])
                self._inner.write(piece, _clamped(timeout, httpcore.WriteTimeout))
            return
        try:
            while view:
                sock.settimeout(_clamped(timeout, httpcore.WriteTimeout))
                sent = sock.send(view)
                view = view[sent:]
        except socket.timeout as exc:
            raise httpcore.WriteTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.WriteError(str(exc)) from exc

    def close(self) -> None:
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _DeadlineStream(
            self._inner.start_tls(
                ssl_context, server_hostname, _clamped(timeout, httpcore.ConnectTimeout)
            )
        )

    def get_extra_info(self, info: str):
        return self._inner.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, inner: httpcore.NetworkBackend) -> None:
        self._inner = inner

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return _DeadlineStream(
            self._inner.connect_tcp(
                host, port, _clamped(timeout, httpcore.ConnectTimeout),
                local_address, socket_options,
            )
        )

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return _DeadlineStream(
            self._inner.connect_unix_socket(
                path, _clamped(timeout, httpcore.ConnectTimeout), socket_options
            )
        )

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def _install_deadline_backend(client: httpx.Client) -> bool:
    """Wrap the network backend of the client's default + proxy transports.

    Uses httpx/httpcore internals (``_transport``/``_mounts``/``_pool``),
    guarded: when they change, the per-chunk deadline check still bounds
    response bodies, and the localhost slow-drip tests flag the regression.
    """
    installed = False
    transports = [getattr(client, "_transport", None)]
    transports += list((getattr(client, "_mounts", None) or {}).values())
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if backend is None:
            continue
        if not isinstance(backend, _DeadlineBackend):
            pool._network_backend = _DeadlineBackend(backend)
        installed = True
    return installed


@contextmanager
def _bounded(deadline: float | None):
    previous = getattr(_call_deadline, "value", None)
    _call_deadline.value = deadline
    try:
        yield
    finally:
        _call_deadline.value = previous


class _Reply:
    """A provider response whose body was received under the deadline and
    byte cap (``error`` bodies are truncated to a snippet)."""

    __slots__ = ("status_code", "headers", "content")

    def __init__(self, status_code: int, headers: httpx.Headers, content: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


def _env_secret_names(cfg, *, in_headers: bool = False) -> set[str]:
    """env: variable names whose values must never appear in messages:
    every reference inside a ``headers`` block, plus key/token-like names."""
    names: set[str] = set()
    if isinstance(cfg, str):
        if cfg.startswith("env:") and (in_headers or _SECRET_NAME_RE.search(cfg[4:])):
            names.add(cfg[4:])
    elif isinstance(cfg, dict):
        for k, v in cfg.items():
            names |= _env_secret_names(v, in_headers=in_headers or k == "headers")
    elif isinstance(cfg, (list, tuple)):
        for v in cfg:
            names |= _env_secret_names(v, in_headers=in_headers)
    return names


def _deadline_error(job_ref: str) -> StudioError:
    """`job_ref` must already be redacted + capped (HttpPollingAdapter._ref):
    truncating a raw id first would cut a reflected secret so the exact-match
    redaction no longer finds it (review B7R3-M1)."""
    return StudioError(
        ErrorCode.PROVIDER_ERROR,
        f"Provider job {job_ref or '(submit)'} exceeded the total "
        f"deadline ({defaults.HTTP_JOB_TOTAL_DEADLINE_SEC:.0f}s); provider "
        "cancel requested.",
        retryable=False,
        details={"reason": "deadline_exceeded"},
    )

# Provider job HANDLE (see ``job_handle``) -> (model_id, http config). The task flow
# (submit -> poll_until_done -> fetch_result) runs inside one task process,
# so the adapter instance that submitted the job is the one that polls it —
# same pattern as the stub adapter. Bounded to avoid unbounded growth in
# long-lived workers.
_MAX_REMEMBERED_JOBS = 10_000

_TERMINAL_STATES = ("queued", "running", "succeeded", "failed")


def job_handle(remote_id: str, model_id: str) -> str:
    """Local provider job handle: ``<remote id>@<model id>#<nonce>``
    (B7-r3 M5).

    The adapter is a process-wide singleton and two endpoints may hand out
    the same remote id ("1"); in-flight state is keyed by this handle, never
    by the bare remote id. The handle is what callers persist as
    ``provider_job_id``: it keeps the remote id readable and names the model,
    so a later process can still poll / fetch / cancel it (``_lookup``
    rebuilds the config from the registry). Model ids never contain ``@`` or
    ``#``; a remote id may (the handle is parsed from the right).
    """
    return f"{remote_id}@{model_id}#{uuid.uuid4().hex[:8]}"


def parse_job_handle(handle: str) -> tuple[str, str] | None:
    """(remote id, model id) of a handle; None for anything else (e.g. a
    bare remote id persisted before B7-r3)."""
    head, sep, nonce = str(handle).rpartition("#")
    if not sep or not nonce:
        return None
    remote, sep, model_id = head.rpartition("@")
    if not sep or not remote or not model_id:
        return None
    return remote, model_id


def remote_job_id(handle: str) -> str:
    """The provider's own job id inside a handle (the handle itself if it
    is not one)."""
    parsed = parse_job_handle(handle)
    return parsed[0] if parsed else str(handle)


# ---------------------------------------------------------------------------
# Config value helpers
# ---------------------------------------------------------------------------

def _resolve_env(value, *, model_id: str):
    """Resolve ``"env:VAR_NAME"`` references from the process environment.

    Only strings that start with ``env:`` are treated as references; everything
    else passes through untouched. A missing/empty variable is a
    MODEL_UNAVAILABLE StudioError naming the variable — never silently empty.
    """
    if isinstance(value, str):
        if not value.startswith("env:"):
            return value
        name = value[4:]
        if not name:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id}: empty 'env:' reference in capabilities.http.",
            )
        secret = os.environ.get(name)
        if not secret:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id} needs the {name} environment variable "
                f"(referenced as 'env:{name}'), but it is not set. "
                "Copy .env.example to .env and fill it in.",
            )
        return secret
    if isinstance(value, dict):
        return {k: _resolve_env(v, model_id=model_id) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_resolve_env(v, model_id=model_id) for v in value]
    return value


def _lookup_placeholder(name: str, ctx: dict, model_id: str, *, optional: bool = False):
    if optional and name in _OPTIONAL_KEYS and ctx.get(name) in (None, ""):
        return _DROP
    if name not in ctx or ctx[name] is None:
        raise StudioError(
            ErrorCode.MODEL_UNAVAILABLE,
            f"Model {model_id}: placeholder '{{{name}}}' has no value in the "
            "submit payload; fix capabilities.http or the caller payload.",
        )
    return ctx[name]


def _render(value, ctx: dict, *, model_id: str):
    """Substitute ``{placeholders}`` from ctx.

    A string that is exactly one placeholder keeps the value's type
    (``"{width}"`` with ``width=1024`` renders as the int 1024); otherwise the
    value is string-interpolated.
    """
    if isinstance(value, str):
        if not _PLACEHOLDER_RE.search(value):
            return value
        full = _PLACEHOLDER_RE.fullmatch(value)
        if full:
            return _lookup_placeholder(full.group(1), ctx, model_id, optional=True)
        return _PLACEHOLDER_RE.sub(
            lambda m: str(_lookup_placeholder(m.group(1), ctx, model_id)), value
        )
    if isinstance(value, dict):
        rendered = {k: _render(v, ctx, model_id=model_id) for k, v in value.items()}
        return {k: v for k, v in rendered.items() if v is not _DROP}
    if isinstance(value, (list, tuple)):
        rendered = [_render(v, ctx, model_id=model_id) for v in value]
        return [v for v in rendered if v is not _DROP]
    return value


_MISSING = object()


def _dig(obj, path: str):
    """JSON path with dot segments: ``"images.0.url"`` -> obj["images"][0]["url"].

    Returns the _MISSING sentinel when any segment is absent (callers decide
    what absence means).
    """
    current = obj
    for seg in path.split("."):
        if isinstance(current, dict):
            if seg not in current:
                return _MISSING
            current = current[seg]
        elif isinstance(current, (list, tuple)) and seg.isdigit():
            idx = int(seg)
            if idx >= len(current):
                return _MISSING
            current = current[idx]
        else:
            return _MISSING
    return current


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _decode_inline_bytes(value: str, *, max_bytes: int | None = None, what: str = "") -> bytes:
    """Decode base64 output bytes, tolerating a ``data:...;base64,`` prefix.

    The encoded length is checked against the media cap BEFORE decoding
    (B7-r2 M2): an oversized result is rejected without allocating it.
    """
    text = value.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    if max_bytes is not None and len(text) > _b64_len(max_bytes):
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what or 'Inline output'}: inline result exceeds the size cap "
            f"({len(text)} base64 chars > {_b64_len(max_bytes)} for {max_bytes} bytes).",
            details={"reason": "result_too_large", "max_bytes": max_bytes},
        )
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise StudioError(
            ErrorCode.PROVIDER_ERROR, f"Provider returned invalid base64 output bytes: {e}."
        ) from e


def _to_progress(value) -> float:
    """Provider progress (0..1 fraction or 0..100 percent) -> 0..100 float."""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return 0.0
    if pct <= 1.0:
        pct *= 100.0
    return max(0.0, min(100.0, pct))


class HttpPollingAdapter(ProviderAdapter):
    """Generic HTTP submit + polling adapter, driven by the model seed's
    ``capabilities.http`` block. See the module docstring for the schema."""

    provider_id = "http_polling"

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        # transport is injectable for tests (httpx.MockTransport); production
        # uses the default transport.
        self._transport = transport
        self._client: httpx.Client | None = None
        self._jobs: dict[str, tuple[str, dict]] = {}
        # handle -> monotonic job deadline / media kind / remote id (ADR-0014).
        self._deadlines: dict[str, float] = {}
        self._modality: dict[str, str] = {}
        self._remote: dict[str, str] = {}
        # env: variable names whose values are redacted from messages.
        self._secret_names: set[str] = set()

    # -- internals ------------------------------------------------------

    def _client_conn(self) -> httpx.Client:
        if self._client is None:
            try:
                # Redirects are followed by ``_stream`` itself (bounded hops,
                # redirect bodies never read; B7-r3 M2).
                self._client = httpx.Client(
                    transport=self._transport, timeout=30.0, follow_redirects=False
                )
            except Exception as e:  # noqa: BLE001 (defensive: see below)
                # Ambient proxy env can be unparseable — e.g. a bracketed IPv6
                # literal in NO_PROXY makes httpx's URLPattern raise InvalidURL
                # at client construction, before any request is made. Fall back
                # to ignoring the ambient proxy config rather than letting a
                # raw exception escape (base.py contract). Proxy behavior is
                # unchanged in sane environments; in hostile ones the adapter
                # stays usable for direct/local providers (e.g. ComfyUI).
                log.warning(
                    "Ignoring unparseable ambient proxy config (%s: %s); "
                    "continuing without proxy env.",
                    type(e).__name__,
                    e,
                )
                self._client = httpx.Client(
                    transport=self._transport,
                    timeout=30.0,
                    follow_redirects=False,
                    trust_env=False,
                )
            if self._transport is None:
                _install_deadline_backend(self._client)
        return self._client

    def _http_config(self, model_id: str) -> dict:
        # Deferred import: registry imports this module at load time.
        from app.models_lib import registry  # noqa: PLC0415 (deferred: same module)

        model = registry.get_model(model_id)  # raises NOT_FOUND when unknown
        http = (model.get("capabilities") or {}).get("http")
        if not isinstance(http, dict) or not http:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id} has no capabilities.http config; the "
                "http_polling adapter cannot drive it. Add the block to the "
                "model seed JSON (see this module's docstring).",
            )
        self._secret_names |= _env_secret_names(http)
        return http

    @staticmethod
    def _modality_of(model_id: str) -> str:
        from app.models_lib import registry  # noqa: PLC0415 (deferred: same module)

        try:
            return str(registry.get_model(model_id).get("modality") or "image")
        except StudioError:
            return "image"

    def _remember(self, model_id: str, cfg: dict, provider_job_id: str, remote_id: str) -> None:
        self._jobs[provider_job_id] = (model_id, cfg)
        self._remote[provider_job_id] = remote_id
        while len(self._jobs) > _MAX_REMEMBERED_JOBS:
            oldest = next(iter(self._jobs))
            self._jobs.pop(oldest)
            self._deadlines.pop(oldest, None)
            self._modality.pop(oldest, None)
            self._remote.pop(oldest, None)

    def deadline_for(self, provider_job_id: str) -> float | None:
        """Monotonic deadline of a submitted job (poll_until_done uses it)."""
        return self._deadlines.get(provider_job_id)

    def _secrets(self) -> list[str]:
        return [os.environ[n] for n in self._secret_names if os.environ.get(n)]

    def _quote(self, text) -> str:
        """Provider text for a message: secret-redacted and length-capped."""
        return _redact(str(text), self._secrets(), limit=defaults.HTTP_ERROR_SNIPPET_CHARS)

    def _ref(self, provider_job_id: str) -> str:
        """A job handle quoted in a message (provider metadata: redacted,
        capped)."""
        return _redact(str(provider_job_id), self._secrets(), limit=_ID_QUOTE_CHARS)

    def sanitize_message(self, text: str) -> str:
        """Final gate for any message leaving the adapter (B7-r3 M1): every
        quoted provider value — snippets, ids, URLs, header values — is
        redacted BEFORE the whole message is length-capped."""
        return _redact(str(text), self._secrets(), limit=_MESSAGE_MAX_CHARS)

    def _sanitized(self, exc: StudioError) -> StudioError:
        exc.message = self.sanitize_message(exc.message)
        exc.args = (exc.message,)
        return exc

    def _json_cap(self, cfg: dict, modality: str, *, carries_result: bool) -> int:
        """Byte cap of a JSON response; one that may carry inline output
        bytes also gets the base64 size of the media cap."""
        cap = int(defaults.HTTP_JSON_MAX_BYTES)
        res = cfg.get("result") or {}
        media_cap = media_check.max_bytes(modality)
        if carries_result and res.get("output_bytes_path") and media_cap is not None:
            cap += _b64_len(media_cap) + _INLINE_JSON_SLACK_BYTES
        return cap

    def _check_deadline(self, provider_job_id: str) -> None:
        deadline = self._deadlines.get(provider_job_id)
        if deadline is not None and _monotonic() >= deadline:
            raise _deadline_error(self._ref(provider_job_id))

    def _timeout(self, configured: float, deadline: float | None, job: str) -> float:
        """Request timeout capped by the time left before the job deadline."""
        if deadline is None:
            return configured
        remaining = deadline - _monotonic()
        if remaining <= 0:
            raise _deadline_error(self._ref(job))
        return min(configured, remaining)

    def _lookup(self, provider_job_id: str) -> tuple[str, dict]:
        entry = self._jobs.get(provider_job_id)
        if entry is not None:
            return entry
        parsed = parse_job_handle(provider_job_id)
        if parsed is None:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Unknown provider job id {self._ref(provider_job_id)!r}; it was not "
                "submitted through this adapter instance.",
            )
        # A handle from another process (restart / reconcile): rebuild the
        # in-flight entry from the registry. No deadline is known here;
        # poll_until_done then starts a fresh one.
        remote, model_id = parsed
        cfg = self._http_config(model_id)
        self._remember(model_id, cfg, provider_job_id, remote)
        self._modality[provider_job_id] = self._modality_of(model_id)
        return model_id, cfg

    def _ctx(self, model_id: str, provider_job_id: str, cfg: dict, payload: dict) -> dict:
        ctx: dict = {}
        ctx.update(payload)
        ctx["model_id"] = model_id
        # URL templates take the PROVIDER's id, never the local handle.
        ctx["job_id"] = (
            self._remote.get(provider_job_id) or remote_job_id(provider_job_id)
            if provider_job_id
            else ""
        )
        submit_url = str((cfg.get("submit") or {}).get("url") or "")
        ctx["base_url"] = _origin(_resolve_env(submit_url, model_id=model_id))
        return ctx

    def _call(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None,
        json_body=None,
        data=None,
        files=None,
        timeout: float,
        what: str,
        deadline: float | None = None,
        job: str = "",
        max_bytes: int | None = None,
    ) -> _Reply:
        """One JSON-ish HTTP call, body capped at ``max_bytes`` (default
        ``HTTP_JSON_MAX_BYTES``). See ``_stream``."""
        cap = int(defaults.HTTP_JSON_MAX_BYTES) if max_bytes is None else max_bytes
        return self._stream(
            method, url, headers=headers, json_body=json_body, data=data,
            files=files, timeout=timeout, what=what, deadline=deadline, job=job,
            max_bytes=cap,
        )

    def _stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None,
        timeout: float,
        what: str,
        deadline: float | None,
        job: str = "",
        max_bytes: int | None,
        json_body=None,
        data=None,
        files=None,
        on_headers=None,
        on_overflow=None,
    ) -> _Reply:
        """One HTTP call, the whole response received under the deadline.

        The body is streamed: the absolute deadline is re-checked per chunk
        (and per socket operation on the real transport) and ``max_bytes``
        caps it — a declared Content-Length over the cap is rejected before
        reading. An error status (>= 400) body is read only up to a short
        snippet. Network failures and the deadline become StudioError; raw
        httpx exceptions never escape (base.py contract).
        """
        timeout = self._timeout(timeout, deadline, job)
        # Compressed bodies are refused (B7-r3 M2): a decoder allocates its
        # output before any cap could see it.
        req_headers = {
            k: v for k, v in (headers or {}).items() if str(k).lower() != "accept-encoding"
        }
        req_headers["Accept-Encoding"] = "identity"
        try:
            with _bounded(deadline):
                client = self._client_conn()
                request = client.build_request(
                    method,
                    url,
                    headers=req_headers,
                    json=json_body,
                    data=data,
                    files=files,
                    timeout=timeout,
                )
                for _hop in range(_MAX_REDIRECTS + 1):
                    resp = client.send(request, stream=True, follow_redirects=False)
                    try:
                        if resp.next_request is not None:
                            # Redirect: its body is closed UNREAD (B7-r3 M2);
                            # httpx builds the next request (method change,
                            # cross-origin Authorization stripped).
                            request = resp.next_request
                            if deadline is not None and _monotonic() >= deadline:
                                raise _deadline_error(self._ref(job))
                            continue
                        return self._receive(
                            resp, what=what, deadline=deadline, job=job,
                            max_bytes=max_bytes, on_headers=on_headers,
                            on_overflow=on_overflow,
                        )
                    finally:
                        resp.close()
                raise StudioError(
                    ErrorCode.PROVIDER_ERROR,
                    f"{what}: more than {_MAX_REDIRECTS} redirects.",
                    details={"reason": "too_many_redirects"},
                )
        except httpx.HTTPError as e:
            if deadline is not None and _monotonic() >= deadline:
                raise _deadline_error(self._ref(job)) from e
            secrets = self._secrets() + [str(v) for v in (headers or {}).values()]
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                _redact(
                    f"{what}: network error ({e.__class__.__name__}: {e}).", secrets,
                    limit=_MESSAGE_MAX_CHARS,
                ),
            ) from e

    def _receive(
        self, resp: httpx.Response, *, what: str, deadline: float | None, job: str,
        max_bytes: int | None, on_headers, on_overflow,
    ) -> _Reply:
        """Read one final response: raw bytes (no decoding), deadline and
        byte cap checked per chunk; an error body only up to a snippet."""
        error = resp.status_code >= 400
        encoding = resp.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):
            if error:  # status still reported; the body is not decoded
                return _Reply(resp.status_code, resp.headers, b"")
            shown = self._quote(encoding)[:40]
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"{what}: provider sent a {shown!r}-encoded response; only "
                "identity bodies are accepted (decompression is not bounded).",
                details={"reason": "response_encoding", "content_encoding": shown},
            )
        limit = _ERROR_BODY_READ_BYTES if error else max_bytes
        if not error:
            if on_headers is not None:
                on_headers(resp)
            declared = resp.headers.get("content-length", "")
            if limit is not None and declared.isdigit() and int(declared) > limit:
                self._overflow(int(declared), limit, what, on_overflow)
        buf = bytearray()
        # An in-memory response (mock transports) was read at construction;
        # a network response is streamed raw (never decoded).
        chunks = [resp.content] if resp.is_stream_consumed else resp.iter_raw()
        for chunk in chunks:
            buf.extend(chunk)
            if deadline is not None and _monotonic() >= deadline:
                raise _deadline_error(self._ref(job))
            if limit is not None and len(buf) > limit:
                if error:
                    del buf[limit:]
                    break
                self._overflow(len(buf), limit, what, on_overflow)
        return _Reply(resp.status_code, resp.headers, bytes(buf))

    @staticmethod
    def _overflow(size: int, limit: int, what: str, on_overflow) -> None:
        if on_overflow is not None:
            on_overflow(size)
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what}: provider response exceeds the {limit}-byte cap.",
            details={"reason": "response_too_large", "max_bytes": limit},
        )

    def _snippet(self, resp: _Reply) -> str:
        try:
            text = resp.text.strip()
        except Exception:  # noqa: BLE001 (best-effort diagnostics)
            return ""
        text = self._quote(text)
        return f" Provider response: {text}" if text else ""

    def _raise_for_status(self, resp: _Reply, *, what: str) -> None:
        status = resp.status_code
        if status < 400:
            return
        if status == 429:
            raise StudioError(
                ErrorCode.RATE_LIMITED,
                f"{what}: provider rate limit (HTTP 429)." + self._snippet(resp),
            )
        raise StudioError(
            ErrorCode.PROVIDER_ERROR,
            f"{what}: HTTP {status}." + self._snippet(resp),
        )

    @staticmethod
    def _as_json(resp: _Reply, *, what: str) -> dict:
        try:
            data = resp.json()
        except ValueError as e:
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"{what}: invalid JSON in provider response (HTTP {resp.status_code}).",
            ) from e
        if not isinstance(data, dict):
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"{what}: expected a JSON object in the provider response, "
                f"got {type(data).__name__}.",
            )
        return data

    def _map_state(self, raw, st: dict, model_id: str) -> tuple[str, str]:
        """Provider state value -> (our state, stage label).

        A missing or unmapped state is an error (ADR-0014): "queued forever"
        hid broken providers. Seeds opt in explicitly with
        ``status.missing_state`` (e.g. ComfyUI: /history is {} until the
        prompt finishes) / ``status.unknown_state``.
        """
        state_map = st.get("state_map") or {}
        if raw is None or raw is _MISSING:
            tolerated = st.get("missing_state")
            if tolerated in ("queued", "running"):
                return tolerated, "submitted"
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"Model {model_id}: provider status response has no state at "
                f"'{st.get('state_path')}'.",
                details={"reason": "status_missing"},
            )
        if isinstance(raw, bool):
            return ("succeeded", "completed") if raw else ("running", "in_progress")
        key = str(raw)
        mapped = state_map.get(key)
        if mapped is None:
            tolerated = st.get("unknown_state")
            shown = self._quote(key)[:100]  # provider text (B7-r2 M1)
            if tolerated in ("queued", "running"):
                # Seed opted in: keep polling, surface the raw state.
                return tolerated, shown
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"Model {model_id}: unrecognized provider state {shown!r} "
                "(not in capabilities.http.status.state_map).",
                details={"reason": "status_unrecognized", "state": shown},
            )
        if mapped not in _TERMINAL_STATES:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id}: capabilities.http.status.state_map maps "
                f"{key!r} to invalid state {mapped!r}; must be one of "
                f"{', '.join(_TERMINAL_STATES)}.",
            )
        return mapped, self._quote(key)[:100]

    def _extract_urls(
        self, value, url_template: str | None, ctx: dict, model_id: str
    ) -> list[str]:
        """Normalize the output_url_path value to a URL list.

        Accepts a string, a list of strings, or a list of dicts (rendered
        through ``output_url_template``, e.g. ComfyUI /view URLs).
        """
        items = value if isinstance(value, list) else [value]
        urls: list[str] = []
        for item in items:
            if item is None or item is _MISSING:
                continue
            if isinstance(item, dict):
                if not url_template:
                    raise StudioError(
                        ErrorCode.MODEL_UNAVAILABLE,
                        f"Model {model_id}: result output is a list of objects "
                        "but capabilities.http.result.output_url_template is not set.",
                    )
                merged = dict(ctx)
                merged.update({k: ("" if v is None else v) for k, v in item.items()})
                urls.append(str(_render(url_template, merged, model_id=model_id)))
            else:
                urls.append(str(item))
        return [u for u in urls if u]

    def _get_status(
        self, provider_job_id: str, model_id: str, cfg: dict
    ) -> tuple[JobStatus, dict]:
        """Poll the status endpoint. Returns (JobStatus, raw response body)."""
        st = cfg.get("status") or {}
        url_template = st.get("url_template")
        if not url_template:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id}: capabilities.http.status.url_template is required.",
            )
        ctx = self._ctx(model_id, provider_job_id, cfg, {})
        url = str(_render(str(url_template), ctx, model_id=model_id))
        headers = _resolve_env(st.get("headers") or {}, model_id=model_id)
        method = str(st.get("method", "GET")).upper()
        what = f"Status poll for provider job {self._ref(provider_job_id)}"
        res = cfg.get("result") or {}
        modality = self._modality.get(provider_job_id) or "image"
        resp = self._call(
            method,
            url,
            headers=headers,
            timeout=float(cfg.get("poll_timeout_sec", defaults.HTTP_POLL_TIMEOUT_SEC)),
            what=what,
            deadline=self._deadlines.get(provider_job_id),
            job=provider_job_id,
            # The status body carries the inline output when there is no
            # separate result endpoint.
            max_bytes=self._json_cap(cfg, modality, carries_result=not res.get("url")),
        )
        if resp.status_code == 404:
            # The provider no longer knows this job: terminal, not retryable
            # as a poll — report failed rather than raising.
            return JobStatus(
                state="failed",
                error_code=ErrorCode.PROVIDER_ERROR,
                error_message=f"Provider job {self._ref(provider_job_id)} not found (HTTP 404).",
            ), {}
        self._raise_for_status(resp, what=what)
        body = self._as_json(resp, what=what)

        state_path = st.get("state_path")
        raw = (
            _dig(body, str(_render(str(state_path), ctx, model_id=model_id)))
            if state_path
            else _MISSING
        )
        state, stage = self._map_state(raw, st, model_id)

        progress = 0.0
        if state == "succeeded":
            progress = 100.0
        elif state == "running" and st.get("progress_path"):
            dug = _dig(
                body, str(_render(str(st["progress_path"]), ctx, model_id=model_id))
            )
            progress = _to_progress(None if dug is _MISSING else dug)

        error_message: str | None = None
        error_code: str | None = None
        if state == "failed":
            error_code = ErrorCode.PROVIDER_ERROR
            if st.get("error_path"):
                dug = _dig(
                    body, str(_render(str(st["error_path"]), ctx, model_id=model_id))
                )
                if dug is not None and dug is not _MISSING:
                    # Provider text -> error_message: redacted (B7-r2 M1).
                    error_message = self._quote(dug)
            if error_message is None:
                error_message = f"Provider job {self._ref(provider_job_id)} failed."

        output_urls: list[str] = []
        if state == "succeeded" and not res.get("url") and res.get("output_url_path"):
            dug = _dig(
                body, str(_render(str(res["output_url_path"]), ctx, model_id=model_id))
            )
            if dug is not None and dug is not _MISSING:
                output_urls = self._extract_urls(
                    dug, res.get("output_url_template"), ctx, model_id
                )

        return JobStatus(
            state=state,
            progress_pct=progress,
            stage=stage,
            output_urls=output_urls,
            error_code=error_code,
            error_message=error_message,
        ), body

    def _upload_inputs(
        self, model_id: str, cfg: dict, ctx: dict, deadline: float | None = None
    ) -> dict:
        """Run every configured upload (``upload`` is one dict or a list).

        One random token per submit (= per job attempt) prefixes every
        uploaded filename (B7-r2 H3)."""
        token = uuid.uuid4().hex[:16]
        uploads = cfg.get("upload") or []
        if isinstance(uploads, dict):
            uploads = [uploads]
        for up in uploads:
            ctx = self._upload_one(model_id, up, ctx, deadline, token)
        return ctx

    def _upload_one(
        self, model_id: str, up: dict, ctx: dict, deadline: float | None,
        token: str = "",
    ) -> dict:
        """Optional pre-submit file upload (e.g. ComfyUI /upload/image).

        ``capabilities.http.upload``::

            "upload": {
              "url": "{base_url}/upload/image",  # rendered against ctx
              "method": "POST",                  # optional, default POST
              "headers": {},                     # optional; env: refs allowed
              "file_param": "image",             # multipart field name
              "file_source": "{init_image}",     # submit-payload param holding
                                                # a LOCAL file path. When it is
                                                # exactly one placeholder whose
                                                # value is missing/empty, the
                                                # whole step is SKIPPED.
              "extra_fields": {"overwrite": "true", "type": "input"},
                                                # optional form fields,
                                                # rendered against ctx
              "filename_response_path": "name",  # JSON path of the stored
                                                # filename in the response
              "subfolder_response_path": "subfolder",  # optional; a non-
                                                # empty value is joined as
                                                # "<subfolder>/<name>"
              "store_as": "uploaded_image",      # ctx key the filename lands
                                                # in (for {..} in the submit
                                                # body_template)
              "timeout_sec": 60.0,               # optional, default 60
            }

        Returns ctx with ``store_as`` set to the provider's stored filename
        — the name the provider RETURNS, never the local basename. The
        multipart filename is ``hs-<token>-<basename>``: unique per submit,
        so two jobs whose inputs share a basename never overwrite each
        other's input on the provider (B7-r2 H3).
        Absent ``upload`` config or absent input file -> ctx unchanged, so a
        text-only job on an upload-capable seed simply skips this step.
        Failures (missing file, HTTP/JSON errors, no filename in the
        response) are StudioError — never raw exceptions.
        """
        if not up:
            return ctx
        source_tpl = up.get("file_source")
        if not source_tpl:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id}: capabilities.http.upload.file_source is "
                "required when the upload block is present.",
            )
        full = _PLACEHOLDER_RE.fullmatch(str(source_tpl))
        if full:
            local = ctx.get(full.group(1))
            if not local:
                log.info(
                    "Model %s: upload step skipped (no '%s' in submit payload).",
                    model_id,
                    full.group(1),
                )
                return ctx
            file_path = str(local)
        else:
            file_path = str(_render(source_tpl, ctx, model_id=model_id))
        path = Path(file_path)
        if not path.is_file():
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"Model {model_id}: upload input file not found: {file_path}.",
            )

        url = str(
            _render(
                str(_resolve_env(up.get("url"), model_id=model_id)),
                ctx,
                model_id=model_id,
            )
        )
        if not url:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id}: capabilities.http.upload.url is required.",
            )
        headers = _resolve_env(up.get("headers") or {}, model_id=model_id)
        method = str(up.get("method", "POST")).upper()
        fields = _render(up.get("extra_fields") or {}, ctx, model_id=model_id)
        file_param = str(up.get("file_param", "image"))
        store_as = str(up.get("store_as", "uploaded_image"))
        timeout = float(up.get("timeout_sec", defaults.HTTP_UPLOAD_TIMEOUT_SEC))
        what = f"Input upload for model {model_id}"

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        upload_name = f"hs-{token or uuid.uuid4().hex[:16]}-{path.name}"
        files = {file_param: (upload_name, path.read_bytes(), content_type)}
        resp = self._call(
            method,
            url,
            headers=headers,
            data={k: str(v) for k, v in fields.items()},
            files=files,
            timeout=timeout,
            what=what,
            deadline=deadline,
        )
        self._raise_for_status(resp, what=what)
        body = self._as_json(resp, what=what)

        name_path = str(up.get("filename_response_path", "name"))
        stored = _dig(body, str(_render(name_path, ctx, model_id=model_id)))
        if stored is None or stored is _MISSING or str(stored) == "":
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"{what}: upload response has no stored filename at "
                f"'{name_path}'.",
            )
        stored = str(stored)
        sub_path = up.get("subfolder_response_path")
        if sub_path:
            sub = _dig(body, str(sub_path))
            if sub is not None and sub is not _MISSING and str(sub).strip("/"):
                stored = f"{str(sub).strip('/')}/{stored}"
        ctx[store_as] = stored
        log.info("Model %s: uploaded %s as %s -> stored as %r.", model_id, path.name,
                 upload_name, ctx[store_as])
        return ctx

    # -- ProviderAdapter interface ------------------------------------

    def _reject_unconsumed(self, model_id: str, cfg: dict, ctx: dict) -> None:
        """Editing inputs the seed cannot take are rejected, never ignored."""
        consumed = capabilities.http_placeholders(cfg)
        extra = [
            key
            for key in (contract.KEY_INIT_IMAGE, contract.KEY_MASK)
            if ctx.get(key) and key not in consumed and f"{key}_data_uri" not in consumed
        ]
        if extra:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Model {model_id} cannot take the input(s) {', '.join(extra)}; "
                "its capabilities.http templates do not consume them.",
                details={"reason": "input_unsupported", "inputs": extra},
            )

    @staticmethod
    def _add_data_uris(model_id: str, cfg: dict, ctx: dict) -> None:
        """Fill ``{<file key>_data_uri}`` placeholders from local files."""
        suffix = "_data_uri"
        for name in capabilities.http_placeholders(cfg):
            if not name.endswith(suffix):
                continue
            local = ctx.get(name[: -len(suffix)])
            if not local:
                continue
            path = Path(str(local))
            if not path.is_file():
                raise StudioError(
                    ErrorCode.PROVIDER_ERROR,
                    f"Model {model_id}: input file not found: {path}.",
                )
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            ctx[name] = f"data:{mime};base64,{encoded}"

    def submit(self, model_id: str, payload: dict) -> str:
        """Returns the local job HANDLE (``job_handle``), not the bare
        remote id."""
        try:
            return self._submit(model_id, payload)
        except StudioError as exc:
            raise self._sanitized(exc)

    def poll(self, provider_job_id: str) -> JobStatus:
        try:
            status = self._poll(provider_job_id)
        except StudioError as exc:
            raise self._sanitized(exc)
        if status.error_message:
            status = dataclasses.replace(
                status, error_message=self.sanitize_message(status.error_message)
            )
        return status

    def fetch_result(self, provider_job_id: str) -> bytes:
        try:
            return self._fetch_result(provider_job_id)
        except StudioError as exc:
            raise self._sanitized(exc)

    def _submit(self, model_id: str, payload: dict) -> str:
        # The job deadline starts before the first byte leaves (uploads count).
        deadline = _monotonic() + defaults.HTTP_JOB_TOTAL_DEADLINE_SEC
        cfg = self._http_config(model_id)
        sub = cfg.get("submit") or {}
        url = _resolve_env(sub.get("url"), model_id=model_id)
        if not url:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id}: capabilities.http.submit.url is required.",
            )
        method = str(sub.get("method", "POST")).upper()
        headers = _resolve_env(sub.get("headers") or {}, model_id=model_id)
        ctx = self._ctx(model_id, "", cfg, payload)
        ctx.setdefault(contract.KEY_NEGATIVE, "")
        self._reject_unconsumed(model_id, cfg, ctx)
        ctx = self._upload_inputs(model_id, cfg, ctx, deadline)
        self._add_data_uris(model_id, cfg, ctx)
        body = _render(sub.get("body_template") or {}, ctx, model_id=model_id)

        what = f"Submit to model {model_id}"
        resp = self._call(
            method,
            str(url),
            headers=headers,
            json_body=body,
            timeout=float(cfg.get("timeout_sec", defaults.HTTP_SUBMIT_TIMEOUT_SEC)),
            what=what,
            deadline=deadline,
        )
        self._raise_for_status(resp, what=what)
        data = self._as_json(resp, what=what)

        job_id_path = cfg.get("job_id_path")
        if not job_id_path:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id}: capabilities.http.job_id_path is required.",
            )
        job_id = _dig(data, str(_render(str(job_id_path), ctx, model_id=model_id)))
        if job_id is None or job_id is _MISSING or str(job_id) == "":
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"{what}: submit response has no job id at '{job_id_path}'.",
            )
        remote_id = str(job_id)
        provider_job_id = job_handle(remote_id, model_id)
        self._remember(model_id, cfg, provider_job_id, remote_id)
        self._deadlines[provider_job_id] = deadline
        self._modality[provider_job_id] = self._modality_of(model_id)
        return provider_job_id

    def _poll(self, provider_job_id: str) -> JobStatus:
        model_id, cfg = self._lookup(provider_job_id)
        self._check_deadline(provider_job_id)
        status, _ = self._get_status(provider_job_id, model_id, cfg)
        return status

    def cancel(self, provider_job_id: str) -> None:
        try:
            model_id, cfg = self._lookup(provider_job_id)
        except Exception:  # noqa: BLE001 (unknown job: best-effort no-op)
            return None
        cancel_cfg = cfg.get("cancel")
        if not cancel_cfg:
            # No cancel endpoint configured: best-effort no-op (base.py contract).
            return None
        try:
            ctx = self._ctx(model_id, provider_job_id, cfg, {})
            url = _render(cancel_cfg.get("url_template"), ctx, model_id=model_id)
            headers = _resolve_env(cancel_cfg.get("headers") or {}, model_id=model_id)
            method = str(cancel_cfg.get("method", "POST")).upper()
            # Not capped by the job deadline (cancel runs after it passed):
            # its own short absolute cap instead (B7-r2 H2).
            self._call(
                method,
                str(url),
                headers=headers,
                timeout=defaults.HTTP_CANCEL_TIMEOUT_SEC,
                what=f"Cancel provider job {self._ref(provider_job_id)}",
                deadline=_monotonic() + defaults.HTTP_CANCEL_TIMEOUT_SEC,
                job=provider_job_id,
                max_bytes=_ERROR_BODY_READ_BYTES,
            )
        except Exception:  # noqa: BLE001 (best-effort cancellation)
            pass
        return None

    def estimate(self, model_id: str, payload: dict) -> EstimatedCost:
        """Cost estimate from the registry price table. No network calls.

        Unit formulas mirror services/cost_estimator.py (duplicated here because
        models_lib may not import services/ per ARCHITECTURE §15).
        """
        from app.models_lib import registry  # noqa: PLC0415 (deferred: same module)

        model = registry.get_model(model_id)
        pricing = model.get("pricing") or {}
        unit = str(pricing.get("unit", "per_image"))
        amount = Decimal(str(pricing.get("amount_usd", "0")))

        text = str(payload.get("text", payload.get("prompt", "")) or "")
        if unit == "per_image":
            count = int(payload.get("count", 1))
            total, qty = amount * count, f"{count} image(s)"
        elif unit == "per_second":
            duration = float(payload.get("duration_sec", 0) or 0)
            total, qty = amount * Decimal(str(duration)), f"{duration}s"
        elif unit == "per_call":
            total, qty = amount, "1 call"
        elif unit == "per_char":
            chars = len(text)
            total, qty = amount * chars, f"{chars} chars"
        elif unit == "per_1k_chars":
            chars = len(text)
            total, qty = amount * math.ceil(chars / 1000), f"{chars} chars"
        elif unit == "per_1k_tokens":
            tokens = payload.get("tokens")
            if tokens is None:
                # Documented heuristic when the caller has no token count.
                tokens = math.ceil(len(text) / 4)
            total = amount * math.ceil(int(tokens) / 1000)
            qty = f"{tokens} tokens"
        else:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Unknown pricing unit for model {model_id}: {unit}.",
            )
        return EstimatedCost(
            amount_usd=total,
            unit_breakdown=f"{model_id}: {unit} x {qty} @ ${amount} = ${total}",
        )

    def _download(
        self, url: str, *, timeout: float, what: str, modality: str,
        deadline: float | None, job: str,
    ) -> bytes:
        """Stream the artifact with a per-media byte cap (ADR-0014 §4).

        Content-Length larger than the cap -> rejected before the body is
        read; a lying/missing Content-Length is caught while streaming.
        Content-Type and magic bytes must match the media kind.
        """
        reply = self._stream(
            "GET",
            url,
            headers=None,
            timeout=timeout,
            what=what,
            deadline=deadline,
            job=job,
            max_bytes=media_check.max_bytes(modality),
            on_headers=lambda resp: media_check.check_content_type(
                resp.headers.get("content-type"), modality, what=what
            ),
            on_overflow=lambda size: media_check.check_size(size, modality, what=what),
        )
        self._raise_for_status(reply, what=what)
        media_check.check_media(reply.content, modality, what=what)
        return reply.content

    def _fetch_result(self, provider_job_id: str) -> bytes:
        model_id, cfg = self._lookup(provider_job_id)
        self._check_deadline(provider_job_id)
        deadline = self._deadlines.get(provider_job_id)
        modality = self._modality.get(provider_job_id) or self._modality_of(model_id)
        status, body = self._get_status(provider_job_id, model_id, cfg)
        if status.state != "succeeded":
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"fetch_result called for provider job {self._ref(provider_job_id)} in state "
                f"{status.state!r}; poll until it reports succeeded first.",
            )
        res = cfg.get("result") or {}
        ctx = self._ctx(model_id, provider_job_id, cfg, {})

        payload_body = body
        if res.get("url"):
            # Providers with split status/result endpoints (e.g. fal queue):
            # output paths dig into the result response, not the status one.
            result_url = str(_render(str(res["url"]), ctx, model_id=model_id))
            result_headers = _resolve_env(res.get("headers") or {}, model_id=model_id)
            result_method = str(res.get("method", "GET")).upper()
            what = f"Result fetch for provider job {self._ref(provider_job_id)}"
            resp = self._call(
                result_method,
                result_url,
                headers=result_headers,
                timeout=float(cfg.get("download_timeout_sec", defaults.HTTP_DOWNLOAD_TIMEOUT_SEC)),
                what=what,
                deadline=deadline,
                job=provider_job_id,
                max_bytes=self._json_cap(cfg, modality, carries_result=True),
            )
            self._raise_for_status(resp, what=what)
            payload_body = self._as_json(resp, what=what)

        bytes_path = res.get("output_bytes_path")
        if bytes_path:
            dug = _dig(
                payload_body, str(_render(str(bytes_path), ctx, model_id=model_id))
            )
            if dug is None or dug is _MISSING or str(dug) == "":
                raise StudioError(
                    ErrorCode.PROVIDER_ERROR,
                    f"Provider job {self._ref(provider_job_id)} succeeded but has no output "
                    f"bytes at '{bytes_path}'.",
                )
            what = f"Inline output of provider job {self._ref(provider_job_id)}"
            if not isinstance(dug, str):
                raise StudioError(
                    ErrorCode.PROVIDER_ERROR,
                    f"{what}: expected a base64 string at '{bytes_path}'.",
                )
            data = _decode_inline_bytes(
                dug, max_bytes=media_check.max_bytes(modality), what=what
            )
            media_check.check_media(data, modality, what=what)
            return data

        url_path = res.get("output_url_path")
        if not url_path:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id}: capabilities.http.result needs "
                "output_url_path or output_bytes_path.",
            )
        dug = _dig(payload_body, str(_render(str(url_path), ctx, model_id=model_id)))
        urls = (
            self._extract_urls(dug, res.get("output_url_template"), ctx, model_id)
            if dug is not None and dug is not _MISSING
            else []
        )
        if not urls:
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"Provider job {self._ref(provider_job_id)} succeeded but has no output "
                f"URLs at '{url_path}'.",
            )
        return self._download(
            urls[0],
            timeout=float(cfg.get("download_timeout_sec", defaults.HTTP_DOWNLOAD_TIMEOUT_SEC)),
            what=f"Output download for provider job {self._ref(provider_job_id)}",
            modality=modality,
            deadline=deadline,
            job=provider_job_id,
        )


def _abandon(adapter: ProviderAdapter, provider_job_id: str) -> None:
    """Best-effort provider cancel when the local job gives up on it."""
    try:
        adapter.cancel(provider_job_id)
    except Exception:  # noqa: BLE001 - best effort by contract
        log.warning("provider cancel failed provider_job_id=%s", provider_job_id)


def poll_until_done(
    adapter: ProviderAdapter, provider_job_id: str, on_progress=None
) -> JobStatus:
    """Shared polling loop with exponential backoff (2s -> 10s max).

    Bounded by a monotonic total deadline (ADR-0014): the adapter's own job
    deadline when it tracks one (http_polling starts it at submit), else
    ``HTTP_JOB_TOTAL_DEADLINE_SEC`` from now. Sleeps never overshoot it. On
    the deadline, a poll error or the iteration cap the provider job is
    canceled best-effort and a StudioError is raised (after submit the
    caller records PROVIDER_UNCERTAIN, plan D7 — never an automatic
    resubmit).
    """
    getter = getattr(adapter, "deadline_for", None)
    deadline = getter(provider_job_id) if callable(getter) else None
    gate = getattr(adapter, "sanitize_message", None)

    ref = getattr(adapter, "_ref", None)

    def _expired() -> StudioError:
        # Redact before capping (B7R3-M1); adapters without _ref quote no id.
        err = _deadline_error(ref(provider_job_id) if callable(ref) else "")
        if callable(gate):
            err.message = gate(err.message)
            err.args = (err.message,)
        return err

    if deadline is None:
        deadline = _monotonic() + defaults.HTTP_JOB_TOTAL_DEADLINE_SEC
    interval = defaults.POLL_INTERVAL_START_SEC
    for _ in range(defaults.POLL_MAX_ITERATIONS):
        if _monotonic() >= deadline:
            _abandon(adapter, provider_job_id)
            raise _expired()
        try:
            status = adapter.poll(provider_job_id)
        except Exception:
            _abandon(adapter, provider_job_id)
            raise
        if on_progress is not None:
            on_progress(status)
        if status.state in ("succeeded", "failed"):
            return status
        remaining = deadline - _monotonic()
        if remaining <= 0:
            _abandon(adapter, provider_job_id)
            raise _expired()
        _sleep(min(interval, remaining))
        interval = min(interval * 2, defaults.POLL_INTERVAL_MAX_SEC)
    _abandon(adapter, provider_job_id)
    raise StudioError(ErrorCode.PROVIDER_ERROR, "Polling timed out waiting for provider.")
