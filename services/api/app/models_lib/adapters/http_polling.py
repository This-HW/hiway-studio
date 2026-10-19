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
          "extra_fields": {"overwrite": "true", "type": "input"},
                                      # optional extra form fields, {..}-rendered
          "filename_response_path": "name",
                                      # JSON path of the stored filename
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
                                      # false=running. A missing path means the
                                      # provider has no status yet -> queued.
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

ComfyUI img2img fits the same schema: the ``upload`` block POSTs the input
image to ``{base_url}/upload/image`` (multipart field ``image``, form fields
``overwrite=true`` + ``type=input``) and the response's ``name`` lands in
``{uploaded_image}`` for the workflow's ``LoadImage`` node::

    "upload": {"url": "{base_url}/upload/image", "file_param": "image",
               "file_source": "{init_image}", "filename_response_path": "name",
               "extra_fields": {"overwrite": "true", "type": "input"},
               "store_as": "uploaded_image"},
    "submit": {"url": "env:COMFYUI_URL", ...,
               "body_template": {"prompt": {
                   "4": {"class_type": "LoadImage",
                         "inputs": {"image": "{uploaded_image}"}},
                   ...}}},
"""
from __future__ import annotations

import base64
import logging
import math
import mimetypes
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.core import defaults
from app.core.errors import ErrorCode, StudioError
from app.models_lib.base import EstimatedCost, JobStatus, ProviderAdapter

log = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# provider_job_id -> (model_id, http config). The task flow
# (submit -> poll_until_done -> fetch_result) runs inside one task process,
# so the adapter instance that submitted the job is the one that polls it —
# same pattern as the stub adapter. Bounded to avoid unbounded growth in
# long-lived workers.
_MAX_REMEMBERED_JOBS = 10_000

_TERMINAL_STATES = ("queued", "running", "succeeded", "failed")


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


def _lookup_placeholder(name: str, ctx: dict, model_id: str):
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
            return _lookup_placeholder(full.group(1), ctx, model_id)
        return _PLACEHOLDER_RE.sub(
            lambda m: str(_lookup_placeholder(m.group(1), ctx, model_id)), value
        )
    if isinstance(value, dict):
        return {k: _render(v, ctx, model_id=model_id) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_render(v, ctx, model_id=model_id) for v in value]
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


def _decode_inline_bytes(value: str) -> bytes:
    """Decode base64 output bytes, tolerating a ``data:...;base64,`` prefix."""
    text = value.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
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

    # -- internals ------------------------------------------------------

    def _client_conn(self) -> httpx.Client:
        if self._client is None:
            try:
                self._client = httpx.Client(
                    transport=self._transport, timeout=30.0, follow_redirects=True
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
                    follow_redirects=True,
                    trust_env=False,
                )
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
        return http

    def _remember(self, model_id: str, cfg: dict, provider_job_id: str) -> None:
        self._jobs[provider_job_id] = (model_id, cfg)
        while len(self._jobs) > _MAX_REMEMBERED_JOBS:
            self._jobs.pop(next(iter(self._jobs)))

    def _lookup(self, provider_job_id: str) -> tuple[str, dict]:
        try:
            return self._jobs[provider_job_id]
        except KeyError:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Unknown provider job id {provider_job_id!r}; it was not "
                "submitted through this adapter instance.",
            ) from None

    def _ctx(self, model_id: str, provider_job_id: str, cfg: dict, payload: dict) -> dict:
        ctx: dict = {}
        ctx.update(payload)
        ctx["model_id"] = model_id
        ctx["job_id"] = provider_job_id
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
    ) -> httpx.Response:
        """One HTTP call. Network-level failures become StudioError(PROVIDER_ERROR).

        Raw httpx exceptions never escape this adapter (base.py contract).
        """
        try:
            return self._client_conn().request(
                method,
                url,
                headers=headers or None,
                json=json_body,
                data=data,
                files=files,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"{what}: network error ({e.__class__.__name__}: {e}).",
            ) from e

    @staticmethod
    def _snippet(resp: httpx.Response) -> str:
        try:
            text = resp.text.strip()
        except Exception:  # noqa: BLE001 (best-effort diagnostics)
            return ""
        return f" Provider response: {text[:300]}" if text else ""

    def _raise_for_status(self, resp: httpx.Response, *, what: str) -> None:
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
    def _as_json(resp: httpx.Response, *, what: str) -> dict:
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

    def _map_state(self, raw, state_map: dict, model_id: str) -> tuple[str, str]:
        """Provider state value -> (our state, stage label)."""
        if raw is None or raw is _MISSING:
            # Provider has no status for the job yet (e.g. ComfyUI history
            # before the first update): treat as queued, keep polling.
            return "queued", "submitted"
        if isinstance(raw, bool):
            return ("succeeded", "completed") if raw else ("running", "in_progress")
        key = str(raw)
        mapped = state_map.get(key)
        if mapped is None:
            # Unknown provider state: keep polling and surface the raw state in
            # `stage` instead of failing the job on a provider-side addition.
            return "running", key
        if mapped not in _TERMINAL_STATES:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id}: capabilities.http.status.state_map maps "
                f"{key!r} to invalid state {mapped!r}; must be one of "
                f"{', '.join(_TERMINAL_STATES)}.",
            )
        return mapped, key

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
        what = f"Status poll for provider job {provider_job_id}"
        resp = self._call(
            method,
            url,
            headers=headers,
            timeout=float(cfg.get("poll_timeout_sec", 15.0)),
            what=what,
        )
        if resp.status_code == 404:
            # The provider no longer knows this job: terminal, not retryable
            # as a poll — report failed rather than raising.
            return JobStatus(
                state="failed",
                error_code=ErrorCode.PROVIDER_ERROR,
                error_message=f"Provider job {provider_job_id} not found (HTTP 404).",
            ), {}
        self._raise_for_status(resp, what=what)
        body = self._as_json(resp, what=what)

        state_path = st.get("state_path")
        raw = (
            _dig(body, str(_render(str(state_path), ctx, model_id=model_id)))
            if state_path
            else _MISSING
        )
        state, stage = self._map_state(raw, st.get("state_map") or {}, model_id)

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
                    error_message = str(dug)
            if error_message is None:
                error_message = f"Provider job {provider_job_id} failed."

        output_urls: list[str] = []
        res = cfg.get("result") or {}
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

    def _upload_inputs(self, model_id: str, cfg: dict, ctx: dict) -> dict:
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
              "store_as": "uploaded_image",      # ctx key the filename lands
                                                # in (for {..} in the submit
                                                # body_template)
              "timeout_sec": 60.0,               # optional, default 60
            }

        Returns ctx with ``store_as`` set to the provider's stored filename.
        Absent ``upload`` config or absent input file -> ctx unchanged, so a
        text-only job on an upload-capable seed simply skips this step.
        Failures (missing file, HTTP/JSON errors, no filename in the
        response) are StudioError — never raw exceptions.
        """
        up = cfg.get("upload") or {}
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
        timeout = float(up.get("timeout_sec", 60.0))
        what = f"Input upload for model {model_id}"

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files = {file_param: (path.name, path.read_bytes(), content_type)}
        resp = self._call(
            method,
            url,
            headers=headers,
            data={k: str(v) for k, v in fields.items()},
            files=files,
            timeout=timeout,
            what=what,
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
        ctx[store_as] = str(stored)
        log.info("Model %s: uploaded %s -> stored as %r.", model_id, path.name, ctx[store_as])
        return ctx

    # -- ProviderAdapter interface ------------------------------------

    def submit(self, model_id: str, payload: dict) -> str:
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
        ctx = self._upload_inputs(model_id, cfg, ctx)
        body = _render(sub.get("body_template") or {}, ctx, model_id=model_id)

        what = f"Submit to model {model_id}"
        resp = self._call(
            method,
            str(url),
            headers=headers,
            json_body=body,
            timeout=float(cfg.get("timeout_sec", 30.0)),
            what=what,
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
        provider_job_id = str(job_id)
        self._remember(model_id, cfg, provider_job_id)
        return provider_job_id

    def poll(self, provider_job_id: str) -> JobStatus:
        model_id, cfg = self._lookup(provider_job_id)
        status, _ = self._get_status(provider_job_id, model_id, cfg)
        return status

    def cancel(self, provider_job_id: str) -> None:
        entry = self._jobs.get(provider_job_id)
        if entry is None:
            return None
        model_id, cfg = entry
        cancel_cfg = cfg.get("cancel")
        if not cancel_cfg:
            # No cancel endpoint configured: best-effort no-op (base.py contract).
            return None
        try:
            ctx = self._ctx(model_id, provider_job_id, cfg, {})
            url = _render(cancel_cfg.get("url_template"), ctx, model_id=model_id)
            headers = _resolve_env(cancel_cfg.get("headers") or {}, model_id=model_id)
            method = str(cancel_cfg.get("method", "POST")).upper()
            self._call(
                method,
                str(url),
                headers=headers,
                timeout=10.0,
                what=f"Cancel provider job {provider_job_id}",
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

    def fetch_result(self, provider_job_id: str) -> bytes:
        model_id, cfg = self._lookup(provider_job_id)
        status, body = self._get_status(provider_job_id, model_id, cfg)
        if status.state != "succeeded":
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"fetch_result called for provider job {provider_job_id} in state "
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
            what = f"Result fetch for provider job {provider_job_id}"
            resp = self._call(
                result_method,
                result_url,
                headers=result_headers,
                timeout=float(cfg.get("download_timeout_sec", 120.0)),
                what=what,
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
                    f"Provider job {provider_job_id} succeeded but has no output "
                    f"bytes at '{bytes_path}'.",
                )
            return _decode_inline_bytes(str(dug))

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
                f"Provider job {provider_job_id} succeeded but has no output "
                f"URLs at '{url_path}'.",
            )
        what = f"Output download for provider job {provider_job_id}"
        resp = self._call(
            "GET",
            urls[0],
            headers=None,
            timeout=float(cfg.get("download_timeout_sec", 120.0)),
            what=what,
        )
        self._raise_for_status(resp, what=what)
        return resp.content


def poll_until_done(
    adapter: ProviderAdapter, provider_job_id: str, on_progress=None
) -> JobStatus:
    """Shared polling loop with exponential backoff (2s -> 10s max)."""
    interval = defaults.POLL_INTERVAL_START_SEC
    for _ in range(defaults.POLL_MAX_ITERATIONS):
        status = adapter.poll(provider_job_id)
        if on_progress is not None:
            on_progress(status)
        if status.state in ("succeeded", "failed"):
            return status
        time.sleep(interval)
        interval = min(interval * 2, defaults.POLL_INTERVAL_MAX_SEC)
    raise StudioError(ErrorCode.PROVIDER_ERROR, "Polling timed out waiting for provider.")
