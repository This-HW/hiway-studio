# 에이전트 진입 가이드

> 독자: 이 프로젝트를 처음 보는 인간 또는 AI 에이전트.
> 목표: **10분 안에 전체 구조를 파악하고, 안전하게 첫 수정을 할 수 있는 상태**가 된다.
> 전제: 시스템 설계서 v1.0(논리), PROJECT_ARCHITECTURE.md(배치), MODULES.md(모듈 카드)가 있다.

---

## 1. 10분 파악 코스 (이 순서대로 읽는다)

1. **본 문서 §2 매핑표** (2분) — "내가 고치려는 게 어디에 있는지"를 찾는다.
2. **PROJECT_ARCHITECTURE.md §8 금지 목록** (2분) — 하지 말아야 할 것을 먼저 안다.
3. **MODULES.md에서 해당 모듈 카드 1개** (3분) — 책임·불변식·확장 절차를 읽는다.
4. **시스템 설계서 해당 §** (3분) — 논리 근거를 확인한다.
5. 수정 후 **§4 체크리스트**로 자가 검증한다.

## 2. 설계서 § → 코드 매핑표

| 설계서 | 코드 위치 | 비고 |
|---|---|---|
| §2 아키텍처 | `services/api/app/`, `apps/web/` | 컴포넌트→폴더 매핑은 PROJECT_ARCHITECTURE §3 |
| §3 모델 추상화 | `app/models_lib/` | 인터페이스 `base.py`는 함부로 바꾸지 않는다 |
| §4 데이터 모델 | `app/models/<도메인>.py` | 테이블 철자는 설계서와 일치 |
| §5 핵심 플로우 F-01~F-15 | `app/services/`, `app/tasks/` | 플로우별 담당 서비스는 MODULES.md 참조 |
| §6 상태머신 5종 | `app/services/*` 전이 함수 + `tests/` 전이 테스트 | 허용되지 않은 전이 추가 금지 |
| §7 프롬프트 조립 | `app/services/prompt_assembly.py` | 조립 순서 변경 시 MODULES 카드도 갱신 |
| §8.1 REST 60여 개 | `app/routers/<리소스>.py` | 라우터는 얇게 (검증→서비스 호출→응답) |
| §8.2 SSE 7종 | `app/core/sse_bridge.py` | 이벤트명은 `packages/contracts`가 정본 |
| §8.4 오류 18종 (`packages/contracts/errors.json`) | `app/core/errors.py` | 새 오류 추가 시 contracts에 먼저 등록 |
| §9 비용 | `app/services/cost_estimator.py`, `budget_guard.py` | 단가는 DB, 기본값은 `core/defaults.py` |
| §10 QC | `app/services/qc_orchestrator.py` | 새 검사는 `CHECKS` 레지스트리에 1행 + 임계값은 `core/defaults.py` (개별 파일 `media/qc_checks/`는 두지 않는다 — MODULES.md) |
| §11 파일 저장 | `app/files/` | 상대경로 강제, 불변 가드 |
| §12 재시도·폴백·큐 | `app/tasks/`, `app/models_lib/router.py` | 수치 변경은 `core/defaults.py`에서만 |
| §13 `[가정]` 목록 | `app/core/defaults.py` | PROJECT_ARCHITECTURE §13 체크리스트로 HW 확정 |
| §14 설계 결정 D-01~D-32 | 설계서 본문 + `docs/adr/` | **새 결정은 ADR로 기록** (§5) |

## 3. 작업 유형별 가이드

