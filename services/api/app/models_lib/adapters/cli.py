"""CLI-subprocess adapter for subscription-based providers (design report §3.4).

kind = "cli". First backend: Codex CLI's built-in ``image_gen`` tool
(gpt-image-2, works on subscription login without an API key).

Contract (design report §3.4):
- ``submit()`` spawns ``codex exec`` non-interactively (Popen, background)
  and returns a provider_job_id immediately. One call = one agent-loop turn,
  so it is slow (minutes, not seconds).
- ``poll()`` checks the process; on clean exit it scans
  ``$CODEX_HOME/generated_images/`` for PNGs newer than the pre-submit
  snapshot and reports success. No new PNG -> PROVIDER_ERROR.
- ``fetch_result()`` returns the PNG bytes. The task layer saves them to
  storage (relative paths in DB); the adapter never writes project files.
- Error mapping: CLI binary missing / not logged in -> MODEL_UNAVAILABLE
  (503). Subscription quota / rate limit -> RATE_LIMITED (429) with
  ``retryable=False`` (no auto-retry: quota is not fixed by retrying).
  Backend lacking the capability (e.g. a CLI with no image tool) is rejected
  explicitly at submit time -- never a silent stub.
- Low-frequency / human-in-the-loop use only. Batch or high-frequency
  generation should use ``local`` / ``api`` providers.

Seed config schema (model seed JSON, ``capabilities.cli``)::

    "capabilities": {
      "cli": {
        "tool": "codex",                 # CLI backend: "codex" | "claude"
        "command": "codex",              # binary; override via CODEX_CLI_BIN
        "supports": ["text_to_image"],   # tasks this backend can do
        "exec_args": ["exec", "--skip-git-repo-check",
                      "--enable", "image_generation",
                      "--approve-for-me"],
        "prompt_template": "Generate an image from this description: {prompt}",
        "timeout_sec": 600,              # agent-loop wall clock (default 600)
        "workdir": "/tmp"                # agent working root (-C)
      }
    }
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from app.core.errors import ErrorCode, StudioError
from app.models_lib.base import EstimatedCost, JobStatus, ProviderAdapter

log = logging.getLogger(__name__)

_MAX_REMEMBERED_JOBS = 10_000

# Backend capability table. "supports" lists the generation tasks the CLI
# backend is known to perform. A backend with an empty tuple rejects every
# request explicitly at submit time (never a silent stub).
_CLI_BACKENDS: dict[str, dict] = {
    "codex": {
        "supports": ("text_to_image",),
        "default_command": "codex",
        "env_bin": "CODEX_CLI_BIN",
        "login_probe": ["login", "status"],
        "not_logged_in_markers": ("not logged in",),
    },
    "claude": {
        # Claude Code CLI has no image-generation tool (it reads images but
        # does not generate them). Kept as an explicit entry so the rejection
        # is loud and documented instead of an "unknown tool" error.
        # DO NOT add a claude seed entry or expose it in provider selection
        # until Claude gains image generation (HW 2026-10-06).
        "supports": (),
        "default_command": "claude",
        "env_bin": "CLAUDE_CLI_BIN",
        "login_probe": [],
        "not_logged_in_markers": (),
    },
}

# stderr markers -> RATE_LIMITED (429, retryable=False: quota is not fixed by retrying)
_QUOTA_MARKERS = (
    "rate limit",
    "rate_limit",
    "quota",
    "usage limit",
    "too many requests",
    "429",
)


@dataclass
class _CliJob:
    model_id: str
    tool: str
    command: str
    proc: subprocess.Popen
    out_dir: Path
    before: set[Path] = field(default_factory=set)
    started_at: float = field(default_factory=time.time)
    timeout_sec: float = 600.0
    stdout_path: Optional[Path] = None
    stderr_path: Optional[Path] = None
    png_path: Optional[Path] = None


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


class CliAdapter(ProviderAdapter):
    provider_id = "cli"

    def __init__(self) -> None:
        self._jobs: dict[str, _CliJob] = {}

    # -- config ---------------------------------------------------------
    def _cli_config(self, model_id: str) -> dict:
        # Deferred import: registry imports this module at load time.
        from app.models_lib import registry  # noqa: PLC0415 (deferred: same module)

        model = registry.get_model(model_id)  # raises NOT_FOUND when unknown
        cli = (model.get("capabilities") or {}).get("cli")
        if not isinstance(cli, dict) or not cli:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Model {model_id} has no capabilities.cli config; the cli "
                "adapter cannot drive it. Add the block to the model seed "
                "JSON (see this module's docstring).",
            )
        return cli

    def _remember(self, provider_job_id: str, job: _CliJob) -> None:
        self._jobs[provider_job_id] = job
        while len(self._jobs) > _MAX_REMEMBERED_JOBS:
            self._jobs.pop(next(iter(self._jobs)))

    def _lookup(self, provider_job_id: str) -> _CliJob:
        try:
            return self._jobs[provider_job_id]
        except KeyError:
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"Unknown cli provider job: {provider_job_id}.",
            )

    # -- backend helpers ------------------------------------------------
    def _resolve_command(self, tool: str, cfg: dict) -> str:
        backend = _CLI_BACKENDS[tool]
        cmd = cfg.get("command") or os.environ.get(backend["env_bin"]) or backend["default_command"]
        if shutil.which(cmd) is None:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"CLI tool '{cmd}' not found on PATH. Install the {tool} CLI "
                f"(or set {backend['env_bin']}) to use subscription-based generation.",
            )
        return cmd

    def _check_login(self, tool: str, command: str) -> None:
        """Fail fast with a clear message when the CLI is not logged in."""
        probe = _CLI_BACKENDS[tool]["login_probe"]
        if not probe:
            return
        try:
            proc = subprocess.run(
                [command, *probe],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return  # let the real exec surface the problem
        out = (proc.stdout + proc.stderr).lower()
        if any(m in out for m in _CLI_BACKENDS[tool]["not_logged_in_markers"]):
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"CLI tool '{command}' is not logged in. Run `{command} login` "
                "interactively once; the studio cannot complete an OAuth flow.",
            )

    # -- ProviderAdapter ------------------------------------------------
    def submit(self, model_id: str, payload: dict) -> str:
        cfg = self._cli_config(model_id)
        tool = str(cfg.get("tool", "codex"))
        backend = _CLI_BACKENDS.get(tool)
        if backend is None:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Unknown cli backend '{tool}'. Known: {sorted(_CLI_BACKENDS)}.",
            )
        if "text_to_image" not in backend["supports"]:
            # Explicit rejection: the backend exists but cannot generate
            # images (e.g. claude). Never fall back to a silent stub.
            # NOTE: the 16-code canon (§8.4) has no 422 code for
            # "provider lacks capability"; MODEL_UNAVAILABLE (503) is the
            # truthful mapping. Flagged for review.
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"CLI backend '{tool}' does not support image generation. "
                "Use a codex.* model or a local/api provider instead.",
            )
        command = self._resolve_command(tool, cfg)
        self._check_login(tool, command)

        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Model {model_id}: submit payload needs a 'prompt'.",
            )
        template = str(cfg.get("prompt_template") or "Generate an image from this description: {prompt}")
        instruction = template.replace("{prompt}", prompt).replace(
            "{negative}", str(payload.get("negative") or "")
        )

        out_dir = _codex_home() / "generated_images"
        out_dir.mkdir(parents=True, exist_ok=True)
        before = set(out_dir.rglob("*.png"))

        exec_args = cfg.get("exec_args") or [
            "exec",
            "--skip-git-repo-check",
            "--enable",
            "image_generation",
            "--approve-for-me",
        ]
        workdir = str(cfg.get("workdir") or tempfile.gettempdir())
        argv = [command, *exec_args, "-C", workdir, instruction]

        stdout_path = Path(tempfile.mkstemp(prefix="cli-adapter-out-")[1])
        stderr_path = Path(tempfile.mkstemp(prefix="cli-adapter-err-")[1])
        try:
            proc = subprocess.Popen(
                argv,
                stdout=stdout_path.open("wb"),
                stderr=stderr_path.open("wb"),
                stdin=subprocess.DEVNULL,
                cwd=workdir,
            )
        except OSError as exc:
            raise StudioError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"Failed to launch CLI tool '{command}': {exc}.",
            )

        provider_job_id = uuid.uuid4().hex
        self._remember(
            provider_job_id,
            _CliJob(
                model_id=model_id,
                tool=tool,
                command=command,
                proc=proc,
                out_dir=out_dir,
                before=before,
                timeout_sec=float(cfg.get("timeout_sec", 600.0)),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            ),
        )
        log.info("cli submit model=%s tool=%s job=%s pid=%s", model_id, tool, provider_job_id, proc.pid)
        return provider_job_id

    def poll(self, provider_job_id: str) -> JobStatus:
        job = self._lookup(provider_job_id)
        rc = job.proc.poll()
        elapsed = time.time() - job.started_at
        if rc is None:
            if elapsed > job.timeout_sec:
                try:
                    job.proc.terminate()
                except OSError:
                    pass
                return JobStatus(
                    state="failed",
                    error_code=ErrorCode.PROVIDER_ERROR,
                    error_message=(
                        f"CLI tool '{job.command}' exceeded timeout "
                        f"({job.timeout_sec:.0f}s) and was terminated."
                    ),
                )
            progress = min(95.0, (elapsed / max(job.timeout_sec, 1.0)) * 100.0)
            return JobStatus(state="running", progress_pct=progress, stage="cli-agent-running")
        return self._finish(job, provider_job_id, rc)

    def _finish(self, job: _CliJob, provider_job_id: str, rc: int) -> JobStatus:
        stderr_tail = ""
        if job.stderr_path and job.stderr_path.exists():
            try:
                stderr_tail = job.stderr_path.read_text(errors="replace")[-2000:]
            except OSError:
                stderr_tail = ""
        if rc != 0:
            lowered = stderr_tail.lower()
            if any(m in lowered for m in _QUOTA_MARKERS):
                # Quota is not fixed by retrying: raise directly (the base
                # contract allows raising StudioError from poll) with
                # retryable=False so the task layer does not requeue.
                raise StudioError(
                    ErrorCode.RATE_LIMITED,
                    f"CLI subscription quota exceeded for '{job.command}': {stderr_tail.strip()[:500]}",
                    retryable=False,
                )
            return JobStatus(
                state="failed",
                error_code=ErrorCode.PROVIDER_ERROR,
                error_message=f"CLI tool '{job.command}' exited with code {rc}: {stderr_tail.strip()[:500]}",
            )
        new_pngs = [
            p for p in set(job.out_dir.rglob("*.png")) - job.before if p.is_file()
        ]
        if not new_pngs:
            return JobStatus(
                state="failed",
                error_code=ErrorCode.PROVIDER_ERROR,
                error_message=(
                    f"CLI tool '{job.command}' finished but produced no PNG under "
                    f"{job.out_dir}. Check {job.stderr_path} for details."
                ),
            )
        newest = max(new_pngs, key=lambda p: p.stat().st_mtime)
        job.png_path = newest
        return JobStatus(
            state="succeeded",
            progress_pct=100.0,
            stage="done",
            output_urls=[newest.name],
        )

    def cancel(self, provider_job_id: str) -> None:
        job = self._jobs.get(provider_job_id)
        if job is None:
            return
        try:
            job.proc.terminate()
        except OSError:
            pass

    def estimate(self, model_id: str, payload: dict) -> EstimatedCost:
        # Subscription-based: marginal $0 per call (the subscription itself is
        # a sunk cost outside the studio's budget model).
        self._cli_config(model_id)  # validates the model exists / has cli config
        return EstimatedCost(
            amount_usd=Decimal("0.00"),
            unit_breakdown="per_image via subscription (marginal $0)",
        )

    def fetch_result(self, provider_job_id: str) -> bytes:
        job = self._lookup(provider_job_id)
        if job.png_path is None or not job.png_path.is_file():
            raise StudioError(
                ErrorCode.PROVIDER_ERROR,
                f"CLI job {provider_job_id} has no retrievable PNG.",
            )
        data = job.png_path.read_bytes()
        # One-shot: drop the in-memory record after a successful fetch.
        self._jobs.pop(provider_job_id, None)
        return data
