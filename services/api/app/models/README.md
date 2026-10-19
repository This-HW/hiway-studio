# models/ — SQLAlchemy (도메인별 1파일)

> MODULES.md 카드 형식.

## 목적 (1줄)

설계서 §4 데이터 모델의 코드 실체. 테이블·컬럼 철자는 설계서와 일치시킨다.

## 책임

- Phase 0: `system.py` (모델 레지스트리 최소), `generation.py` (잡 행 최소).
- Phase별 추가: `project.py`(1), `story.py`(1), `character.py`(1), `asset.py`(1),
  `style.py`(1), `production.py`(2), `timeline.py`(3), `audio.py`(4), `subtitle.py`(4),
  `qc.py`(4), `multishot.py`(5). `system.py`는 Phase 1/4에 컬럼 확장.

## 공개 인터페이스

- `app.models.{system, generation}`의 ORM 클래스.
- `Base.metadata` — Alembic autogenerate와 테스트의 `create_all`에 사용.

## 불변식

1. 경로 컬럼(`output_path` 등)은 **상대경로만** 저장한다. 절대경로 저장 금지.
2. 휴대성: postgres 전용 타입 대신 `Uuid`·`JSON` 등 이식 가능한 타입을 쓴다.
3. 컬럼 추가 = Alembic 마이그레이션 필수.

## 의존성

- `db`(Base), `core`(기본값). 도메인 `services`를 import하지 않는다.

## 함정

- `project_id`에 FK가 없는 것은 Phase 0 한정이다. Phase 1 `project.py` 추가 시 FK를 건다.
- Phase 0에서 generation 행이 큐 잡 행을 겸한다. Phase 2에서 전용 jobs/배치 구조로 분리한다.
