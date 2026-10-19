# services/ — 도메인 로직

> MODULES.md 카드 형식. Phase 0은 디렉토리 + 본 README만 둔다.

## 목적 (1줄)

라우터와 태스크가 호출하는 도메인 로직. "얇은 라우터"를 가능하게 한다.

## 책임

- 도메인 CRUD 서비스 (라우터별 1파일): `project.py`, `story.py`, `character.py`, `asset.py`, `style_kit.py`
- 파이프라인·횡단 서비스: `character_pipeline.py` (P0-5 시트 생성), `episode_clone.py` (D-23),
  `file_serve.py` (`GET /files` 서빙), `model_registry.py` (읽기 전용 뷰)
- Phase 2: `prompt_assembly.py`, `approval.py`, `continuity.py`, `idempotency.py` (D-13.2, Phase 1에서 이월)
- Phase 4: `cost_estimator.py`, `budget_guard.py`, `qc_orchestrator.py`
- Phase 3: `render.py`

상세 카드(인터페이스·불변식·함정)는 `docs/MODULES.md`의 services 섹션 참조.

## 불변식

1. 라우터는 services 경유, DB 직접 접근 금지 — 이 모듈이 그 경계다.
2. `[가정]` 수치는 `core/defaults.py`에서만 읽는다.
3. 상태 전이 함수는 허용되지 않은 전이에 대해 실패 케이스 테스트를 갖는다.

## 의존성

- `models`, `models_lib`, `files`, `media`, `core`, `db`, `schemas`. `routers` 금지.

## 함정

- `media`는 `services`를 import하지 않는다 (역방향). 미디어 처리가 필요하면 services가 media를 호출한다.
