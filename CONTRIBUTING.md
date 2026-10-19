# Contributing

## 시작하기

1. `docs/AGENT_GUIDE.md`를 먼저 읽는다 (10분 온보딩).
2. 작업하려는 모듈의 카드를 `docs/MODULES.md`에서 확인한다.
3. `make demo`로 현재 상태가 깨지지 않았는지 확인한다.

## 커밋 규칙

- 형식: `<범위>: <내용>` (영어, 간결한 현재형).
  - 예: `Phase 2: shots/images — scenes/shots CRUD, frame batch generation`
  - 예: `docs: sync MODULES.md with actual layout`
- 하나의 커밋은 하나의 논리 단위. Phase 단위 기능은 Phase별 커밋.
- 커밋 전 체크리스트:
  - [ ] `pytest -q` (services/api) 통과
  - [ ] `python3 tools/lint_imports.py` 통과 — **의존성 방향 위반 시 커밋 금지**
  - [ ] 새 설계 결정이 있으면 ADR 작성 (아래)
  - [ ] 새 의존성이 있으면 `docs/licenses.md`에 라이선스 기록
  - [ ] `git status`에 private 파일(`.env`, `projects/`, `library/`, `infra/backups/`)이 없는지 확인

## ADR 규칙

- 코드보다 **먼저** 기록한다: `docs/adr/NNNN-제목.md` (`0000-template.md` 복사).
- 번호는 0001부터 순차. 상태: `제안` → `승인` → `대체됨(superseded)`.
- 승인 없이 `승인`으로 바꾸지 않는다.
- 기존 ADR을 뒤집는 결정은 새 ADR로 기록하고 기존 것은 `대체됨`으로 표시 (삭제 금지).

## 모듈 규칙 (요약)

상세는 `docs/PROJECT_ARCHITECTURE.md` §15.

- 의존성 방향: `routers → services → models`, `tasks → services/models_lib/media/files`.
  `media`는 `services`를 import하지 않는다. `core`는 아무것도 import하지 않는다.
- 라우터는 얇게 (검증 → 서비스 호출 → 응답). 프롬프트 조립·비용 계산·상태 전이는 서비스에.
- Celery 태스크 인자는 `{generation_id}` 단일.
- DB에는 상대경로만 저장 (`app/files/paths.py` 경유).
- 완료된 산출물은 불변 — 덮어쓰기 금지, 재생성 시 새 파일 + 새 잡.
- Phase에 없는 파일은 미리 만들지 않는다.

## 시크릿·개인정보 규칙

- API 키·비밀번호·토큰은 `.env`에서만 읽는다. 코드·로그·에러 메시지에 하드코딩 금지.
- 로그·에러 메시지에 절대경로·개인 파일명을 노출하지 않는다.
- 개인 산출물은 `projects/`, `library/` (gitignored)에만 둔다.
- 이슈·PR에 API 키나 개인 에셋을 첨부하지 않는다.

## 라이선스

- 본 저장소는 MIT (LICENSE).
- 새 의존성 추가 시 `docs/licenses.md`에 기록. GPL 계열과 결합될 가능성이 있으면
  반드시 ADR을 먼저 작성한다 (ComfyUI처럼 별도 프로세스 분리가 원칙).