| 작업 | 읽을 것 | 만들/고칠 것 |
|---|---|---|
| 버그 수정 | MODULES 해당 카드의 "함정" | 원인 모듈 1곳만. 여러 모듈을 동시에 뜯어고치지 않는다 |
| 새 REST 엔드포인트 | PROJECT_ARCHITECTURE §7.1 | schemas→services→routers→tests 순서 |
| 새 HTTP 모델 추가 | PROJECT_ARCHITECTURE §7.2 | `infra/seeds/models/*.json` 1행. 코드 생성 금지 |
| 새 QC 검사 | PROJECT_ARCHITECTURE §7.3 | `services/qc_orchestrator.py`의 `CHECKS`에 1행 + `core/defaults.py`에 임계값 1행 (개별 파일 `media/qc_checks/`는 두지 않는다) |
| DB 스키마 변경 | 설계서 §4 + `app/models/` | Alembic 마이그레이션 필수. `make migrate` |
| 새 어댑터 (예외적) | `models_lib/base.py` 인터페이스 | 인터페이스 준수 테스트 먼저 |
| 프론트 화면 | `apps/web` 구조 (§6 매핑) | `lib/api-client.ts` 경유, 타입은 코드젠 |

## 4. 수정 전 체크리스트

- [ ] 건드리는 모듈의 MODULES 카드를 읽었는가
- [ ] 불변식(immutable 출력, 상대경로, 단일 진실 공급원)을 깨지 않는가
- [ ] 금지 목록(ARCHITECTURE §8)에 걸리지 않는가
- [ ] `[가정]` 수치를 코드에 하드코딩하지 않았는가 (`defaults.py` 사용)
- [ ] 상태 전이를 추가했다면 테스트의 실패 케이스도 추가했는가
- [ ] 새 설계 결정이 생겼다면 ADR을 썼는가 (§5)

## 5. ADR (설계 결정 기록) 규칙

- 위치: `docs/adr/NNNN-제목.md` (템플릿: `docs/adr/0000-template.md`)
- 작성 시점: D-01~D-32급의 "왜 이렇게 했는지"가 생길 때마다. 코드보다 먼저 쓴다.
- 기존 결정 변경 시: ADR을 새로 쓰고 기존 ADR에 "superseded by NNNN"을 기록한다. 옛날 ADR을 고쳐 쓰지 않는다.

## 6. 모듈 README 규칙

- `app/` 아래 모든 모듈 디렉토리(`services/`, `models_lib/`, `media/`, `tasks/`, `core/`, `files/`, `models/`, `routers/`)는 `README.md`를 갖는다.
- README 형식 = MODULES.md의 카드 형식을 그대로 따른다 (목적·책임·인터페이스·불변식·의존성·확장·함정).
- 모듈을 고치면 그 모듈의 README도 함께 고친다. 문서→코드 순서.

## 7. 용어 고정

- "모델 추상화 계층" = Registry + Router + Provider Adapters. "드라이버"라는 말은 쓰지 않는다. 프로바이더 kind 3종: `api`(HTTP submit+폴링), `local`(로컬 프로세스/소켓), `cli`(구독 기반 CLI 에이전트 — Codex CLI `image_gen` 등).
- "승인" = 사용자의 명시적 버튼 액션 (`approved_frame_generation_id` 기록). 자동 승인은 없다.
- "연속 생성" = 이전 샷 마지막 프레임 + 상태 전달 (P0-12, 단일 기능).
- "프로젝트" 간 공유되는 것은 `library/`의 전역 에셋·캐릭터·스타일 키트뿐이다.
- "스타일 바이블" = 잠긴 스타일 키트의 팔레트·조명·렌즈·그레이드 고정 토큰 — 모든 프롬프트에 verbatim 주입된다 (ADR-0011 §13).
- "CharacterPack" = 캐릭터의 canonical 레퍼런스 이미지 + 외형 고정 문구(identity clause). 씬 프롬프트에서 얼굴 재묘사 금지 (ADR-0011 §4).
- "GenerationSnapshot" = generation별 모델/시드/샘플러/CFG/스타일 토큰/네거티브/레퍼런스 ID 기록 — 재현성의 단위 (ADR-0011 §14).
- "카메라 무브 enum" = 영상 프롬프트용 명명된 카메라 움직임 어휘 (22종, `contracts/enums.json`의 `camera_movement`). 자유 텍스트 카메라 묘사는 쓰지 않는다 (ADR-0011 §8).
- "연속성 앵커" = 멀티샷 연속 생성 시 매 샷마다 명시하는 5종 (정체성·의상·이동 방향·카메라 거동·환경 마커 2개) (ADR-0011 §9).
