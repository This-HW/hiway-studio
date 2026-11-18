"""Unit C: generic OpenAI-compatible vision client + advisory QC wiring.

- Client speaks POST {base_url}/chat/completions (httpx, no vendor SDK).
- Images travel as base64 data URLs.
- Malformed / HTTP / transport failures -> VisionError (no raw tracebacks).
- QC checks stay advisory: warn at most, NEVER fail — even when the vision
  endpoint is down or returns garbage. Client problems degrade to warn.
"""
from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.services import qc_content, vision_client
from app.services.qc_orchestrator import ADVISORY_CHECKS, QCContext, _overall


@pytest.fixture()
def vision_env(monkeypatch):
    monkeypatch.setenv("QC_VISION_BASE_URL", "http://vision.local:11434/v1")
    monkeypatch.setenv("QC_VISION_MODEL", "qwen2.5vl:7b")
    monkeypatch.delenv("QC_VISION_API_KEY", raising=False)
    return monkeypatch


@pytest.fixture()
def no_vision_env(monkeypatch):
    monkeypatch.delenv("QC_VISION_BASE_URL", raising=False)
    monkeypatch.delenv("QC_VISION_API_KEY", raising=False)
    monkeypatch.delenv("QC_VISION_MODEL", raising=False)
    return monkeypatch


@pytest.fixture()
def png_image(tmp_path):
    p = tmp_path / "frame.png"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(p)
    return p


def _ok_payload(score=85, notes="looks good", content=None):
    return {
        "choices": [
            {"message": {"content": content
                         if content is not None
                         else json.dumps({"score": score, "notes": notes})}}
        ]
    }


def _mock(payload=None, status=200, raw=None):
    """MockTransport + captured requests."""
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler), seen


def _image_parts(req: httpx.Request):
    body = json.loads(req.content)
    assert body["model"] == "qwen2.5vl:7b"
    assert req.url.path == "/v1/chat/completions"
    user = body["messages"][1]
    return [p for p in user["content"] if p["type"] == "image_url"]


# --- availability ----------------------------------------------------------

def test_available_needs_base_url_and_model(vision_env, no_vision_env):
    assert vision_client.available() is False
    no_vision_env.setenv("QC_VISION_BASE_URL", "http://x/v1")
    assert vision_client.available() is False
    no_vision_env.delenv("QC_VISION_BASE_URL")
    no_vision_env.setenv("QC_VISION_MODEL", "m")
    assert vision_client.available() is False
    no_vision_env.setenv("QC_VISION_BASE_URL", "http://x/v1")
    assert vision_client.available() is True


# --- score_shot ------------------------------------------------------------

def test_score_shot_success(vision_env, png_image):
    transport, seen = _mock(_ok_payload(85, "faithful"))
    vs = vision_client.score_shot(
        [png_image], system_prompt="sys", user_text="rate",
        transport=transport,
    )
    assert vs.score == 85 and vs.notes == "faithful"
    assert len(seen) == 1


def test_score_shot_clamps(vision_env, png_image):
    t, _ = _mock(_ok_payload(150))
    assert vision_client.score_shot([png_image], system_prompt="s",
                                    user_text="u", transport=t).score == 100
    t, _ = _mock(_ok_payload(-20))
    assert vision_client.score_shot([png_image], system_prompt="s",
                                    user_text="u", transport=t).score == 0


def test_score_shot_tolerates_prose_around_json(vision_env, png_image):
    t, _ = _mock(_ok_payload(
        content='Here is my judgement: {"score": 70, "notes": "ok"} done'))
    vs = vision_client.score_shot([png_image], system_prompt="s",
                                  user_text="u", transport=t)
    assert (vs.score, vs.notes) == (70, "ok")


def test_score_shot_malformed_raises(vision_env, png_image):
    t, _ = _mock(_ok_payload(content="absolutely not json"))
    with pytest.raises(vision_client.VisionError):
        vision_client.score_shot([png_image], system_prompt="s",
                                 user_text="u", transport=t)


def test_score_shot_missing_score_raises(vision_env, png_image):
    t, _ = _mock(_ok_payload(content='{"notes": "no score here"}'))
    with pytest.raises(vision_client.VisionError):
        vision_client.score_shot([png_image], system_prompt="s",
                                 user_text="u", transport=t)


def test_score_shot_http_error_raises(vision_env, png_image):
    t, _ = _mock(None, status=500, raw=b"internal error")
    with pytest.raises(vision_client.VisionError, match="500"):
        vision_client.score_shot([png_image], system_prompt="s",
                                 user_text="u", transport=t)


def test_score_shot_malformed_envelope_raises(vision_env, png_image):
    t, _ = _mock({"unexpected": "shape"})
    with pytest.raises(vision_client.VisionError, match="Malformed"):
        vision_client.score_shot([png_image], system_prompt="s",
                                 user_text="u", transport=t)


def test_score_shot_non_json_body_raises(vision_env, png_image):
    t, _ = _mock(None, status=200, raw=b"<html>nope</html>")
    with pytest.raises(vision_client.VisionError):
        vision_client.score_shot([png_image], system_prompt="s",
                                 user_text="u", transport=t)


def test_score_shot_unconfigured_raises(no_vision_env, png_image):
    with pytest.raises(vision_client.VisionError, match="not configured"):
        vision_client.score_shot([png_image], system_prompt="s", user_text="u")


def test_score_shot_no_images_raises(vision_env):
    with pytest.raises(vision_client.VisionError, match="at least one image"):
        vision_client.score_shot([], system_prompt="s", user_text="u")


def test_images_sent_as_data_urls(vision_env, png_image):
    transport, seen = _mock(_ok_payload())
    vision_client.score_shot([png_image], system_prompt="s", user_text="u",
                             transport=transport)
    parts = _image_parts(seen[0])
    assert len(parts) == 1
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "authorization" not in {k.lower() for k in seen[0].headers}


