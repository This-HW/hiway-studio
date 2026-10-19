# 의존성 라이선스 기록

2026-09-30 마무리 단계에서 전수 확인: `pyproject.toml`·`package.json`·`app/` import 스캔 결과,
아래 목록이 전체이며 누락 없음. `imageio-ffmpeg`는 사용하지 않음 (코드·의존성 모두 없음).
패키지 버전이 확정되면 각 패키지의 실제 배포 라이선스를 재확인할 것.

| 패키지 | 용도 | 라이선스 |
|---|---|---|
| fastapi | API 서버 | MIT |
| uvicorn | ASGI 서버 | MIT |
| celery | 작업 큐 (워커) | BSD-3 |
| sqlalchemy | ORM | MIT |
| alembic | DB 마이그레이션 | MIT |
| psycopg | PostgreSQL 드라이버 | BSD-3 |
| redis-py | Redis 클라이언트 (SSE/Celery) | MIT |
| pydantic | 스키마/검증 | MIT |
| pydantic-settings | 설정 관리 | MIT |
| httpx | HTTP 클라이언트 | BSD-3 |
| pytest | 테스트 | MIT |
| pillow | 이미지 썸네일 생성 (Phase 1) | HPND (permissive, MIT 호환) |
| python-multipart | 파일 업로드 파싱 (Phase 1) | Apache-2.0 |
| next | 웹 프레임워크 | MIT |
| react | 웹 UI | MIT |
| typescript | 웹 타입 시스템 | Apache-2.0 |
| zustand | 웹 상태 관리 | MIT |
| tanstack-query | 웹 데이터 패칭 | MIT |

## Phase 3 추가

| 패키지 | 용도 | 라이선스 |
|---|---|---|
| ffmpeg (+ffprobe) | 클립 정규화·concat·렌더 (시스템 바이너리, pip 아님) | LGPL/GPL 빌드 의존 — Dockerfile에 LGPL 빌드 설치. OSS_PLAN.md §의존성 참고 |

## Unit B 추가 (OCR 엔진, 2026-10-01)

PaddleOCR PP-OCR 모델을 ONNX Runtime으로 구동 (paddlepaddle 불필요). 전부 permissive — GPL 결합 없음.

| 패키지 | 용도 | 라이선스 |
|---|---|---|
| rapidocr_onnxruntime | QC text_ocr_match/text_artifact OCR 파이프라인 | Apache-2.0 |
| onnxruntime | ONNX 모델 실행 | MIT |
| opencv-python | 이미지 전처리 | Apache-2.0 |
| numpy | 수치 연산 | BSD-3-Clause |
| Shapely | 텍스트 박스 기하 연산 (rapidocr 의존) | BSD-3-Clause |
| pyclipper | 텍스트 박스 기하 연산 (rapidocr 의존) | MIT |
| protobuf | onnx 직렬화 (rapidocr 의존) | BSD-3-Clause |
| tqdm | 진행 표시 (rapidocr 의존) | MIT (+MPL-2.0 듀얼, MIT 선택) |
| flatbuffers | onnx 직렬화 (rapidocr 의존) | Apache-2.0 |

모델: korean_PP-OCRv5_mobile_rec (PaddlePaddle 공식 ONNX export, Apache-2.0 계열).
첫 실행 시 공식 호스트(paddle-model-ecology.bj.bcebos.com)에서 다운로드 후
SHA-256 검증, `~/.cache/ai-studio/ocr/`에 캐시. 저장소에는 모델 바이너리 미포함.

## Unit C 추가 (QC vision 스코어러, 2026-10-01)

새 pip 의존성 없음. `app/services/vision_client.py`는 기존 의존성만 사용:
httpx (HTTP, BSD-3-Clause), Pillow (프레임 다운스케일, HPND). vision 모델
자체는 외부 엔드포인트(Ollama /v1, vLLM, OpenAI 등)로 연결되며 저장소나
배포물에 포함되지 않으므로 라이선스 검토 대상이 아님. 모델 선택은 사용자
env 설정에 위임.

메모: 버전 확정 후 재확인 필요. 새 의존성을 추가할 때마다 이 파일에 기록하고, 라이선스 호환성(OSS 공개 전제)을 확인하세요.
