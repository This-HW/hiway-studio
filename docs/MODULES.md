# 모듈 카드 목록

> 각 모듈의 "에이전트용 설명서". 형식: **목적(1줄) · 책임 · 공개 인터페이스 · 불변식 · 의존성 · 확장 절차 · 함정**.
> 코드 구현 시 각 모듈 디렉토리의 `README.md`는 이 카드 형식을 그대로 따른다 (AGENT_GUIDE §6).
> 표로만 정리된 영역(models/, routers/, 프론트)은 CRUD 매핑이라 카드 대신 1줄 설명 + 설계서 링크로 충분하다.

---

## services/ — 도메인 로직

### prompt_assembly.py
- 목적: 샷 생성에 필요한 모든 컨텍스트를 순서대로 조립해 모델용 프롬프트를 만든다.
- 책임: 설계서 §7 조립 순서 구현, 레퍼런스 이미지 주입(§7.2), 길이 초과 절단(§7.3), 모델별 순서 오버라이드(§7.4), 시드 기록(§7.5).
- 인터페이스: `assemble_shot_prompt(shot_id, model_id) -> AssembledPrompt {text, negative, reference_paths[], seed, truncated_sections[]}`.
- 불변식: ① 절단된 섹션은 반드시 `truncated_sections`에 기록 ② 레퍼런스 상한(기본 4장, 모델 capability 우선) 초과 시 우선순위 `primary > expression > outfit > location > continuity`로 절단 ③ 원본 컨텍스트를 변경하지 않는다(읽기 전용) ④ 슬롯 순서 고정 (ADR-0011): `주제 → 배경 → 스타일/매체 → 조명 → 카메라/렌즈 → 색감/분위기 → 파라미터`. 주제는 5~8단어로 맨 앞에. 모순되는 스타일 2개 이상 금지 ⑤ 캐릭터의 얼굴·핵심 외형은 씬 프롬프트에서 재묘사하지 않는다 — 정체성은 레퍼런스 이미지가 잡는다 (ADR-0011 §4). 외형 고정 문구(identity clause)는 캐릭터 레퍼런스에서만 온다.
- 의존성: models(production, character, style), files/paths.
- 확장: 새 컨텍스트 소스 추가 시 조립 순서표(§7.1)에 위치를 먼저 정하고, 절단 우선순위도 함께 정의한다.
- 함정: 프롬프트 길이는 "문자 수" 기준이 아니라 모델별 토너/문자 정책(`constraints.max_prompt_chars`)을 따른다. 하드코딩 금지. SD 1.5는 77토큰 하드캡 — 초과분은 조용히 잘리므로 밀도 있게 쓰고 품질 부스터를 앞에 둔다. 가중치 문법 `(단어:1.3)`·`((단어))`·`[약화]` 지원. 네거티브는 `prompt_snippets`의 품질/신체/텍스트/구도 기본 세트를 사용하고, 거대한 복사-붙여넣기 네거티브는 금지한다 (ADR-0011 §2·§3).

### cost_estimator.py
- 목적: 생성 전 예상 비용(USD), 생성 후 실제 비용을 계산·기록한다.
- 책임: 설계서 §9. 단위 산식(§9.1), 추정 시점(§9.2), 배치·프로젝트 추정(§9.3).
- 인터페이스: `estimate(generation_params) -> USD`, `record_actual(generation_id, provider_report)`.
- 불변식: ① 통화는 USD 단일 ② 재시도된 시도 비용도 합산 ③ 추정치와 실제치 차이 20% 초과 시 대시보드 표시(차단 아님).
- 의존성: models/system(model_pricing).
- 함정: 제공자가 실제 과금을 보고하면 그 값을 우선한다. "추정=청구"로 오해하지 않는다.

### budget_guard.py
- 목적: 예산 초과 시 신규 잡 등록을 차단한다 (D-28).
- 인터페이스: `check_budget(db, project_id, *, estimated_new) -> None | raise StudioError(BUDGET_EXCEEDED, 403)`, `lock_project`, `add_hold`, `reserve_llm_call`/`settle_llm_call`, `summary`.
- 불변식: ① 차단은 신규 잡에만 적용, 진행 중 잡은 유지 ② 경고 80%·차단 100% (defaults `BUDGET_WARN_RATIO`/`BUDGET_BLOCK_RATIO`). 차단은 `(committed + 신규)/예산 >= 1.0`, 경고는 이번 요청이 80%를 넘을 때 한 번 SSE `cost.alert` {`project_id`, `usage_pct`, `spent_usd`, `budget_usd`, `threshold_pct`}를 보낸다 ③ 월 기간 판정은 `created_at`이 아니라 `generations`/`audio_generations.reserved_at`(생성 시각, 수동 retry 때 갱신)이고, 보류는 `budget_holds.period_at`(보류한 옛 시도의 기간)이다(B5-r2).
- 예약(B5, 계획 D1): `check_budget`은 잡 행을 넣는 같은 트랜잭션에서 프로젝트 행을 먼저 잠그고(잠금 순서 Project → generation → 배치 부모 → Video → Shot) committed = 지출 + 예약(queued/running leaf 잡, `PROVIDER_UNCERTAIN` 실패 행, pending/uncertain LLM 호출, fan-out 전 멀티샷 세트, `budget_holds`)으로 판정한다. 배치 부모는 집계하지 않는다. failed·canceled는 상태만으로 해제된다. 수동 retry도 다시 예약하고, 불확실 시도는 `budget_holds`로 예약을 유지한다. LLM 호출은 pending 행을 먼저 commit한 뒤 외부 호출한다.
- 함정: "표시만"이 아니라 강제다. 우회 플래그를 만들지 않는다.
- 상태 의미 (2026-09-30 검수 확정): `Generation` 집계는 `status='succeeded'` 기준 — 워커가 완료 시 쓰는 실제 상태다. `AudioGeneration`은 로컬 등록이라 `status='done'` 기준. 둘을 섞으면 예산 집계가 깨진다.

### approval.py
- 목적: 샷 승인 상태 전이를 담당한다 (D-13, D-26).
- 인터페이스: `approve_frame(shot_id, child_generation_id)`, `revoke_approval(shot_id)`.
- 불변식: ① 승인은 사용자 명시 액션만 ② `approved_frame_generation_id`는 **자식(배치 테이크) ID**를 가리킨다 ③ 미승인 샷의 영상화 요청은 409.
- 함정: 부모 배치 ID를 승인 대상으로 쓰면 "몇 번째 테이크인지"를 잃는다.

### continuity.py
- 목적: 연속 생성 시 이전 샷의 마지막 프레임과 상태를 다음 샷에 전달한다 (P0-12).
- 인터페이스: `resolve_continuity_input(shot_id) -> {frame_path, state} | None`.
- 불변식: 폴백 체인 ① 이전 클립 마지막 프레임 → ② 승인 스틸 → ③ 비활성+사유 표시 (D-15). 조용히 건너뛰지 않는다.
- 연속성 앵커 (ADR-0011 §9): 멀티샷 연속 생성 시 매 샷마다 5개 앵커를 명시한다 — 정체성·의상·이동 방향·카메라 거동·환경 마커 2개. 조명·팔레트 토큰은 샷마다 verbatim 반복한다. 확장 체인이 길어지면 원본 클립에서 re-seed하고, 클립 꼬리 8~12프레임은 트림한다.
- 시드 규율 (ADR-0011 §6): 탐색은 랜덤 시드 배치 → 선택 → 시드 고정 → 프롬프트만 반복 개선. 같은 시퀀스 내 샷들은 동일 시드 공유.
- 함정: 이전 샷에 영상이 없을 때가 정상 케이스다. 에러가 아니라 체인으로 처리한다.

### qc_orchestrator.py
- 목적: QC 게이트 실행을 조율한다 (P0-16 + P2 내용 검수, D-06/ADR-0010).
- 책임: 설계서 §10. 검사 실행 순서, 종합 판정(§10.3), 실행 시점(§10.4).
- 인터페이스: `run_qc(qc_report_id) -> QCReport {overall: pass|warn|fail, checks[15]}`.
  리포트는 실행 전 `overall='pending'`으로 먼저 생성된다.
