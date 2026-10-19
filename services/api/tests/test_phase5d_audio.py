"""Phase-5D completion proof: TTS dialogue, emotion weights, voice clone,
dubbing lip-sync, narration B-roll suggest (stub adapters, no API keys).

  1. POST /shots/{id}/dialogue {character_id, text, emotion_weights} -> 202
     {job_id}; eager Celery completes inline; GET /audio/{job_id} shows
     status 'done', a real .wav under projects/{id}/audio/, and the stored
     emotion_weights.
  2. emotion_weights outside 0..1 -> 422; same Idempotency-Key -> same job.
  3. POST /characters/{id}/voice/clone {sample_asset_id} -> {voice_id}
     "voice_<cid-short>"; persisted on the character; later dialogue jobs
     carry that voice_id.
  4. services.lipsync: ±10% band adjusts via atempo (probe proves the new
     duration); beyond the band warns and leaves the audio untouched.
     The TTS worker applies the fit when target_duration_sec is set.
  5. POST /projects/{id}/broll-suggest {timeline_id} matches narration
     keywords against background/location assets -> scored suggestions.

Celery runs eager (task_always_eager), so 202 responses complete inline.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import base64 as _base64

_TINY_PNG = _base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwAD"
    "hgGAWjR9awAAAABJRU5ErkJggg=="
)

import app.db.session as sess_mod
from app.core.config import get_settings
from app.core.sse_bridge import reset_bridge
from app.db.base import Base
from app.db.session import reset_engine, session_scope
from app.tasks import tasks_audio  # noqa: F401 (registers the q.audio dispatcher)
from app.tasks.celery_app import celery_app

import app.main  # noqa: E402


@pytest.fixture()
def api_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))
    monkeypatch.setenv("SSE_BACKEND", "memory")
    get_settings.cache_clear()
    reset_bridge()

    test_engine = create_engine(f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setattr(sess_mod, "get_engine", lambda: test_engine)
    reset_engine()
    sess_mod._SessionLocal = sessionmaker(bind=test_engine, expire_on_commit=False)
    Base.metadata.create_all(test_engine)

    celery_app.conf.task_always_eager = True
    client = TestClient(app.main.app)
    yield client, tmp_path
    celery_app.conf.task_always_eager = False
    get_settings.cache_clear()
    reset_bridge()
    reset_engine()


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                   check=True, timeout=300)


def _sine_wav(dst: Path, seconds: float = 4.0) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-ar", "16000", "-ac", "1", str(dst))
    return dst


def _project_scene_shot_character(client: TestClient):
    r = client.post("/api/v1/projects", json={"title": "P5D proof"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = client.post(f"/api/v1/projects/{pid}/scenes",
                    json={"title": "Opening", "location": "한옥 마당"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = client.post(f"/api/v1/scenes/{sid}/shots",
                    json={"action": "Hero speaks", "duration_sec": 5})
    assert r.status_code == 201, r.text
    shot_id = r.json()["id"]
    r = client.post("/api/v1/characters",
                    json={"name": "Hero",
                          "appearance_prompt": "a young woman",
                          "project_id": pid})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    return pid, shot_id, cid


def _upload_wav(client: TestClient, pid: str, dst_name: str = "sample.wav") -> str:
    wav = _sine_wav(Path("/tmp") / f"p5d_{uuid.uuid4().hex}.wav", seconds=2.0)
    with wav.open("rb") as f:
        r = client.post("/api/v1/assets/upload",
                        files={"file": (dst_name, f, "audio/wav")},
                        data={"kind": "audio", "project_id": pid})
    assert r.status_code == 201, r.text
    return r.json()["asset_id"]


def test_dialogue_tts_end_to_end(api_env):
    client, tmp_path = api_env
    pid, shot_id, cid = _project_scene_shot_character(client)

    r = client.post(
        f"/api/v1/shots/{shot_id}/dialogue",
        json={"character_id": cid,
              "text": "안녕하세요, 오늘 날씨가 참 좋네요",
              "emotion_weights": {"joy": 0.8, "anger": 0.1}},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    # Eager celery: the TTS worker already ran.
    r = client.get(f"/api/v1/audio/{job_id}")
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "done"
    assert job["kind"] == "dialogue"
    assert job["character_id"] == cid
    assert job["emotion_weights"] == {"joy": 0.8, "anger": 0.1}
    assert job["output_path"].endswith(".wav")
    assert job["duration_sec"] and job["duration_sec"] > 0

    wav_abs = tmp_path / job["output_path"]
    assert wav_abs.exists()
    assert wav_abs.stat().st_size > 44  # more than a bare WAV header

    # Output path is relative (DB invariant) and immutable-recorded.
    assert not str(job["output_path"]).startswith("/")

    # Same Idempotency-Key replays the original job instead of creating one.
    key = f"p5d-{uuid.uuid4().hex}"
    r1 = client.post(f"/api/v1/shots/{shot_id}/dialogue",
                     json={"character_id": cid, "text": "다시"},
                     headers={"Idempotency-Key": key})
    r2 = client.post(f"/api/v1/shots/{shot_id}/dialogue",
                     json={"character_id": cid, "text": "다시"},
                     headers={"Idempotency-Key": key})
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["job_id"] == r2.json()["job_id"]


def test_dialogue_emotion_weights_validation(api_env):
    client, _ = api_env
    _, shot_id, cid = _project_scene_shot_character(client)

    r = client.post(f"/api/v1/shots/{shot_id}/dialogue",
                    json={"character_id": cid, "text": "hi",
                          "emotion_weights": {"joy": 1.5}})
    assert r.status_code in (400, 422), r.text

    r = client.post(f"/api/v1/shots/{shot_id}/dialogue",
                    json={"character_id": cid, "text": "   "})
    assert r.status_code in (400, 422), r.text


def test_voice_clone_and_dialogue_uses_voice(api_env):
    client, _ = api_env
    pid, shot_id, cid = _project_scene_shot_character(client)

    sample_id = _upload_wav(client, pid)
    r = client.post(f"/api/v1/characters/{cid}/voice/clone",
                    json={"sample_asset_id": sample_id})
    assert r.status_code == 200, r.text
    voice_id = r.json()["voice_id"]
    assert voice_id == f"voice_{cid.replace('-', '')[:8]}"

    # Persisted on the character row (CharacterRead is owned by another
    # track; verify at the DB level here).
    with session_scope() as db:
        from app.models.character import Character
        row = db.get(Character, uuid.UUID(cid))
        assert row.voice_id == voice_id

    # A later dialogue job carries the cloned voice id.
    r = client.post(f"/api/v1/shots/{shot_id}/dialogue",
                    json={"character_id": cid, "text": "복제된 목소리로 말하기"})
    assert r.status_code == 202, r.text
    r = client.get(f"/api/v1/audio/{r.json()['job_id']}")
    assert r.json()["voice_id"] == voice_id


def test_voice_clone_rejects_non_audio(api_env):
    client, tmp_path = api_env
    pid, _, cid = _project_scene_shot_character(client)

    png = tmp_path / "not_audio.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    with png.open("rb") as f:
        r = client.post("/api/v1/assets/upload",
                        files={"file": ("x.png", f, "image/png")},
                        data={"kind": "background", "project_id": pid})
    assert r.status_code == 201, r.text
    r = client.post(f"/api/v1/characters/{cid}/voice/clone",
                    json={"sample_asset_id": r.json()["asset_id"]})
    assert r.status_code in (400, 422), r.text


def test_lipsync_service_within_and_beyond_band(tmp_path):
    from app.services import lipsync

    src = _sine_wav(tmp_path / "src.wav", seconds=4.0)

    # Within ±10%: 4.0s -> 4.2s (rate 0.952) gets atempo-adjusted.
    dst = tmp_path / "fit.wav"
    res = lipsync.fit_audio_to_duration(src, 4.2, dst)
    assert res["warning"] is None
    assert abs(res["applied_rate"] - 4.0 / 4.2) < 1e-3
    from app.media import ffmpeg_ops
    dur = float(ffmpeg_ops.probe(dst)["duration_sec"])
    assert abs(dur - 4.2) < 0.15

    # Beyond ±10%: 4.0s -> 8.0s warns and leaves the audio untouched.
    dst2 = tmp_path / "unfit.wav"
    res2 = lipsync.fit_audio_to_duration(src, 8.0, dst2)
    assert res2["warning"] is not None
    assert "±10%" in res2["warning"]
    assert res2["applied_rate"] == 1.0
    dur2 = float(ffmpeg_ops.probe(dst2)["duration_sec"])
    assert abs(dur2 - 4.0) < 0.15

    # Beyond the band the other way (too short target).
    res3 = lipsync.fit_audio_to_duration(src, 2.0, tmp_path / "unfit2.wav")
    assert res3["warning"] is not None


def test_dialogue_worker_applies_lipsync(api_env):
    client, tmp_path = api_env
    _, shot_id, cid = _project_scene_shot_character(client)

    # 20 chars -> stub wav is 2.4s; target 2.5s -> rate 0.96 (within band).
    text = "가" * 20
    r = client.post(f"/api/v1/shots/{shot_id}/dialogue",
                    json={"character_id": cid, "text": text,
                          "target_duration_sec": 2.5})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/v1/audio/{r.json()['job_id']}").json()
    assert job["status"] == "done"
    assert job["output_path"].endswith("_lipsync.wav")
    assert (tmp_path / job["output_path"]).exists()

    # Beyond band: target 10s on a 2.4s wav -> warning, original kept.
    r = client.post(f"/api/v1/shots/{shot_id}/dialogue",
                    json={"character_id": cid, "text": text,
                          "target_duration_sec": 10.0})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/v1/audio/{r.json()['job_id']}").json()
    assert job["status"] == "done"
    assert job["output_path"].endswith(".wav")
    assert not job["output_path"].endswith("_lipsync.wav")
    with session_scope() as db:
        from app.models.audio import AudioGeneration
        row = db.get(AudioGeneration, uuid.UUID(job["id"]))
        lip = (row.params or {}).get("lipsync") or {}
        assert lip.get("warning") is not None
        assert "±10%" in lip["warning"]


def test_broll_suggest_matches_narration_keywords(api_env):
    client, tmp_path = api_env
    pid, _, _ = _project_scene_shot_character(client)

    # Timeline + subtitle track carrying narration keywords.
    r = client.post(f"/api/v1/projects/{pid}/timelines", json={"name": "Cut 1"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = client.post(f"/api/v1/projects/{pid}/subtitle-tracks",
                    json={"language_code": "ko", "label": "narration"})
    assert r.status_code == 201, r.text
    track_id = r.json()["id"]
    r = client.put(f"/api/v1/subtitle-tracks/{track_id}/cues", json={"cues": [
        {"start_sec": 0.5, "end_sec": 3.0,
         "text": "옛 한옥 마당에 아침 햇살이 스며든다"},
        {"start_sec": 3.5, "end_sec": 6.0,
         "text": "멀리 바다 파도 소리가 들려온다"},
    ]})
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/v1/timelines/{tid}",
                     json={"subtitle_track_ids": [track_id]})
    assert r.status_code == 200, r.text

    # Two backdrop assets: one matching, one not.
    for name in ("hanok.png", "office.png"):
        r = client.post("/api/v1/assets/upload",
                        files={"file": (name, _TINY_PNG, "image/png")},
                        data={"kind": "background", "project_id": pid})
        assert r.status_code == 201, r.text
    with session_scope() as db:
        from app.models.asset import Asset
        hanok = db.query(Asset).filter(Asset.name == "hanok.png").one()
        hanok.tags = ["한옥", "전통", "마당"]

    r = client.post(f"/api/v1/projects/{pid}/broll-suggest",
                    json={"timeline_id": tid, "limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["narration_chars"] > 0
    assert "한옥" in body["keywords"] or "마당" in body["keywords"]
    names = [s["asset_name"] for s in body["suggestions"]]
    assert "hanok.png" in names
    assert "office.png" not in names
    hanok_sug = next(s for s in body["suggestions"]
                     if s["asset_name"] == "hanok.png")
    assert hanok_sug["keyword"] in ("한옥", "마당")
    assert hanok_sug["score"] > 0
