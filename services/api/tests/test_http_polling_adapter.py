"""Tests for the generic HTTP polling adapter (models_lib/adapters/http_polling.py).

All provider HTTP is faked with httpx.MockTransport — no real network calls.
"""
from __future__ import annotations

import base64
import io
import json
from decimal import Decimal

import httpx
import pytest
from PIL import Image

from app.core import defaults
from app.core.errors import ErrorCode, StudioError
from app.models_lib import registry
from app.models_lib.adapters.http_polling import (
    HttpPollingAdapter,
    poll_until_done,
    remote_job_id,
)


# Results must be real media (magic bytes + Pillow decode, ADR-0014 / audit
# P16, B7-r2 M3): a real, decodable PNG. Tests append a marker after IEND to
# tell results apart (still a decodable image).
def _real_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


_PNG = _real_png()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _http_config(**over):
    cfg = {
        "timeout_sec": 5,
        "submit": {
            "url": "https://provider.test/v1/generate",
            "method": "POST",
            "headers": {"Authorization": "env:FAKE_API_KEY"},
            "body_template": {
                "prompt": "{prompt}",
                "image_size": {"width": "{width}", "height": "{height}"},
                "seed": "{seed}",
                "static": True,
            },
        },
        "job_id_path": "id",
        "status": {
            "url_template": "https://provider.test/v1/jobs/{job_id}",
            "method": "GET",
            "state_path": "state",
            "state_map": {
                "QUEUED": "queued",
                "RUNNING": "running",
                "DONE": "succeeded",
                "ERROR": "failed",
            },
            "progress_path": "progress",
            "error_path": "error",
        },
        "result": {"output_url_path": "output.0"},
        "cancel": {
            "url_template": "https://provider.test/v1/jobs/{job_id}/cancel",
            "method": "POST",
        },
    }
    cfg.update(over)
    return cfg


def _model(http=_http_config(), pricing=None, model_id="fake.t2i"):
    return {
        "model_id": model_id,
        "modality": "image",
        "provider_id": "http_polling",
        "status": "active",
        "pricing": pricing if pricing is not None else {"unit": "per_image", "amount_usd": "0.025"},
        "capabilities": {"http": http},
    }


