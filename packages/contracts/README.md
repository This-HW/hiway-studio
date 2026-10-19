# contracts

에러 코드, SSE 이벤트, enum의 단일 진실 공급원(SSOT)입니다. 백엔드와 웹이 공유합니다.

## 정본 위치

- 에러: `services/api/app/core/errors.py` (정본) → `packages/contracts/errors.json` (미러)
- SSE 이벤트: `packages/contracts/sse_events.json`
- enum: `packages/contracts/enums.json`

## 파생 방향

contracts(JSON) → TS 타입 (웹에서 소비)

## 수정 절차

1. `packages/contracts/*.json`을 먼저 수정합니다 (에러는 `errors.py`도 함께).
2. `make gen-types`로 유효성을 검증합니다 (Phase 0은 검증만 수행, TS 코드젠은 Phase 1+).
3. 정본(JSON)과 미러(errors.py)가 항상 일치하는지 확인합니다.
