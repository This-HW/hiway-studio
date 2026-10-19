"""Fake provider for Phase-0 completion tests and keyless demos.

Never delete this adapter: it is the offline proof that the whole pipeline
(queue -> adapter -> file -> SSE) works without API keys.
"""
from __future__ import annotations

import base64
import uuid
from decimal import Decimal

from app.models_lib.base import EstimatedCost, JobStatus, ProviderAdapter

# A valid 1x1 transparent PNG. The stub "renders" this for every request.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)
_TINY_PNG_BYTES = base64.b64decode(_TINY_PNG_B64)

# A valid 2s 640x360 24fps H.264 mp4 (generated from the PNG above with
# `ffmpeg -loop 1 -t 2 -vf scale=640:360 -r 24 -pix_fmt yuv420p`).
# The stub "renders" this for every video request.
_TINY_MP4_B64 = (
    "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAAIZnJlZQAABzdtZGF0AAACVAYF//9Q3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NCByMzEwOCAzMWUxOWY5IC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyMyAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTAgcmVmPTEgZGVibG9jaz0wOjA6MCBhbmFseXNlPTA6MCBtZT1kaWEgc3VibWU9MCBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0wIG1lX3JhbmdlPTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MCA4eDhkY3Q9MCBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lwPTEgY2hyb21hX3FwX29mZnNldD0wIHRocmVhZHM9MyBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTAgd2VpZ2h0cD0wIGtleWludD0yNTAga2V5aW50X21pbj0yNCBzY2VuZWN1dD0wIGludHJhX3JlZnJlc2g9MCByYz1jcmYgbWJ0cmVlPTAgY3JmPTIzLjAgcWNvbXA9MC42MCBxcG1pbj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MACAAAACzmWIhDoRigACBlHAAEJWOAAIY8nJycnJycnJycnJycnJycnJycnJycnJycnJycnJycnJycnJycnJyddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddeAAAAAHQZogKoAczAAAAAdBmkAqgBzMAAAAB0GaYC6AHMwAAAAHQZqALoAczAAAAAdBmqAugBzMAAAAB0GawC6AHMwAAAAHQZrgLoAczAAAAAdBmwAugBzMAAAAB0GbIC6AHMwAAAAHQZtALoAczAAAAAdBm2AugBzMAAAAB0GbgC6AHMwAAAAHQZugLoAczAAAAAdBm8AugBzMAAAAB0Gb4C6AHMwAAAAHQZoALoAczAAAAAdBmiAugBzMAAAAB0GaQC6AHMwAAAAHQZpgLoAczAAAAAdBmoAugBzMAAAAB0GaoC6AHMwAAAAHQZrALoAczAAAAAdBmuAugBzMAAAAB0GbAC6AHMwAAAAHQZsgLoAczAAAAAdBm0AugBzMAAAAB0GbYC6AHMwAAAAHQZuALoAczAAAAAdBm6AugBzMAAAAB0GbwC6AHMwAAAAHQZvgLoAczAAAAAdBmgAugBzMAAAAB0GaIC6AHMwAAAAHQZpALoAczAAAAAdBmmAugBzMAAAAB0GagC6AHMwAAAAHQZqgLoAczAAAAAdBmsAugBzMAAAAB0Ga4C6AHMwAAAAHQZsALoAczAAAAAdBmyAugBzMAAAAB0GbQC6AHMwAAAAHQZtgLoAczAAAAAdBm4AugBzMAAAAB0GboC6AHMwAAAAHQZvALoAczAAAAAdBm+AugBzMAAAD1G1vb3YAAABsbXZoZAAAAAAAAAAAAAAAAAAAA+gAAAfQAAEAAAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAL+dHJhawAAAFx0a2hkAAAAAwAAAAAAAAAAAAAAAQAAAAAAAAfQAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAKAAAABaAAAAAAAJGVkdHMAAAAcZWxzdAAAAAAAAAABAAAH0AAAAAAAAQAAAAACdm1kaWEAAAAgbWRoZAAAAAAAAAAAAAAAAAAAMAAAAGAAVcQAAAAAAC1oZGxyAAAAAAAAAAB2aWRlAAAAAAAAAAAAAAAAVmlkZW9IYW5kbGVyAAAAAiFtaW5mAAAAFHZtaGQAAAABAAAAAAAAAAAAAAAkZGluZgAAABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAHhc3RibAAAAKlzdHNkAAAAAAAAAAEAAACZYXZjMQAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAKAAWgASAAAAEgAAAAAAAAAARVMYXZjNjIuMjguMTAyIGxpYngyNjQAAAAAAAAAAAAAABj//wAAAC9hdmNDAULAHv/hABhnQsAe2gKAv+WEAAADAAQAAAMAwDxYuoABAARozg/IAAAAFGJ0cnQAAAAAAAAcvAAAAAAAAAAYc3R0cwAAAAAAAAABAAAAMAAAAgAAAAAUc3RzcwAAAAAAAAABAAAAAQAAABxzdHNjAAAAAAAAAAEAAAABAAAAMAAAAAEAAADUc3RzegAAAAAAAAAAAAAAMAAABSoAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAAAsAAAALAAAACwAAABRzdGNvAAAAAAAAAAEAAAAwAAAAYnVkdGEAAABabWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAtaWxzdAAAACWpdG9vAAAAHWRhdGEAAAABAAAAAExhdmY2Mi4xMi4xMDI="
)
_TINY_MP4_BYTES = base64.b64decode(_TINY_MP4_B64)