class _FakeProvider:
    """Stateful fake: submit -> QUEUED -> RUNNING -> DONE."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.polls = 0
        self.fail_mode: str | None = None  # "error-state" | "connect" | "timeout"
        self.submit_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/generate":
            if self.fail_mode == "connect":
                raise httpx.ConnectError("connection refused", request=request)
            if self.fail_mode == "timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            if self.submit_status != 200:
                return httpx.Response(self.submit_status, json={"error": "denied"})
            return httpx.Response(200, json={"id": "job-1"})
        if path == "/v1/jobs/job-1":
            if self.fail_mode == "connect":
                raise httpx.ConnectError("connection refused", request=request)
            if self.fail_mode == "timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            self.polls += 1
            if self.fail_mode == "error-state":
                return httpx.Response(200, json={"state": "ERROR", "error": "content moderated"})
            seq = [
                {"state": "QUEUED"},
                {"state": "RUNNING", "progress": 0.5},
                {"state": "DONE", "output": ["https://cdn.test/out.png"]},
            ]
            return httpx.Response(200, json=seq[min(self.polls - 1, 2)])
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=_PNG + b"-FAKE-BYTES")
        if path == "/v1/jobs/job-1/cancel":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "not found"})


def _install(monkeypatch, model, handler) -> HttpPollingAdapter:
    monkeypatch.setattr(registry, "get_model", lambda model_id, db=None: model)
    return HttpPollingAdapter(transport=httpx.MockTransport(handler))


def _payload(**over):
    p = {"prompt": "a cat", "width": 1024, "height": 768, "seed": 42}
    p.update(over)
    return p


# ---------------------------------------------------------------------------
# End-to-end flow
# ---------------------------------------------------------------------------

def test_submit_poll_fetch_result_end_to_end(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "Bearer test-key")
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(), fake)

    job_id = adapter.submit("fake.t2i", {"prompt": "a cat", "width": 1024, "height": 768, "seed": 42})
    assert remote_job_id(job_id) == "job-1"  # submit returns a local handle (B7-r3 M5)

    # submit: headers resolved from env, placeholders substituted with types kept
    req = fake.requests[0]
    assert req.headers["authorization"] == "Bearer test-key"
    assert json.loads(req.content) == {
        "prompt": "a cat",
        "image_size": {"width": 1024, "height": 768},
        "seed": 42,
        "static": True,
    }

    s1 = adapter.poll(job_id)
    assert (s1.state, s1.progress_pct) == ("queued", 0.0)
    s2 = adapter.poll(job_id)
    assert s2.state == "running" and s2.progress_pct == 50.0
    s3 = adapter.poll(job_id)
    assert s3.state == "succeeded"
    assert s3.progress_pct == 100.0
    assert s3.output_urls == ["https://cdn.test/out.png"]

    assert adapter.fetch_result(job_id) == _PNG + b"-FAKE-BYTES"


def test_poll_until_done_uses_shared_loop(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "x")
    monkeypatch.setattr(defaults, "POLL_INTERVAL_START_SEC", 0.0)
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(), fake)

    job_id = adapter.submit("fake.t2i", _payload())
    seen = []
    final = poll_until_done(adapter, job_id, on_progress=seen.append)
    assert final.state == "succeeded"
    assert [s.state for s in seen] == ["queued", "running", "succeeded"]


def test_failed_state_mapping(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "x")
    fake = _FakeProvider()
    fake.fail_mode = "error-state"
    adapter = _install(monkeypatch, _model(), fake)

    job_id = adapter.submit("fake.t2i", _payload())
    status = adapter.poll(job_id)
    assert status.state == "failed"
    assert status.error_code == ErrorCode.PROVIDER_ERROR
    assert status.error_message == "content moderated"


def test_unmapped_provider_state_stays_running(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "x")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "job-9"})
        return httpx.Response(200, json={"state": "MYSTERIOUS"})

    adapter = _install(monkeypatch, _model(), handler)
    job_id = adapter.submit("fake.t2i", _payload())
    # ADR-0014: an unmapped state is an error by default (no "running forever").
    with pytest.raises(StudioError) as ei:
        adapter.poll(job_id)
    assert ei.value.details["reason"] == "status_unrecognized"

    # Explicit seed opt-in keeps polling with the raw state surfaced.
    http = _http_config()
    http["status"] = dict(http["status"], unknown_state="running")
    adapter = _install(monkeypatch, _model(http=http), handler)
    job_id = adapter.submit("fake.t2i", _payload())
    status = adapter.poll(job_id)
    assert status.state == "running"
    assert status.stage == "MYSTERIOUS"  # raw state surfaced, not failed


# ---------------------------------------------------------------------------
# Error handling: raw exceptions must never escape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fail_mode", ["connect", "timeout"])
def test_transient_network_error_becomes_provider_error(monkeypatch, fail_mode):
    monkeypatch.setenv("FAKE_API_KEY", "x")
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(), fake)

    # submit path
    fake.fail_mode = fail_mode
    with pytest.raises(StudioError) as ei:
        adapter.submit("fake.t2i", _payload())
    assert ei.value.code == ErrorCode.PROVIDER_ERROR
    assert not isinstance(ei.value, httpx.HTTPError)

    # poll path: submit fine first, then the network drops
    fake.fail_mode = None
    job_id = adapter.submit("fake.t2i", _payload())
    fake.fail_mode = fail_mode
    with pytest.raises(StudioError) as ei2:
        adapter.poll(job_id)
    assert ei2.value.code == ErrorCode.PROVIDER_ERROR
    assert not isinstance(ei2.value, httpx.HTTPError)


def test_submit_http_errors(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "x")
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(), fake)

    fake.submit_status = 401
    with pytest.raises(StudioError) as ei:
        adapter.submit("fake.t2i", _payload())
    assert ei.value.code == ErrorCode.PROVIDER_ERROR

    fake.submit_status = 429
    with pytest.raises(StudioError) as ei2:
        adapter.submit("fake.t2i", _payload())
    assert ei2.value.code == ErrorCode.RATE_LIMITED


def test_missing_env_secret_is_clear(monkeypatch):
    monkeypatch.delenv("FAKE_API_KEY", raising=False)
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(), fake)

    with pytest.raises(StudioError) as ei:
        adapter.submit("fake.t2i", _payload())
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert "FAKE_API_KEY" in ei.value.message


def test_missing_http_config_is_clear(monkeypatch):
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(http=None) | {"capabilities": {}}, fake)
    with pytest.raises(StudioError) as ei:
        adapter.submit("fake.t2i", _payload())
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert "capabilities.http" in ei.value.message


def test_submit_without_job_id_in_response(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": 1})

    adapter = _install(monkeypatch, _model(), handler)
    with pytest.raises(StudioError) as ei:
        adapter.submit("fake.t2i", _payload())
    assert ei.value.code == ErrorCode.PROVIDER_ERROR
    assert "job id" in ei.value.message


def test_unknown_provider_job_id(monkeypatch):
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(), fake)
    with pytest.raises(StudioError) as ei:
        adapter.poll("never-submitted")
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

def test_cancel_without_config_is_best_effort_noop(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "x")
    fake = _FakeProvider()
    http = _http_config()
    del http["cancel"]
    adapter = _install(monkeypatch, _model(http=http), fake)

    job_id = adapter.submit("fake.t2i", _payload())
    assert adapter.cancel(job_id) is None
    assert all("cancel" not in r.url.path for r in fake.requests)


def test_cancel_calls_configured_endpoint(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "x")
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(), fake)

    job_id = adapter.submit("fake.t2i", _payload())
    assert adapter.cancel(job_id) is None
    cancel_reqs = [r for r in fake.requests if r.url.path.endswith("/cancel")]
    assert len(cancel_reqs) == 1
    assert cancel_reqs[0].method == "POST"


def test_cancel_unknown_job_is_noop(monkeypatch):
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(), fake)
    assert adapter.cancel("never-submitted") is None
    assert fake.requests == []


# ---------------------------------------------------------------------------
# estimate: registry pricing, no network
# ---------------------------------------------------------------------------

def test_estimate_from_registry_pricing(monkeypatch):
    fake = _FakeProvider()
    adapter = _install(monkeypatch, _model(), fake)

    est = adapter.estimate("fake.t2i", {"prompt": "x", "count": 2})
    assert est.amount_usd == Decimal("0.05")
    assert "per_image" in est.unit_breakdown
    # No HTTP calls happened for estimate.
    assert fake.requests == []


def test_estimate_units(monkeypatch):
    fake = _FakeProvider()

    adapter = _install(
        monkeypatch,
        _model(pricing={"unit": "per_second", "amount_usd": "0.10"}),
        fake,
    )
    assert adapter.estimate("fake.t2i", {"duration_sec": 5.0}).amount_usd == Decimal("0.5")

    adapter = _install(
        monkeypatch, _model(pricing={"unit": "per_call", "amount_usd": "2.5"}), fake
    )
    assert adapter.estimate("fake.t2i", {}).amount_usd == Decimal("2.5")

    adapter = _install(
        monkeypatch, _model(pricing={"unit": "per_char", "amount_usd": "0.001"}), fake
    )
    assert adapter.estimate("fake.t2i", {"text": "abcd"}).amount_usd == Decimal("0.004")


def test_estimate_unknown_unit(monkeypatch):
    fake = _FakeProvider()
    adapter = _install(
        monkeypatch, _model(pricing={"unit": "per_furlong", "amount_usd": "1"}), fake
    )
    with pytest.raises(StudioError) as ei:
        adapter.estimate("fake.t2i", {})
    assert ei.value.code == ErrorCode.VALIDATION_ERROR


# ---------------------------------------------------------------------------
# result variants: separate result endpoint, inline bytes, ComfyUI shape
# ---------------------------------------------------------------------------

def test_result_from_separate_endpoint(monkeypatch):
    """fal-queue style: status and result are different endpoints."""
    monkeypatch.setenv("FAKE_API_KEY", "x")
    http = _http_config()
    http["result"] = {
        "url": "https://provider.test/v1/jobs/{job_id}/result",
        "output_url_path": "images.0.url",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "job-1"})
        if request.url.path == "/v1/jobs/job-1":
            return httpx.Response(200, json={"state": "DONE"})
        if request.url.path == "/v1/jobs/job-1/result":
            return httpx.Response(200, json={"images": [{"url": "https://cdn.test/r.png"}]})
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=_PNG + b"RESULT-BYTES")
        return httpx.Response(404, json={})

    adapter = _install(monkeypatch, _model(http=http), handler)
    job_id = adapter.submit("fake.t2i", _payload())
    assert adapter.poll(job_id).state == "succeeded"
    assert adapter.fetch_result(job_id) == _PNG + b"RESULT-BYTES"


def test_result_inline_base64_bytes(monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "x")
    http = _http_config()
    http["result"] = {"output_bytes_path": "image_b64"}
    raw = base64.b64encode(_PNG + b"INLINE-BYTES").decode()
    data_uri = f"data:image/png;base64,{raw}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/generate":
            return httpx.Response(200, json={"id": "job-1"})
        # plain base64 and data-URI both decode
        payload = {"state": "DONE", "image_b64": data_uri}
        return httpx.Response(200, json=payload)

    adapter = _install(monkeypatch, _model(http=http), handler)
    job_id = adapter.submit("fake.t2i", _payload())
    assert adapter.poll(job_id).state == "succeeded"
    assert adapter.fetch_result(job_id) == _PNG + b"INLINE-BYTES"


def test_comfyui_seed_shape(monkeypatch):
    """ComfyUI: /prompt submit, /history poll, /view download — one seed config."""
    http = {
        "submit": {
            "url": "http://127.0.0.1:8188/prompt",
            "method": "POST",
            "headers": {},
            "body_template": {
                "prompt": {
                    "3": {
                        "class_type": "KSampler",
                        "inputs": {"seed": "{seed}", "steps": 20},
                    }
                }
            },
        },
        "job_id_path": "prompt_id",
        "status": {
            "url_template": "http://127.0.0.1:8188/history/{job_id}",
            "state_path": "{job_id}.status.completed",
            "state_map": {"success": "succeeded", "error": "failed"},
        },
        "result": {
            "output_url_path": "{job_id}.outputs.9.images",
            "output_url_template": "http://127.0.0.1:8188/view"
            "?filename={filename}&subfolder={subfolder}&type={type}",
        },
    }

    history = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            body = json.loads(request.content)
            assert body["prompt"]["3"]["inputs"]["seed"] == 7  # int type kept
            return httpx.Response(200, json={"prompt_id": "p1", "number": 1, "node_errors": {}})
        if request.url.path == "/history/p1":
            history["calls"] += 1
            completed = history["calls"] >= 2
            entry: dict = {"status": {"completed": completed}}
            if completed:
                entry["outputs"] = {
                    "9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}
                }
            return httpx.Response(200, json={"p1": entry})
        if request.url.path == "/view":
            assert request.url.params["filename"] == "a.png"
            return httpx.Response(200, content=_PNG + b"COMFY-BYTES")
        return httpx.Response(404, json={})

    adapter = _install(monkeypatch, _model(http=http, model_id="comfy.t2i"), handler)
    job_id = adapter.submit("comfy.t2i", {"prompt": "x", "seed": 7})
    assert remote_job_id(job_id) == "p1"

    running = adapter.poll(job_id)
    assert running.state == "running"  # completed=false -> running
    done = adapter.poll(job_id)
    assert done.state == "succeeded"
    assert done.output_urls == [
        "http://127.0.0.1:8188/view?filename=a.png&subfolder=&type=output"
    ]
    assert adapter.fetch_result(job_id) == _PNG + b"COMFY-BYTES"


# ---------------------------------------------------------------------------
# Hostile proxy environment: client construction must never raise raw
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_no_proxy", ["[::1]", "[fd8b:4f84:7d32:99::1]"])
def test_client_survives_unparseable_proxy_env(monkeypatch, bad_no_proxy):
    """Bracketed IPv6 in NO_PROXY crashes httpx.Client() (InvalidURL) at
    construction. The adapter must fall back to trust_env=False instead of
    letting the raw exception escape (base.py contract)."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", f"localhost,127.0.0.1,{bad_no_proxy}")
    monkeypatch.setattr(registry, "get_model", lambda model_id, db=None: _model())

    adapter = HttpPollingAdapter()  # no transport -> ambient proxy env is parsed
    client = adapter._client_conn()  # must not raise
    assert client is adapter._client_conn()  # cached
    assert client._trust_env is False  # fell back to ignoring ambient proxies