- 불변식: ① P0는 형식 검수 13종, P2는 내용 검수 2종(advisory — warn까지만, fail 불가) ② 타임라인 수정 이후 생성된 리포트만 유효 (D-32, `timeline_content_version` 스냅샷 비교). 무효(stale) 리포트로 렌더 시도 시 409 거부 ③ 렌더 게이트: 리포트 없음·실행 중·stale·fail → 409 `QC_FAILED` (details.reason), warn은 `ignore_warnings=true` 필요 (ADR-0006). ④ "최신 리포트"는 `created_at`이 아니라 타임라인별 발행 순번 `seq`(UNIQUE(target_type, target_id, seq), 마이그레이션 0015)의 최댓값이다. `request_qc`가 타임라인 행을 잠근 뒤 max+1로 부여하므로 같은 시각의 이전 pass/warn이 새 pending/fail을 가리지 않는다(B4R3-H1, ADR-0013 B4-r3). 게이트와 `GET /timelines/{id}/qc-latest`는 모두 `latest_report` 하나를 쓴다. ⑤ 0015 이전 리포트 중 같은 대상에서 `created_at`이 겹친 행은 발행 순서를 알 수 없으므로 `order_ambiguous=true`로 표시되고, 최신 리포트가 그런 행이면 결과와 무관하게 렌더 게이트가 409 `QC_FAILED`(reason='stale', `order_ambiguous=true`)로 거부한다 — 새 QC가 필요하다. 새 리포트는 항상 false(B4R4-H1).
- 검사 15종 (`CHECKS` 레지스트리): text_ocr_match, text_artifact, subtitle_content, subtitle_timing, subtitle_cps, subtitle_safe_area, continuity_first_frame, continuity_between_shots, audio_peak, audio_loudness, lipsync_drift, encoding_match, black_frame (이상 13종 형식) + story_fidelity, performance_quality (이상 2종 advisory 내용).
- `ADVISORY_CHECKS = {"story_fidelity", "performance_quality"}` — `_overall`의 fail 분기에서 제외하는 이중 보장.
- 스텁 경계: rapidocr_onnxruntime 미설치 → text_ocr_match(의도 텍스트 있을 때만)/text_artifact는 `stub=true` + warn으로 기록 (엔진 설치 시 실동작 — Unit B, 2026-10-01). continuity 2종은 numpy 그레이스케일 SSIM 실측(B3)이며, 측정할 것이 없거나(ffmpeg·클립·스틸 없음) numpy가 없으면 `stub=true` + warn — pass로 보이지 않는다. 검사 예외는 필수 검사면 fail, advisory면 warn(`CHECK_ERROR: …`)이고, 검사 바깥 실패(컨텍스트·저장)는 리포트를 별도 commit으로 `overall='fail'`로 남긴다(계획 D5). `encoding_match`는 렌더 전 QC에서 `applicable=false`(판정·집계에서 제외, 웹 `QcReportView`는 "미적용"으로 표시)이고, 출력 인코딩과 burn-in 자막의 실제 검증은 렌더 postflight(`postflight_encoding`/`postflight_burnin`)가 한다. 측정 대상이 없는 continuity(모두 t2v 클립, 연속 체인 샷 쌍 없음)도 `applicable=false`다. vision 엔드포인트 미설정(`QC_VISION_BASE_URL`/`QC_VISION_MODEL` 없음) → 내용 2종은 `stub=true` + warn. 설정 시 `app/services/vision_client.py` (제네릭 OpenAI-호환 chat completions, Unit C, 2026-10-01)가 샷 대표 프레임 + 스토리 컨텍스트를 보내 0-100 스코어 수신 — warn까지만, fail 불가, 오류 시 warn으로 강등. 스텁은 자동 통과로 숨기지 않는다.
- 확장: 새 검사는 이 파일의 `CHECKS`에 1행 등록 + 임계값은 `core/defaults.py`. (개별 파일 `media/qc_checks/`는 두지 않는다.)
- 함정: `warn`은 게이트를 막지 않는다. `fail`만 막는다. vision 모델 없이는 advisory stub이 항상 warn이므로 QC overall은 'pass'가 될 수 없다 — 렌더 시 `ignore_warnings=true`가 설계된 플로우다.

### qc_content.py
- 목적: P2 내용 검수 2종 (ADR-0010, D-06) — story_fidelity(승인 프레임 vs 샷 프롬프트/스토리), performance_quality(클립 연기 자연스러움).
- 인터페이스: `check_story_fidelity(ctx)`, `check_performance_quality(ctx)` → `CheckResult`.
- 불변식: ① 조언(advisory) 등급 — warn까지만, fail 불가. `_overall`의 fail 분기에서 `ADVISORY_CHECKS` 제외로 이중 보장 ② vision 모델 미연결 시 `stub=true` + warn + "vision 모델 미연결" 명시 (자동 통과로 숨기지 않음) ③ 임계값은 `core/defaults.py` (`QC_STORY_FIDELITY_WARN_SCORE`, `QC_PERFORMANCE_WARN_SCORE`).
- 함정: `_score_*` 훅은 `NotImplementedError`로 명시 — 설정 없이 호출될 수 없는 경로다.

### render.py
- 목적: 타임라인을 최종 영상으로 렌더한다 (F-15). 실행 소유권·게시 규칙은 ADR-0013 §렌더(계획 D4/D8).
- 인터페이스: `request_render(db, timeline_id, export_preset_id, ignore_warnings)` → 202 (QC 게이트 통과 후 조건부 UPDATE로 claim + `q.post.render_timeline` enqueue), `render_timeline(timeline_id) -> dict` (워커), `begin_execution` / `publish_output` / `record_render_failure` (토큰 조건부 쓰기), `burnin_presence` (트랙별 글리프 영역 픽셀 존재 검사), `render_locked(timeline_id)` (409 오류 생성).
- 불변식: ① 유효한 QC 리포트(`overall != fail`, warn이면 `ignore_warnings=true`) 없으면 409 `QC_FAILED` (ADR-0006). claim은 `status != 'rendering'` + QC가 본 `content_version` 조건의 UPDATE 한 번이고, 0행이면 409 `RENDER_LOCKED`(또는 그사이 편집이 끼면 stale `QC_FAILED`) ② 워커는 `heartbeat_at IS NULL`일 때만 시작 표시를 하므로 중복 전달은 no-op. 렌더 중 heartbeat가 lease를 늘린다 ③ 시도마다 `timelines/<id>/attempts/<token>/`에서 작업하고, 자막 없는 중간본을 postflight 기준본으로 남긴 뒤 최종본을 불변 파일 `output_<token8>.mp4`로 링크한다(덮어쓰기 없음, 공용 파일 rename 없음) ④ postflight: `postflight_encoding` pass 필수. burn-in 검증 대상 트랙이 있으면 그 probe의 길이를 큐 범위 계산에 재사용하고, 그때 길이가 불명·0이면 미게시한다. 큐는 출력에 보이는 구간으로 잘라 검사한다(끝을 걸친 큐도 검사). burn-in 트랙마다 픽셀 존재 검사 pass 필수 — 그 큐를 해당 트랙 스타일로 단독 렌더한 글리프 영역(다른 트랙 영역 제외) 안에서 기준본 대비 변화가 있어야 한다(B4b-fix), 글리프가 `RENDER_BURNIN_OCR_MIN_GLYPH_PX` 이상이면 `postflight_burnin`(OCR) pass도 필수. OCR 엔진 없음·글리프가 너무 작음·글자/숫자가 없는 기호 전용 큐(`...`, `♪`; `burnin_symbol_only_unverified`)는 게시하되 `render_meta.unverified`에 남긴다. 그 밖의 비-pass는 게시하지 않는다(`QC_FAILED`) ⑤ 게시는 `id + exec_token + status='rendering'` 조건부 UPDATE(`output_path`, `status='done'`, `render_meta`)뿐이다. 0행이면 아무것도 바뀌지 않고 시도 파일은 고아로 남는다 ⑥ 실패(StudioError·예기치 않은 예외·게시 진입 오류 모두)는 같은 토큰 조건으로 `failed`를 commit한 뒤 raise하며(`done` commit 뒤 응답만 잃은 경우는 0행이라 `done` 유지), 기존 `output_path`는 건드리지 않는다. SSE `render.completed`는 commit 뒤에만 ⑦ 자막 burn-in은 렌더 시점에만 수행 (사이드카가 정본, D-14). burn_subtitles 프리셋 + subtitle_mode != none + 큐 있는 트랙 선택 시에만, dual은 2트랙 ⑧ BGM/SFX 클립은 지연·게인 적용 후 amix.
- 의존성: media/ffmpeg_ops·subtitle_render, files/, services/qc_orchestrator(postflight 공개 함수), models/timeline.
- 함정: 다운로드·조회는 항상 `output_path`를 따른다(B4b 이전 행의 `.../output.mp4`도 그대로 제공). 실패·추월된 시도의 `output_<token8>.mp4`는 참조되지 않는 고아라 정리 대상이다. 렌더 중 편집은 `timelines.bump_content_version`의 조건부 UPDATE가 막는다(클립·오디오·자막 공통).

### episode_clone.py
- 목적: 에피소드 템플릿 복제 (P1-10, D-23).
- 인터페이스: `clone_episode(episode_id) -> new_episode_id`.
- 불변식: ① deep copy(전부 신규 ID) ② `generations`·`videos`는 제외하고 `draft` 상태로 재생성 대상화 ③ 씬·샷 바인딩(캐릭터·스타일 참조)은 유지.
- 함정: 얕은 복사를 하면 원본과 산출물을 공유하게 된다. 반드시 신규 ID.

### export_edl.py
- 목적: 타임라인 → FCP7 XML / CMX3600 EDL 내보내기 (P2, ADR-0010, Premiere·DaVinci 연동).
- 인터페이스: `gather_timeline_media(timeline_id)`, `build_cmx3600_edl(export) -> str`, `build_fcp7_xml(export) -> bytes`, `to_timecode(sec, fps)`.
- 불변식: ① 순수 결정적 — 같은 타임라인이면 바이트 동일 ② 소스 경로는 DB 상대경로 그대로 참조 (pathurl `file://./...`) ③ 내보낼 클립 0개 → 400 VALIDATION_ERROR ④ 무료 (잡·비용 없음) ⑤ fps = 출력 프리셋 → 첫 클립 소스 → 24.
- 함정: EDL 오디오 이벤트는 `A` 트랙, 소스 in은 00:00:00:00 기준.

### bible.py (services)
- 목적: 월드 바이블 — 프로젝트별 서사 설정집 CRUD (P2, ADR-0010).
- 인터페이스: `list_entries(project_id, category?/tag?/q?)`, `create_entry`, `get_entry`, `update_entry`, `delete_entry`.
- 불변식: ① category는 contract enum `bible_category` (character|faction|location|rule|event|item) ② 프로젝트 스코프 — 타 프로젝트 항목 접근 불가 (404) ③ 무료 (잡·비용 없음).

### previz.py (services)
- 목적: P2 3D 카메라 프리비즈 — 샷별 카메라 키프레임 CRUD + 보간 경로 (ADR-0010).
- 인터페이스: `list_keyframes`, `create_keyframe`, `update_keyframe`, `delete_keyframe`, `camera_path(db, shot_id, samples=50) -> {shot_id, keyframe_count, samples[]}`.
- 불변식: ① 좌표계는 미터·Y-up (모델·스키마·UI에 명시) ② UNIQUE(shot_id, t_sec) — 중복은 422 `DUPLICATE_KEYFRAME`, t_sec<0은 400 ③ 보간은 키프레임 간 선형, 렌더러가 아님 ④ 전부 무료 (잡·예산 가드 없음).
- 의존성: models(camera_previz, production), core, db.

