# 모델 seed

`provider_id: "stub"`인 개발용 모델 정의입니다. `make seed`가 `services/api/app/models_lib/seed.py`를 실행해 로드합니다.

## 스키마

각 파일은 `{"models": [...]}` 형태이며, 모델 항목 필드는 다음과 같습니다.

- `model_id`: 모델 식별자
- `modality`: image | video | text | tts
- `provider_id`: 제공자 ID (stub 전용)
- `status`: active | disabled | deprecated
- `quality_rank`: 낮을수록 빠름/저품질, 높을수록 고품질
- `params_schema` / `capabilities`: 제공자별 파라미터 스키마와 능력 (stub은 빈 객체)
- `constraints`: max_prompt_chars, aspect_ratios 제한
- `fallback_model_ids`: 실패 시 대체 모델 목록
- `pricing`: `{unit, amount_usd}` — 단가 단위와 금액

## 멱등 규칙

- `make seed`는 여러 번 실행해도 같은 결과를 만들어야 합니다: `model_id` 기준 upsert, 없는 항목은 추가, 삭제는 하지 않음.

## comfyui.json — 로컬 ComfyUI (수동 활성화 필요)

- `model_id: comfyui.t2i-sd15` — 로컬 ComfyUI의 SD1.5 text-to-image 워크플로.
  `http_polling` 어댑터로 `POST {COMFYUI_URL}` → `/history/{prompt_id}` 폴링 →
  `/view` 다운로드를 수행한다.
- `model_id: comfyui.i2i-sd15` — 로컬 ComfyUI의 SD1.5 image-to-image 워크플로
  (denoise 0.75). submit 전에 어댑터 `upload` 단계가 `init_image`(로컬 경로)를
  `{base_url}/upload/image`에 multipart 업로드하고, 응답의 `name`을
  `{uploaded_image}`로 치환해 `LoadImage` 노드에 주입한다. `init_image`가 없는
  작업은 upload 단계가 스킵된다.
- **기본 `status: disabled`** — 라우터가 ComfyUI 미설치 환경에서 이 모델을
  자동 선택하지 않도록. 활성화: 이 파일의 `status`를 `"active"`로 바꾸고
  `make seed` (seed가 DB 값을 덮어쓰므로 파일이 스위치다).
- 활성화 전 준비:
  1. ComfyUI 실행 (API 모드, 기본 `http://127.0.0.1:8188`).
  2. 체크포인트를 `models/checkpoints/`에 설치하고 아래 `ckpt_name`을 맞춤.
  3. `.env`에 `COMFYUI_URL=http://127.0.0.1:8188/prompt` 기입
     (submit 전체 URL — `/prompt` 포함).
- `ckpt_name` (현재 `v1-5-pruned-emaonly.safetensors`)은 설치된 체크포인트
  파일명으로 바꿔야 한다. 워크플로 노드 ID 7(SaveImage)의 출력이 결과물이다.
- 로컬 실행이라 `pricing`은 `0.00`, 취소는 미지원(ComfyUI의 중단은
  전역 interrupt라 per-job 취소로 연결하지 않음).

## 단가 안내

- 현재 단가는 모두 플레이스홀더이며 stub 제공자는 무료(`0.00`)입니다.
- 실제 제공자 단가는 Phase 1+에서 공식 가격 페이지를 재확인한 뒤 입력합니다.