# ---------------------------------------------------------------------------
# Real ComfyUI seed (infra/seeds/models/comfyui.json): exact HTTP sequence
# ---------------------------------------------------------------------------

def _comfyui_seed_model(monkeypatch):
    import pathlib
    seed_path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "infra" / "seeds" / "models" / "comfyui.json"
    )
    model = json.loads(seed_path.read_text())["models"][0]
    assert model["model_id"] == "comfyui.t2i-sd15"
    assert model["provider_id"] == "http_polling"
    monkeypatch.setenv("COMFYUI_URL", "http://127.0.0.1:8188/prompt")
    monkeypatch.setattr(registry, "get_model", lambda model_id, db=None: model)
    return model


def test_comfyui_seed_http_sequence(monkeypatch):
    """The shipped ComfyUI seed must speak the real ComfyUI REST dialect:
    POST /prompt {prompt: <workflow>} -> {"prompt_id"}; GET /history/{id};
    GET /view?filename=...&subfolder=...&type=... ."""
    _comfyui_seed_model(monkeypatch)
    prompt_id = "c0mfy-1"
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, str(req.url)))
        body = json.loads(req.content or b"{}")
        if req.url.path == "/prompt":
            wf = body["prompt"]
            assert wf["1"]["class_type"] == "CheckpointLoaderSimple"
            assert wf["2"]["inputs"]["text"] == "a cat"
            assert wf["4"]["inputs"]["width"] == 512
            assert isinstance(wf["4"]["inputs"]["width"], int)
            assert wf["5"]["inputs"]["seed"] == 7
            assert isinstance(wf["5"]["inputs"]["seed"], int)
            return httpx.Response(200, json={"prompt_id": prompt_id})
        if req.url.path == f"/history/{prompt_id}":
            return httpx.Response(200, json={prompt_id: {
                "status": {"status_str": "success", "completed": True,
                           "messages": []},
                "outputs": {"7": {"images": [
                    {"filename": "studio_00001_.png", "subfolder": "",
                     "type": "output"}]}}}})
        if req.url.path == "/view":
            assert dict(req.url.params) == {
                "filename": "studio_00001_.png", "subfolder": "", "type": "output"}
            return httpx.Response(200, content=_PNG + b"-REAL")
        raise AssertionError(f"unexpected {req.method} {req.url}")

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    pid = adapter.submit("comfyui.t2i-sd15",
                         {"prompt": "a cat", "width": 512, "height": 512, "seed": 7})
    assert remote_job_id(pid) == prompt_id
    done = poll_until_done(adapter, pid)
    assert done.state == "succeeded"
    assert done.output_urls == [
        "http://127.0.0.1:8188/view"
        "?filename=studio_00001_.png&subfolder=&type=output"
    ]
    assert adapter.fetch_result(pid) == _PNG + b"-REAL"


