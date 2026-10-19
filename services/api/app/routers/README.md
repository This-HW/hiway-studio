# routers/ — REST (리소스별 1파일)

> MODULES.md 카드 형식.

## 목적 (1줄)

얇은 HTTP 경계. 검증 → 서비스 호출 → 응답만 한다.

## 책임

- Phase 0: `health.py` (`GET /healthz`), `events.py` (`GET /api/v1/events` SSE).
- Phase별 추가: projects(1), stories(1), characters(1), assets(1), style_kits(1),
  category_packs(1), scenes(2), generations(2), videos(3), timelines(3), subtitles(4),
  audio(4), qc(4), costs(4), models(레지스트리 상태), exports(ADR-0004), multishots(5).

## 공개 인터페이스

- `router: APIRouter` per module, mounted in `app/main.py` under `/api/v1`.

## 불변식

1. 라우터에 도메인 로직을 쓰지 않는다 (프롬프트 조립·비용 계산·상태 전이 금지).
2. DB 직접 접근 금지. `services` 경유.
3. 잡 등록 시 태스크 인자는 `{generation_id}` 단일.

## 의존성

- `services`, `schemas`, `core`. `db`·`models`·`media`·`tasks` 직접 import 금지.

## 함정

- `events.py`는 SSE 단일 전역 스트림이다. 화면마다 EventSource를 만들지 않는다 (웹은 `lib/sse.ts`).
