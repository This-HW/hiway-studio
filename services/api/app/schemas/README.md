# schemas/ — Pydantic (도메인별 1파일, models/와 1:1)

> MODULES.md 카드 형식. Phase 0은 디렉토리 + 본 README만 둔다.

## 목적 (1줄)

요청/응답 스키마. 리소스마다 별도 DTO 파일을 만들지 않는다.

## 책임

- Phase 1부터: `project.py`, `story.py`, `character.py`, `asset.py`, `style.py` …
- 각 파일은 대응하는 `models/<도메인>.py`의 요청/응답 형태를 함께 둔다.

## 불변식

1. 도메인별 1파일. 화면별·리소스별 중복 타입 금지.
2. 새 enum·에러가 필요하면 `packages/contracts`에 먼저 등록 → `make gen-types`.

## 의존성

- `core`만. `models`(ORM)를 import하지 않는다.

## 함정

- 스키마는 입출력 경계용이다. 도메인 로직을 여기에 넣지 않는다.
