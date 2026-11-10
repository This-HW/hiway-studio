# tasks/ — Celery (큐별 1파일)

> MODULES.md 카드 형식.

## 목적 (1줄)

4개 고정 큐의 워커 진입점. API와 같은 패키지를 공유한다.

## 책임

| 파일 | 큐 | 담당 |
|---|---|---|
| `tasks_image.py` | `q.image` (4) | 이미지 생성 파이프라인 (Phase 0 동작) |
| `tasks_video.py` | `q.video` (2) | I2V/T2V (Phase 3) |
| `tasks_audio.py` | `q.audio` (2) | TTS (Phase 4) |
| `tasks_post.py` | `q.post` (2) | ffmpeg·썸네일·QC·렌더 (Phase 3/4) |
| `tasks_p1.py` | `q.post` 공용 (태스크명 `q.p1.*`) | 멀티샷·LoRA 학습·스타일 추출 (Phase 5) |

## 공개 인터페이스

- 태스크명: `q.image.run_generation` 등 (`<큐>.<동작>`).
- 시그니처: `run_generation(generation_id: str)` — **단일 인자 고정**. 프롬프트 원문을 인자로 넣지 않는다.

## 불변식

1. 새 큐를 추가하지 않는다. 동시성은 설정으로만 조정.
2. 단계마다 `job_events` 채널에 `job.progress`를 publish한다.
3. `routers`를 import하지 않는다.
4. tasks는 `db`·`models`를 직접 읽을 수 있다 (자신의 `generation_id` 행을 처리해야 하므로).
   도메인 로직이 커지면 `services/`로 옮긴다.

## 의존성

- `services`, `models_lib`, `media`, `files`, `core`, `db`, `models`. `routers` 금지.

## 함정

- `bind=True` + `max_retries`는 재시도 정책(§12)의 Celery 표현이다. 단, 자동 재시도는 provider에 확실히 전송되지 않은
  실패만이다. submit 이후 결과가 불확실하면 `failed`+`PROVIDER_UNCERTAIN`이고 자동 재제출하지 않는다(ADR-0013 D7).
- 태스크는 시작 시 행을 claim(`queued → running`, `exec_token` + lease)한다. 0행(중복 전달·terminal 행)이면 no-op으로 ACK한다.
  이후 쓰기는 `id + exec_token + status` 조건부이고, 산출물은 토큰 앞 8자를 붙인 시도별 파일로 쓴다
  (`services/job_execution.py`, ADR-0013). 잃은 잡은 API lifespan의 reconcile 루프가 복구한다(`services/job_recovery.py`).
- Phase 0에서 generation 행이 잡 행을 겸한다. SSE의 `job_id` = `generation_id`.
