# AI 스튜디오 — 프로젝트 아키텍처 (구현 기준서)

> 목적: 이 문서만 보고 개발할 수 있게, **"무엇을 어디에 만드는지"**를 고정한다.
> 불필요한 파일·모듈이 생기지 않게, 만드는 규칙과 만들지 않는 규칙을 함께 정의한다.
>
> - 상위 설계: `research_notes/ai-studio-system-design-20260929-2357/report.md` (시스템 설계서 v1.0, 이하 "설계서")
> - 기능 스펙: `AI 스튜디오 스펙 조사` 아티팩트 (ai-2) v3
> - 본 문서는 설계서의 **구현 배치도**다. 설계서와 충돌하면 설계서가 우선이며, 본 문서를 고친다.
>
> ## 공식 방법론
> 본 프로젝트는 **Modular Monolith + Ports & Adapters + Agent-First Docs**를 따른다.
> - **Modular Monolith**: 단일 배포 단위, 모듈 간 경계는 코드·의존성 방향으로 강제 (§15). 1인 + 로컬 실행 조건에서 마이크로서비스의 운영 부담을 지지 않는다.
> - **Ports & Adapters**: 변동성이 큰 외부 경계(모델 제공자 API)에만 헥사고날을 적용한다 (`models_lib/base.py`가 포트). 도메인 전체에 엄격한 DDD를 씌우지 않는다.
> - **Agent-First Docs**: 컨텍스트 없는 인간·에이전트가 10분 안에 파악할 수 있게, 지도(AGENT_GUIDE) → 모듈 카드(MODULES) → 코드 순서의 점진적 공개 구조를 유지한다.
> - 의도적으로 쓰지 않는 것: 마이크로서비스, 이벤트 소싱/CQRS, 엄격한 DDD 애그리거트 — 1인용 스코프에 필요 없는 복잡도이므로 제외한다.
>
> ## 관련 문서
> - `docs/AGENT_GUIDE.md` — 처음 보는 인간·에이전트의 10분 진입 가이드, 설계서 §→코드 매핑표, 작업 유형별 가이드
> - `docs/MODULES.md` — 모듈별 에이전트용 설명 카드 (목적·책임·인터페이스·불변식·의존성·확장·함정). 구현 시 각 모듈 README의 형식 기준
> - `docs/OSS_PLAN.md` — OSS 공개 검토서 (적합성·라이선스·분리 규칙·공개 전 체크리스트)
> - `docs/adr/` — 앞으로의 설계 결정 기록 (템플릿: `0000-template.md`). D-01~D-32는 설계서 §14에 있음

---

## 1. 설계 완성 상태 판정

| 영역 | 상태 |
|---|---|
| 논리 설계 (아키텍처·데이터 모델·플로우·API·상태머신·QC·재시도·비용) | **완성 (v1.0)** — 25 테이블, REST 60여 개, SSE 7종, 오류 16종, 상태머신 5종, QC 15종(13 형식+2 내용 advisory), 설계 결정 D-01~D-32 |
| `[가정]` 기본값 (재시도 간격·큐 상한·임계값 등) | **HW 확정 필요** — §13 표 참조. 구현 착수 전 한 번에 확정 |
| P0 화면 9종 설계 | 완료 (`docs/P0_SCREENS.md`, 19개 화면 정의) |
| 프로젝트 아키텍처 (폴더·코드 구조·파일 생성 규칙) | **본 문서로 확정** |

---

## 2. 전체 레포 구조

모노레포 1개. API와 워커는 **같은 Python 패키지를 공유**한다 (중복 코드 방지).

