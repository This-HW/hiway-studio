# db/ — PostgreSQL + Alembic

> MODULES.md 카드 형식.

## 목적 (1줄)

DB 연결과 마이그레이션을 담당한다. 스키마 변경은 Alembic을 통해서만 한다.

## 책임

- `session.py`: engine·SessionLocal·`get_db`(FastAPI 의존성)·`session_scope`(워커용).
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
