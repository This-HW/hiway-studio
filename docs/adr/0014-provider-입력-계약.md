# ADR-0014: provider 입력 계약과 실행 경계 (레퍼런스·capability·CLI 귀속·HTTP 상한)

## 상태
제안

## 배경
감사(계획 `docs/plans/2026-11-09-audit-roadmap-fixes/plan.md` B7, G8 F08·F14·F15·F16, 프로브 P11–P19, SA-B3)에서
생성 품질 입력이 provider까지 닿지 않거나, provider 경계가 열려 있는 결함이 확인됐다.

- 프롬프트 조립기는 레퍼런스 경로를 계산했지만 enqueue는 ID만 남겼고, 워커 payload에는 레퍼런스가 없었다(P11).
  캐릭터 시트도 승인된 마스터 이미지 없이 텍스트만 보냈다.
- 후처리 payload 키(`images`)와 ComfyUI i2i seed의 키(`{init_image}`)가 달라 업로드가 건너뛰어지고
  `{uploaded_image}` 누락으로 실패했다(P19). inpaint/upscale/outpaint는 모델 능력과 무관하게 접수됐다.
- 스타일 리졸버가 프로젝트 키트를 보지 않았고(P12), 잠긴 키트의 슬롯을 요청 override·절단으로 지울 수 있었다(P13, ADR-0011 §13 위반).
  프롬프트 변형은 시드까지 바꿔 두 변수를 동시에 바꿨고, 워커가 뽑은 랜덤 시드는 저장되지 않았다.
  CLI 어댑터는 `negative` 키를 읽어 계약 키 `negative_prompt`를 놓쳤다.
- CLI 잡 두 개가 같은 공용 디렉토리의 "가장 최신 PNG"를 골라 같은 파일을 가져갈 수 있었다(P14).
- http_polling은 전체 deadline이 없고(최대 600회 × sleep), 상태 누락을 영원히 `queued`로 봤고,
  timeout 뒤 provider cancel이 없었다(P15). `200 text/html`도 결과 바이트로 승인했고(P16), 다운로드 크기 상한이 없었다.
  영상 워커는 probe 실패 시 요청값으로 대체하고 `done`으로 진행했다.

## 결정
1. **이미지 입력 계약 (단일 payload)** — `models_lib/contract.py`가 provider 중립 키를 정한다:
   `task, prompt, negative_prompt, seed, width, height, init_image, mask, references[{path, role, weight}],
   reference_image, reference_weight`. enqueue 시점에 한 번 해석해 `generations.params["snapshot"]`에 저장한다
   (상대 경로, 역할, 가중치, 모델 id·설정 해시, 시드와 출처, 크기, 워크플로가 고정한 sampler 값, 네거티브,
   init/mask, 경고, 스타일 키트 버전). 워커는 스냅샷으로 payload를 만들고(행의 seed가 기준), 어댑터가 자기 키로 매핑한다.
   ComfyUI는 `upload`(단일 또는 목록)로 파일을 올려 `{uploaded_image}`/`{uploaded_mask}`를 채우고,
   다른 http seed는 `{<파일키>_data_uri}`로 인라인할 수 있다. 캐릭터 시트는 승인된 마스터(primary 레퍼런스)를
   `identity` 레퍼런스로, 캐릭터 레퍼런스 가중치는 `character.default_strength`를 쓴다.
2. **capability 사전 검사** — seed가 `capabilities.tasks`(잡 종류)와 `capabilities.inputs`
   (`negative_prompt, seed, init_image, mask, reference_images`)를 선언한다. 편집 작업(inpaint·outpaint·upscale·i2i)은
   명시 선언이 있어야 하며, 지원하지 않는 작업/편집 입력은 **잡 행·예산 예약·디스패치 전에** `VALIDATION_ERROR`(400)로
   거절한다. 명시 모델의 작업 불일치도 503이 아니라 400이다. 레퍼런스를 받을 수 없는 모델은 **거절하지 않고 강등**한다:
   텍스트만으로 실행하되 스냅샷에 `references_applied=false`와 `reference_unsupported` 경고를 기록한다.
   어댑터도 소비하지 않는 편집 입력은 HTTP 호출/프로세스 생성 전에 거절한다(조용한 무시 금지).