def test_comfyui_seed_queued_then_failed(monkeypatch):
    """Empty /history -> queued; status_str=error -> failed with messages."""
    _comfyui_seed_model(monkeypatch)
    prompt_id = "c0mfy-2"
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": prompt_id})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={})  # still queued/running
        return httpx.Response(200, json={prompt_id: {
            "status": {"status_str": "error", "completed": False,
                       "messages": [["exception", "bad ckpt"]]}}})

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    pid = adapter.submit("comfyui.t2i-sd15",
                         {"prompt": "x", "width": 512, "height": 512, "seed": 1})
    assert adapter.poll(pid).state == "queued"
    failed = adapter.poll(pid)
    assert failed.state == "failed"
    assert "bad ckpt" in (failed.error_message or "")


# ---------------------------------------------------------------------------
# Upload stage (capabilities.http.upload): pre-submit file upload, e.g.
# ComfyUI /upload/image for img2img
# ---------------------------------------------------------------------------

def _upload_config(**over):
    cfg = _http_config()
    cfg["upload"] = {
        "url": "{base_url}/upload/image",
        "method": "POST",
        "headers": {},
        "file_param": "image",
        "file_source": "{init_image}",
        "extra_fields": {"overwrite": "true", "type": "input"},
        "filename_response_path": "name",
        "store_as": "uploaded_image",
        "timeout_sec": 60.0,
    }
    cfg["submit"]["body_template"] = {
        "prompt": "a cat",
        "init_image_name": "{uploaded_image}",
    }
    cfg.update(over)
    return cfg