```
ai-studio/
├── docker-compose.yml          # postgres, redis, api, worker (빌드 컨텍스트 = 레포 루트, API는 127.0.0.1에만 게시. ComfyUI는 compose 밖 별도 프로세스 — COMFYUI_URL)
├── .env.example                # 모든 환경변수 목록 (값은 .env에만)
├── Makefile                    # make dev / migrate / seed / gen-types / gen-types-check / test / verify-cjk / lint-imports / demo
├── .github/workflows/ci.yml    # CI: api(pytest SQLite+Postgres·ruff·lint-imports·contracts·compose loopback) / web / image
├── tools/                      # lint_imports.py, demo.py, check_compose_loopback.py, verify_cjk_burnin.py 등
├── apps/
│   └── web/                    # Next.js 14 App Router + TS + Zustand + TanStack Query
│       ├── app/                # 라우트 (아래 §5)
│       ├── components/         # 여러 화면에서 쓰는 것만 (버튼·모달·잡 뱃지 등)
│       ├── lib/                # api-client.ts, sse.ts, query-keys.ts (API 호출은 여기로만)
│       └── stores/             # zustand 스토어 (화면별 1파일)
├── services/
│   └── api/                    # FastAPI + Celery (단일 Python 패키지)
│       ├── app/
│       │   ├── main.py
│       │   ├── core/           # config, errors(18종), middleware, sse_bridge, defaults, celery_app, contracts
│       │   ├── db/             # session, base, alembic/ (마이그레이션)
│       │   ├── models/         # SQLAlchemy — 도메인별 1파일 (§4.1)
│       │   ├── schemas/        # Pydantic — 도메인별 1파일 (§4.2)
│       │   ├── routers/        # REST — 리소스별 1파일 (§4.3)
│       │   ├── services/       # 도메인 로직 (§4.4)
│       │   ├── models_lib/     # 모델 추상화: registry + router + adapters (§4.5)
│       │   ├── media/          # ffmpeg·OCR·썸네일·자막 렌더 (§4.6)
│       │   ├── files/          # 저장 규칙 유틸 (§11 구현)
│       │   └── tasks/          # Celery 태스크 — 큐별 1파일 (§4.7)
│       ├── tests/              # app/ 구조를 그대로 미러
│       └── pyproject.toml
├── packages/
│   └── contracts/              # 단일 진실 공급원: 에러코드·SSE 이벤트명·enum → TS 타입 생성
├── infra/
│   ├── seeds/                  # 모델 레지스트리 seed, 카테고리 팩 3종 seed (SQL/JSON)
│   └── backups/                # (런타임 산출물, git 제외)
├── projects/                   # (런타임 산출물, git 제외 — 설계서 §11)
├── library/                    # (런타임 산출물, git 제외)
└── docs/
    └── PROJECT_ARCHITECTURE.md # 본 문서 (레포로 옮길 때 여기로)
```

### Phase → 폴더 매핑

| Phase | 만드는 것 |
|---|---|
| Phase 0 기반 | `services/api` 전체 골격, `db/`+마이그레이션, `models_lib/` (stub+http 어댑터), `tasks/` 스켈레톤, `core/` (에러·SSE), `files/`, `infra/seeds` (모델 seed), `packages/contracts`, docker-compose, 의존성 방향 린트 (`make lint-imports`, §15 기계적 강제) |
| Phase 1 기획·캐릭터 | `routers/` (projects, stories, characters, assets, style_kits, category_packs), `services/` (episode_clone), `media/` 썸네일 기초, `apps/web` 라우트 절반 |
| Phase 2 샷·이미지 | `routers/` (scenes, generations), `services/` (prompt_assembly, approval, continuity), `tasks/tasks_image.py` |
| Phase 3 영상·타임라인 | `routers/` (videos, timelines, exports), `tasks/tasks_video.py`, `media/ffmpeg_ops.py` |
| Phase 4 자막·오디오·QC·비용 | `routers/` (subtitles, audio, qc, costs), `services/` (qc_orchestrator, cost_estimator, budget_guard), `tasks/tasks_audio.py`+`tasks_post.py`, `media/` 나머지 |
| Phase 5 P1 | `routers/multishots.py`, `models_lib/adapters/` 추가 없음이 원칙(§4.5), 각 기능은 기존 폴더에 1파일씩 |
| Phase 6 P2 | `routers/` (bible, previz — exports는 기존 파일에 핸들러 추가), `services/` (export_edl, bible, previz, qc_content), `models/` (world_bible, camera_previz) — S3/CDN·클라우드 백업·멀티사용자는 제외/연기 (ADR-0010) |

**규칙: Phase에 없는 폴더·파일을 미리 만들지 않는다.** P1/P2 전담 파일은 해당 Phase 때 생성.

---

## 3. 컴포넌트 → 폴더 매핑

| 설계서 컴포넌트 (§2.2) | 폴더 | 경계 규칙 |
|---|---|---|
| 웹 UI | `apps/web` | AI 규격 지식 금지. 모델 선택은 ID 수준까지만 |
| API 게이트웨이 | `services/api/app` (routers+services) | 잡 등록 시 태스크 인자는 대상 행 ID 단일 (프롬프트 원문 중복 금지, ADR-0008) |
| 워커 | `services/api/app/tasks` | API와 같은 패키지. 큐 4종 고정 (`q.image`/`q.video`/`q.audio`/`q.post`) |
| 모델 추상화 | `services/api/app/models_lib` | Registry+Router+Adapters. 새 모델 ≠ 새 코드 (§4.5) |
| ffmpeg | `services/api/app/media` | 워커에서만 호출. API 라우터에서 직접 호출 금지 |
| PostgreSQL | `services/api/app/db` + `models/` | 마이그레이션은 Alembic만 |
| 파일시스템 | `services/api/app/files` | DB에는 프로젝트 루트 기준 **상대경로만** |