def test_api_key_sent_as_bearer(vision_env, png_image):
    vision_env.setenv("QC_VISION_API_KEY", "sekret")
    transport, seen = _mock(_ok_payload())
    vision_client.score_shot([png_image], system_prompt="s", user_text="u",
                             transport=transport)
    assert seen[0].headers["authorization"] == "Bearer sekret"


def test_unsupported_image_type_raises(vision_env, tmp_path):
    p = tmp_path / "frame.tiff"
    p.write_bytes(b"nope")
    with pytest.raises(vision_client.VisionError, match="Unsupported image type"):
        vision_client.score_shot([p], system_prompt="s", user_text="u")


# --- QC wiring: advisory invariant -----------------------------------------

def _shot(sid, action="a cat sits", dialogue="meow", media=None):
    return types.SimpleNamespace(id=sid, action=action, dialogue=dialogue)


def _ctx(shots, media_by_id):
    return QCContext(timeline=None, preset=None, shots=shots,
                     shot_media=media_by_id)


def test_unconfigured_checks_are_stubs(no_vision_env, png_image):
    ctx = _ctx([_shot("s1")], {"s1": png_image})
    for fn in (qc_content.check_story_fidelity,
               qc_content.check_performance_quality):
        r = fn(ctx)
        assert r.status == "warn" and r.stub is True
        assert "vision 모델 미연결" in r.details


def _mock_score(monkeypatch, score, notes="n"):
    t, _ = _mock(_ok_payload(score, notes))
    monkeypatch.setattr(vision_client, "_client",
                        lambda transport=None: httpx.Client(transport=t))


def test_story_fidelity_pass_on_high_score(vision_env, monkeypatch, png_image):
    _mock_score(monkeypatch, 90, "faithful")
    r = qc_content.check_story_fidelity(_ctx([_shot("s1")], {"s1": png_image}))
    assert r.status == "pass", r.details
    assert r.measured == 0.9 and r.stub is False


def test_story_fidelity_warn_on_low_score(vision_env, monkeypatch, png_image):
    _mock_score(monkeypatch, 30, "off-brief")
    r = qc_content.check_story_fidelity(_ctx([_shot("s1")], {"s1": png_image}))
    assert r.status == "warn", r.details
    assert r.status != "fail"
    assert r.threshold == 0.70


def test_performance_quality_warn_on_low_score(vision_env, monkeypatch, png_image):
    _mock_score(monkeypatch, 20, "uncanny")
    r = qc_content.check_performance_quality(
        _ctx([_shot("s1", action="", dialogue="")], {"s1": png_image}))
    assert r.status == "warn" and r.status != "fail"


def test_client_error_degrades_to_warn(vision_env, monkeypatch, png_image):
    t, _ = _mock(None, status=500, raw=b"boom")
    monkeypatch.setattr(vision_client, "_client",
                        lambda transport=None: httpx.Client(transport=t))
    for fn in (qc_content.check_story_fidelity,
               qc_content.check_performance_quality):
        r = fn(_ctx([_shot("s1")], {"s1": png_image}))
        assert r.status == "warn", r.details
        assert r.status != "fail" and r.stub is False


def test_malformed_reply_degrades_to_warn(vision_env, monkeypatch, png_image):
    t, _ = _mock(_ok_payload(content="garbage [[["))
    monkeypatch.setattr(vision_client, "_client",
                        lambda transport=None: httpx.Client(transport=t))
    r = qc_content.check_story_fidelity(_ctx([_shot("s1")], {"s1": png_image}))
    assert r.status == "warn" and r.status != "fail"


def test_no_media_warns_without_fail(vision_env, monkeypatch):
    _mock_score(monkeypatch, 95)
    r = qc_content.check_story_fidelity(_ctx([_shot("s1")], {}))
    assert r.status == "warn" and r.status != "fail"


def test_no_story_context_skips_story_check(vision_env, monkeypatch, png_image):
    _mock_score(monkeypatch, 95)
    r = qc_content.check_story_fidelity(
        _ctx([_shot("s1", action="", dialogue="")], {"s1": png_image}))
    assert r.status == "warn" and r.status != "fail"


def test_advisory_results_never_fail_overall(vision_env, monkeypatch, png_image):
    # Worst case: every advisory path warns; overall must stay warn, never fail.
    _mock_score(monkeypatch, 5, "terrible")
    results = [
        qc_content.check_story_fidelity(_ctx([_shot("s1")], {"s1": png_image})),
        qc_content.check_performance_quality(
            _ctx([_shot("s1")], {"s1": png_image})),
    ]
    assert {r.status for r in results} == {"warn"}
    assert all(r.code in ADVISORY_CHECKS for r in results)
    assert _overall(results) == "warn"


@pytest.mark.needs_ffmpeg
def test_video_clip_frames_reach_model(vision_env, monkeypatch, tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=160x120:rate=10:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
        check=True,
    )
    transport, seen = _mock(_ok_payload(80, "natural"))
    # Plumb the mock transport through score_shot's test seam.
    orig = vision_client.score_shot

    def spy(paths, **kw):
        return orig(paths, transport=transport, **kw)

    monkeypatch.setattr(vision_client, "score_shot", spy)
    r = qc_content.check_performance_quality(
        _ctx([_shot("s1", action="", dialogue="")], {"s1": clip}))
    assert r.status == "pass", r.details
    parts = _image_parts(seen[0])
    # 3 spread frames (0.25/0.5/0.75), downscaled JPEG data URLs
    assert len(parts) == 3
    assert all(p["image_url"]["url"].startswith("data:image/jpeg;base64,")
               for p in parts)