3. **스타일 권위·재현성** — 잠긴 프로젝트 키트가 유일한 스타일 권위다(샷/씬 키트가 대체 불가). 잠긴 키트가 채우는
   슬롯(style_medium·lighting·camera_lens·color_mood)은 override로 바꾸거나 지울 수 없고(`STYLE_KIT_LOCKED` 409),
   절단 대상에서도 빠진다. 잠기지 않은 키트의 동작은 그대로다. 프롬프트 변형은 시드를 고정한다(한 번에 한 변수).
   랜덤 시드는 enqueue에서 뽑아 행과 스냅샷에 저장하고, 시드 없는 과거 행은 워커가 submit 전에 저장한다.
4. **CLI 귀속** — 잡마다 전용 디렉토리(`-C <job dir>`)와 명시 출력 파일(`{output_path}`)을 지시하고 그 파일을 결과로 쓴다.
   공용 디렉토리 폴백은 B7-r2에서 제거했다(아래 B7-r2 §H1). 네거티브는 `negative_prompt`(구 `negative` 별칭)로 지시문에 들어간다.
5. **HTTP 경계** — provider 잡마다 monotonic 총 deadline(`HTTP_JOB_TOTAL_DEADLINE_SEC`, submit→다운로드 끝)을 두고
   모든 요청 timeout을 남은 시간으로 자른다. deadline·폴링 오류·반복 상한에서 provider cancel을 best-effort로 시도하고
   실패시킨다. submit 이후이므로 결과는 불확실 → `PROVIDER_UNCERTAIN`, 자동 재제출 없음(ADR-0013 D7 그대로).
   상태 누락·state_map 밖 값은 오류다(seed가 `missing_state`/`unknown_state`로 명시 허용 가능, ComfyUI `/history`는
   `missing_state: queued`). 결과 다운로드는 스트리밍하며 매체별 바이트 상한(`HTTP_RESULT_MAX_BYTES_IMAGE/_VIDEO/_AUDIO`)을
   Content-Length(거짓·부재 포함)와 무관하게 적용하고, Content-Type과 매직 바이트가 매체와 맞아야 한다.
   오류 메시지에 인용하는 응답 조각은 시크릿(헤더 env 값, Bearer/키 패턴)을 가린다. 영상 워커는 다운로드한 클립의
   ffprobe가 실패하거나 길이가 0이면 잡을 실패시킨다(ffprobe 바이너리 부재만 매직 바이트 검사로 강등).

## 근거
- 계약을 enqueue 스냅샷에 고정해야 "무엇을 보냈는가"가 재현·감사 가능하다(ADR-0011 §14). 워커가 매번 DB 관계를 다시
  풀면 enqueue 이후 바뀐 레퍼런스가 섞인다.
- 레퍼런스 강등 vs 거절: 현재 실 provider 중 레퍼런스를 받는 seed가 없다. 거절하면 캐릭터가 등장하는 모든 샷이
  막힌다. 대신 강등 사실을 스냅샷에 남겨 결과를 해석할 수 있게 한다. 편집 작업은 원본 없이 실행하면 결과가 무의미하므로 거절한다.
- 버린 대안: provider별 payload 빌더(어댑터마다 다른 키) — P19가 바로 이 불일치였다. 잡별 `CODEX_HOME` 격리 —
  로그인 토큰 파일 공유 방식이 검증되지 않아 보류. 요청별 timeout만 유지 — 600회 × timeout이 총시간을 보장하지 못한다.