---

## 4. 백엔드 내부 구조

### 4.1 models/ — 도메인별 1파일 (17개)

`project.py`, `story.py` (stories·beats·episodes), `character.py` (characters·character_references·character_sheets),
`asset.py`, `style.py` (style_kits·category_packs), `production.py` (scenes·shots·shot_references),
`generation.py` (generations·generation_attempts·videos), `timeline.py` (timelines·timeline_clips),
`audio.py` (audio_generations·timeline_audio_clips), `subtitle.py` (tracks·cues·styles),
`qc.py` (qc_reports), `system.py` (models·model_providers·model_pricing·llm_calls·export_presets·prompt_snippets),
`multishot.py` (P1-1), `camera.py` (P1-2 camera_presets), `lora.py` (P1-4 lora_models),
`world_bible.py` (P2), `camera_previz.py` (P2).

### 4.2 schemas/ — models/와 1:1 대응

요청/응답 Pydantic을 도메인 파일에 함께 둔다. **리소스마다 별도 DTO 파일을 만들지 않는다.**

### 4.3 routers/ — 리소스별 1파일 (27개)

`projects`, `stories`, `characters`(+lora/train, +voice/clone), `assets`, `style_kits`(+extract),
`category_packs`, `scenes`(+camera-check), `generations` (프레임 생성·배치·리테이크·inpaint·upscale·outpaint·승인·잡 조회 — upscale/outpaint는 ADR-0003),
`videos`, `timelines`, `subtitles`(+apply-to-timeline), `audio`(+dialogue, +broll-suggest), `qc`, `costs`,
`models` (레지스트리 상태), `exports` (프리셋 목록 조회 — ADR-0004; 타임라인 XML/EDL 내보내기 — P2), `events` (SSE),
`files` (정적 파일 서빙), `health` (헬스체크),
`snippets` (P1-8), `camera_presets` (P1-2), `variations` (P1-3),
`multishots` (P1-1), `thumbnails` (P1-9), `episodes` (P1-10),
`bible` (P2 월드 바이블 — ADR-0010), `previz` (P2 카메라 프리비즈 — ADR-0010).

라우터는 **얇게**: 검증 → 서비스 호출 → 응답. 프롬프트 조립·비용 계산·상태 전이를 라우터에 직접 쓰지 않는다.

정적 파일 서빙: `GET /files/{image_path}` (routers/files.py → services/file_serve.py,
설계서 §5). DB의 상대경로를 요청 시점에 PROJECTS_ROOT 기준으로 해석하며,
path traversal·절대경로는 404. 로컬 전제라 CDN 없음.

### 4.4 services/ — 도메인 로직 (9개)

`prompt_assembly.py` (§7), `cost_estimator.py` (§9), `budget_guard.py` (D-28: 403 차단),
`approval.py` (D-13, D-26), `continuity.py` (D-15 폴백 체인), `qc_orchestrator.py` (§10),
`render.py` (타임라인 렌더), `episode_clone.py` (D-23 deep copy), `idempotency.py` (D-13.2 멱등키).
이후 단계에서 도메인별 서비스가 더해졌다. 잡 실행 소유권은 `job_execution.py`, 잃은 잡 복구는
`job_recovery.py`(ADR-0013), provider 입력 스냅샷은 `provider_input.py`(ADR-0014)가 맡는다.
전체 카드는 `docs/MODULES.md`.

### 4.5 models_lib/ — 모델 추상화