def _is_text_model(model_id: str) -> bool:
    return model_id.startswith("stub.text")


def _is_video_model(model_id: str) -> bool:
    return model_id.startswith(("stub.i2v", "stub.t2v"))


def _is_tts_model(model_id: str) -> bool:
    return model_id.startswith("stub.tts")


def _tts_wav_bytes(text: str) -> bytes:
    """Synthesize a sine-wave WAV whose duration scales with text length.

    P1-5 stub TTS: duration = 0.12s per char, clamped to [1s, 30s]
    ([가정]; the point is a *valid, length-proportional* wav file without
    API keys, not real speech). 16kHz mono 16-bit, 440Hz sine.
    """
    import io
    import math
    import struct
    import wave

    chars = max(1, len(text))
    duration = min(30.0, max(1.0, chars * 0.12))
    sample_rate = 16000
    n = int(duration * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
            for i in range(n)
        )
        w.writeframes(frames)
    return buf.getvalue()


def _canned_text(task: str, prompt: str) -> str:
    """Deterministic fake LLM output. Clearly marked as stub content."""
    short = prompt.strip().replace("\n", " ")[:120]
    if task == "beats":
        import json as _json

        beats = [
            {"act": 1, "title": "시작", "summary": f"이야기가 시작된다: {short}"},
            {"act": 2, "title": "전개", "summary": f"갈등이 깊어진다: {short}"},
            {"act": 3, "title": "결말", "summary": f"이야기가 마무리된다: {short}"},
        ]
        return _json.dumps({"beats": beats}, ensure_ascii=False)
    return f"[stub synopsis] {short} ... 이야기가 펼쳐진다."


class StubAdapter(ProviderAdapter):
    provider_id = "stub"

    def __init__(self) -> None:
        # provider_job_id -> text payload (for stub.text models only).
        self._text_jobs: dict[str, str] = {}
        # provider_job_id -> model_id (to pick the fake media type).
        self._model_of: dict[str, str] = {}
        # provider_job_id -> TTS text (for stub.tts models; emotion_weights
        # are accepted in the payload and ignored, per F-13).
        self._tts_jobs: dict[str, str] = {}

    def submit(self, model_id: str, payload: dict) -> str:
        provider_job_id = f"stub-{uuid.uuid4().hex[:12]}"
        self._model_of[provider_job_id] = model_id
        if _is_text_model(model_id):
            task = str(payload.get("task", "synopsis"))
            prompt = str(payload.get("prompt", ""))
            self._text_jobs[provider_job_id] = _canned_text(task, prompt)
        if _is_tts_model(model_id):
            self._tts_jobs[provider_job_id] = str(payload.get("text", ""))
        return provider_job_id

    def _ext_for(self, provider_job_id: str) -> str:
        model_id = self._model_of.get(provider_job_id, "")
        if _is_tts_model(model_id):
            return "wav"
        return "mp4" if _is_video_model(model_id) else "png"

    def poll(self, provider_job_id: str) -> JobStatus:
        # The stub completes immediately; a real adapter would poll the provider.
        ext = self._ext_for(provider_job_id)
        return JobStatus(
            state="succeeded",
            progress_pct=100.0,
            stage="done",
            output_urls=[f"stub://{provider_job_id}/0.{ext}"],
        )

    def cancel(self, provider_job_id: str) -> None:
        return None

    def estimate(self, model_id: str, payload: dict) -> EstimatedCost:
        return EstimatedCost(amount_usd=Decimal("0"), unit_breakdown="stub: free")

    def fetch_result(self, provider_job_id: str) -> bytes:
        text = self._text_jobs.pop(provider_job_id, None)
        if text is not None:
            return text.encode("utf-8")
        tts_text = self._tts_jobs.pop(provider_job_id, None)
        if tts_text is not None:
            return _tts_wav_bytes(tts_text)
        model_id = self._model_of.pop(provider_job_id, "")
        if _is_video_model(model_id):
            return _TINY_MP4_BYTES
        return _TINY_PNG_BYTES
