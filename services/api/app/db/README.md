# db/ — PostgreSQL + Alembic

> MODULES.md 카드 형식.

## 목적 (1줄)

DB 연결과 마이그레이션을 담당한다. 스키마 변경은 Alembic을 통해서만 한다.

## 책임

- `session.py`: engine·SessionLocal·`get_db`(FastAPI 의존성)·`session_scope`(워커용)·`dispatch_after_commit`
  (commit 뒤에만 잡 전송, 콜백 하나가 실패해도 나머지를 시도 — 놓친 전송은 reconcile이 복구, ADR-0013).
- `base.py`: SQLAlchemy `Base`.
- `alembic/`: 마이그레이션 스크립트. `make migrate` = `alembic upgrade head`.

## 공개 인터페이스

- `get_db()`, `session_scope()`, `get_engine()`
- `reset_engine()` — 테스트 전용.

## 불변식

1. DSN은 `Settings.database_url`에서만 읽는다. 코드에 URL 기본값을 두지 않는다.
2. `models/` 직접 수정 후에는 반드시 `alembic revision --autogenerate` → 검토 → `upgrade`.
3. 새 Phase의 모델 모듈은 `alembic/env.py`에 import로 등록한다.

## 의존성

- `core`만 (설정). `models`를 import하지 않는다 (순환 방지).

## 함정

- `get_db`는 yield 후 commit한다. 라우터에서 예외가 나면 rollback된다.
- SQLite 테스트에서는 `Base.metadata.create_all`을 써도 되지만, 그건 테스트 전용이다.
- 테스트는 서비스 `DATABASE_URL`을 쓰지 않는다(`tests/conftest.py`가 SQLite로 덮어씀). Postgres 테스트는
  `TEST_DATABASE_URL`(DB 이름이 `test`로 끝나야 함)을 줄 때만 돈다.
- 기존 설치를 업데이트하면 `make migrate`를 다시 실행한다(현재 head `0016_budget_idempotency`).