```
models_lib/
├── base.py          # ProviderAdapter 인터페이스 (설계서 §3.4) — 변경 금지급
├── registry.py      # DB 기반 레지스트리 조회·캐시
├── router.py        # 라우터 규칙 (설계서 §3.3)
├── seed.py          # 초기 모델 seed 로더
├── contract.py      # provider 입력 계약 (이미지 잡 payload 키, ADR-0014)
├── capabilities.py  # seed의 tasks/inputs 해석 + enqueue 사전 검사 (ADR-0014)
├── media_check.py   # 결과 Content-Type·바이트 상한·매직 바이트 검사 (ADR-0014)
└── adapters/
    ├── stub.py          # 테스트용 가짜 제공자 (Phase 0)
    ├── http_polling.py  # fal/Replicate/직접 API/ComfyUI 공용 (Phase 0). ComfyUI도 seed(comfyui.json)로 붙는다
    └── cli.py           # 구독 기반 CLI 에이전트 (Codex CLI image_gen 등, kind=cli)
```

전용 `comfyui.py`(REST+WS) 어댑터는 아직 없다. WebSocket 진행 이벤트가 필요해지면 그때 만든다.

**핵심 규칙: 새 모델 추가 ≠ 새 어댑터 파일.** HTTP 호환 모델은 `infra/seeds/models/*.json`에 1행 추가로 끝낸다.
새 어댑터는 "프로토콜이 다른 경우" (WebSocket·로컬 프로세스 등)에만 만든다.

### 4.6 media/ — ffmpeg·OCR 등 (워커 전용)

`ffmpeg_ops.py` (정규화·concat·믹싱·burn-in, 프레임 추출 `extract_frame` — 별도 `frame_extract.py`는 두지 않았다),
`thumbnails.py`, `thumbnail_text.py` (썸네일 타이틀 합성), `ocr.py` (PaddleOCR 계열 rapidocr, 한국어 인식 모델),
`loudness.py`, `subtitle_render.py` (한글 오버레이, D-17).
한글 렌더에는 CJK 폰트가 필요하다(이미지에 `fonts-noto-cjk`, 로컬 후보는 `defaults.CJK_FONT_CANDIDATES`).

### 4.7 tasks/ — 큐별 1파일 (4개)

`tasks_image.py` (q.image), `tasks_video.py` (q.video), `tasks_audio.py` (q.audio),
`tasks_post.py` (q.post: ffmpeg·썸네일·QC·LoRA는 q.image).
태스크 함수 시그니처: 대상 행 ID 단일 인자 고정 (ADR-0008) —
`run_generation(generation_id: str)`, `render_timeline(timeline_id: str)`,
`run_qc(qc_report_id: str)`. 인자에 프롬프트·파라미터 원문을 중복 전달하지 않고
DB를 진실 공급원으로 삼는다.

### 4.8a core/celery_app.py — 공유 Celery 인스턴스 (Phase 1)

`tasks/`와 `services/`가 서로를 import하지 않으면서 잡을 주고받기 위해 공유
인스턴스를 `core/`에 둔다 (core는 아무것도 import하지 않으므로 DAG가 깨지지
않는다). `dispatch_job(task_name, ...)`은 등록된 dispatcher가 있으면 직접
호출하고 (워커 프로세스·eager 테스트), 없으면 `send_task`로 Redis 경유
(API 프로세스 → 워커).

### 4.8b db/session.py — after-commit dispatch (Phase 1)

`dispatch_after_commit(db, fn)`: generation 행이 커밋된 **후에만** 잡을 큐에
넣는다. 커밋 전 enqueue 시 빠른 워커가 존재하지 않는 행을 읽는 레이스를
방지. 롤백 시 콜백은 버려진다. 콜백 하나가 예외를 내도 나머지는 모두 시도하고
로그만 남긴다. 놓친 전송은 API lifespan의 reconcile 루프가 다시 보낸다 (ADR-0013).

### 4.8d 잡 실행 소유권·복구 (ADR-0013)

워커는 잡 행을 `queued → running` 조건부 UPDATE로 claim하면서 실행 토큰(`exec_token`)과
lease를 받는다. 이후 모든 쓰기는 `id + exec_token + 허용 상태` 조건이고, heartbeat가 lease를 늘린다.
provider submit 전에 `submit_state=intent`를 commit하므로, 제출 여부가 불확실한 실패는
`PROVIDER_UNCERTAIN`으로 끝나고 **자동 재제출하지 않는다**. retry·cancel은 job_type별 태스크로 간다
(`job_execution.JOB_TASKS`). 지속 복구는 API lifespan의 주기 루프(`job_recovery.reconcile_once`,
`RECONCILE_ENABLED`: 미설정이면 켜짐, `SSE_BACKEND=memory`면 꺼짐)와 워커 `worker_ready` 1회 실행이다.
타임라인 렌더도 같은 토큰 조건을 쓰며, 결과는 시도별 불변 파일(`output_<token8>.mp4`)로 두고
postflight(인코딩·burn-in 검증)를 통과하면 `output_path`를 조건부 UPDATE로 바꿔 게시한다 (계획 D8).

