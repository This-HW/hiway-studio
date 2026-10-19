# media/ — 워커 전용 미디어 처리

> MODULES.md 카드 형식. Phase 0은 디렉토리 + 본 README만 둔다.

## 목적 (1줄)

ffmpeg·OCR·썸네일·자막 렌더 등 무거운 미디어 처리. 워커에서만 호출한다.

## 책임

- Phase 1: `thumbnails.py` (기초)
- Phase 3: `ffmpeg_ops.py`, `frame_extract.py`
- Phase 4: `ocr.py`, `loudness.py`, `subtitle_render.py`, `qc_checks/`

상세 카드(책임·불변식)는 `docs/MODULES.md`의 media 섹션 참조.

## 불변식

1. **API 라우터에서 직접 호출 금지.** 워커 태스크 경유만.
2. 한글 썸네일 문자는 이미지 모델에 맡기지 않는다. 서버 사이드 렌더 후 오버레이 (D-17).
3. 출력은 타임라인의 출력 프리셋을 따른다.

## 의존성

- `files`, `core`. **`services`를 import하지 않는다** (순환 방지).

## 함정

- 무거운 네이티브 의존성(ffmpeg 등)은 해당 Phase에서 Dockerfile에 추가한다.
  OCR은 pip 패키지(`rapidocr-onnxruntime`, Unit B)로 해결 — paddlepaddle 불필요.
  인식 모델(korean_PP-OCRv5_mobile_rec)은 첫 실행 시 공식 호스트에서 다운로드 후
  `~/.cache/ai-studio/ocr/`에 캐시 (SHA-256 검증).
