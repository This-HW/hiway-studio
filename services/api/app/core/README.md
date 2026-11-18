# core/ — 공통 기반

> MODULES.md 카드 형식. 독자: 처음 보는 인간·에이전트.

## 목적 (1줄)

모든 모듈이 의존하는 기반. 설정·에러·기본값·SSE 브릿지·미들웨어를 둔다.

## 책임

- `config.py`: 환경변수 로딩 (3층 설정 중 2층). `.env.example`에 없는 변수를 읽지 않는다.
  빈 값은 미설정으로 취급한다(`env_ignore_empty`). `RECONCILE_ENABLED`로 API lifespan의 잡 복구 루프를 켜고 끈다(ADR-0013).
- `errors.py`: 오류 18종 정본 (설계서 §8.4 + 이후 추가: LORA_TOO_FEW_IMAGES, DUPLICATE_KEYFRAME, PROVIDER_UNCERTAIN, IDEMPOTENCY_CONFLICT). `StudioError`와 §8.4 JSON 변환.
- `defaults.py`: 모든 `[가정]` 기본값의 단일 집합. 코드 내 매직넘버 금지.
- `sse_bridge.py`: Redis Pub/Sub → SSE 브릿지. 이벤트명 정본은 `packages/contracts`.
- `middleware.py`: 에러 포맷 통일. 로그에 시크릿·절대경로·개인 파일명을 찍지 않는다.

## 공개 인터페이스

- `get_settings() -> Settings` (lru_cache; 테스트는 `cache_clear()` 후 env 재설정)
- `StudioError(code, message, retryable?, retry_after_sec?, details?)`
- `error_body(code, message, ...) -> dict` (§8.4 형태)
- `get_bridge() -> SseBridge` / `reset_bridge()` (테스트용)
- `SseBridge.publish(channel, event, data)` (동기 — Celery 태스크에서 호출)
- `SseBridge.subscribe(channel)` (async generator)
- `register_middleware(app)`

## 불변식

1. core는 내부 모듈을 import하지 않는다 (`make lint-imports`로 강제).
2. `DATABASE_URL`·`REDIS_URL`·`PROJECTS_ROOT`는 코드 기본값이 없다. 없거나 비어 있으면 시작 시점에 명확히 실패한다. 선택 변수는 비워 두면 기본값을 쓴다.
3. 로그·에러 details에 시크릿·절대경로·개인 파일명을 포함하지 않는다.

## 의존성

없음 (가장 아래 레이어).

## 확장 절차

- 새 에러 코드: `errors.py`에 먼저 추가 → `packages/contracts/errors.json`에 미러 → `make gen-types`.
- 새 `[가정]` 기본값: `defaults.py`에 상수로 추가하고 설계서 §13 표와 동기화.

## 함정

- `get_settings()`는 캐시된다. 테스트에서 env를 바꾸면 `get_settings.cache_clear()` + `reset_bridge()`를 호출해야 한다.
- memory SSE 백엔드는 프로세스 내 전용이다. API와 워커가 분리된 프로세스면 반드시 redis 백엔드를 쓴다.
