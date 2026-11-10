"""CLI-subprocess adapter for subscription-based providers (design report §3.4).

kind = "cli". First backend: Codex CLI's built-in ``image_gen`` tool
(gpt-image-2, works on subscription login without an API key).

Contract (design report §3.4):
- ``submit()`` spawns ``codex exec`` non-interactively (Popen, background)
  and returns a provider_job_id immediately. One call = one agent-loop turn,
  so it is slow (minutes, not seconds).
- Output attribution (ADR-0014 §4, B7-r2 H1): every job runs in its OWN
  directory (``-C <job dir>``, created by ``mkdtemp``) and the instruction
  names an explicit output file inside it (``{output_path}``). ``poll()`` on
  clean exit takes the explicit file, else a PNG the CLI wrote inside that
  per-job directory (only this job's process works there). The CLI's shared
  image folder (``$CODEX_HOME/generated_images/``) is NEVER used: nothing
  there proves which job produced a file, so a CLI that ignored the per-job
  path fails the job (PROVIDER_ERROR) instead of guessing.
- Provider input contract: ``prompt`` + ``negative_prompt`` (``{negative}``
  or ``{negative_prompt}`` in the template, else appended as "Avoid: ...").
  Editing inputs (``init_image`` / ``mask``) are rejected (VALIDATION_ERROR)
  before anything is spawned; reference images are not consumed (the
  enqueue snapshot records that degrade).
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
                                         # optional {negative} / {output_path}
        "timeout_sec": 600,              # agent-loop wall clock (default 600)
        "workdir": "/tmp"                # parent of the per-job directories
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
from app.models_lib.redaction import redact

log = logging.getLogger(__name__)

_MAX_REMEMBERED_JOBS = 10_000
# stderr bytes read for an error message (the tail; see _stderr_tail).
_STDERR_READ_BYTES = 64 * 1024
_STDERR_QUOTE_CHARS = 500

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
    out_dir: Path  # the job's OWN directory (the only attributable outputs)
    output_path: Optional[Path] = None  # explicit file the instruction names
    started_at: float = field(default_factory=time.time)
    timeout_sec: float = 600.0
    stdout_path: Optional[Path] = None
    stderr_path: Optional[Path] = None
    png_path: Optional[Path] = None


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


_OUTPUT_FILENAME = "output.png"


def _stderr_tail(path: Optional[Path]) -> str:
    """The end of the CLI's stderr for an error message, redacted BEFORE it
    is cut (B7-r3 M1).

    Cutting first could drop a credential's prefix ("Authorization: ...")
    and leave the value unrecognizable to the redactor. When the file is
    longer than what is read, the first (partial) line of the read window is
    dropped for the same reason — a fragment is never quoted.
    """
    if path is None:
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _STDERR_READ_BYTES:
                fh.seek(size - _STDERR_READ_BYTES)
            raw = fh.read(_STDERR_READ_BYTES)
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace")
    if size > _STDERR_READ_BYTES:
        newline = text.find("\n")
        text = text[newline + 1 :] if newline >= 0 else ""
    text = redact(text.strip())  # redact the whole window first ...
    return text[-_STDERR_QUOTE_CHARS:]  # ... then cut


def _instruction(template: str, prompt: str, negative: str, output_path: Path) -> str:
    """Render the CLI instruction from the provider input contract."""
    text = template.replace("{prompt}", prompt)
    if "{negative}" in text or "{negative_prompt}" in text:
        text = text.replace("{negative}", negative).replace("{negative_prompt}", negative)
    elif negative:
        text += f"\nAvoid: {negative}"
    if "{output_path}" in text:
        text = text.replace("{output_path}", str(output_path))
    else:
        text += f"\nSave the final image as a PNG file at exactly this path: {output_path}"
    return text


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
        editing = [k for k in ("init_image", "mask") if payload.get(k)]
        if editing:
            raise StudioError(
                ErrorCode.VALIDATION_ERROR,
                f"Model {model_id}: the cli backend cannot take {', '.join(editing)}.",
                details={"reason": "input_unsupported", "inputs": editing},
            )
        # Contract key is negative_prompt ("negative" kept as a legacy alias).
        negative = str(payload.get("negative_prompt") or payload.get("negative") or "").strip()
        template = str(cfg.get("prompt_template") or "Generate an image from this description: {prompt}")

        workdir = str(cfg.get("workdir") or tempfile.gettempdir())
        Path(workdir).mkdir(parents=True, exist_ok=True)
        job_dir = Path(tempfile.mkdtemp(prefix="cli-job-", dir=workdir))
        output_path = job_dir / _OUTPUT_FILENAME
        instruction = _instruction(template, prompt, negative, output_path)

        exec_args = cfg.get("exec_args") or [
            "exec",
            "--skip-git-repo-check",
            "--enable",
            "image_generation",
            "--approve-for-me",
        ]
        argv = [command, *exec_args, "-C", str(job_dir), instruction]

        # Logs live in the job directory (cleaned with it after a successful
        # fetch). The parent's handles are closed right after the spawn; the
        # child keeps its own copies (B7-r3 M4: no descriptor per submit is
        # left open in a long-lived worker).
        stdout_path = job_dir / "cli.stdout.log"
        stderr_path = job_dir / "cli.stderr.log"
        try:
            with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
                proc = subprocess.Popen(
                    argv,
                    stdout=out,
                    stderr=err,
                    stdin=subprocess.DEVNULL,
                    cwd=str(job_dir),
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
                out_dir=job_dir,
                output_path=output_path,
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
        # Provider text lands in error_message: redacted, then capped.
        stderr_tail = _stderr_tail(job.stderr_path)
        if rc != 0:
            lowered = stderr_tail.lower()
            if any(m in lowered for m in _QUOTA_MARKERS):
                # Quota is not fixed by retrying: raise directly (the base
                # contract allows raising StudioError from poll) with
                # retryable=False so the task layer does not requeue.
                raise StudioError(
                    ErrorCode.RATE_LIMITED,
                    f"CLI subscription quota exceeded for '{job.command}': {stderr_tail}",
                    retryable=False,
                )
            return JobStatus(
                state="failed",
                error_code=ErrorCode.PROVIDER_ERROR,
                error_message=f"CLI tool '{job.command}' exited with code {rc}: {stderr_tail}",
            )
        picked = self._own_output(job)
        if picked is None:
            # No shared-folder guessing (B7-r2 H1): a PNG elsewhere cannot be
            # attributed to this job.
            return JobStatus(
                state="failed",
                error_code=ErrorCode.PROVIDER_ERROR,
                error_message=(
                    f"CLI tool '{job.command}' finished but did not write the "
                    f"per-job output file {job.output_path} (no PNG under "
                    f"{job.out_dir}). An image it may have left in the shared "
                    f"folder {_codex_home() / 'generated_images'} cannot be "
                    "attributed to this job and is not used. "
                    f"Check {job.stderr_path} for details."
                ),
            )
        newest = picked
        job.png_path = newest
        return JobStatus(
            state="succeeded",
            progress_pct=100.0,
            stage="done",
            output_urls=[newest.name],
        )

    @staticmethod
    def _own_output(job: _CliJob) -> Optional[Path]:
        """The explicit output file, else any PNG in the job's own directory."""
        if job.output_path is not None and job.output_path.is_file():
            return job.output_path
        own = [p for p in job.out_dir.rglob("*.png") if p.is_file()]
        return max(own, key=lambda p: p.stat().st_mtime) if own else None

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
        # One-shot: drop the in-memory record (and the job directory) after
        # a successful fetch.
        self._jobs.pop(provider_job_id, None)
        shutil.rmtree(job.out_dir, ignore_errors=True)
        return data