def _model_with(cfg):
    return {
        "model_id": "test.upload-model",
        "provider_id": "http_polling",
        "capabilities": {"http": cfg},
    }


def test_upload_stage_substitutes_filename(monkeypatch, tmp_path):
    """init_image present -> multipart upload first, stored filename lands in
    the submit body via {uploaded_image}."""
    monkeypatch.setenv("FAKE_API_KEY", "k")
    monkeypatch.setattr(registry, "get_model",
                        lambda model_id, db=None: _model_with(_upload_config()))
    init = tmp_path / "ref.png"
    init.write_bytes(b"\x89PNG-fake")
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/upload/image":
            assert req.headers["content-type"].startswith("multipart/form-data")
            assert b'name="image"' in req.content
            assert b"ref.png" in req.content
            assert b"overwrite" in req.content and b"true" in req.content
            assert b'name="type"' in req.content and b"input" in req.content
            return httpx.Response(200, json={"name": "ref_up_0001.png",
                                             "subfolder": "", "type": "input"})
        if req.url.path == "/v1/generate":
            body = json.loads(req.content)
            assert body["init_image_name"] == "ref_up_0001.png"
            return httpx.Response(200, json={"id": "job-1"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    pid = adapter.submit("test.upload-model",
                         {"prompt": "a cat", "init_image": str(init)})
    assert remote_job_id(pid) == "job-1"
    assert seen == ["/upload/image", "/v1/generate"]  # upload runs BEFORE submit


def test_upload_skipped_without_input(monkeypatch):
    """No init_image in the payload -> the upload step is skipped silently and
    submit proceeds normally."""
    cfg = _upload_config()
    cfg["submit"]["body_template"] = {"prompt": "{prompt}"}  # no {uploaded_image}
    monkeypatch.setenv("FAKE_API_KEY", "k")
    monkeypatch.setattr(registry, "get_model",
                        lambda model_id, db=None: _model_with(cfg))
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        assert req.url.path != "/upload/image"
        return httpx.Response(200, json={"id": "job-2"})

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    assert remote_job_id(adapter.submit("test.upload-model", {"prompt": "a cat"})) == "job-2"
    assert seen == ["/v1/generate"]


def test_upload_failure_is_predictable(monkeypatch, tmp_path):
    """Upload endpoint 500 -> submit raises StudioError(PROVIDER_ERROR), no
    half-submitted job."""
    monkeypatch.setenv("FAKE_API_KEY", "k")
    monkeypatch.setattr(registry, "get_model",
                        lambda model_id, db=None: _model_with(_upload_config()))
    init = tmp_path / "ref.png"
    init.write_bytes(b"\x89PNG-fake")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/upload/image":
            return httpx.Response(500, json={"error": "disk full"})
        raise AssertionError("submit must not run after a failed upload")

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(StudioError) as exc:
        adapter.submit("test.upload-model",
                       {"prompt": "a cat", "init_image": str(init)})
    assert exc.value.code == ErrorCode.PROVIDER_ERROR
    assert "upload" in str(exc.value).lower()


def test_upload_missing_file_is_predictable(monkeypatch):
    """init_image points at nothing -> StudioError, before any HTTP call."""
    monkeypatch.setenv("FAKE_API_KEY", "k")
    monkeypatch.setattr(registry, "get_model",
                        lambda model_id, db=None: _model_with(_upload_config()))
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        raise AssertionError("no HTTP call expected")

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(StudioError) as exc:
        adapter.submit("test.upload-model",
                       {"prompt": "a cat", "init_image": "/no/such/file.png"})
    assert exc.value.code == ErrorCode.PROVIDER_ERROR
    assert "not found" in str(exc.value)
    assert calls == []


def test_base_url_follows_submit_env(monkeypatch):
    """Moving the machine = changing COMFYUI_URL only: status and result
    follow {base_url} derived from the submit URL."""
    _comfyui_seed_model(monkeypatch)
    monkeypatch.setenv("COMFYUI_URL", "http://gpu-box:8188/prompt")
    prompt_id = "c0mfy-3"
    hosts: set[str] = set()

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.add(req.url.host)
        if req.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": prompt_id})
        if req.url.path == f"/history/{prompt_id}":
            return httpx.Response(200, json={prompt_id: {
                "status": {"status_str": "success", "completed": True,
                           "messages": []},
                "outputs": {"7": {"images": [
                    {"filename": "studio_00001_.png", "subfolder": "",
                     "type": "output"}]}}}})
        if req.url.path == "/view":
            return httpx.Response(200, content=_PNG + b"-REAL")
        raise AssertionError(f"unexpected {req.method} {req.url}")

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    pid = adapter.submit("comfyui.t2i-sd15",
                         {"prompt": "a cat", "width": 512, "height": 512, "seed": 7})
    assert poll_until_done(adapter, pid).state == "succeeded"
    assert adapter.fetch_result(pid) == _PNG + b"-REAL"
    assert hosts == {"gpu-box"}  # every stage followed the new machine


def _comfyui_i2i_seed_model(monkeypatch):
    import pathlib
    seed_path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "infra" / "seeds" / "models" / "comfyui.json"
    )
    model = json.loads(seed_path.read_text())["models"][1]
    assert model["model_id"] == "comfyui.i2i-sd15"
    assert model["provider_id"] == "http_polling"
    monkeypatch.setenv("COMFYUI_URL", "http://127.0.0.1:8188/prompt")
    monkeypatch.setattr(registry, "get_model", lambda model_id, db=None: model)
    return model


def test_comfyui_i2i_seed_upload_sequence(monkeypatch, tmp_path):
    """The shipped comfyui.i2i-sd15 seed speaks the real ComfyUI dialect with
    no code changes: POST /upload/image -> POST /prompt (LoadImage gets the
    stored filename) -> GET /history -> GET /view."""
    _comfyui_i2i_seed_model(monkeypatch)
    init = tmp_path / "init.png"
    init.write_bytes(b"\x89PNG-init")
    prompt_id = "c0mfy-i2i"
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/upload/image":
            assert b'name="image"' in req.content
            return httpx.Response(200, json={"name": "init_up.png",
                                             "subfolder": "", "type": "input"})
        if req.url.path == "/prompt":
            wf = json.loads(req.content)["prompt"]
            assert wf["4"]["class_type"] == "LoadImage"
            assert wf["4"]["inputs"]["image"] == "init_up.png"
            assert wf["5"]["class_type"] == "VAEEncode"
            assert wf["6"]["inputs"]["denoise"] == 0.75
            return httpx.Response(200, json={"prompt_id": prompt_id})
        if req.url.path == f"/history/{prompt_id}":
            return httpx.Response(200, json={prompt_id: {
                "status": {"status_str": "success", "completed": True,
                           "messages": []},
                "outputs": {"8": {"images": [
                    {"filename": "studio_00001_.png", "subfolder": "",
                     "type": "output"}]}}}})
        if req.url.path == "/view":
            assert dict(req.url.params) == {
                "filename": "studio_00001_.png", "subfolder": "", "type": "output"}
            return httpx.Response(200, content=_PNG + b"-I2I")
        raise AssertionError(f"unexpected {req.method} {req.url}")

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    pid = adapter.submit("comfyui.i2i-sd15",
                         {"prompt": "a cat", "seed": 7, "init_image": str(init)})
    assert remote_job_id(pid) == prompt_id
    assert seen[0] == "/upload/image"  # upload precedes submit
    assert poll_until_done(adapter, pid).state == "succeeded"
    assert adapter.fetch_result(pid) == _PNG + b"-I2I"


def test_worker_payload_satisfies_real_comfyui_t2i_seed(monkeypatch):
    """Integration contract: the image worker's adapter payload must fill every
    {placeholder} in the shipped comfyui.t2i-sd15 seed. The stub adapter
    ignored extras, so the missing width/height went unnoticed until real use
    (submit would have raised MODEL_UNAVAILABLE for '{width}')."""
    import pathlib
    from types import SimpleNamespace

    from app.tasks import tasks_image

    seed_path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "infra" / "seeds" / "models" / "comfyui.json"
    )
    model = json.loads(seed_path.read_text())["models"][0]
    assert model["model_id"] == "comfyui.t2i-sd15"
    monkeypatch.setenv("COMFYUI_URL", "http://127.0.0.1:8188/prompt")
    monkeypatch.setattr(registry, "get_model", lambda model_id, db=None: model)

    gen = SimpleNamespace(
        prompt_final="a lighthouse at dawn",
        negative_final="",
        seed=7,
        job_type="frame",
        params={},
        input_paths=None,
    )
    payload = tasks_image._adapter_payload(None, gen)

    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/prompt":
            seen["workflow"] = json.loads(req.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "p1"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    adapter.submit("comfyui.t2i-sd15", payload)
    latent = seen["workflow"]["4"]["inputs"]
    assert isinstance(latent["width"], int) and latent["width"] > 0
    assert isinstance(latent["height"], int) and latent["height"] > 0


def test_worker_payload_without_seed_satisfies_real_comfyui_t2i_seed(monkeypatch):
    """Character master/sheet generations don't pin a seed (gen.seed is None),
    but the comfyui.t2i-sd15 seed template requires {seed} for KSampler.
    The worker must draw a fresh random seed instead of failing submit with
    MODEL_UNAVAILABLE (found during the real-ComfyUI E2E: master stayed
    queued after 4 MODEL_UNAVAILABLE retries)."""
    import pathlib
    from types import SimpleNamespace

    from app.tasks import tasks_image

    seed_path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "infra" / "seeds" / "models" / "comfyui.json"
    )
    model = json.loads(seed_path.read_text())["models"][0]
    assert model["model_id"] == "comfyui.t2i-sd15"
    monkeypatch.setenv("COMFYUI_URL", "http://127.0.0.1:8188/prompt")
    monkeypatch.setattr(registry, "get_model", lambda model_id, db=None: model)

    gen = SimpleNamespace(
        prompt_final="portrait of an explorer",
        negative_final="",
        seed=None,  # master/sheet style: no pinned seed
        job_type="text_to_image",
        params={},
        input_paths=None,
    )
    payload = tasks_image._adapter_payload(None, gen)
    assert isinstance(payload.get("seed"), int)
    assert 0 <= payload["seed"] < 2**31

    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/prompt":
            seen["workflow"] = json.loads(req.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "p1"})
        raise AssertionError(f"unexpected {req.method} {req.url}")

    adapter = HttpPollingAdapter(transport=httpx.MockTransport(handler))
    adapter.submit("comfyui.t2i-sd15", payload)  # must not raise MODEL_UNAVAILABLE
    sampler = seen["workflow"]["5"]["inputs"]
    assert isinstance(sampler["seed"], int)
