"""Tests for the CLI-subprocess adapter (models_lib/adapters/cli.py).

The real `codex` CLI is not logged in on this VM/CI, so every subprocess
interaction is faked. A live-CLI test is a documented manual step for HW:
run `codex login` interactively, flip the codex.t2i-gptimage2 seed row to
active, and submit one real generation.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

from app.core.errors import ErrorCode, StudioError
from app.models_lib import registry
from app.models_lib.adapters.cli import CliAdapter


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _cli_config(**over):
    cfg = {
        "tool": "codex",
        "command": "codex",
        "supports": ["text_to_image"],
        "exec_args": ["exec", "--skip-git-repo-check"],
        "prompt_template": "Generate an image from this description: {prompt}",
        "timeout_sec": 600,
        "workdir": "/tmp",
    }
    cfg.update(over)
    return cfg


class FakePopen:
    """Popen stand-in with a scripted poll() sequence."""

    def __init__(self, script, **kwargs):
        self._script = list(script)
        self.pid = 4242
        self.terminated = False
        self.kwargs = kwargs

    def poll(self):
        if not self._script:
            return 0
        return self._script.pop(0)

    def terminate(self):
        self.terminated = True


def _patched(adapter, tmp_path, script):
    """Patch binary lookup, login check, codex home, and Popen."""
    patches = [
        mock.patch("app.models_lib.adapters.cli.shutil.which", return_value="/usr/bin/codex"),
        mock.patch.object(CliAdapter, "_check_login", lambda self, tool, cmd: None),
        mock.patch("app.models_lib.adapters.cli._codex_home", return_value=tmp_path),
        mock.patch("app.models_lib.adapters.cli.subprocess.Popen", lambda *a, **k: FakePopen(script, **k)),
        mock.patch.object(CliAdapter, "_cli_config", lambda self, model_id: _cli_config()),
    ]
    for p in patches:
        p.start()
    return patches


@pytest.fixture()
def adapter(tmp_path):
    ad = CliAdapter()
    patches = _patched(ad, tmp_path, [None, 0])
    yield ad, tmp_path
    for p in patches:
        p.stop()


def _submit(ad, prompt="a lighthouse at dawn"):
    return ad.submit("codex.t2i-gptimage2", {"prompt": prompt})


# ---------------------------------------------------------------------------
# Registration + seed contract
# ---------------------------------------------------------------------------

def test_adapter_registered():
    assert isinstance(registry.get_adapter("cli"), CliAdapter)


def test_seed_row_shape():
    cache = registry._load_seed_cache()
    model = cache["codex.t2i-gptimage2"]
    assert model["provider_id"] == "cli"
    assert model["modality"] == "image"
    assert model["capabilities"]["cli"]["tool"] == "codex"
    assert "text_to_image" in model["capabilities"]["cli"]["supports"]
    # Disabled until the operator runs `codex login` interactively.
    assert model["status"] == "disabled"


# ---------------------------------------------------------------------------
# Happy path (mocked subprocess)
# ---------------------------------------------------------------------------

def test_submit_poll_fetch_success(adapter):
    ad, tmp_path = adapter
    job_id = _submit(ad)
    assert job_id

    first = ad.poll(job_id)
    assert first.state == "running"

    # The CLI writes the per-job output file the instruction names (the
    # shared generated_images folder is never used, B7-r2 H1).
    png = ad._jobs[job_id].output_path
    png.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")

    final = ad.poll(job_id)
    assert final.state == "succeeded"
    assert final.progress_pct == 100.0

    assert ad.fetch_result(job_id) == b"\x89PNG\r\n\x1a\nfakepng"
    # One-shot: the in-memory record is dropped after fetch.
    assert job_id not in ad._jobs


def test_estimate_is_zero_marginal(adapter):
    ad, _ = adapter
    est = ad.estimate("codex.t2i-gptimage2", {"prompt": "x"})
    assert est.amount_usd == Decimal("0.00")
    assert "subscription" in est.unit_breakdown


def test_cancel_unknown_job_is_noop(adapter):
    ad, _ = adapter
    ad.cancel("no-such-job")  # must not raise


# ---------------------------------------------------------------------------
# Explicit rejections (never a silent stub)
# ---------------------------------------------------------------------------

def test_submit_unknown_tool_rejected(adapter):
    ad, _ = adapter
    with mock.patch.object(CliAdapter, "_cli_config", lambda self, m: _cli_config(tool="nope")):
        with pytest.raises(StudioError) as ei:
            _submit(ad)
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE


def test_submit_unsupported_backend_rejected_loudly(adapter):
    """Claude CLI has no image tool: explicit rejection, never a stub image."""
    ad, _ = adapter
    with mock.patch.object(CliAdapter, "_cli_config", lambda self, m: _cli_config(tool="claude")):
        with pytest.raises(StudioError) as ei:
            _submit(ad)
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert "does not support image generation" in ei.value.message


def test_submit_missing_binary_rejected(adapter):
    ad, tmp_path = adapter
    with mock.patch("app.models_lib.adapters.cli.shutil.which", return_value=None):
        with pytest.raises(StudioError) as ei:
            _submit(ad)
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE


def test_submit_empty_prompt_rejected(adapter):
    ad, _ = adapter
    with pytest.raises(StudioError) as ei:
        ad.submit("codex.t2i-gptimage2", {"prompt": "   "})
    assert ei.value.code == ErrorCode.VALIDATION_ERROR


def test_submit_without_cli_config_rejected():
    ad = CliAdapter()
    with mock.patch.object(
        CliAdapter, "_cli_config",
        side_effect=StudioError(ErrorCode.MODEL_UNAVAILABLE, "nope"),
    ):
        with pytest.raises(StudioError) as ei:
            ad.submit("codex.t2i-gptimage2", {"prompt": "x"})
    assert ei.value.code == ErrorCode.MODEL_UNAVAILABLE


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

def test_poll_quota_raises_429_no_retry(tmp_path):
    ad = CliAdapter()
    patches = _patched(ad, tmp_path, [1])
    try:
        job_id = _submit(ad)
        job = ad._jobs[job_id]
        Path(job.stderr_path).write_text("Error: rate limit exceeded for subscription\n")
        with pytest.raises(StudioError) as ei:
            ad.poll(job_id)
    finally:
        for p in patches:
            p.stop()
    assert ei.value.code == ErrorCode.RATE_LIMITED
    assert ei.value.retryable is False


def test_poll_generic_failure_reports_provider_error(tmp_path):
    ad = CliAdapter()
    patches = _patched(ad, tmp_path, [1])
    try:
        job_id = _submit(ad)
        job = ad._jobs[job_id]
        Path(job.stderr_path).write_text("boom\n")
        status = ad.poll(job_id)
    finally:
        for p in patches:
            p.stop()
    assert status.state == "failed"
    assert status.error_code == ErrorCode.PROVIDER_ERROR


def test_poll_no_png_after_exit_is_provider_error(tmp_path):
    ad = CliAdapter()
    patches = _patched(ad, tmp_path, [0])
    try:
        job_id = _submit(ad)
        status = ad.poll(job_id)  # rc=0 immediately, no PNG on disk
    finally:
        for p in patches:
            p.stop()
    assert status.state == "failed"
    assert status.error_code == ErrorCode.PROVIDER_ERROR


def test_poll_timeout_terminates(tmp_path):
    ad = CliAdapter()
    patches = _patched(ad, tmp_path, [None, None, None])
    try:
        with mock.patch.object(CliAdapter, "_cli_config", lambda self, m: _cli_config(timeout_sec=0)):
            job_id = _submit(ad)
        job = ad._jobs[job_id]
        status = ad.poll(job_id)
    finally:
        for p in patches:
            p.stop()
    assert status.state == "failed"
    assert "exceeded timeout" in status.error_message
    assert job.proc.terminated is True


def test_fetch_unknown_job_rejected(adapter):
    ad, _ = adapter
    with pytest.raises(StudioError) as ei:
        ad.fetch_result("no-such-job")
    assert ei.value.code == ErrorCode.PROVIDER_ERROR


def test_poll_unknown_job_rejected(adapter):
    ad, _ = adapter
    with pytest.raises(StudioError) as ei:
        ad.poll("no-such-job")
    assert ei.value.code == ErrorCode.PROVIDER_ERROR