### idempotency.py
- 목적: 중복 생성 요청(더블클릭 등) 방지 (설계서 §13.2).
- 인터페이스: `idempotency.run(db, request, key, body=…, create=…, response_model=…)` — 잡 생성 라우터 8개 파일의 모든 잡 생성 엔드포인트(캐릭터 master·sheet 포함)가 공유. `Idempotency-Key` 헤더, `IDEMPOTENCY_TTL_SEC`(86400) 안의 동일 키 + 동일 본문 → 저장된 응답 그대로 재생(예산 재검사·새 잡 없음), 다른 본문 → 409 `IDEMPOTENCY_CONFLICT`.
- 불변식: DB 테이블 `idempotency_records`, 키 스코프는 UNIQUE(method, **실제 요청 경로**, key)다(같은 키라도 샷이 다르면 별개). 레코드는 잡·예산 예약과 같은 트랜잭션에 기록(2xx만)되고, 동일 키 경합의 패자는 롤백 후 승자 응답을 재생한다(계획 D2).

### scene.py
- 목적: 씬·샷의 CRUD·순서·복제·레퍼런스와 보드 read model (P0-9).
- 인터페이스: `create/update/delete_scene`, `create/update/delete_shot`, `reorder_shots`, `duplicate_shot`, `add_reference`, `delete_reference(db, shot_id, reference_id)` (`DELETE /shots/{shot_id}/references/{reference_id}`, 다른 샷 소속이면 404 — ADR-0012), `shot_card` (thumbnail·candidate_count·stale 계산).
- 불변식: ① 프롬프트 관련 필드 변경 후 기존 테이크가 있으면 `frame_stale=true` (새 생성 시작 시 해제 — 단 enqueue가 프롬프트를 조립할 때 본 샷 입력(`generations.FRAME_PROMPT_FIELDS`)과 마지막 샷 잠금에서 다시 읽은 값이 같고 그사이 stale 표시가 새로 켜지지 않았을 때만. 그사이 commit된 편집의 경고는 유지, B4R3-M1. 무효화는 `Shot.mark_frame_stale()`로만 켜며 `frame_stale_rev`를 +1 하므로, 이미 stale인 샷의 새 무효화도 리비전 비교로 감지된다 — B4R4-M1) ② `shot_card.thumbnail`은 승인 프레임 우선, 없으면 최신 테이크 ③ `duplicate_shot`은 프롬프트·레퍼런스만 복사하고 generations는 제외.
- 함정: 보드 목록은 `list_scenes` + `shot_card` 조합으로 만든다. 샷 read model을 라우터에서 직접 조립하지 않는다.

### generations.py
- 목적: 샷 프레임 생성 잡의 enqueue (P0-10/11).
- 인터페이스: `enqueue_frame` (배치 parent+children), `enqueue_retake`, `enqueue_inpaint/upscale/outpaint`, `retry_job`, `cancel_job`, `list_jobs` (필터).
- 불변식: ① 배치 크기가 1이어도 parent(`is_batch=true`) + child 구조 유지 (D-25) ② 프롬프트 스냅샷의 `reference_ids`는 cap 적용 후 실제 전달된 레퍼런스만 ③ enqueue는 DB commit 이후에만 수행 (`dispatch_after_commit`) ④ 큐 포화 시 429 ⑤ GenerationSnapshot (ADR-0011 §14): 모든 generation에 모델/시드/샘플러/CFG/스타일 토큰/네거티브/레퍼런스 ID를 스냅샷으로 저장한다. 같은 스냅샷+고정 시드면 같은 결과가 나와야 한다 ⑥ 멀티패스 (ADR-0011 §10·§11): hires-fix·얼굴 복원·인페인팅 수정은 **승인된 후보에만** 적용한다. 수정은 reroll이 아니라 작은 마스크 인페인팅(한 번에 한 영역)으로, 요소 추가 시 `(요소:1.2)`를 프롬프트 맨 앞에 둔다 ⑦ 재생성 규율 (ADR-0011 §12): 한 번의 재생성에서 프롬프트·시드·모델 중 하나만 바꾼다.
- 함정: 생성 태스크 인자는 `{generation_id}` 단일. 프레임 샷 상태 재계산(`recompute_frame_shot_status`)·배치 부모 집계(`aggregate_parent`/`aggregate_batch_parent`)는 이 서비스에 있고, 워커·cancel·reconcile이 `job_execution.on_generation_terminal`을 통해 호출한다. 집계는 형제 상태를 읽기 전에 부모·샷 행을 잠근다(`lock_row`; SQLite에는 행 잠금이 없어 먼저 `UPDATE t SET pk=pk`로 DB 쓰기 잠금(RESERVED)을 잡아 이후 읽기-쓰기를 직렬화한다, B4R4-M2). 잠금 순서는 자식 행 → 배치 부모 → Video → Shot이며, cancel/retry는 자식 행을 모두 쓴 뒤에 종속 갱신을 한다(ADR-0013 B4a-fix). enqueue·retry 같은 대기 전이도 `mark_frame_shot_pending`으로 샷을 항상 잠그고 다시 읽은 뒤 현재 대기 테이크로 결정한다(이미 generating이어도 건너뛰지 않음, B4a-r2 R2-H1). `cancel_job`의 배치 부모 판정은 부모 잠금 아래 자식 상태를 다시 조회한다(R2-M4). `retry_job`은 `failed`만, job_type별 태스크로 재전송한다(ADR-0013). 잡 행이 없는 옛 스타일 원장 retry는 400 VALIDATION_ERROR(재요청 안내)다(R2-M3).


### provider_input.py
- 목적: 이미지 잡의 provider 입력을 enqueue 시점에 한 번 해석해 스냅샷으로 고정한다 (ADR-0014).
- 인터페이스: `image_snapshot(...)` (→ `generations.params["snapshot"]`), `with_seed(snapshot, seed)`, `draw_seed()`.
- 불변식: ① 스냅샷에 모델 id·설정 해시, 작업, 시드와 출처, 크기, 워크플로 고정 sampler 값, 네거티브, 실제 사용한 레퍼런스(상대경로·역할·가중치), init/mask, 경고를 남긴다. 워커는 이 스냅샷으로 payload를 만든다(키는 `models_lib/contract.py`) ② capability 사전 검사도 여기서 한다 — 미지원 작업·편집 입력은 잡 행·예산 예약·디스패치 전에 4xx ③ 랜덤 시드는 enqueue에서 뽑아 행과 스냅샷에 저장한다. 프롬프트 변형은 시드를 고정한다(한 번에 한 변수).
- 함정: 레퍼런스를 받을 수 없는 모델은 거절하지 않고 텍스트 전용으로 강등하며, 스냅샷에 `references_applied=false`와 `reference_unsupported` 경고를 남긴다. 프롬프트 본문은 스냅샷이 아니라 `Generation.prompt_final`에 있고 워커가 합친다. 썸네일 자식 잡(`params._thumbnail`)은 스냅샷 없이 행 필드로 같은 키를 만드는 legacy 경로를 쓴다(작업 지원 여부는 라우터에서 검사).

### job_execution.py
- 목적: 워커 잡의 실행 소유권과 provider 제출 규칙 (ADR-0013, 계획 D4/D7).
- 인터페이스: `claim(model, id)` (queued→running + 새 `exec_token` + lease, generation이면 attempt 행 생성, 0행이면 None), `guarded_update(ex, values, after=)` / `require` (id+token+running 조건부 UPDATE를 짧은 트랜잭션으로 commit, heartbeat 겸함), `keepalive(ex)` (백그라운드 heartbeat), `submit_poll_fetch` (intent commit → submit → submitted commit → poll → fetch), `classify`/`record_failure` (실패 commit 후 retry|failed|lost 판정), `on_generation_terminal` (배치 부모·샷·Video·멀티샷 프레임·LoRA 종속 갱신), `JOB_TASKS`/`dispatch_target`/`schedule_dispatch` (job_type → 태스크·대상 ID).
- 불변식: ① claim 이후 모든 쓰기는 `id+exec_token+status='running'` 조건이며 0행이면 작업 중단·미게시(파일은 고아) ② provider 폴링 중 DB 트랜잭션을 열어두지 않는다 ②' 워커 산출물(이미지·썸네일·캐릭터·클립·오디오·립싱크)은 실행 토큰 앞 8자를 붙인 시도별 불변 파일로 먼저 쓰고, 경로는 토큰 조건 성공 UPDATE에서만 게시한다. 실패·추월된 시도의 파일은 다음 시도를 막지 않는 고아다(B4a-fix) ③ `submit_state`가 `none`이 아니면 자동 재제출 금지 — 결과 불확실은 `PROVIDER_UNCERTAIN` ④ 실패 기록은 raise·SSE 전에 commit.
- 함정: 확실히 전송 전인 실패만 재시도 대상이다(httpx Connect*/Pool/Proxy 원인 또는 네트워크 원인 없는 확정 거절 코드). 어댑터 내부 업로드 단계의 네트워크 실패도 불확실로 분류된다(보수적). 새 job_type을 추가하면 `JOB_TASKS`에 등록해야 retry·reconcile이 동작한다.