## 영향
- 코드: `models_lib/contract.py`·`capabilities.py`·`media_check.py`(신규), `models_lib/router.py`,
  `models_lib/adapters/http_polling.py`·`cli.py`, `services/provider_input.py`(신규), `services/generations.py`
  (스냅샷·변형 시드·편집 사전 검사), `services/character_pipeline.py`, `services/multishot.py`, `services/prompt_assembly.py`,
  `tasks/tasks_image.py`(payload), `tasks/tasks_video.py`(probe), `core/defaults.py`(HTTP 절),
  `infra/seeds/models/*.json`(tasks/inputs, `comfyui.inpaint-sd15` 추가, ComfyUI 네거티브 `{negative_prompt}`).
- 문서: `docs/PROVIDERS.md`(입력 계약·capability·HTTP 상한·CLI 귀속).
- 호환성: DB 스키마 변경 없음(스냅샷은 `params` JSON). 스냅샷 없는 과거 행은 저장된 `input_paths`를 같은 계약 키로
  매핑한다. 기존 설치는 `make seed`로 seed를 다시 올려야 stub 모델의 편집 작업 선언이 반영된다(재시드 전에는 편집
  요청이 400). 상태 누락/미지 상태를 허용하던 외부 seed는 `missing_state`/`unknown_state`를 명시해야 한다.
- 미검증: 실 provider(ComfyUI·codex CLI·Higgsfield) E2E — 특히 codex가 지시한 출력 경로를 따르는지, ComfyUI
  `LoadImageMask`·`VAEEncodeForInpaint` 워크플로가 실제 체크포인트에서 동작하는지는 범위 밖이다.

## 관련
- 계획 B7, G8 F08·F14·F15·F16, 감사 프로브 P11–P19, SA-B3
- ADR-0011 §13(스타일 바이블)·§14(GenerationSnapshot), ADR-0013(잡 실행 소유권, D7)
- 테스트: `services/api/tests/test_provider_contract.py`

## B7-r2 (리뷰 반영, 2026-11-10)
리뷰 `review-codex-B7.md` H1–H3·M1–M4·L1 반영. 위 결정 중 바뀐 것만 적는다.

- **H1 CLI 귀속** — 공용 `$CODEX_HOME/generated_images` 폴백을 없앴다. 원자적 claim은 중복 소비만 막을 뿐
  어느 잡이 만든 파일인지 증명하지 못한다(두 잡이 끝난 뒤 poll하면 결과가 뒤바뀌었다). 결과는 잡 전용 디렉토리
  (`mkdtemp`, 그 잡의 프로세스만 쓰는 곳)의 명시 출력 파일, 없으면 그 디렉토리 안의 PNG뿐이다. CLI가 잡 경로를
  따르지 않으면 `PROVIDER_ERROR`로 실패한다. 버린 대안: mtime·원자적 rename 휴리스틱(귀속 불가), 에이전트가 쓰는
  매니페스트(에이전트 자기 신고라 검증 불가), 잡별 `CODEX_HOME`(로그인 토큰 공유 방식 미검증, 기존 판단 유지).
- **H2 호출 내부 절대 deadline** — 모든 응답(JSON·오류 본문 포함)을 스트리밍으로 받으며 청크마다 deadline과
  바이트 상한을 다시 본다. 실 transport에서는 httpcore 네트워크 백엔드를 감싸 소켓 connect/read/write마다
  timeout을 남은 시간으로 자른다(헤더를 조금씩 보내는 서버도 막는다). httpx 내부 속성(`_transport`/`_mounts`/
  `_pool`)에 기대므로 바뀌면 청크 검사만 남는다 — localhost slow-drip 회귀가 이를 잡는다. cancel은 job deadline과
  별개로 `HTTP_CANCEL_TIMEOUT_SEC` 절대 상한을 갖는다. submit 응답을 deadline 안에 다 받지 못하면 결과 불확실 →
  `PROVIDER_UNCERTAIN`, 자동 재제출 없음(D7 그대로).
