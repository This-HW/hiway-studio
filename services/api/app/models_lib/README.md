# models_lib/ — 모델 추상화 (Registry + Router + Provider Adapters)

> MODULES.md 카드 형식. Ports & Adapters는 이 경계에만 적용한다.

## 목적 (1줄)

어떤 이미지·영상 모델로도 갈아끼울 수 있게, 호출 규격을 추상화한다.

## 책임

- `base.py`: `ProviderAdapter` 인터페이스 (submit/poll/cancel/estimate/fetch_result). 가장 바꾸기 어렵다.
- `registry.py`: DB 기반 조회 + seed JSON 부트스트랩. 어댑터 인스턴스 보관.
- `router.py`: 명시 지정 → 프로젝트 기본 → 규칙 기반 자동 선택 (설계서 §3.3).
- `seed.py`: `infra/seeds/models/*.json` 로더. `make seed`는 멱등.
- `adapters/stub.py`: 가짜 제공자. Phase 0 완료 조건 + 데모용. 절대 삭제 금지.
- `adapters/http_polling.py`: fal/Replicate/직접 API/ComfyUI 공용. seed의
  `capabilities.http` 블록으로 동작하는 제네릭 어댑터 (스키마는 모듈 독스트링 참조).
  잡별 총 deadline·결과 바이트 상한·매직 바이트 검사·시크릿 가림 (ADR-0014).
- `adapters/cli.py`: 구독형 CLI(codex). 잡별 전용 디렉토리 + 명시 출력 경로 (ADR-0014).
- `contract.py`: 이미지 잡 provider 입력 계약(키·스냅샷 → payload). 순수 함수.
- `capabilities.py`: seed `capabilities.tasks`/`inputs` 해석, enqueue 사전 검사, sampler·설정 해시.
- `media_check.py`: 결과 Content-Type·크기 상한·매직 바이트·이미지 디코드(단일 프레임 정지 이미지만) 검사.
- `redaction.py`: 메시지에 인용하는 provider 텍스트의 시크릿 가림·길이 제한 (순수 함수).

## 공개 인터페이스

- `registry.get_adapter(provider_id)`, `registry.get_model(model_id, db=None)`, `registry.list_by_modality(modality, db=None)`
- `registry.pinned_model(model)`: 워커가 DB 오버레이로 해석한 모델 항목을 submit 동안 고정한다. 어댑터의 `get_model(model_id)`(db 없음)이 그 항목을 받는다 (ADR-0014 B7-r2).
- `router.route(task, modality, explicit_model_id?, project_default_model_id?, quality_tier?, db=None)`
- `seed.apply_seed() -> int`

## 불변식

1. 어댑터는 모델의 "호출 규격"만 안다. 라우팅·재시도·폴백은 어댑터 밖.
2. 어댑터 예외는 `StudioError` 18종으로 변환해서 던진다. submit 이후 결과가 불확실한 실패(응답 유실·deadline 초과 등)는 태스크 층에서 `PROVIDER_UNCERTAIN`이 되고 자동 재제출하지 않는다(ADR-0013 D7, ADR-0014).
3. **새 모델 ≠ 새 파일.** HTTP 호환 모델은 seed JSON 1행으로 끝낸다.
4. 텍스트 LLM도 `modality='text'`로 레지스트리에 포함된다 (D-16).
5. `estimate()`는 네트워크 호출 없이 계산한다.

## 의존성

- `db`(세션·조회), `models`(Model 테이블), `core`. 도메인 `services`를 import하지 않는다.

## 확장 절차

- 새 HTTP 모델: `infra/seeds/models/*.json`에 1행(`capabilities.tasks`/`inputs` 선언 포함) → `make seed`. 코드 생성 금지.
  seed를 바꾼 뒤(업데이트 포함)에는 기존 설치에서도 `make seed`를 다시 실행해야 DB 레지스트리에 반영된다.
- 새 프로토콜 어댑터(예외적): `adapters/<프로토콜>.py` 1개 + 인터페이스 준수 테스트.
- 인터페이스 변경이 필요하면 ADR을 먼저 쓴다.

## 함정

- 설계서 §3.4의 인터페이스에는 `fetch_result`가 없지만, MODULES.md 카드에는 있다.
  산출물 바이트를 가져오는 방법이 없으면 태스크가 파일을 만들 수 없으므로,
  본 구현은 카드 버전을 따른다 (submit→poll→fetch_result).
- `registry.get_model(db=None)`은 고정된 항목(`pinned_model`)이 있으면 그것을, 없으면 seed 캐시를 쓴다. 운영에서는 DB 오버레이가 우선하며, 워커는 그 항목을 고정해 어댑터가 사전 검사와 같은 설정으로 실행하게 한다.