### job_recovery.py
- 목적: 브로커·워커가 잃은 잡의 지속 복구 (ADR-0013, H1).
- 인터페이스: `reconcile_once(now=None) -> dict` (uncertain/requeued/failed/stuck/redispatched/renders_*/multishot_*/batch_parents_aggregated/shots_recomputed/style_redispatched/style_failed 건수).
- 불변식: ① lease 만료 `running` 중 `submit_state` intent/submitted → `failed`+`PROVIDER_UNCERTAIN`, 재전송 없음 ② `none` → `queued`(토큰 제거, retry_count+1) 후 재전송, 재시도 예산 소진 시 `failed` ③ `queued`인데 `RECONCILE_REDISPATCH_AFTER_SEC` 동안 재전송 없던 행 → 1회 재전송(`dispatched_at` 갱신) ④ P1 원장이 `RECONCILE_P1_STUCK_AFTER_SEC` 넘게 queued → `failed` ⑤ 토큰·lease가 모두 없는 `running`(마이그레이션 0012 이전 행)이 `LEASE_TTL_SEC` 넘게 기척이 없으면 `failed`+`PROVIDER_UNCERTAIN`, 재전송 없음 ⑥ 자식이 모두 끝났는데 진행 중인 배치 부모는 재집계, `running` 멀티샷은 훅을 잃은 프레임을 반영한 뒤 항상 전체 프레임으로 재집계(모든 프레임이 끝났는데 세트가 running인 경우 포함), 대기 잡 없는 `frame_generating`/`video_generating` 샷은 재계산하고, 반대로 대기 이미지 테이크가 있는 `draft|frame_done|frame_approved|failed` 샷은 `frame_generating`, 대기 영상 잡이 있는 샷(`video_generating`/`frame_generating` 제외)은 `video_generating`으로 되돌린다(샷 잠금 후 재확인, R2-H1) ⑦ 원장 없는 스타일 추출(전역 키트): `RECONCILE_REDISPATCH_AFTER_SEC`보다 오래된 `queued` 행은 패스마다 재전송(claim이 1회 실행 보장), `RECONCILE_P1_STUCK_AFTER_SEC` 넘게 queued이거나 생성 후 `LEASE_TTL_SEC` 넘게 `running`이면 `failed`(R2-M2) ⑧ 모든 전이는 조건부 UPDATE라 중복 실행·워커와의 경합에 안전.
- 함정: 배치 부모는 lease 만료/레거시 처리에서 제외하고 집계로만 수렴시킨다. 구동은 API lifespan 루프(`RECONCILE_ENABLED`, 기본 켜짐·memory SSE면 꺼짐)와 워커 `worker_ready` 1회.

---

## models_lib/ — 모델 추상화 (Registry + Router + Provider Adapters)

### base.py
- 목적: 모든 어댑터가 지켜야 하는 인터페이스 (설계서 §3.4). **이 파일은 가장 바꾸기 어렵다.**
- 인터페이스: `submit(payload) -> provider_job_id`, `poll(provider_job_id) -> status`, `fetch_result(...) -> bytes|url`, `estimate_cost(payload) -> USD`.
- 불변식: ① 어댑터는 모델의 "호출 규격"만 안다. 라우팅·재시도·폴백은 어댑터 밖(router·tasks) ② 예외는 `core/errors.py`의 18종으로 변환해서 던진다.
- 확장: 인터페이스 변경이 필요하면 ADR을 먼저 쓴다.

### registry.py / router.py / seed.py
- registry: DB(`models`·`model_providers`·`model_pricing`) 기반 조회·캐시. `get_model(model_id)`, `list_by_modality(modality)`.
- router: 명시 지정 → 프로젝트 기본 → 규칙 기반 자동 선택 (설계서 §3.3). 품질 티어는 `quality_rank`로 판단.
- seed: `infra/seeds/models/*.json` 로더. `make seed`는 멱등.
- 불변식: 텍스트 LLM도 `modality='text'`로 레지스트리에 포함된다 (D-16). 이미지·영상만 있는 게 아니다.

### adapters/
| 파일 | 프로토콜 | 비고 |
|---|---|---|
| `stub.py` | 가짜 제공자 | Phase 0 완료 조건 + OSS 데모 모드(`make demo`)용. 절대 삭제 금지 |
| `http_polling.py` | HTTP submit+폴링 | fal/Replicate/직접 API/ComfyUI 공용(ComfyUI는 `infra/seeds/models/comfyui.json` seed). 폴링 간격 기본 2초→최대 10초. 잡마다 총 deadline(`HTTP_JOB_TOTAL_DEADLINE_SEC`, submit→다운로드 끝), deadline·폴링 오류 시 provider cancel 시도 후 `PROVIDER_UNCERTAIN`. 결과는 스트리밍 다운로드 + 매체별 바이트 상한(`HTTP_RESULT_MAX_BYTES_*`) + Content-Type·매직 바이트 검사. 상태 누락·`state_map` 밖 값은 오류(seed가 `missing_state`/`unknown_state`로 명시 허용) (ADR-0014) |
| (`comfyui.py` 없음) | — | 전용 REST+WebSocket 어댑터는 아직 없다. ComfyUI는 위 http_polling seed(`/prompt` submit → `/history` 폴링 → `/view` 다운로드)로 붙는다. 레퍼런스 컨디셔닝(IP-Adapter/InstantID) 워크플로는 seed DSL의 확장 슬롯으로 수용 (ADR-0011 §5, 구현 시 실검증 선행) |
| `cli.py` | CLI 서브프로세스 (non-interactive) | 구독 기반 CLI 에이전트. 첫 대상은 Codex CLI 내장 `image_gen`(gpt-image-2, 구독 로그인으로 API 키 불필요). kind=`cli`. 호출 1회=에이전트 루프 1회전이라 느림. 잡마다 전용 디렉토리(`-C <job dir>`)와 명시 출력 파일(`{output_path}`)을 지시하고 그 파일을 결과로 쓴다. CLI가 경로를 무시했을 때만 `$CODEX_HOME/generated_images/` 후보를 원자적 rename으로 잡 디렉토리에 가져온다(두 잡이 같은 파일을 갖지 않음, ADR-0014). `init_image`/`mask`는 실행 전 거절. 쿼터 초과는 429이나 재시도 금지 (설계서 §3.4). 배치·고빈도에는 local/api 권장. Docker: 워커 이미지에 codex 내장, 호스트의 `~/.codex`를 `/root/.codex`로 마운트 — 호스트에서 `codex login` 1회 필요 |
| `contract.py` / `capabilities.py` / `media_check.py` | (어댑터 공용) | 이미지 잡 provider 입력 계약(`prompt, negative_prompt, seed, width, height, init_image, mask, references…`, enqueue 시 `params.snapshot`에 고정), seed `capabilities.tasks`/`inputs` 사전 검사(미지원 편집 작업·입력은 잡 행·예산 예약 전 400, 레퍼런스 미지원은 텍스트 전용으로 강등 + 경고), 결과 매체 검사 (ADR-0014). 기존 설치는 `make seed`를 다시 실행해야 선언이 반영된다 |

- **새 모델 ≠ 새 파일.** HTTP 호환 모델은 seed JSON 1행으로 끝낸다 (ARCHITECTURE §7.2). CLI 모델도 마찬가지: `provider_id: "cli"` + `capabilities.cli` 1행이면 된다.

---

## media/ — 워커 전용 미디어 처리

| 모듈 | 책임 | 불변식·함정 |
|---|---|---|
| `ffmpeg_ops.py` | 정규화·concat·믹싱·burn-in·인코딩 검증 | 출력은 타임라인 프리셋 준수. API 라우터에서 직접 호출 금지 |
| (`frame_extract.py` 없음) | 첫/마지막 프레임 추출은 `ffmpeg_ops.extract_frame` | 연속 생성·QC의 입력. 추출 실패는 조용히 넘기지 않는다 |
| `thumbnails.py` | 썸네일 생성·캐시 | 생성 png, 썸네일 jpg(q85). 원본 대신 썸네일 우선 서빙 |
| `ocr.py` | PaddleOCR 계열(rapidocr_onnxruntime, 한글 PP-OCRv5 rec) 텍스트 검수 + 렌더 burn-in OCR | `shots.intended_text`와 대조. match: 프레임별 신뢰도 0.5 이상 검출과 비교, 불일치→fail. artifact: 신뢰도 0.5 미만·2자 이상 검출→warn. 인식 모델은 `OCR_MODELS_DIR`(기본 `~/.cache/ai-studio/ocr/`)에 SHA-256 검증 후 캐시. Docker 이미지는 빌드 시 `/opt/ai-studio/ocr`로 받아 두므로 네트워크 없이 동작 |
| `thumbnail_text.py` | 썸네일 타이틀 텍스트 합성 (PIL) | 폰트는 `defaults.CJK_FONT_CANDIDATES`에서 첫 번째로 존재하는 것. Docker 이미지는 `fonts-noto-cjk` 포함 |
| `loudness.py` | 음량 측정 (loudnorm) | 피크 -1 dBTP, LUFS -16±3 |
| `subtitle_render.py` | 자막 burn-in용 ASS 생성 (libass) | **한글은 이미지 모델에 맡기지 않는다.** 서버 사이드 ASS 렌더 (D-17). dual 모드는 트랙별 스타일(S0/S1) + 위치 충돌 시 후순위 트랙을 반대편으로 이동 |
| `qc_checks/` 항목 없음 | 개별 QC 검사는 `services/qc_orchestrator.py`의 `CHECKS` 레지스트리에 인라인 | 검사 13종(P0 형식)이 설계서 §10.1에 고정된 P0 범위이므로 단일 파일 응집 유지 (+ 내용 2종은 advisory, 총 15종) |

---

## Phase 5 (P1) 서비스

### prompt_snippets.py
- 목적: 프롬프트 스니펫 보관함 CRUD (P1-8, 설계서 §4.20).
- 인터페이스: `list_snippets(category/tag/scope/project_id)`, `create/update/delete_snippet`, `get_snippet`.
- 불변식: ① `project_id IS NULL` = 전역 스니펫 ② scope=project 조회는 project_id 필수 ③ 스니펫 CRUD는 무료 (잡·비용 없음) ④ 네거티브 프롬프트 라이브러리(ADR-0011 §3): 품질/신체/텍스트/구도 4세트의 기본 세트를 전역 스니펫으로 시드한다. `prompt_assembly`는 이 세트를 기본 네거티브로 사용한다.
- 함정: 거대한 복사-붙여넣기 네거티브를 기본값으로 넣지 않는다 — 모델과 싸워서 오히려 해가 된다.