### 4.8c app/seed.py — 통합 seed 진입점 (Phase 1)

`make seed` → `python -m app.seed`. 모델 레지스트리·카테고리 팩 seed를 한
진입점에서 순서대로 실행 (멱등).

### 4.9 files/ — 저장 규칙

`paths.py` (PROJECTS_ROOT 해결·상대경로 강제. symlink까지 resolve한 실제 위치가 루트 안에 있어야 한다),
`naming.py` (`{uuid}[_{batch}].{ext}`), `immutability.py` (덮어쓰기 방지 가드 — 하드링크/배타 생성이라
경쟁 중에도 기존 파일을 덮지 않음), `orphans.py` (고아 파일 주간 리포트, 자동삭제 금지).
워커 산출물은 실행 토큰 앞 8자를 붙인 시도별 이름(예: `{generation_id}_{batch}_{tag}.png`)으로 쓰고,
LoRA 가중치 파일명은 사용자 입력과 무관한 UUID(`lora/{lora_id}.safetensors`)다.

---

## 5. 프론트엔드 내부 구조

```
apps/web/app/
├── projects/                 # 목록
├── projects/[id]/            # 워크스페이스 셸 (탭 내비)
│   ├── story/                # 스토리·플롯·비트
│   ├── characters/           # 캐릭터 랩
│   ├── characters/[cid]/sheet/ # 캐릭터 시트
│   ├── board/                # 씬·샷 스토리보드
│   ├── review/               # 이미지 비교·승인·리테이크
│   ├── timeline/             # 비디오·타임라인 (자막은 내 탭으로 포함 — ADR-0002)
│   ├── qc/                   # QC 리포트
│   └── costs/                # 비용 대시보드
├── library/                  # 전역 에셋·캐릭터·스타일 키트
└── settings/models/          # 모델 레지스트리·어댑터 상태
```

- API 호출은 `lib/api-client.ts` 경유만. 컴포넌트에서 `fetch` 직접 호출 금지.
- SSE 구독은 `lib/sse.ts` 단일 훅. 화면마다 EventSource를 새로 만들지 않는다.
  서버는 이름 있는 이벤트(`event: job.progress` 등)를 보내므로 훅이 계약의 이벤트명마다 리스너를 단다.
  끊긴 동안의 이벤트는 재전송되지 않으므로, 재연결 시 `onResync`로 화면 상태를 다시 읽는다.
- 화면별 상태는 `stores/<화면>.ts`에 co-location. 전역 공유 상태는 최소화.
- 타입은 `packages/contracts`에서 생성된 것만 사용 (`make gen-types`). 수동 타입 중복 정의 금지.

---

## 6. 단일 진실 공급원 (SSOT) 목록

| 정보 | 정본 위치 | 파생물 |
|---|---|---|
| 에러 코드 18종 | `core/errors.py` (미러: `packages/contracts/errors.json`) | `apps/web/lib/contracts.gen.ts` (`make gen-types`, 검사 `make gen-types-check`) |
| SSE 이벤트 7종 (이름·payload 필드) | `packages/contracts/sse_events.json` | `apps/web/lib/contracts.gen.ts`. pytest autouse 훅이 테스트 중 발행된 SSE payload를 이 계약으로 검사 |
| 화면비·상태 enum | `packages/contracts/enums.*` | DB CHECK + TS |
| `[가정]` 기본값 | `core/defaults.py` (§13) | 전부 여기. 코드 내 하드코딩 금지 |
| 모델 단가 | `model_pricing` 테이블 | seed JSON |
| 프롬프트 조립 순서 | `services/prompt_assembly.py` | — |

---

## 7. 새 기능 추가 플로우

### 7.1 새 REST 엔드포인트

1. `schemas/<도메인>.py`에 요청/응답 스키마 추가 (새 파일 X)
2. `services/<도메인>.py`에 로직 작성 (라우터에 직접 X)
3. `routers/<리소스>.py`에 얇은 핸들러 추가
4. `tests/routers/test_<리소스>.py`에 1개 이상 케이스
5. enum·에러가 새로 필요하면 `packages/contracts`에 먼저 추가 → `make gen-types`

### 7.2 새 HTTP 모델 추가 (코드 변경 없음이 정상)