- **H3 ComfyUI 업로드 이름** — multipart 파일명을 submit(=시도)마다 고유한 `hs-<token>-<basename>`으로 바꾸고,
  workflow에는 서버가 **반환한** `name`(비어 있지 않은 `subfolder`는 `<subfolder>/<name>`)을 넣는다. seed는
  `overwrite=false`: 혹시 충돌하면 서버가 이름을 바꿔 돌려주고 그 이름을 쓴다.
- **M1 시크릿 가림** — provider 텍스트가 메시지로 가는 모든 경로(HTTP 오류 조각, 200 본문의 `error_path` 값,
  state/stage 값, CLI stderr)에 같은 가림·길이 제한(`models_lib/redaction.py`, `HTTP_ERROR_SNIPPET_CHARS`)을 쓴다.
- **M2 inline 결과** — JSON 응답 상한 `HTTP_JSON_MAX_BYTES`(8 MiB, inline 출력을 실을 수 있는 응답은 매체 상한의
  base64 크기 + 64 KiB 추가)를 수신 중에 적용하고, base64 문자열 길이를 **디코드 전에** 매체 상한과 비교한다.
- **M3 이미지 유효성** — 이미지 결과는 Pillow `verify()` + `load()`를 통과해야 한다. http 어댑터의
  `check_media`와 이미지 워커(모든 어댑터, 게시 전)가 검사한다. 서명만 있는 PNG는 잡 실패(`PROVIDER_ERROR`).
- **M4 실행 설정** — 결정: **현재 레지스트리 설정으로 실행하고, 실제 실행한 설정을 기록한다**(submit 전 실패가 아님).
  워커는 사전 검사와 같은 출처(DB 오버레이, `registry.get_model(id, db)`)로 모델을 읽어
  `registry.pinned_model`로 어댑터에 넘긴다 — 이전에는 사전 검사는 DB, 어댑터는 seed 캐시를 읽어 운영자가 DB에서
  고친 설정이 실행되지 않았다. submit 전에 `params["executed"]`에 `model_config_sha256`·`model_version`·
  `sampler`·`snapshot_model_config_sha256`·`config_matches_snapshot`를 남기고, 불일치는 경고 로그로도 남긴다.
  근거: 옛 워크플로는 레지스트리에 남아 있지 않아 재생할 수 없고, 해시 불일치로 실패시키면 seed를 한 번 고칠
  때마다(이번 B7-r2의 ComfyUI seed 변경 포함) 대기·실패 잡 전부가 재시도로도 복구되지 않는다. 스냅샷(의도)과
  `executed`(사실)를 나란히 두면 감사 기록이 결과를 설명한다. 스냅샷 자체는 바꾸지 않는다.
- **L1** — `capabilities.tasks: []`는 "아무 작업도 지원하지 않음"이다. 키가 없거나 `null`일 때만 미선언(legacy).
- 미검증(그대로): 실 ComfyUI의 `overwrite=false`·반환 이름 처리, 실 codex의 명시 경로 준수.

## B7-r3 (재검토 반영, 2026-11-10)
리뷰 `review-codex-B7-r2.md` B7R2-H1·M1–M5·L1 반영.

- **H1 요청 송신 deadline** — httpcore의 `SyncStream.write`는 버퍼가 빌 때까지 `sock.send`를 **같은 timeout**으로
  반복한다. 큰 JSON/data-URI 본문은 한 번의 write이므로 느리게 읽는 상대가 송신을 deadline 너머로 늘릴 수 있었다.
  감싼 스트림의 `write`가 send 루프를 직접 돌며 **부분 전송마다** 남은 시간으로 `settimeout`하고, 0이 되면
  `WriteTimeout`으로 끝낸다. 직접 다룰 수 없는 스트림(TLS 프록시 안의 TLS)은 16 KiB 조각마다 남은 시간을 준다
  (조각 하나만큼의 초과가 남는 한계). 결과 불확실 → `PROVIDER_UNCERTAIN`, 재제출 없음(D7).