### camera.py
- 목적: 카메라 프리셋 + 180도 규칙 체크 (P1-2).
- 인터페이스: `ensure_builtin_camera_presets` (6종 멱등 시드), `list_presets`, `get_preset`, `check_camera_axis(scene_id) -> violations`.
- 불변식: ① 빌트인 프리셋은 API에서 수정 불가 ② 180도 체크는 left↔right 연속 뒤집힘만 위반, center·NULL은 중립 ③ 결과는 경고 목록이지 에러가 아님 ④ 생성용 카메라 무브 enum (ADR-0011 §8): 영상 프롬프트의 카메라는 자유 텍스트가 아니라 명명된 무브 enum에서 선택한다 (`contracts/enums.json`의 `camera_movement`, 22종 — dolly in/out·orbit·orbit_360·pan·tilt·truck·crane·jib up/down·zoom·crash zoom·dolly zoom·whip pan·snorricam·hyperlapse·handheld·FPV·static). 한 클립당 1무브·1액션·1조명 규칙. 프리셋 이름은 provider 프롬프트에 verbatim 전달된다.
- 함정: 무브 enum은 "연출 용어"이지 물리 시뮬레이션이 아니다. provider가 모르는 이름은 무시될 수 있으므로 seed DSL의 카메라 슬롯과 1:1로 매핑한다.

### generations.py (P1-3 추가분)
- 인터페이스 추가: `enqueue_variations(count 2..4, variation_axis="prompt"|"seed")`.
- 불변식: ① 명시 seed는 `shot.seed`에 고정, 없으면 핀된 seed 재사용, 둘 다 없으면 호출별 랜덤 (핀하지 않음) ② seed 축 = 1 parent + N children, prompt 축 = N개 단일 배치 ③ variations도 budget guard + Idempotency-Key 적용 ④ 배치 → 선택 → 고정 → 반복 (ADR-0011 §6): 탐색용 variations는 랜덤 시드로 뽑고, 선택된 테이크의 시드를 `shot.seed`에 핀한 뒤 프롬프트만 다듬는다.

### multishot.py
- 목적: P1-1 멀티샷 커버리지 — 승인 프레임 1장을 6개 앵글로 자동 전개 (F-09, §4.18).
- 인터페이스: `request_coverage(db, shot_id, model_id?) -> {multishot_set_id, job_id, estimated_cost_usd}`, `run_multishot(db, set_id)` (워커, task arg = set id 하나, ADR-0008).
- 불변식: ① 베이스 샷은 `frame_approved` 필수 (409 APPROVAL_REQUIRED) ② 6개 앵글 샷은 베이스 바로 뒤에 삽입 (order_index shift) ③ 상태 전이 `queued→running→done|partial|failed` ④ 장면 상태는 `state_snapshot`에 잠금 ⑤ 세트 집계(`aggregate_set`)는 세트 행을 잠근 뒤 모든 프레임 상태로 계산한다(프레임 훅·reconcile 공통, 잠금 순서 프레임 → 세트). 진행 30% 이벤트는 자식 dispatch보다 먼저 등록되고, 집계 SSE는 commit 뒤에 보낸다(B4b-fix).

### lora.py
- 목적: P1-4 LoRA 학습 잡 관리 (§4.7). **학습 백엔드는 stub** — 워커가 플레이스홀더 `.safetensors`를 기록하고 `ready`로 표시한다. 실제 학습(GPU)은 외부 백엔드 연동 시 교체.
- 인터페이스: `request_train(db, character_id, training_image_ids, rank?, steps?, trigger_word, confirm=False)`, `complete_train` / `fail_train` (워커), `get_lora`, `list_loras`.
- 불변식: ① 8장 미만 + `confirm` 없음 → 422 `LORA_TOO_FEW_IMAGES` ② 완료 시 가중치 파일은 `projects/{p}/characters/{c}/lora/{lora_id}.safetensors` (상대경로). 파일명은 사용자 입력(trigger word)과 무관한 LoRA 행 UUID다(경로 안전, 계획 B2) ③ 완료 후 캐릭터 `consistency_method='lora'` ④ 비용은 generations 원장 행(`job_type='lora_train'`)으로 집계.

### style_extract.py
- 목적: P1-7 참고 이미지 3~8장에서 스타일 키트 필드 추출.
- 인터페이스: `request_extract(db, kit_id, source_asset_ids) -> {job_id, estimated_cost_usd, warning?, images_used}`, `complete_extract(db, job_id)` (워커, stub 고정값 기록), `fail_extract`, `on_ledger_terminal`/`on_ledger_retried` (원장 cancel·reconcile·retry 연동), `require_job_row` (잡 행 없는 원장 retry 거절).
- 불변식: ① 3장 미만은 warning + 진행 ② 8장 초과는 처음 8장만 ③ 추출 후 `kit.version += 1` ④ 잠긴 키트(`is_locked`)는 409 `STYLE_KIT_LOCKED` ⑤ 요청 1건 = `style_extractions` 행 1개(전역 키트 포함). 그 id가 태스크 인자이자 `job_id`이고, 프로젝트 키트의 비용 원장 generation도 같은 id다 ⑥ 워커는 그 행만 queued→running으로 claim하고 같은 트랜잭션에서 키트·행·원장을 완료한다. terminal 행은 no-op(재전달·중복 1회 효과) ⑦ 입력 스냅샷(`source_ids`)은 잡 행에 있고 완료 시 `kit.extraction_source_ids`로 옮긴다 ⑧ 잠금 순서는 모든 경로(워커·실패 기록·retry·cancel·reconcile)에서 원장 generation → `style_extractions` → 키트다(R2-M1, 40P01 교착 방지).
- 함정: 마이그레이션 0014 이전에 queued였던 원장은 잡 행이 없어 메시지가 no-op이고, reconcile (c)가 `failed`로 끝낸다. 그런 원장의 retry는 400으로 거절된다(스냅샷을 지어내지 않음 — 키트에서 추출 재요청). 전역 키트 잡은 원장이 없어 reconcile (h)가 `style_extractions` 행으로 직접 재전송·실패 처리한다(행에 `dispatched_at`이 없어 창 단위 1회가 아니라 패스마다 재전송).

### thumbnail.py + media/thumbnail_text.py
- 목적: P1-9 썸네일 배치 생성기 (§10.5).
- 인터페이스: `request_thumbnails(db, project_id, timeline_id?, title_text?, count=4) -> {job_id(배치 부모), estimated_cost_usd, count, child_job_ids}`, `render_thumbnail(png_bytes, title_text) -> PNG bytes` (1280×720 캔버스 + 타이틀 밴드).
- 불변식: ① **타이틀 텍스트는 이미지 모델이 렌더링하지 않고 PIL 후처리로만 합성** (§10.5) ② count 2~8 ③ 자식은 `is_batch` 부모-자식 generations 구조 (D-25).

### episode.py
- 목적: 에피소드 CRUD 얇은 래퍼 (P1-10). deep copy는 `services/episode_clone.py` 재사용 (D-23: 새 ID everywhere, 복사 샷은 draft, generations/videos/audio 미복사).

### subtitle_edit.py
- 목적: P1-6 자막 기반 편집 — 분석·제안 전용. 큐 시간 윈도우와 타임라인 클립 절대 윈도우를 겹침 매칭 → `suggested_dur_sec = 글자수 / cps`.
- 불변식: ① DB 쓰기 없음 — 타임라인을 직접 바꾸지 않는다 (사용자 확정은 `PATCH /timelines/{id}`) ② 트랙·타임라인 프로젝트 불일치 → 400.

### dialogue.py
- 목적: TTS 대사 잡 생성과 음성 복제 (P1-5, §8.1/F-13).
- 인터페이스: `enqueue_dialogue(shot_id, character_id, text, emotion_weights?, model_id?, target_duration_sec?) -> {job_id, estimated_cost_usd}`, `clone_voice(character_id, sample_asset_id) -> voice_id`, `get_audio_job(audio_id)`.
- 불변식: ① 감정 가중치는 0~1, DB `emotion_weights`에 그대로 저장 (stub은 무시, 실제 어댑터용) ② TTS 비용은 per_1k_chars/per_char ③ 큐 100 초과 시 429, 예산 100% 시 403 ④ 워커 인자는 audio generation id 단일 (ADR-0008).
- 함정: `AudioGeneration.status='done'`은 "로컬 등록 완료" 의미 — TTS 잡은 queued→running→done을 거친다. `target_duration_sec`는 워커가 `services.lipsync`로 사후 맞춤한다.

### lipsync.py
- 목적: 더빙 립싱크 — TTS 오디오를 클립 길이에 맞춰 속도 미세 조정 (F-13).
- 인터페이스: `fit_audio_to_duration(src, target_sec, dst) -> {dst_path, src_duration_sec, target_sec, applied_rate, warning}`.
- 불변식: ① 허용 밴드 ±10% (0.9~1.1) — 초과 시 조정하지 않고 `warning` 반환 ② `|rate-1|≤0.005`면 복사만.
- 함정: atempo는 0.5~2.0 범위지만 설계상 ±10%만 사용한다. 경고를 무시하고 늘리면 음성이 깨진다.

### broll.py
- 목적: 내레이션 기반 B-roll 자동 제안 (P1-5) — 자막 텍스트 키워드와 배경/장소 에셋 매칭.
- 인터페이스: `suggest_broll(project_id, timeline_id, limit=5) -> {suggestions[{asset_id, asset_name, keyword, score}], keywords, narration_chars}`.
- 불변식: ① 내레이션 출처 = 타임라인 `subtitle_track_ids` 자막 큐 (없으면 프로젝트 기본 트랙) ② 후보 에셋 = kind background|location ③ 점수 = 태그 3 + 이름 2 + 프롬프트 1.
- 함정: 간이 키워드 매칭이라 유의어/문맥은 못 잡는다 (LLM 시맨틱 매칭은 후속 개선).

---

## tasks/ — Celery (큐별 1파일)

| 파일 | 큐 | 담당 |
|---|---|---|
| `tasks_image.py` | `q.image` (동시 4) | 이미지 생성·배치·inpaint/outpaint |
| `tasks_video.py` | `q.video` (동시 2) | I2V/T2V·연속 생성. 키프레임-퍼스트 (ADR-0011 §7): 스틸 키프레임을 먼저 생성·승인하고 I2V 프롬프트는 **모션만** 묘사한다 (피사체·구도·색감 재묘사 금지) |
| `tasks_audio.py` | `q.audio` (동시 2) | TTS·음성 복제 |
| `tasks_post.py` | `q.post` (동시 2) | ffmpeg·썸네일·QC·렌더 |
| `tasks_p1.py` | (파일만 분리, 큐는 `q.post` 공용) | 멀티샷·LoRA 학습·스타일 추출. 태스크명은 `q.p1.*`이나 라우팅은 `q.post` 큐로 — 큐는 4개 고정 (ARCHITECTURE §8, ADR-0006). 워커 `include`에 등록 필수 |