1. `infra/seeds/models/<provider>.json`에 1행 추가 (model_id·단가·capabilities·fallback 체인)
2. `make seed` → 설정 화면에서 상태 확인
3. 끝. 어댑터·라우터·서비스 파일 생성 금지

### 7.3 새 QC 검사 추가

1. `services/qc_orchestrator.py`의 `CHECKS` 레지스트리에 1행 등록 (개별 파일 `media/qc_checks/`는 두지 않는다)
2. 임계값은 `core/defaults.py`에 추가 (함수 내 하드코딩 금지)
3. `tests/`에 케이스 1개

### 7.4 새 프로토콜 어댑터 (예외적)

`models_lib/adapters/<프로토콜>.py` 1개 + `base.py` 인터페이스 준수 테스트.
"모델이 새로 나왔다"는 이유만으로 만들지 않는다 (§4.5 규칙).

---

## 8. 파일 생성 규칙 — 만들지 않는 것

| 금지 | 대신 |
|---|---|
| 모델별 개별 서비스 클래스 | 어댑터 1개 + seed 1행 |
| 리소스별·화면별 중복 DTO/타입 | 도메인별 1파일, TS는 코드젠 |
| 태스크 인자에 프롬프트·파라미터 원문 | 대상 행 ID 단일 (`{generation_id}`·`{timeline_id}`·`{qc_report_id}`), DB가 진실 공급원 (ADR-0008) |
| DB에 절대경로 저장 | 상대경로 + `PROJECTS_ROOT` |
| 산출물 덮어쓰기 | 새 파일 + 새 잡 행 (불변) |
| 라우터에 도메인 로직 | `services/`로 분리 |
| API 라우터에서 ffmpeg 직접 호출 | 워커 태스크로만 |
| 화면마다 EventSource 생성 | `lib/sse.ts` 단일 훅 |
| 컴포넌트에서 직접 fetch | `lib/api-client.ts` |
| 새 큐 (`q.*` 추가) | 4종 고정. 동시성은 설정으로만 조정 |
| 임계값·단가·매직넘버 하드코딩 | `core/defaults.py` 또는 DB |
| 잡다한 `utils.py` | 도메인 폴더 (`files/`, `media/` 등)에 둠 |
| Phase에 없는 파일 미리 만들기 | 해당 Phase 때 생성 (§2 표) |
| 인증·멀티유저·결제 코드 | 스코프 제외 (설계서 §1.2, P2) |

---

## 9. 명명·환경변수 규칙

- 파일명: `snake_case.py` / 컴포넌트 `PascalCase.tsx` / 라우트 `kebab-case`
- DB 테이블·컬럼: 설계서 §4와 철자 일치 (임의 변경 금지)
- API 경로: `/api/v1/<복수 리소스>` (설계서 §8.1 그대로)
- Celery 태스크명: `<큐>.<동작>` (예: `q.image.run_generation`)
- 환경변수: `PROJECTS_ROOT`, `DATABASE_URL`, `REDIS_URL`, `COMFYUI_URL` 등 — 전부 `.env.example`에 문서화. 코드에 기본 URL 하드코딩 금지

## 10. DB·시드 규칙

- 스키마 변경은 Alembic 마이그레이션만. `models/` 직접 수정 후 `make migrate` (autogenerate → 검토 → 적용)
- seed는 `infra/seeds/`에 JSON/SQL로 버전 관리. `make seed`는 멱등 (중복 실행해도 같은 결과)
- seed 종류: `models/` (레지스트리), `category_packs/` (3종), `export_presets/`, `prompt_snippets/`

## 11. 테스트 최소 규칙

- 어댑터: `base.py` 인터페이스 준수 테스트 (stub으로 전 어댑터 실행)
- 상태머신: 전이표 (§6) 단위 테스트 — 허용되지 않은 전이는 반드시 실패 케이스로
- 프롬프트 조립: 절단 정책 (D-30) 케이스 + 슬롯 순서(주제→배경→스타일/매체→조명→카메라/렌즈→색감/분위기→파라미터, ADR-0011 §1) + 네거티브 라이브러리 기본 세트 적용 케이스
- API: 라우터별 스모크 1개 이상
- 목표: Phase 완료 조건(§2 표)을 테스트로 표현. 커버리지 수치 목표는 두지 않는다
- DB 격리: 테스트는 서비스 `DATABASE_URL`을 쓰지 않는다(`tests/conftest.py`가 SQLite로 덮어씀).
  Postgres 테스트는 `TEST_DATABASE_URL`을 명시했을 때만 돌고, DB 이름이 `test`로 끝나지 않으면 아무것도 지우지 않고 실패한다