- **M1 가림 순서·메타데이터** — CLI stderr는 64 KiB 꼬리를 읽어 **먼저 가린 뒤** 500자로 자른다. 읽기 창이 줄
  중간에서 시작하면 그 첫 조각 줄은 버린다(접두가 잘린 credential을 인용하지 않는다). http 어댑터의 공개 메서드
  (submit/poll/fetch_result)를 빠져나가는 모든 메시지는 같은 가림 + 길이 제한(1000자)을 거치고, 메시지에 넣는
  provider job id는 가림 + 120자로 자른다. 워커의 이미지 검사 메시지에는 provider id를 넣지 않는다.
- **M2 수신 상한 우회** — httpx 자동 redirect는 중간 응답 본문을 상한 밖에서 전부 읽었다. 클라이언트는
  `follow_redirects=False`이고 `_stream`이 직접 최대 5 hop을 따라가며 redirect 본문은 읽지 않고 닫는다(다음 요청은
  httpx가 만든 `next_request`: 메서드 변경·교차 origin Authorization 제거 그대로). 압축은 `Accept-Encoding:
  identity`를 보내고 identity가 아닌 `Content-Encoding`은 거절한다(오류 상태면 본문 없이 상태만 보고). 본문은
  `iter_raw`로 읽어 해제 할당이 없다. 버린 대안: 출력 상한을 둔 스트리밍 해제 — decoder별 구현이 필요하고
  provider 결과에 압축이 필요한 근거가 없다.
- **M3 다중 프레임** — 결정: **다중 프레임 이미지는 거절**한다(프레임 수 1만 허용). 파이프라인이 게시하는 것은
  정지 프레임(샷 테이크·캐릭터 시트·썸네일)이라 애니메이션은 첫 프레임으로 조용히 평탄화되고, `load()`는 첫
  프레임만 디코드해 손상된 뒷 프레임을 놓친다. 모든 프레임을 프레임 수·누적 픽셀 상한 아래에서 디코드하는 것보다
  단순하고 엄격하다. 디코드 전에 픽셀 수도 `Image.MAX_IMAGE_PIXELS`로 제한한다.
- **M4 CLI fd** — `mkstemp`가 연 fd를 닫지 않아 submit마다 2개가 남았다. stdout/stderr 로그를 잡 디렉토리
  (`cli.stdout.log`/`cli.stderr.log`)에 두고, 부모 쪽 파일은 spawn 직후 닫는다.
- **M5 원격 id 충돌** — 싱글턴 어댑터가 원격 id로 상태를 저장해, 같은 id "1"을 주는 두 endpoint가 서로의
  config/deadline을 덮었다. submit은 로컬 핸들 `<원격 id>@<model id>#<nonce>`를 반환하고 상태는 핸들로 저장한다.
  URL 템플릿의 `{job_id}`는 여전히 원격 id다. 지금 영속되는 것은 `provider_job_id`(이제 핸들)이며 재시작 후 이를
  다시 읽는 코드는 없다(D7: 불확실은 실패로 남김). 핸들은 모델 id를 담으므로 다른 프로세스에서도
  `_lookup`이 레지스트리에서 설정을 복원해 poll/fetch/cancel할 수 있다(이때 deadline은 새로 시작). B7-r3 이전에
  저장된 맨 원격 id는 예전과 같이 새 프로세스에서 알 수 없는 id다(poll → MODEL_UNAVAILABLE, cancel → no-op).
- **L1** — 테스트 helper 반환 annotation을 세 값으로 고쳤다.
- 미검증(그대로): DNS 조회 시간, TLS-in-TLS 프록시 경로의 조각 단위 초과, httpx/httpcore 내부 속성 의존.