- 태스크 시그니처: 대상 행 ID 단일 인자 고정 (ADR-0008). 프롬프트 원문을 인자로 넣지 않는다 (DB가 진실 공급원).
- 진행률: 단계마다 `publish("job_events", {job_id, status, progress_pct, stage, eta_sec})`.
- 재시도: 최대 3회, 5→10→20초 지수 백오프+지터. 소진 시 `auto_fallback`이면 `fallback_model_ids` 순서대로 각 1회 (재시도 없음). 단, 자동 재시도는 provider에 확실히 전송되지 않은 실패(`submit_state=none`, 연결 실패·확정 거절 코드)만이다. `submit_state=intent` 이후 결과가 불확실한 실패는 `failed`+`PROVIDER_UNCERTAIN`이고 self.retry·재전달·reconcile 어디서도 자동 재제출하지 않는다 (ADR-0013 D7). 수동 retry는 `failed`에서만, job_type별 태스크로 간다.
- 실행 소유권: 태스크는 시작 시 행을 claim(`exec_token` + lease)하고 이후 쓰기는 토큰 조건부다. 중복 전달·terminal 행은 no-op으로 ACK된다(`task_acks_late` + `task_reject_on_worker_lost`). 산출물 파일명에 토큰 앞 8자를 붙여 시도마다 새 파일을 쓴다 (ADR-0013).

## core/ — 공통 기반

| 모듈 | 책임 |
|---|---|
| `config.py` | 환경변수 로딩. `.env.example`에 없는 변수를 코드에서 읽지 않는다. 빈 값은 "미설정"으로 취급(`env_ignore_empty`)해 선택 변수는 기본값을 쓴다. `RECONCILE_ENABLED`(미설정이면 켜짐, `SSE_BACKEND=memory`면 꺼짐) |
| `errors.py` | 오류 18종 정본 (Phase 5에서 LORA_TOO_FEW_IMAGES, Phase 6에서 DUPLICATE_KEYFRAME, ADR-0013에서 PROVIDER_UNCERTAIN, 계획 D2에서 IDEMPOTENCY_CONFLICT 추가). 새 오류는 `errors.py`에 추가 → `packages/contracts/errors.json`에 미러 → `make gen-types` |
| `defaults.py` | 모든 `[가정]` 기본값의 단일 집합. 코드 내 매직넘버 금지. lease·reconcile·provider HTTP 상한·멱등 TTL·CJK 폰트 후보도 여기 |
| `sse_bridge.py` | Redis Pub/Sub → SSE 브릿지. 이벤트명·payload 필드 정본은 `packages/contracts/sse_events.json` |
| `middleware.py` | 에러 포맷 통일 (멱등키는 `services/idempotency.py`) |

## files/ — 저장 규칙 구현 (설계서 §11)

| 모듈 | 책임 |
|---|---|
| `paths.py` | `PROJECTS_ROOT` 해결. **DB에는 상대경로만** 저장되도록 강제. `resolve()`는 절대경로·`..`를 거부하고, symlink까지 따라간 실제 위치가 루트 안에 있어야 한다 |
| `naming.py` | `{uuid}[_{batch}].{ext}` 명명. 사람이 읽는 이름은 DB 필드가 담당. 워커 산출물은 여기에 시도 태그(실행 토큰 앞 8자)를 더한다(예: `{generation_id}_{batch}_{tag}.png`, ADR-0013) |
| `immutability.py` | `succeeded` 산출물 덮어쓰기 방지 가드. 임시 파일 → 하드링크(불가하면 배타 생성)로 게시하므로 경쟁 중에도 기존 파일을 덮지 않는다 |
| `orphans.py` | 고아 파일 주간 검출·리포트. **자동 삭제 금지** |

---

## models/ — 테이블별 1줄 매핑 (설계서 §4)

| 파일 | 테이블 | 한 줄 |
|---|---|---|
| `project.py` | projects | 월 예산, 기본 스타일 키트·출력 프리셋 |
| `story.py` | stories, beats, episodes | beats는 별도 테이블 (JSONB 배열 금지, D-12) |
| `character.py` | characters, character_references, character_sheets | 수정 시 version 증가 → 의존 샷 `frame_stale` (D-31). CharacterPack (ADR-0011 §4·§5): 캐릭터마다 canonical 레퍼런스 이미지 + 외형 고정 문구(identity clause). 씬 프롬프트에서 얼굴 재묘사 금지. IP-Adapter/InstantID 레퍼런스 컨디셔닝 수용 (구현 시 실검증 선행) |
| `asset.py` | assets | 전역/프로젝트 스코프 구분 |
| `style.py` | style_kits, category_packs, style_extractions | `style_extractions` = 스타일 추출 요청 1건당 잡 행(0014, ADR-0013). 버전 잠금. 카테고리 팩 3종 seed. 스타일 바이블 (ADR-0011 §13): 잠긴 키트(`is_locked`)의 팔레트·조명·렌즈·그레이드 고정 토큰은 `prompt_assembly`에서 모든 프롬프트에 verbatim 주입된다. **스타일 권위는 프로젝트당 하나** — 잠긴 키트 사용 중에는 샷 단위 스타일 재정의 불가 (Higgsfield Manual Style 방식: 대체이지 병합이 아님). 다른 룩이 필요하면 `duplicate`로 새 버전 생성 후 재정의 (잠긴 키트 직접 수정 불가) |
| `production.py` | scenes, shots, shot_references | shot_references는 FK 2개+CHECK (D-11). `shots.frame_stale_rev` = 무효화 리비전(0015, `Shot.mark_frame_stale`) |
| `generation.py` | generations, generation_attempts, videos | 배치는 부모+자식 구조 (D-25). 실행 필드 `exec_token`·`lease_expires_at`·`heartbeat_at`·`submit_state`·`dispatched_at`(0012), 예산 기간 `reserved_at`(0016) |
| `timeline.py` | timelines, timeline_clips | 시간 단위는 초(float) 통일 (D-27). 렌더 실행 필드 `exec_token`·`lease_expires_at`·`heartbeat_at`·`render_meta`(0013) |
| `audio.py` | audio_generations, timeline_audio_clips | TTS·더빙·BGM. 실행 필드(0012)와 `reserved_at`(0016)은 generations와 같다 |
| `subtitle.py` | subtitle_tracks, subtitle_cues, subtitle_styles | 사이드카가 정본 (D-14) |
| `qc.py` | qc_reports | 스냅샷 JSON 보관. `seq` = 타임라인별 발행 순번(최신 판정 기준, 0015). `order_ambiguous` = 순서 불명 legacy 동률(게이트 불인정) |
| `system.py` | models, model_providers, model_pricing, llm_calls, export_presets, prompt_snippets, idempotency_records, budget_holds | 레지스트리 실체. `idempotency_records`(멱등 응답, 계획 D2)·`budget_holds`(불확실 시도 예약 보류, D1/D7)는 0016 |
| `multishot.py` | multishot_sets, multishot_frames | P1-1 (Phase 5) |
| `camera.py` | camera_presets | P1-2 (Phase 5) |
| `lora.py` | lora_models | P1-4 (Phase 5) |
| `world_bible.py` | world_bible_entries | P2 (Phase 6, ADR-0010) |
| `camera_previz.py` | camera_keyframes | P2 (Phase 6, ADR-0010) |

## services/ — 도메인 로직 (추가 카드)

### asset.py
- 목적: 에셋(이미지·오디오) 업로드 저장 + 목록·삭제 (설계서 §4.10).
- 책임: 업로드 검증·파일 저장, scope별 목록(project|global|all), 참조 중인 에셋 삭제 방지.
- 인터페이스: `upload_asset(db, data, filename, kind, name, project_id, prompt_fragment, tags) -> (asset_id, image_path)`, `list_assets(db, project_id, scope="all", kind?)`, `delete_asset(db, asset_id)`.
- 불변식: ① 이미지 확장자는 `.png/.jpg/.jpeg/.webp`, `kind=audio`는 `.wav/.mp3/.m4a/.aac/.ogg/.flac` ② `UPLOAD_MAX_BYTES` 초과 업로드 거부 ③ `ShotReference`에 참조 중이면 삭제 불가 ④ DB에는 상대경로만(`paths.assert_relative`).
- 의존성: core/contracts(check_enum), core/defaults, files/paths.
- 함정: `kind` enum 유효성 검사는 서비스에서 — 라우터의 `kind=background`는 폼 기본값일 뿐이다.

### audio.py
- 목적: P0 오디오(업로드 BGM/SFX) 등록 + 타임라인 배치 (설계서 §4.14).
- 책임: 업로드 에셋을 `audio_generations` 행으로 등록(ffmpeg probe), 타임라인 `timeline_audio_clips` 배치·삭제.
- 인터페이스: `register_from_asset(db, asset_id, kind="bgm", project_id?)`, `list_audio(db, project_id, kind?)`, `place_clip(db, timeline_id, audio_id, start_sec, duration_sec?, gain_db=0.0)`, `delete_clip(db, clip_id)`, `list_clips(db, timeline_id)`.
- 불변식: ① P0 업로드 오디오는 `status='done'` 로컬 등록 (TTS/더빙은 P1) ② 배치 대상 오디오는 같은 프로젝트 + `status='done'` + `output_path` 필수 ③ 배치·삭제 시 타임라인 `bump_content_version` (QC stale 게이트 연동) ④ 타 프로젝트 에셋 배치 불가.
- 함정: `register_from_asset`은 에셋 파일을 복사하지 않고 `image_path`를 공유한다. 이 서비스는 "생성"이 아니라 업로드 등록만 담당한다.