- 외부 도구: ffmpeg·CJK 폰트가 필요한 테스트는 `needs_ffmpeg`/`needs_cjk_font` 마커를 단다. 없으면 보이는 skip,
  `REQUIRE_MEDIA_TOOLS=1`(CI·Docker 이미지·`make test`)이면 실패
- lint: ruff 최소 규칙(문법 오류·정의되지 않은 이름, `services/api/pyproject.toml`). CI는 `.github/workflows/ci.yml`

## 12. 로컬 실행

```bash
make dev      # docker-compose up (pg+redis+api+worker) & web dev 서버
make migrate  # alembic upgrade head
make seed     # 레지스트리·팩 seed (멱등)
make gen-types# contracts → TS 타입 생성 (apps/web/lib/contracts.gen.ts)
make gen-types-check  # 생성물이 contracts JSON과 일치하는지 검사 (불일치 시 nonzero)
make test     # docker api 컨테이너에서 pytest(REQUIRE_MEDIA_TOOLS=1, SQLite) + web typecheck
make verify-cjk    # 이미지 안에서 한글 burn-in 자막·썸네일 OCR 검증
make lint-imports  # §15 의존성 방향 위반 검사 (순환 import·역방향 의존 차단)
```

API 이미지는 레포 루트를 빌드 컨텍스트로 쓴다(`docker build -f services/api/Dockerfile .`, 이미지 안 `/repo/services/api`).
compose는 API를 `127.0.0.1`에만 게시한다. 기존 설치를 업데이트하면 `make migrate`·`make seed`를 다시 실행한다.

`projects/`, `library/`, `infra/backups/`는 git 제외 (런타임 산출물).

---

## 13. `[가정]` 기본값 확정 목록

**HW 확정일: 2026-09-30 — #14는 ADR-0001(증분 백업)로 변경, 나머지 14종은 원안대로 확정.**
**이후 변경: #15는 감사 수정 계획 D2(`docs/plans/2026-11-09-audit-roadmap-fixes/plan.md`)로 DB 기록·24시간 TTL로 바뀌었다.**
확정값은 구현 시 `app/core/defaults.py`에 그대로 반영한다.

설계서 v1.0에서 `[가정]`으로 표시된 항목 중 주요 수치. `core/defaults.py` 한 곳에 모아 구현한다.

| # | 항목 | 현재 기본값 | HW 결정 |
|---|---|---|---|
| 1 | 재시도 횟수·간격 | 최대 3회, 5→10→20초 지수 백오프+지터 | 자동 재시도는 provider에 확실히 전송되지 않은 실패만. submit 이후 결과가 불확실하면 `PROVIDER_UNCERTAIN`, 자동 재제출 없음 (ADR-0013, 계획 D7) |
| 2 | 큐 상한·포화 응답 | 100개 초과 시 429, retry_after 60초 | |
| 3 | 워커 동시성 | image 4 / video 2 / audio 2 / post 2 | |
| 4 | 출력 정규화 | 1920×1080·24fps·H.264·AAC 48kHz, 레터박스 | |
| 5 | 예산 | 경고 80%·차단 100%, 초과 시 신규 잡 403 | 판정 기준은 지출 + 예약(대기·실행 중 잡, 불확실 시도 보류, pending LLM). 프로젝트 행 잠금 아래 같은 트랜잭션에서 확인·예약 (계획 D1) |
| 6 | 자막 QC 임계 | 큐 1~7초, CPS 20, 겹침 fail | |
| 7 | 연속성 QC 임계 | 첫 프레임 SSIM 0.90, 샷 간 0.85 | |
| 8 | 오디오 QC 임계 | 피크 -1 dBTP, LUFS -16±3 | |
| 9 | 립싱크 허용 | ±10% 속도 조정, 0.5초 초과 시 warn | |
| 10 | 샷당 레퍼런스 상한 | 4장 (모델 capability 우선) | |
| 11 | 프롬프트 길이 상한 | 2000자 (초과 시 섹션 절단). 단, 모델별 토큰 정책이 우선 — SD 1.5는 77토큰 하드캡 (ADR-0011 §2) | |
| 12 | OCR | PaddleOCR, 신뢰도 0.85/0.5, 클립 1초 샘플링 | |
| 13 | 저장 포맷 | 생성 png, 썸네일 jpg(q85), TTS wav, BGM mp3 | |
| 14 | 백업 | 파일: 매일 rsync 하드링크 스냅샷 30세대 / DB 덤프: 매일 14일 보관 (ADR-0001) | |
| 15 | 멱등키 | `Idempotency-Key` 헤더, DB `idempotency_records`(UNIQUE method+실제 경로+key), `IDEMPOTENCY_TTL_SEC` 86400초. 같은 본문 → 저장된 응답 재생, 다른 본문 → 409 `IDEMPOTENCY_CONFLICT` | 계획 D2로 변경 (원안: 60초 메모리 윈도우) |

