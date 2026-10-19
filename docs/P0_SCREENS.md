# P0 화면 설계서

> 대상: P0-1~P0-19 (P1·P2 제외). 시스템 설계서 v1.0 + `PROJECT_ARCHITECTURE.md` 기준.
> 본 문서의 API·SSE·에러 코드는 설계서 §8의 실존 항목만 사용한다. 없는 것은 지어내지 않고 ADR 초안으로 기록한다.
> API 경로 표기는 설계서 §8.1을 따르며, 실제 호출 시 `/api/v1` prefix를 붙인다.
> `[가정]` 확정값(2026-09-30, #14는 ADR-0001)을 반영한다.

---

## 0. 공통 사항

### 0.1 워크스페이스 셸과 전역 내비

- `app/projects/`: 프로젝트 목록. 카드 그리드.
- `app/projects/[id]/`: 워크스페이스 셸. 좌측 탭 내비 + 상단 헤더 + 메인 영역.
  탭 구성: **스토리 / 캐릭터 / 보드 / 리뷰 / 타임라인 / QC·출력 / 비용 / 설정**
  (자막은 타임라인 화면 내 탭으로 포함 — ADR-0002. 독립 탭을 두지 않는다.)
- `app/library/`: 전역 라이브러리 (프로젝트 바깥).
- `app/settings/models/`: 모델 레지스트리 조회 화면. 본 문서 범위 밖이며, 각 화면의 모델 선택 드롭다운의 데이터원으로만 사용한다 (`GET /models?modality=`).
- 탭 전환은 클라이언트 라우팅. 프로젝트 미선택 상태에서 워크스페이스 탭 URL 직접 접근 시 프로젝트 목록으로 리다이렉트한다.

### 0.2 잡 진행률 뱃지·SSE 연결 상태

- 헤더 우측 잡 뱃지: `GET /jobs?status=queued`로 초기 개수를 받고, 이후 SSE `job.progress`로 증감한다.
  클릭 시 드롭다운: 진행 중 잡 목록 (`job_type`, `progress_pct`, `stage`, `eta_sec?`).
  각 행 클릭 → 해당 잡이 속한 화면으로 이동. 행 내 취소 버튼 → `DELETE /jobs/{id}`.
- SSE 연결 상태: 헤더에 상태 점 표시 (녹색=연결됨, 황색=재연결 중, 적색=끊김).
  재연결 시 `Last-Event-ID`로 이어받고, 끊겼던 동안의 상태는 `GET /jobs`로 보정한다.
- SSE 구독은 `lib/sse.ts` 단일 훅만 사용한다. 화면마다 EventSource를 생성하지 않는다.

### 0.3 비용 표시 규칙

- 통화는 USD 단일. 형식: 소수점 2자리. 예상 비용은 `~` 접두사 + "(예상)" 라벨, 실제 비용은 접두사 없이 "(실제)" 라벨.
  예: `~$1.24 (예상)` / `$1.18 (실제)`. 단가 0(stub 어댑터)은 `$0.00`.
- 모든 생성 버튼 옆에 예상 비용을 표시한다. 생성 API의 `202` 응답에 `estimated_cost_usd`가 포함되므로,
  버튼 클릭 전에 미리보기(모델·배치 수 기준 추정치)를 보여주고, 응답 값으로 확정 표시한다.
- 프로젝트 예산(`monthly_budget_usd`): 설정 탭과 비용 화면에 진행률 바로 표시.
  80% 도달 시 황색 + SSE `cost.alert(level='warn')` 토스트. 100% 초과 시 적색 + 모든 신규 생성 버튼 비활성화
  (서버도 `403 BUDGET_EXCEEDED`로 차단한다).
- 추정치와 실제치 차이가 20%를 초과하면 해당 잡 상세에 차이 배지를 표시한다 (§9.2).

### 0.4 에러·확인 다이얼로그 규칙

| 상황 | UI 처리 |
|---|---|
| `409 APPROVAL_REQUIRED` | "승인이 필요합니다" 모달 + 해당 샷의 리뷰 화면으로 이동하는 버튼. 경고 후 진행이 아니라 차단이므로 우회 버튼을 두지 않는다. |
| `409 QC_FAILED` | 실패 항목 요약과 함께 리포트 링크 표시. 렌더 버튼 비활성화. |
| `409 RENDER_LOCKED` | "렌더가 끝난 뒤 수정할 수 있습니다" 안내. 타임라인 편집 UI 전체를 읽기 전용으로 전환한다. |
| `403 BUDGET_EXCEEDED` | 예산 상향 유도 문구 + 설정 탭 링크. |
| `422 SUBTITLE_OVERLAP` | 겹치는 큐 구간을 적색 하이라이트하고 저장을 막는다. |
| `429 QUEUE_FULL` | `retry_after_sec` 카운트다운 표시 후 재시도 버튼 활성화. |
| 잡 실패 (`job.failed`) | `error_code` 한글 메시지 + `retryable=true`면 "재시도" 버튼 (`POST /jobs/{id}/retry`). 폴백 체인이 있으면 시도 내역을 함께 보여준다. |
| 삭제 (프로젝트·씬·샷·캐릭터·에셋) | 확인 모달. 프로젝트 삭제는 `?confirm=true` + 제목 직접 입력 확인 (디렉토리 영구 삭제이므로). |
| 생성 버튼 | 요청 중 비활성화 + `Idempotency-Key` 헤더 전송 (더블클릭 중복 방지). 60초 윈도우. |

### 0.5 자막·QC·비용 일관 규칙

- 자막: 사이드카(트랙·큐)가 정본이다. 자막 편집 UI는 타임라인 화면에만 둔다. burn-in은 최종 렌더 시점에만 수행된다.
- QC: 타임라인(`clip_order`·오디오·자막)을 수정하면 기존 리포트는 무효가 된다.
  무효 리포트로 렌더 시도 시 `details.reason='stale'` 안내 + "QC 다시 실행" 버튼.
- 비용: 모든 화면의 집계 기준은 `generations` + `audio_generations` + `llm_calls`의 `*_cost_usd` 합산으로 통일한다.

---

## 1. 프로젝트 (목록 + 워크스페이스 셸)

**목적**: 프로젝트의 생성·선택·복제·아카이브·삭제와, 프로젝트 수준 설정(예산·기본 모델·스타일 키트·카테고리 팩)을 관리한다. 모든 작업의 진입점이다. (P0-1, P0-19)

**라우트**: `app/projects/`, `app/projects/[id]/`

**레이아웃**
- 목록: 상단 헤더(타이틀 + "새 프로젝트" 버튼 + 아카이브 보기 토글) / 메인 카드 그리드.
- 셸: 좌측 탭 내비(0.1) / 상단 헤더(프로젝트 선택기·잡 뱃지·SSE 상태·당월 비용 요약) / 메인 탭 영역.

**주요 컴포넌트·인터랙션**
- 프로젝트 카드: 대표 썸네일(첫 씬 첫 샷의 승인 프레임, 없으면 플레이스홀더), 제목, 화면비·카테고리 팩 뱃지,
  샷 수 / 승인된 샷 수 / 클립 수, 당월 비용(`~$` 예상+`$` 실제 합산). 클릭 → 워크스페이스 진입.
- 생성 다이얼로그: `title`, `aspect_ratio`(enum: `16:9, 9:16, 1:1, 2.39:1, 4:5`, 기본 `16:9`),
  `category_pack_code`(선택, `GET /category-packs` 목록), `default_models`(선택).
  카테고리 팩 선택 시 적용되는 기본값(예: 지식·정보형 → 자막 `dual`)을 미리보기로 보여준다.
- 설정 탭: 제목 수정, `monthly_budget_usd`, 기본 모델 지정, 화면비(생성 후 변경 시 경고 — 기존 샷과 불일치 가능),
  스타일 키트 관리(목록·생성·복제·잠금). 잠긴 키트는 직접 수정 불가 — 변경 시 복제로 새 버전 생성 안내.
- 스타일 키트 행: 이름·버전·잠금 상태. 잠긴 키트는 "복제 후 수정" 버튼만 (`409 STYLE_KIT_LOCKED` 방지).
  수정 시 `version` 자동 증가 + "이 키트를 쓰는 샷이 구식으로 표시될 수 있습니다" 안내 (D-31).
- 아카이브: `POST /projects/{id}/archive` → zip 생성 중 SSE `job.progress` → 완료 후 목록에서 "아카이브됨"으로 이동.
  복제: `POST /projects/{id}/duplicate` → deep copy (F-01, D-23).

**상태 처리**
- 빈 상태: "첫 프로젝트를 만드세요" + 생성 다이얼로그 바로가기.
- 로딩: 카드 스켈레톤. 아카이브·복제는 잡 뱃지로 진행률 표시.
- 에러: 삭제 실패 시 사유 표시. 프로젝트 삭제는 제목 입력 확인을 거쳐 `?confirm=true`.

**API·SSE 매핑**

| 화면 동작 | API / SSE |
|---|---|
| 목록·생성 | `GET /projects`, `POST /projects` |
| 상세·수정·삭제 | `GET /projects/{id}`, `PATCH /projects/{id}`, `DELETE /projects/{id}?confirm=true` |
| 아카이브·복제 | `POST /projects/{id}/archive`, `POST /projects/{id}/duplicate` |
| 스타일 키트 | `GET/POST /projects/{id}/style-kits`, `PATCH /style-kits/{id}`, `POST /style-kits/{id}/lock`, `POST /style-kits/{id}/duplicate` |
| 카테고리 팩 목록 | `GET /category-packs` |
| 진행률 | `job.progress`, `job.completed`, `job.failed` |

**관련 플로우·상태머신**: F-01. 프로젝트·스타일 키트에는 상태머신이 없다.

**불변식·주의**
- 프로젝트 삭제는 파일 디렉토리까지 영구 삭제다. 휴지통이 없으므로 확인 절차를 생략하지 않는다.
- 카테고리 팩은 생성 시점에만 지정한다. 생성 후 변경 UI는 두지 않는다 (pipeline 기본값이 이미 적용된 뒤 바꾸면 혼란).

---

## 2. Story/Plot (스토리·플롯 편집기)

**목적**: 로그라인 → 시놉시스 → 비트 확정을 한 화면에서 끝내고, 스크립트 임포트로 씬 초안을 만든다. (P0-2, P0-3)

**라우트**: `app/projects/[id]/story/`

**레이아웃**
- 좌측: 스토리 카드 (로그라인·시놉시스 편집 + "LLM 생성" 버튼).
- 중앙: 비트 타임라인 (막 구조 순서 리스트, `order_index`).
- 우측: 비트 상세 (요약 편집 + 연결된 씬 목록 + "씬으로 분해" 이동 버튼).

**주요 컴포넌트·인터랙션**
- "시놉시스 생성" / "비트 생성": 모델 선택 드롭다운(`GET /models?modality=text`) + 예상 비용(`per_1k_tokens` 기준) 표시 →
  `POST /projects/{id}/story/generate-synopsis`, `POST .../generate-beats` → `202` → SSE `job.completed` 후 에디터에 삽입.
  생성 결과는 항상 "초안"으로 들어가며, 사용자가 확정 버튼을 눌러야 저장된다 (`PUT /projects/{id}/story`).
  `llm_calls`에 프롬프트 템플릿+버전이 기록됨을 UI에 노출하지 않는다 (내부 기록).
- 비트 CRUD: 추가·수정·삭제. 삭제 시 `order_index` 재정렬은 서버가 수행하므로 UI는 목록만 갱신한다.
- 비트-씬 연결: 각 비트 행에 연결된 씬 칩 표시. 씬은 보드 화면에서 생성되며, 여기서는 `beat_id` 연결만 변경한다
  (씬 상세의 beat 선택과 동일한 필드).
- 스크립트 임포트: 파일 드롭존 (fountain/fdx/txt) → `POST /projects/{id}/story/import` →
  `{scenes_created}` 토스트 + "보드에서 확인" 버튼. `beat_id` 매핑은 비워지므로 "비트에 연결해 주세요" 안내를 남긴다.
- 카테고리 팩 뱃지: 프로젝트에 적용된 팩과 그 기본값(자막 모드 등)을 읽기 전용으로 표시.

**상태 처리**
- 빈 상태: 로그라인 입력 유도 ("로그라인 한 줄이면 시작할 수 있습니다").
- 로딩: LLM 생성 중에는 에디터에 "생성 중…" 플레이스홀더 + 잡 뱃지. 취소 가능 (`DELETE /jobs/{id}`).
- 에러: `422 MODERATION_BLOCKED` 시 "제공자 정책으로 차단됨" 안내 + 프롬프트 수정 유도.

**API·SSE 매핑**

| 화면 동작 | API / SSE |
|---|---|
| 스토리 조회·저장 | `GET/PUT /projects/{id}/story` |
| LLM 생성 | `POST /projects/{id}/story/generate-synopsis`, `POST /projects/{id}/story/generate-beats` |
| 비트 관리 | `GET/POST /stories/{story_id}/beats`, `PATCH/DELETE /beats/{id}` |
| 스크립트 임포트 | `POST /projects/{id}/story/import` |
| 진행률 | `job.progress`, `job.completed`, `job.failed` |

**관련 플로우·상태머신**: F-02. 스토리·비트에는 상태머신이 없다 (상태 없음).

**불변식·주의**
- 비트 삭제는 연결된 씬을 삭제하지 않는다. 씬의 `beat_id`만 null이 되며, 보드에서 "미지정 씬"으로 보인다.

---

## 3. Character Lab (캐릭터 생성·관리)

**목적**: 캐릭터 엔티티를 만들고 마스터 초상을 생성·업로드하며, 프로젝트 간 재사용을 관리한다. (P0-4)

**라우트**: `app/projects/[id]/characters/`

**레이아웃**
- 상단: 스코프 탭 (내 프로젝트 / 전역 / 전체) + "새 캐릭터" 버튼.
- 메인: 캐릭터 카드 그리드 (마스터 썸네일·이름·`version`·사용 샷 수).
- 우측(선택 시): 캐릭터 상세 패널 (레퍼런스·의상 탭).

**주요 컴포넌트·인터랙션**
- 생성 다이얼로그: `name`, `description`, `appearance_prompt`(외형 앵커), `consistency_method`(기본 `hybrid`).
  마스터 이미지는 "AI 생성" 또는 "업로드" 중 선택.
  - AI 생성: `POST /characters/{id}/master` (`{prompt?, model_id?}` + 예상 비용) → `202` → SSE로 썸네일 갱신.
    재생성 시 새 파일(`master_{job8}.png`) + 새 레퍼런스 행이 primary가 되고, 기존 primary는 강등된다 (ADR-0005).
  - 업로드: `POST /assets/upload` 후 `POST /characters/{id}/references`로 `source='uploaded'` 등록.
- 상세 패널:
  - 레퍼런스 탭: `role` 태그(마스터·표정·의상 등)별 썸네일, `is_primary` 토글 (`PATCH /references/{id}`),
    삭제. 마스터 교체 시 이전 마스터는 히스토리로 유지된다 (불변 규칙).
  - 의상 탭: `GET/POST /characters/{id}/wardrobe` 목록·추가.
- 캐릭터 수정: 외형 관련 필드(`appearance_prompt` 등) 변경 시 확인 모달 —
  "외형이 바뀌면 `version`이 증가하고 이 캐릭터를 쓰는 샷에 '구식' 배지가 붙습니다" (D-31).
  `PATCH /characters/{id}` 응답 후 의존 샷 수를 표시한다.
- 전역 스코프: `?scope=global` 캐릭터는 읽기 전용 카드 + "상세 보기". 프로젝트로 "가져오기" 버튼은 두지 않는다.
  전역 캐릭터는 샷 레퍼런스 선택 시 `?scope=all`로 함께 노출되어 바로 쓸 수 있다.

**상태 처리**
- 빈 상태: "첫 캐릭터를 만드세요" + 외형 앵커 작성 팁.
- 로딩: 마스터 생성 중 카드에 프로그레스 오버레이 + 취소 버튼.
- 에러: 마스터 생성 실패 시 `error_code` 한글 메시지 + 재시도.

**API·SSE 매핑**

| 화면 동작 | API / SSE |
|---|---|
| 목록·생성 | `GET/POST /projects/{id}/characters` (`?scope=project\|global\|all`) |
| 상세·수정·삭제 | `GET/PATCH/DELETE /characters/{id}` |
| 마스터 초상 생성 | `POST /characters/{id}/master` |
| 레퍼런스 | `GET/POST /characters/{id}/references`, `PATCH/DELETE /references/{id}` |
| 의상 | `GET/POST /characters/{id}/wardrobe` |
| 업로드 | `POST /assets/upload` |
| 진행률 | `job.progress`, `job.completed`, `job.failed` |

**관련 플로우·상태머신**: F-03. 캐릭터 자체에는 상태머신이 없다.

**불변식·주의**
- 음성 복제·TTS UI는 두지 않는다 (P1-5). 보이스 샘플 업로드 UI도 P1에서 함께 제공한다.
- LoRA 학습 UI는 두지 않는다 (P1-4).
- 캐릭터 삭제 시 이 캐릭터를 참조하는 샷이 있으면 참조 수와 함께 경고하고, 삭제 후 해당 샷의 레퍼런스에서 제거된다.

---

## 4. Character Sheet (캐릭터 시트 뷰어)

**목적**: 턴어라운드·표정·의상·포즈·디테일을 자동 생성해 한눈에 보고, 실전에서 쓸 레퍼런스를 지정한다. (P0-5)

**라우트**: `app/projects/[id]/characters/[cid]/sheet/`

**레이아웃**
- 상단: 캐릭터 헤더(마스터 썸네일·이름) + "시트 생성" 버튼 + 생성 옵션.
- 메인: 섹션별 그리드 — 턴어라운드 / 표정 / 의상 / 포즈 / 디테일.

**주요 컴포넌트·인터랙션**
- 시트 생성: `{views?, expressions?, outfits?, poses?}` 옵션 (기본값: 권장 순서 전체) + 예상 비용 →
  `POST /characters/{id}/sheet` → `202 {job_ids[]}` → 각 잡이 `job.completed`될 때마다 해당 섹션 그리드에 이미지가 채워진다.
  권장 순서: 턴어라운드 4뷰(정면·3/4·측면·후면) → 표정 7종(중립·기쁨·슬픔·분노·놀람·결의·취약) →
  기본 의상+대체 2종 → 액션 포즈 3종 → 디테일 클로즈업(얼굴·손·시그니처 소품).
- 이미지 카드: 썸네일, `role` 태그, 잡 상태, "주 레퍼런스로 지정" 토글 (`PATCH /references/{id}`, `is_primary`).
  용도별(대표·표정·의상)로 주 레퍼런스를 지정하면 샷 생성 시 자동 주입된다.
- 실패한 이미지는 카드에 "실패" 표시 + 개별 재시도 버튼.

**상태 처리**
- 빈 상태: "시트를 생성하면 캐릭터의 모든 각도를 레퍼런스로 쓸 수 있습니다."
- 로딩: 섹션별 스켈레톤 + 전체 진행률 (완료된 잡 수 / 전체).
- "승인" 개념: 별도의 승인 버튼을 두지 않는다. 주 레퍼런스 지정이 곧 "실전에 쓴다"는 선택이며,
  이를 화면 상단에 한 줄로 안내한다 (F-04).

**API·SSE 매핑**

| 화면 동작 | API / SSE |
|---|---|
| 시트 생성 | `POST /characters/{id}/sheet` |
| 레퍼런스 조회·지정·삭제 | `GET /characters/{id}/references`, `PATCH/DELETE /references/{id}` |
| 진행률 | `job.progress`, `job.completed`, `job.failed` |

**관련 플로우·상태머신**: F-04. 개별 이미지는 `generations` 상태머신(§6.1)을 따른다.

**불변식·주의**
- 시트 이미지는 `source='sheet_derived'`로 저장되며, 마스터(`source='generated'`)와 구분된다.
- 주 레퍼런스 지정은 역할당 1개다. 새로 지정하면 이전 지정은 자동 해제된다.

---

## 5. Asset Library (에셋 라이브러리)

**목적**: 배경·장소·소품 에셋과 전역 캐릭터를 모아 프로젝트 간에 재사용한다. (P0-7)

**라우트**: `app/library/`

**레이아웃**
- 상단: 탭 (에셋 / 전역 캐릭터) + "업로드" 버튼 + `kind` 필터 (전체·배경·장소·소품).
- 메인: 에셋 카드 그리드 (썸네일·이름·`kind`·등록일).

**주요 컴포넌트·인터랙션**
- 업로드: `POST /assets/upload` (multipart. 20MB 제한, `png/jpg/webp/mp4/wav/mp3`).
  `project_id` 없이 올리면 전역(`library/assets/`)에 저장된다. 업로드 시 `kind`를 지정한다.
- 에셋 카드: 썸네일, `kind` 뱃지, 버전 표기 (참조 시점 버전 핀 — 스펙 ASSET LIBRARY BLOCKER 04), "사용처" (이 에셋을 참조하는 샷 수 — 클릭 시 해당 샷 목록).
  삭제: 참조 중인 샷이 있으면 경고 후 진행 (`DELETE /assets/{id}`).
- 전역 캐릭터 탭: `GET /projects/{id}/characters?scope=global` 목록 (읽기 전용).
  각 카드에 "이 프로젝트에서 쓰기" 안내 — 실제 사용은 샷 레퍼런스 선택 시 `?scope=all`로 노출되므로 별도 가져오기 동작은 없다.
- 에셋 상세: 미리보기 + 메타 + "샷에 연결" 버튼 → 샷 선택 모달 → `POST /shots/{id}/references`.

**상태 처리**
- 빈 상태: "에셋을 업로드하면 모든 프로젝트에서 재사용할 수 있습니다."
- 로딩: 업로드 중 프로그레스 바 (multipart 진행률).
- 에러: 용량·확장자 위반 시 즉시 안내 (서버 400 전에 클라이언트에서 1차 검증).

**API·SSE 매핑**

| 화면 동작 | API / SSE |
|---|---|
| 업로드 | `POST /assets/upload` |
| 목록·삭제 | `GET /projects/{id}/assets` (`?kind=`), `DELETE /assets/{id}` |
| 전역 캐릭터 | `GET /projects/{id}/characters?scope=global` |
| 샷 연결 | `POST /shots/{id}/references` |

**관련 플로우·상태머신**: F-03(재사용), F-05(샷 레퍼런스). 에셋에 상태머신은 없다.

**불변식·주의**
- 전역 스타일 키트 브라우징 UI는 두지 않는다. 스타일 키트는 프로젝트 설정 탭에서 관리한다 (프로젝트 스코프).
- 에셋 삭제는 참조 무결성을 깬다. 참조 중인 샷이 있으면 삭제를 막지 않되, 경고하고 해당 샷의 레퍼런스에서 제거됨을 명시한다.

---

## 6. Scene/Shot Board (씬·샷 스토리보드 보드)

**목적**: 씬·샷의 생성·순서·상세를 한 보드에서 관리하고, 샷별 상태를 추적한다. (P0-9)

**라우트**: `app/projects/[id]/board/`

**레이아웃**
- 좌측: 씬 컬럼 (칸반). 씬 헤더(제목·장소·시간대·무드) + 샷 카드 리스트.
- 우측(샷 선택 시): 샷 상세 패널.
- 상단: "씬 추가" + 씬별 "예상 비용" 버튼.

**주요 컴포넌트·인터랙션**
- 씬 CRUD: `GET/POST /projects/{id}/scenes`, `GET/PATCH/DELETE /scenes/{id}`.
- 샷 카드: 썸네일(승인 프레임 우선, 없으면 최신 테이크, 없으면 플레이스홀더),
  상태 배지 (`draft`/`frame_generating`/`frame_done`/`frame_approved`/`video_generating`/`video_done`/`failed`),
  `frame_stale`이면 "구식" 배지 추가, 샷 타입·카메라·지속시간.
- 순서 변경: 드래그 앤 드롭 → `POST /shots/reorder {ordered_ids[]}` (씬 내).
- 샷 복제: `POST /shots/{id}/duplicate` (프롬프트 필드·레퍼런스 함께 복사).
- 샷 상세 패널: `shot_type`, `camera_angle`, `movement`, `duration_sec`, `action`, `dialogue`,
  `emotion_weights`, `intended_text`(화면 내 의도 텍스트 — OCR 대조 기준), `sfx_notes`.
  첫 샷의 `shot_type` 미지정 시 `establishing`+`wide` 프리필됨을 표시 (규칙 기반, 변경 가능).
- 레퍼런스: 씬에 귀속된 캐릭터는 자동 상속되어 표시되고, 샷 단위로 추가·제거 가능
  (`GET/POST /shots/{id}/references`, `purpose`: `primary|expression|outfit|location|continuity`).
- 스타일 키트 재정의: 프로젝트 기본값 표시 + 샷별 재정의 선택. 잠긴 키트(`is_locked`) 사용 중에는
  재정의 컨트롤 자체가 비활성화된다 — 스타일 권위는 프로젝트당 하나 (ADR-0011 §13, Higgsfield Manual Style 방식).
  다른 룩이 필요하면 `POST /style-kits/{id}/duplicate`로 새 버전을 만든 뒤 재정의한다 (잠긴 키트 자체는 수정 불가).
- 씬별 "예상 비용": `POST /costs/estimate` (씬 내 전체 샷 합산) → `~$X.XX` 표시.

**상태 처리**
- 빈 상태: "비트에서 씬을 만드세요" (스토리 화면 링크) / 씬은 있는데 샷이 없으면 "첫 샷 추가".
- 로딩: 샷 생성 중 카드에 상태 배지로 표시 (SSE `job.progress` 연동).
- 실패: `failed` 배지 + 실패 지점(이미지/영상)에 따른 "재시도" 버튼.

**API·SSE 매핑**

| 화면 동작 | API / SSE |
|---|---|
| 씬·샷 CRUD | `GET/POST /projects/{id}/scenes`, `GET/PATCH/DELETE /scenes/{id}`, `GET/POST /scenes/{id}/shots`, `GET/PATCH/DELETE /shots/{id}` |
| 순서·복제 | `POST /shots/reorder`, `POST /shots/{id}/duplicate` |
| 레퍼런스 | `GET/POST /shots/{id}/references` |
| 비용 추정 | `POST /costs/estimate` |
| 진행률 | `job.progress`, `job.completed`, `job.failed` |

**관련 플로우·상태머신**: F-05. 샷 상태머신 §6.2.

**불변식·주의**
- `frame_stale`은 상태가 아니라 플래그다. 배지만 띄우고 상태 전이를 막지 않는다.
- 타임라인에서 사용 중인 샷을 삭제하면 확인 모달에 사용처(타임라인 이름)를 표시한다.

---

## 7. Image Review (이미지 리뷰·승인)

**목적**: 샷 이미지 후보를 비교·수정·리테이크하고, 최종 프레임을 승인한 뒤 영상화로 넘긴다. (P0-10, P0-11)

**라우트**: `app/projects/[id]/review/`

**레이아웃**
- 좌측: 샷 목록 (상태 필터: 전체/생성 중/승인 대기/승인됨/실패).
- 중앙: 후보 그리드 + 비교 뷰 (2분할).
- 우측: 생성 패널 (모델·배치·오버라이드·예상 비용) + 선택된 테이크 상세.

**주요 컴포넌트·인터랙션**
- 프레임 생성: 모델 선택 (`GET /models?modality=image`, 미지정 시 라우터), `batch_size` 1~4,
  프롬프트 오버라이드, 예상 비용 → `POST /shots/{id}/frame` → `202 {job_id, estimated_cost_usd}`.
  배치는 부모+자식 잡 구조이며, 후보는 자식(테이크) 단위로 표시된다 (D-25).
- 후보 카드: 테이크 썸네일, 잡 상태, 실제 비용, 시드 값, "크게 보기". 클릭 2개를 비교 뷰에 올린다.
  시드 표시 + "이 시드로 고정" 액션 → `PATCH /shots/{id} {seed}` (ADR-0011 §6: 랜덤 배치 → 선택 → 시드 고정 → 프롬프트만 반복).
- 수정:
  - 영역 수정(inpaint): 브러시 마스크 에디터 + 수정 프롬프트 → `POST /shots/{id}/inpaint {generation_id, mask_path, prompt}`.
    새 잡으로 기록되며 후보에 추가된다.
  - 리테이크: `POST /shots/{id}/retake {overrides?, batch_size?}`. 기존 후보는 유지(히스토리).
  - 업스케일·아웃페인트: 승인된 테이크에만 버튼 표시 → `POST /shots/{id}/upscale {generation_id}`,
    `POST /shots/{id}/outpaint {generation_id, ...}` (ADR-0003). ADR-0011 §10 멀티패스의 UI 표면 —
    미승인 후보에는 버튼을 두지 않는다 (후처리는 승인된 후보에만).
- 승인: `status='succeeded'`인 테이크에만 "승인" 버튼 활성화 →
  `POST /shots/{id}/frame/approve {generation_id}` (자식 ID) → 샷이 `frame_approved`로 전이, 승인 배지+시각 표시.
  승인된 상태에서 리테이크해도 승인은 유지된다 (승인 대상이 특정 `generation_id`에 묶여 있음).
  새 테이크를 승인하면 `approved_frame_generation_id`가 교체된다.
- 영상화 진입: `frame_approved` 샷에만 "클립 생성" 버튼 활성화.
  미승인 샷은 버튼 비활성화 + "프레임을 먼저 승인하세요" 툴팁 (서버의 `409 APPROVAL_REQUIRED`를 UI에서 선방지).
  클릭 시 모달: `mode`(`i2v` 기본, `t2v` 선택 가능), `motion_prompt`, `duration_sec`, 모델, 예상 비용 →
  `POST /shots/{id}/video` → `202` → "타임라인에서 확인" 안내.
  `t2v` + 캐릭터 등장 샷이면 "캐릭터 일관성이 약할 수 있습니다" 경고 표시 (차단은 안 함).
- 연속 생성: 다음 샷 생성 시 이전 샷의 마지막 프레임/승인 스틸을 쓰는 옵션.
  원천이 없으면 "연속 생성을 쓸 수 없음 (사유)"을 표시하고 일반 생성으로 진행한다 (D-15).

**상태 처리**
- 빈 상태: 샷을 선택하세요 (보드 링크).
- 로딩: 잡별 프로그레스 바 (`job.progress`의 `progress_pct`·`stage`). 배치면 자식별 표시.
- 에러: 실패 테이크는 카드에 `error_code` 한글 메시지 + 재시도 버튼 (`POST /jobs/{id}/retry`).
- 승인 전/후: 승인 전에는 후보 비교가 중심, 승인 후에는 승인 프레임이 크게 고정 표시되고 "클립 생성"이 1차 액션이 된다.

**API·SSE 매핑**

| 화면 동작 | API / SSE |
|---|---|
| 프레임 생성·리테이크 | `POST /shots/{id}/frame`, `POST /shots/{id}/retake` |
| 영역 수정 | `POST /shots/{id}/inpaint` |
| 승인 | `POST /shots/{id}/frame/approve` |
| 영상화 | `POST /shots/{id}/video` |
| 잡 조회·재시도·취소 | `GET /jobs/{id}`, `POST /jobs/{id}/retry`, `DELETE /jobs/{id}` |
| 모델 목록 | `GET /models?modality=image`, `GET /models?modality=video` |
| 진행률 | `job.progress`, `job.completed`, `job.failed`, `job.canceled` |

**관련 플로우·상태머신**: F-06, F-07, F-08, F-10(진입). 잡 §6.1, 샷 §6.2.

**불변식·주의**
- 승인은 사용자 버튼으로만 일어난다. 자동 승인·기본 승인 없음.
- `approved_frame_generation_id`는 배치 자식 ID를 가리킨다. 부모 ID를 쓰지 않는다 (D-26).
- I2V 모션 프롬프트는 "이미지에 있는 것을 다시 설명하지 않고, 움직일 것·변할 것만 기술"한다. 모달에 가이드 문구를 둔다.

---

## 8. Video/Timeline (영상·타임라인·자막·오디오)

**목적**: 클립을 확인·재생성하고, 타임라인에 배치·편집하며, 자막과 오디오를 입혀 렌더를 요청한다. (P0-13, P0-14, P0-15)

**라우트**: `app/projects/[id]/timeline/` (자막은 이 화면 내 탭 — ADR-0002)

**레이아웃**
- 상단: 클립 스트립 (샷별 최신 클립 썸네일 + 상태 + "재생성").
- 중앙: 타임라인 트랙 (클립 레인 / 오디오 레인 / 자막 레인) + 프리뷰 플레이어.
- 하단: 탭 (자막 / 오디오).
- 우측: 렌더 패널 (출력 프리셋·QC 상태·렌더 버튼).

**주요 컴포넌트·인터랙션**
- 클립 스트립: 샷별 최신 `done` 클립. `video_done`이 아니면 "클립 없음 — 리뷰에서 생성" 안내.
  재생성 버튼 → 리뷰 화면의 "클립 생성" 모달과 동일한 폼 (또는 리뷰로 딥링크 — 딥링크로 통일한다).
- 타임라인:
  - "샷 순서대로 자동 배열": `POST /timelines/{id}/build-from-shots` (샷 `order_index` + 최신 `done` 클립).
  - 클립 트림: 인/아웃 포인트 조정 (`in_sec/out_sec`, 초 단위 float). 순서 변경은 드래그.
  - 변경은 `PATCH /timelines/{id}` (`clip_order`)로 저장. `rendering` 중에는 전체 읽기 전용
    (`409 RENDER_LOCKED`, SSE `render.completed` 후 해제).
- 자막 탭:
  - 트랙 목록·생성 (언어별): `GET/POST /projects/{id}/subtitle-tracks`.
  - 큐 에디터: 시작·종료(ms 정수)·텍스트 행 편집 → `PUT /subtitle-tracks/{id}/cues` (배열 일괄 저장). 시간 단위는 ms (스펙 SUBTITLE 큐 `start_ms/end_ms`).
    구간 겹침은 `422 SUBTITLE_OVERLAP` + 행 하이라이트.
  - "대사에서 자동 생성": `POST /projects/{id}/subtitle-tracks/generate` → `202` → SSE 후 큐에 반영.
  - 스타일: `GET/PUT /projects/{id}/subtitle-style` (폰트·크기·색상·위치·줄바꿈).
    프리뷰에 안전영역(기본 5%) 가이드 표시.
  - 다운로드: `GET /subtitle-tracks/{id}/export?format=srt|vtt`.
- 오디오 탭:
  - 업로드된 오디오 에셋(`POST /assets/upload`로 올린 wav/mp3) 목록 → 타임라인에 배치
    (`GET/POST /timelines/{id}/audio-clips`, 시작 시각·볼륨·페이드).
  - TTS·더빙·립싱크 생성 UI는 두지 않는다 (P1-5).
- 렌더 패널: 출력 프리셋 선택 (P0-18 — 목록 조회는 ADR-0004),
  QC 상태 표시 (최신 리포트 `overall`, 무효 시 "QC 다시 실행" 유도),
  "렌더" 버튼 → `POST /timelines/{id}/render {export_preset_id, ignore_warnings?}`.
  `warn`이면 "경고 무시하고 렌더" 체크박스가 필수다. `fail`이면 버튼 비활성화.

**상태 처리**
- 빈 상태: 타임라인이 없으면 "새 타임라인" + "샷에서 자동 배열". 클립이 하나도 없으면 리뷰로 유도.
- 로딩: 렌더 중 프로그레스 (`render.completed` 대기). QC 실행 중 상태 표시 (`qc.completed` 대기).
- 에러: 렌더 실패 시 `timelines.status='failed'` → `draft`로 복귀 안내 + 로그.

**API·SSE 매핑**

| 화면 동작 | API / SSE |
|---|---|
| 타임라인 관리 | `GET/POST /projects/{id}/timelines`, `GET/PATCH /timelines/{id}`, `POST /timelines/{id}/build-from-shots` |
| 오디오 배치 | `GET/POST /timelines/{id}/audio-clips` |
| 자막 | `GET/POST /projects/{id}/subtitle-tracks`, `GET/PUT /subtitle-tracks/{id}/cues`, `GET /subtitle-tracks/{id}/export`, `POST /projects/{id}/subtitle-tracks/generate`, `GET/PUT /projects/{id}/subtitle-style` |
| QC·렌더 | `POST /timelines/{id}/qc`, `POST /timelines/{id}/render` |
| 진행률 | `job.progress`, `job.completed`, `job.failed`, `qc.completed`, `render.completed` |

**관련 플로우·상태머신**: F-10(클립), F-12, F-13(자막). 타임라인 렌더 §6.5.

**불변식·주의**
- 자막 burn-in은 렌더 시점에만 수행된다. 타임라인에서 보는 자막은 항상 사이드카 프리뷰다 (D-14).
- `rendering` 중 `clip_order`·오디오·자막 수정은 서버가 409로 막는다. UI도 동일하게 잠근다.

---

## 9. QC/Export (QC 리포트·비용 대시보드·출력)

**목적**: 출고 전 자동 검수 결과를 확인·처리하고, 최종 출력물을 만들며, 프로젝트 비용을 집계한다. (P0-16, P0-17, P0-18)

**라우트**: `app/projects/[id]/qc/`, `app/projects/[id]/costs/`

**레이아웃**
- QC 화면: 좌측 리포트 목록 (타임라인별 최신 + 이력), 우측 리포트 상세.
- 비용 화면: 상단 예산 바 + 월간 요약 카드, 메인 집계 테이블/차트 (`group_by` 전환).

**주요 컴포넌트·인터랙션**
- QC:
  - "QC 실행": `POST /timelines/{id}/qc` → `202 {qc_report_id}` → SSE `qc.completed` 후 상세 갱신.
  - 리포트 상세: 15개 검사 항목 테이블 (코드·대상·결과·측정값·임계값) — 13종 형식 + 2종 내용(advisory, warn까지만). `fail`은 적색+"렌더 차단" 뱃지, `warn`은 황색+무시 가능 안내.
  - 종합 판정: `pass`(바로 렌더) / `warn`("경고 무시하고 렌더" 체크 시 진행) / `fail`(렌더 버튼 비활성화).
  - 타임라인 수정 후 리포트는 "무효" 배지 + "QC 다시 실행" 버튼 (`details.reason='stale'`).
- 출력:
  - 프리셋 선택: YouTube 16:9 / Shorts 9:16 / Reels / TikTok / 커스텀 (목록 조회는 ADR-0004).
  - 렌더 버튼: 유효한 리포트(`overall != 'fail'`)가 있을 때만 활성화.
    `POST /timelines/{id}/render {export_preset_id, ignore_warnings?}` → SSE `render.completed` → `output.mp4` 다운로드+플레이어.
- 비용 대시보드:
  - `GET /projects/{project_id}/costs/summary?group_by=month|project|model` 집계 테이블.
  - 예산 바: 당월 `actual_cost_usd` / `monthly_budget_usd` (80% 황색, 100% 적색).
  - 잡 단위 내역: `GET /jobs?project_id=` 로 추정/실제 비교 목록.

**상태 처리**
- 빈 상태: QC 리포트가 없으면 "타임라인을 선택하고 QC를 실행하세요".
- 로딩: QC 실행 중 / 렌더 중 프로그레스.
- 에러: `409 QC_FAILED`로 렌더 시도 시 리포트 링크와 함께 차단 사유 표시.

**API·SSE 매핑**

| 화면 동작 | API / SSE |
|---|---|
| QC 실행·조회 | `POST /timelines/{id}/qc`, `GET /qc-reports/{id}` |
| 렌더 | `POST /timelines/{id}/render` |
| 비용 집계·추정 | `GET /projects/{project_id}/costs/summary`, `POST /costs/estimate` |
| 잡 내역 | `GET /jobs` |
| 진행률 | `qc.completed`, `render.completed`, `job.progress`, `cost.alert` |

**관련 플로우·상태머신**: F-14, F-15. 타임라인 렌더 §6.5. QC 판정 규칙 §10.3.

**불변식·주의**
- `overall='fail'`이면 렌더가 차단된다. "무시하고 진행" 옵션은 `warn`에만 있다.
- 렌더는 QC 리포트가 타임라인 최종 수정 이후에 생성된 것일 때만 허용된다.

---

## A. ADR (정본은 `docs/adr/` — 아래는 색인)

| ADR | 제목 | 상태 |
|---|---|---|
| [ADR-0002](adr/0002-화면-구성.md) | P0 화면 구성 (셸 8탭, 자막은 타임라인 내 탭, 설정 탭에서 스타일 키트·예산 관리) | 승인 (2026-09-30) |
| [ADR-0003](adr/0003-업스케일-아웃페인트-엔드포인트.md) | `POST /shots/{id}/upscale`, `POST /shots/{id}/outpaint` 추가 (§8.1에 빠져 있었음) | 승인 (2026-09-30) |
| [ADR-0004](adr/0004-프리셋-목록-조회.md) | `GET /export-presets` 추가 (§8.1에 빠져 있었음) | 승인 (2026-09-30) |
| [ADR-0005](adr/0005-마스터-재생성-규칙.md) | 마스터 초상 재생성 파일명 규칙 (덮어쓰기 금지, 히스토리 유지) | 승인 (2026-09-30) |
| [ADR-0006](adr/0006-렌더-QC게이트-연기.md) | 렌더 QC 게이트 연결 (fail→409 차단, warn→ignore_warnings 필요) | 승인 (2026-09-30) |
| [ADR-0007](adr/0007-타임라인-클립-정규화.md) | `timeline_clips` 테이블이 클립 순서의 유일한 정본 | 승인 (2026-09-30) |
| [ADR-0008](adr/0008-태스크-인자-대상-행-ID.md) | 태스크 인자는 "대상 행 ID 하나 + DB 정본"으로 일반화 | 승인 (2026-09-30) |
| [ADR-0009](adr/0009-API-경로-정합성-조정.md) | API 경로 4건의 설계서 불일치 해소 (구현 경로를 정본으로 유지, §8.1 수정) | 승인 (2026-09-30) |
| [ADR-0010](adr/0010-Phase6-범위.md) | Phase 6 (P2) 범위 — 4종 구현, 3종 제외/연기 | 승인 (2026-09-30) |
| [ADR-0011](adr/0011-생성-품질-일관성-규칙.md) | 생성 품질·일관성 규칙 — 프롬프트 슬롯 순서·토큰 예산·네거티브 라이브러리·캐릭터 정체성·시드 규율·키프레임-퍼스트·카메라 무브 enum·멀티패스·스타일 바이블·GenerationSnapshot | 제안 (2026-10-04) |

---

*작성일: 2026-09-30, 최종 수정: 2026-10-05. 시스템 설계서 v1.0 + PROJECT_ARCHITECTURE.md + MODULES.md 기준.*
*구현 중 화면 구조 변경이 필요하면 본 문서를 먼저 고치고 코드를 고친다 (문서→코드 순서).*