### character.py
- 목적: 캐릭터 CRUD·레퍼런스·워드로브 (설계서 §4.7).
- 책임: 캐릭터 생명주기, 레퍼런스 이미지 관리(primary 1장), 의상 세트, 마스터 썸네일 조회.
- 인터페이스: `create/get/list/update/delete/archive_character`, `add/list/update/delete_reference`, `create_wardrobe/list_wardrobe`, `get_master_thumbnail`.
- 불변식: ① `is_primary` 레퍼런스는 캐릭터당 1장 — 새 primary 지정 시 기존 해제 ② 외형 필드(`_APPEARANCE_FIELDS`: appearance_prompt·negative_prompt·consistency_method·default_strength·style_notes) 변경 시 `version += 1` + 테이크 있는 의존 샷 `frame_stale=true`·`frame_stale_rev += 1` (D-31, `Shot.mark_frame_stale`, B4R4-M1) ③ 샷에서 참조 중이면 삭제 불가(아카이브 권장) ④ 삭제 시 디스크 디렉토리도 함께 제거.
- 함정: `version += 1`은 모든 필드 변경 시 적용되지만, `frame_stale` 마킹은 외형 필드 변경 시에만 적용된다.

### character_pipeline.py
- 목적: 캐릭터 마스터/시트 생성 잡 등록 (D-18, D-19).
- 책임: Generation 행 생성(`params`의 `_character_id/_reference_role/_source` 계약), 워커 완료 후처리용 `reference_for_job`.
- 인터페이스: `enqueue_master(db, character, prompt?, model_id?, project_id) -> uuid`, `enqueue_sheet(db, character, project_id, views?, expressions?, outfits?, poses?) -> (job_ids, cost)`, `reference_for_job(db, generation, image_rel_path) -> CharacterReference` (워커 호출).
- 불변식: ① enqueue 전 `budget_guard.check_budget` (403, 시트는 합산 비용으로 fail fast — 부분 배치 없음) ② 시트 역할명은 `reference_role` enum 강제 (expression_/outfit_ prefix 매핑) ③ 마스터 재생성 시 기존 primary 강등 (ADR-0005, one-primary partial unique index 충돌 회피) ④ 글로벌 캐릭터도 요청 프로젝트 컨텍스트에서 생성.
- 의존성: models_lib(router·registry), services(budget_guard, cost_estimator), dispatch_after_commit.
- 함정: `reference_for_job`은 API가 아니라 워커(tasks.post_generation)가 호출하는 계약이다.

### exports.py
- 목적: 출력 프리셋 관리 (P0-18, ADR-0004). EDL/XML 본문 빌더는 `export_edl.py`.
- 책임: 빌트인 프리셋 4종 멱등 시드, 커스텀 프리셋 CRUD.
- 인터페이스: `ensure_builtin_presets(db) -> int`, `list_presets(db, project_id?)`, `create_custom_preset(db, project_id, name, aspect_ratio, resolution, fps=24, codec="h264", audio_codec="aac", burn_subtitles=False)`, `update_custom_preset(db, preset_id, fields)`, `delete_custom_preset(db, preset_id)`.
- 불변식: ① 빌트인(`project_id IS NULL`)은 API로 수정 불가 — "duplicate해서 커스터마이즈" ② 타임라인이 쓰는 프리셋은 삭제 불가(재할당 선행) ③ 이름 빈 문자열 불가 ④ 수정 가능 필드는 7종 화이트리스트.
- 함정: 빌트인 4종은 YouTube 16:9 / Shorts 9:16 / Reels 9:16 / TikTok 9:16.

### file_serve.py
- 목적: 정적 파일 서빙 경로 해결 (설계서 §5, 로컬 전용·CDN 없음).
- 인터페이스: `resolve_file(rel_path) -> Path`.
- 불변식: ① 경로 순회(traversal)·절대경로·루트 밖을 가리키는 symlink·누락 파일은 전부 404 (상세 사유 노출 금지, `paths.resolve` 경유) ② DB 상대경로 + `PROJECTS_ROOT` 기준 해석 (요청 시점 해석이라 테스트의 PROJECTS_ROOT 변경 반영).
- 함정: 라우터는 이 함수의 결과에만 의존한다 — 직접 경로 조립 금지.

### model_registry.py
- 목적: 모델 피커용 레지스트리 읽기 + 운영자 오버레이 편집 (설계서 §3.2, §8.1).
- 인터페이스: `list_models(db, modality?, status="active")`, `get_model(db, model_id)`, `update_model(db, model_id, status?, fallback_model_ids?)`.
- 불변식: ① 읽기 전용 뷰 — 쓰기는 status·fallback_chain 오버레이만 ② 오버레이는 시드 JSON에서 복사한 DB 행 upsert, 이후 레지스트리는 DB 행 우선 ③ fallback 모델은 전부 존재해야 함 (없으면 404) ④ modality/status 필터는 DB 오버레이 반영 후 적용.
- 스텁 경계: 시드 JSON의 `stub.*` 모델(`stub.t2i-fast`, `stub.t2i-quality`, `stub.text`, `stub.t2v-*`, `stub.i2v-*`, `stub.tts`, `stub.lora-train`, `stub.style-extract`)이 기본 활성 — Phase 0·`make demo`용. 실제 모델 연동 시 status 비활성화 또는 오버레이로 교체.
- 함정: `modality='text'` LLM도 레지스트리에 포함된다 (D-16) — 이미지·영상만 있는 게 아니다.

### project.py
- 목적: 프로젝트 CRUD·아카이브·복제.
- 인터페이스: `create_project`, `list_projects(is_archived=False)`, `get_project` (요약 `_enrich` 포함), `card_stats(db, project_ids)`, `update_project`, `delete_project(confirm)`, `archive_project`, `unarchive_project`, `duplicate_project`.
- 카드 요약(P0_SCREENS §1, `card_stats`): `episode_count`, `scene_count`, `thumbnail`(첫 씬 첫 샷의 승인 프레임 상대경로 또는 null, `GET /files/{path}`), `shot_count`, `approved_shot_count`, `clip_count`(done 영상), `monthly_cost_actual_usd`(비용 요약 `total_actual_usd`와 같은 이번 UTC 달 지출), `monthly_cost_estimated_usd`. generations·audio의 월 기간은 `reserved_at` 기준(budget_guard와 같음).
- 불변식: ① 삭제는 `?confirm=true` 없으면 거부 ② 아카이브는 프로젝트 디렉토리 zip 후 `is_archived=true` (동기 수행) ③ 복제는 `services/episode_clone.duplicate_project` 재사용 — 신규 ID everywhere ④ 생성 시 `projects/{id}/` 디렉토리 + `project.json` 생성.
- 함정: 목록은 `card_stats`가 프로젝트 수와 무관한 고정 개수의 그룹 쿼리로 요약을 만든다(N+1 없음). 다만 목록 조회에 페이징은 없다.

### story.py
- 목적: 스토리·비트 CRUD + stub LLM 시놉시스/비트 생성 + 대본 임포트 (G-5, G-12).
- 책임: 프로젝트당 1개 Story 자동 생성, 비트 순서 관리, txt/fountain/fdx 대본→씬 임포트.
- 인터페이스: `get_story`, `update_story`, `generate_synopsis(db, project_id, logline, model_id?) -> (llm_call_id, synopsis)`, `generate_beats(db, project_id, act_count?, model_id?) -> (llm_call_id, beats)`, `list/create/update/delete_beat`, `import_script(db, project_id, content, script_format="txt") -> scenes_created`.
- 불변식: ① LLM 호출은 동기 — API가 201로 결과 반환 (큐 없음) ② 비트 생성은 기존 비트 전부 교체 (원자적) ③ 비트 생성 전 시놉시스 필수 ④ `LLMCall` 행에 템플릿 버전(`SYNOPSIS_TEMPLATE`/`BEATS_TEMPLATE` v1.0) 기록 ⑤ 비트 `order_index`는 0부터 연속 유지 (삽입·삭제 시 재정렬).
- 스텁 경계: 텍스트 LLM은 stub 어댑터의 결정적 canned 출력 — 실제 제공자는 어댑터 도착 시 교체. Phase 1은 API 키 없이 동작한다.
- 함정: 비트 생성은 "추가"가 아니라 "교체" — 기존 비트가 사라진다.

### style_kit.py
- 목적: 스타일 키트 CRUD·잠금·복제 + 카테고리 팩 (설계서 §4.17, G-17, ADR-0013).
- 인터페이스: `list/create/update/lock/duplicate_style_kit`, `list_category_packs`, `seed_category_packs(db, seed_dir) -> int`.
- 불변식: ① 잠긴 키트(`is_locked`) 수정 불가 — 복제 후 편집 ② 수정 시 `version += 1` ③ 카테고리 팩 시드는 code 기준 멱등 upsert, `is_builtin=true`.
- 함정: 잠긴 키트의 스타일 토큰은 `prompt_assembly`에서 모든 프롬프트에 verbatim 주입된다 (스타일 바이블, ADR-0011 §13) — 잠금 해제는 없고 복제가 정석이다.

### subtitles.py
- 목적: 자막 트랙·큐·스타일 관리 (F-13, P0-15, 설계서 §4.16). 큐 텍스트가 정본(G-09), burn-in은 렌더 시점만(D-14).
- 책임: 트랙 CRUD, 큐 원자 교체+검증, 스타일 upsert, SRT/VTT 내보내기, 대사→큐 휴리스틱 생성.
- 인터페이스: `list/create/delete_track`, `get_cues`, `save_cues(db, track_id, cues)` (원자 교체), `get_style`/`save_style`, `to_srt`/`to_vtt`, `generate_cues_from_dialogue`.
- 불변식: ① `start_sec > 0`, `end_sec > start_sec`, 트랙 내 겹침 → 422 (SUBTITLE_OVERLAP) ② 큐 길이 1~7s 이탈은 거부가 아니라 QC warn (`subtitle_timing`) ③ 큐·스타일·모드·트랙 변경은 영향 타임라인 `content_version` bump (QC stale). 큐·스타일·트랙 삭제와 타임라인 트랙 선택 변경은 같은 트랜잭션에서 프로젝트 행 잠금(`lock_subtitle_scope`)을 먼저 잡고 그 뒤에 선택 타임라인을 읽는다(B4B-H3, 잠금 순서 프로젝트 → 타임라인) ④ 첫 트랙이 자동 default ⑤ 스타일 미저장 시 미저장 기본값 반환 (Noto Sans KR 48px 등, 영속화 안 함).
- 함정: `save_cues`는 전체 교체 — 부분 수정 API가 없다. `generate_cues_from_dialogue`는 2.5s 큐를 0.5s 간격으로 이어 붙이는 출발점용 휴리스틱이다.