---

## 14. Public / Private 경계 (OSS 전제)

전제: 추후 OSS 공개를 가정하고, **첫 커밋부터 public repo에 올라갈 수 있는 상태**를 유지한다.
private은 오직 gitignore된 경로에만 존재한다.

| 구분 | 대상 | 위치 |
|---|---|---|
| Public | 전체 코드, docs, seed 템플릿, docker-compose, `.env.example`, Makefile | repo |
| Private (git 제외) | API 키·시크릿 (`.env`), 개인 프로젝트 산출물 (`projects/`), 전역 라이브러리 (`library/`), 백업 (`infra/backups/`), 개인 seed 오버라이드 (`infra/seeds/local/`), 모델 가중치 (볼륨 마운트) | 로컬 전용 |

### 경계 규칙

1. **시크릿은 `.env`에서만 읽는다.** `.env.example`에는 변수 이름+설명만. 코드에 키 기본값을 넣지 않는다.
2. **private 경로는 `files/paths.py`를 통해서만 접근**한다. `projects/`, `library/`를 코드 어디에서도 직접 import·참조하지 않는다.
3. **설정은 3층이다.** `core/defaults.py` (공개 기본값) < 환경변수 (비공개 오버라이드) < DB (사용자 설정). 층을 건너뛰지 않는다.
4. **seed는 템플릿만 공개.** 개인용 모델 키·프리셋은 `infra/seeds/local/` (git 제외)에 오버라이드한다.
5. **로그·에러 메시지에 시크릿·절대경로·개인 파일명을 찍지 않는다.** (이슈·로그 공개 시 유출 방지)
6. **새 의존성 추가 시 라이선스를 `docs/licenses.md`에 기록**한다. 목표 라이선스 MIT 유지가 깨지는 의존성(GPL 결합 등)은 ADR로 결정한다.
7. ComfyUI·모델 가중치는 레포에 포함하지 않는다 (볼륨 마운트 + 문서 안내).

### .gitignore (필수)

```
.env
projects/
library/
infra/backups/
infra/seeds/local/
*.safetensors
*.pt
__pycache__/ / *.pyc
```

## 15. 모듈 의존성 방향 (스파게티 방지)

레이어는 아래 방향으로만 의존한다. **역방향·건너뛰기·순환 import 금지.**

```
routers ──→ services ──→ models
   │             ├────→ models_lib ──→ db
   │             ├────→ files
   │             └────→ media ──→ files
   └────→ schemas        (routers의 DB 직접 접근 금지)
tasks ──→ services, models_lib, media, files   (routers import 금지)
core ←── 모두가 의존 (core는 아무것도 import하지 않음)
```

| 규칙 | 이유 |
|---|---|
| 라우터는 services 경유, DB 직접 접근 금지 | "얇은 라우터" 강제. 도메인 로직이 라우터에 스며들면 스파게티 시작 |
| routers → db 예외: `db.session.get_db`만 허용 | FastAPI `Depends` 배선용. 그 외 db import (Base·Session 등)는 위반. `make lint-imports`가 강제 |
| media는 services를 import하지 않음 | 미디어 처리가 도메인 로직을 알게 되면 순환 의존 발생 |
| models_lib는 도메인 services를 모름 | 모델 추상화가 특정 기능에 결합되면 새 모델 추가가 어려워짐 |
| core는 어떤 모듈도 import하지 않음 | 기반이 흔들리면 전체가 흔들림 |
| web은 `lib/api-client.ts` 경유만 | API 규격 변경점이 한 곳으로 모임 |

위반이 발견되면 리팩터링하거나, 불가피하면 ADR로 기록한다 (규칙을 조용히 깨지 않는다).

---

*본 문서 확정일: 2026-10-05. 설계서 v1.0 + 스펙 ai-2 v3 기준.*
*구현 중 구조 변경이 필요하면 본 문서를 먼저 고치고 코드를 고친다 (문서→코드 순서).*
