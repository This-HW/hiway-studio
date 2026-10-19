# AI Studio — 1인용 AI 이미지·영상 스토리텔링 스튜디오

텍스트에서 이미지·영상·음성을 만들고, 장면 단위 타임라인으로 편집·자막·QC·내보내기까지
한 저장소에서 처리하는 로컬 우선(local-first) 1인 제작 도구다.

핵심 아이디어:

- **캐릭터를 한 번 만들면 계속 쓴다** — 프로젝트 간 재사용 가능한 캐릭터·에셋 라이브러리,
  캐릭터 시트(턴어라운드·표정), 직전 샷의 마지막 프레임을 참조하는 연속성 생성.
- **모델은 갈아끼운다** — 모델 레지스트리 + 라우터 + 제공자 어댑터 구조라서
  이미지/영상/음성 모델을 언제든 교체할 수 있다 (Ports & Adapters는 모델 경계에만 적용).
- **내보내기 전 자동 검수** — 자막 타이밍·OCR·연속성·립싱크·음량·인코딩 13종 QC 게이트.
- **API 키 없이 체험** — stub 어댑터로 전체 파이프라인을 돌리는 `make demo`.

## 아키텍처 (텍스트 그림)

```
apps/web (Next.js) ──REST/SSE──▶ services/api (FastAPI + Celery, 단일 Python 패키지)
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
              models_lib/            media/              files/
        (레지스트리·라우터·어댑터)   (ffmpeg·자막·QC)    (상대경로·불변성)
                    │
        ┌───────────┴───────────┐
        │                       │
   adapters/stub.py     adapters/http_polling.py
   (키 없이 데모·테스트)   (fal/Replicate/직접 API 공용)

PostgreSQL (메타데이터) · Redis (큐·SSE) · 로컬 파일시스템 (산출물)
```

- Modular Monolith: `routers → services → models` 단방향 의존 (순환 금지,
  `make lint-imports`로 기계적 검사).
- DB에는 상대경로만 저장, 완료된 산출물은 불변(immutable).
- 상세: `docs/PROJECT_ARCHITECTURE.md`, `docs/MODULES.md`, `docs/AGENT_GUIDE.md`.

## 빠른 시작

### 0. API 키 없이 전체 플로우 체험 (5분)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e "services/api[test]"
make demo
```

`make demo`는 stub 어댑터로 실제 파이프라인을 전부 돌린다:
프로젝트 생성 → 캐릭터(마스터 초상+시트) → 씬 1개·샷 3개 → 프레임 생성·승인 →
I2V 클립 3개 → 타임라인 조립 → 자막 3개 큐 → QC(13종) → mp4 1개 출력.
산출물은 `projects/demo-<시각>/`에 모이며, git에 들어가지 않는다.

### 1. 로컬 개발 스택

```bash
cp .env.example .env   # 값 입력 — 시크릿은 .env에만, git에 올리지 마세요
make dev               # postgres, redis, api, worker 기동 (docker)
make migrate           # DB 마이그레이션
make seed              # 모델·카테고리팩·프리셋 seed (멱등)
```

### 2. 검증

```bash
make test          # pytest + web typecheck (docker 없이: services/api에서 pytest -q)
make lint-imports  # 모듈 의존성 방향 검사 — 위반 시 커밋 금지
```

### 시스템 요구사항

- **ffmpeg + ffprobe** (클립 정규화·렌더용. Docker 이미지에 포함, 로컬 실행 시 별도 설치.
  `FFMPEG_BIN` 환경변수로 경로 지정 가능)
- Python 3.11+, Node 20+

## 문서 지도

- `docs/PROJECT_ARCHITECTURE.md` — 전체 아키텍처와 모듈 설계 (정본)
- `docs/AGENT_GUIDE.md` — 10분 온보딩 (처음 보는 사람·에이전트용)
- `docs/MODULES.md` — 모듈별 목적·인터페이스·불변식·함정
- `docs/P0_SCREENS.md` — P0 화면 9종 설계
- `docs/BACKUP_RESTORE.md` — 백업·복원 절차
- `docs/OSS_PLAN.md` — 오픈소스 공개 계획 (검토용)
- `docs/licenses.md` — 의존성 라이선스 기록
- `docs/adr/` — 아키텍처 결정 기록 (0000 템플릿)
- `CONTRIBUTING.md` — 기여 규칙 (커밋·ADR·lint)
- `packages/contracts/` — 에러 코드·SSE 이벤트·enum JSON (단일 진실 공급원)

## 백업

```bash
infra/scripts/backup.sh          # 파일 하드링크 스냅샷 30세대 + DB 덤프 14일
infra/scripts/restore.sh --list  # 스냅샷 목록
```

상세: `docs/BACKUP_RESTORE.md`.

## OSS 공개 전제

Day-one부터 OSS 공개를 전제로 개발한다:

- 시크릿은 `.env`로만 관리 (`.env.example`에는 빈 값만).
- 개인 산출물은 gitignored 경로(`projects/`, `library/`, `infra/backups/`)에만 저장.
- 새 의존성은 `docs/licenses.md`에 라이선스 기록 (GPL 결합 가능성은 ADR 필요).
- ComfyUI는 저장소에 포함하지 않고 별도 프로세스로 연동 (GPL 분리).
- **공개 전환은 저장소 소유자의 명시적 승인 후에만.**