### timelines.py
- 목적: 타임라인 조립·편집 (F-12, P0-14). 컷 리스트 정본은 `timeline_clips` (ADR-0007, D-12), API엔 `clip_order` 배열로 노출.
- 책임: 타임라인 CRUD, 클립 원자 교체·검증, 자동 조립(build-from-shots), `content_version` 관리.
- 인터페이스: `list/create_timelines`, `get_clip_order`, `update_timeline(name?, clip_order?, subtitle_mode?, subtitle_track_ids?)`, `build_from_shots`, `bump_content_version`.
- 불변식: ① `status='rendering'` 중엔 읽기 전용 — 수정 시 409 RENDER_LOCKED ② 클립은 샷·비디오 쌍 검증 + `status='done'` 비디오만 + 범위를 실제 길이에 clamp ③ 자막 모드당 트랙 수 제한 (single 1개, dual 2개) ④ 렌더 내용을 바꾸는 수정은 `content_version += 1` ⑤ 렌더 내용을 바꾼 finished 타임라인만 `draft`로 복귀 — 현재 행에 대한 조건부 UPDATE(`done`일 때만)라 동시에 commit된 render claim(`rendering`)을 덮지 않는다. 이름 변경은 상태를 바꾸지 않는 조건부 UPDATE(렌더 중이면 409) ⑥ 트랙 선택·자막 모드 변경은 자막 편집과 같은 프로젝트 잠금을 먼저 잡는다(B4B-H3/H4).
- 함정: `clip_order`는 read model — DB 테이블은 `timeline_clips`다. `build-from-shots`는 각 샷의 최신 done 클립만 사용한다.

### videos.py
- 목적: 샷 영상 생성(I2V/T2V) 잡 등록 (F-10, P0-13).
- 인터페이스: `enqueue_video(db, shot_id, mode, motion_prompt, duration_sec?, model_id?) -> {job_id, video_id, estimated_cost_usd}`, `list_videos`, `latest_done_video`.
- 불변식: ① I2V는 승인 프레임 필수 — 미승인 시 409 APPROVAL_REQUIRED ② T2V는 승인 없이 가능 (서버는 차단 안 함) ③ `motion_prompt` 필수 + 길이 상한 (`PROMPT_MAX_CHARS`) — 모션만 묘사 (시맨틱 규칙은 UI/API 문서에서 안내, 기계적 검증 불가) ④ duration은 모델 `max_duration_sec`으로 clamp (N-01) ⑤ enqueue 전 큐 포화(429)·예산(403) 체크 ⑥ 워커 인자는 generation id 단일.
- 함정: 등록·retry 시 샷은 `mark_video_shot_pending`으로 잠그고 다시 읽은 뒤 `video_generating`으로 둔다(잠금 순서 generation → Video → Shot, R2-H1). 등록 시 `Video.status='generating'`, `output_path`는 워커가 사후 채움. 해상도는 프로젝트 화면비→기본값, 워커가 probe 실측으로 덮어쓴다.

### vision_client.py
- 목적: 제네릭 OpenAI-호환 vision 클라이언트 — QC 내용 검수(advisory)용 (P2, Unit C).
- 인터페이스: `available() -> bool` (base_url+model 둘 다 설정 시만 True), `score_shot(image_paths, system_prompt, user_text, max_tokens=512, transport?) -> VisionScore(0..100, notes)`.
- 불변식: ① 벤더 SDK 없음 — 순수 httpx로 `POST {base_url}/chat/completions` ② 설정은 호출 시점에 env에서 읽음 (`QC_VISION_BASE_URL`, `QC_VISION_MODEL`, 선택 `QC_VISION_API_KEY`, `QC_VISION_TIMEOUT_SEC`) — 테스트 monkeypatch 가능 ③ 이미지는 base64 data URL로 전송 (원격 엔드포인트가 로컬 파일을 읽을 수 없으므로) ④ 실패는 `VisionError`로만 — 서드파티 traceback 누출 금지 ⑤ 응답의 `score`는 0..100 clamp, notes는 500자 절단.
- 스텁 경계: 미설정 시 `available()=False` → QC 내용 2종은 `stub=true` + warn 유지 (기존 stub 동작). 설정해도 advisory는 warn까지만 — 렌더 게이트 차단 불가.
- 함정: `temperature=0.0` 고정 + `response_format: json_object` — JSON이 아니면 VisionError.

---

## routers/ — 리소스별 1줄 매핑 (설계서 §8.1)

`projects` `stories` `characters(+lora/train, +voice/clone)` `assets(+upload)` `style_kits(+extract)`
`category_packs` `scenes(+camera-check)` `generations`(프레임·배치·리테이크·inpaint·승인·잡 조회)
`videos` `timelines` `subtitles(+apply-to-timeline)` `audio(+dialogue, +broll-suggest)` `qc` `costs`
`models`(레지스트리 상태) `exports`(+timeline export: xml|edl) `events`(SSE) `snippets`(P1-8) `camera_presets`(P1-2)
`variations`(P1-3) `multishots`(P1-1) `thumbnails`(P1-9) `episodes`(P1-10)
`bible`(P2: 월드 바이블) `previz`(P2: 카메라 키프레임·경로)
`files`(파일 서빙) `health`(헬스체크).
라우터는 얇게 유지한다. 샷 이미지 후처리(upscale·outpaint·inpaint)는 `generations` 라우터의
`POST /shots/{id}/upscale|outpaint|inpaint` (ADR-0003).
레퍼런스 삭제는 두 경로다: 캐릭터 레퍼런스 `DELETE /references/{reference_id}`(`characters`),
샷 레퍼런스 `DELETE /shots/{shot_id}/references/{reference_id}`(`scenes`, ADR-0012). 같은 (method, path)를
두 라우터에 등록하지 않는다(`tests/test_web_contract.py`가 검사).
잡 생성 엔드포인트는 `Idempotency-Key` 헤더를 받는다(`services/idempotency.py`).

## apps/web — 화면별 1줄 매핑

| 라우트 | 화면 | 대응 스펙 |
|---|---|---|
| `projects/` | 프로젝트 목록 | P0-1 |
| `projects/[id]/story` | 스토리·플롯·비트 | P0-2 |
| `projects/[id]/characters` | 캐릭터 랩 | P0-4 |
| `projects/[id]/characters/[cid]/sheet` | 캐릭터 시트 뷰어 | P0-5 |
| `projects/[id]/board` | 씬·샷 스토리보드 | P0-9 |
| `projects/[id]/review` | 이미지 비교·승인·리테이크 | P0-10/11 |
| `projects/[id]/timeline` | 비디오·타임라인 (자막은 내 탭 — ADR-0002) | P0-13/14/15 |
| `projects/[id]/qc` | QC 리포트 | P0-16 |
| `projects/[id]/costs` | 비용 대시보드 | P0-17 |
| `projects/[id]/settings` | 프로젝트 설정 탭 (제목·예산·기본 모델·화면비·스타일 키트·카테고리 팩) | P0-1, P0-19 |
| `library/` | 전역 에셋·캐릭터·스타일 키트 | P0-7 |
| `settings/models` | 모델 레지스트리·어댑터 상태 | §3 |
| `snippets/` | 프롬프트 스니펫 보관함 | P1-8 |
| `projects/[id]/episodes` | 에피소드 목록·복제 | P1-10 |
| `projects/[id]/subtitle-edit` | 자막 기반 편집 분석 | P1-6 |
| `projects/[id]/dialogue` | 대사 TTS·음성 복제·B-roll 제안 | P1-5 |
| `projects/[id]/bible` | 월드 바이블 (서사 설정집) | P2 (ADR-0010) |
| `projects/[id]/previz` | 카메라 프리비즈 뷰어 (동선 확인용) | P2 (ADR-0010) |
| `projects/[id]/board` §멀티샷 | 샷 커버리지 생성·세트 뷰어 | §05C-1 |
| `projects/[id]/board` §카메라 프리셋 | 샷 편집 폼 카메라 프리셋 적용 | §05C-2 |
| `projects/[id]/review` §배리에이션 | 프레임 변형 후보 생성 | §05C-3 |
| `projects/[id]/characters` §LoRA 학습 | 캐릭터별 LoRA 학습·목록 | §05C-4 |
| `projects/[id]/settings` §스타일 추출 | 스타일 키트 이미지 추출 | §05C-5 |
| `projects/[id]/timeline` §썸네일 | 썸네일 묶음 생성 | §05C-6 |

공통: `lib/api-client.ts`(유일한 API 경유), `lib/sse.ts`(유일한 SSE 훅), `stores/<화면>.ts`.
- `lib/sse.ts`: 서버는 이름 있는 이벤트를 보내므로 계약의 이벤트명(`SSE_EVENT_NAMES`)마다 리스너를 단다. 끊긴 동안의
  이벤트는 재전송되지 않으므로 재연결 시 `onResync`를 부르고, 프로젝트 워크스페이스(`projects/[id]/layout.tsx`)가 화면 상태를 다시 읽는다.
  테스트: `npm run test:sse`, `npm run test:resync`.
- `lib/contracts.gen.ts`: `packages/contracts`(오류 코드·SSE 이벤트·payload·enum)에서 생성 (`make gen-types`, 검사 `make gen-types-check`). 손으로 고치지 않는다.
