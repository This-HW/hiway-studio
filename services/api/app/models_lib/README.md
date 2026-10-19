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

## 공개 인터페이스

- `registry.get_adapter(provider_id)`, `registry.get_model(model_id, db=None)`, `registry.list_by_modality(modality, db=None)`
- `router.route(task, modality, explicit_model_id?, project_default_model_id?, quality_tier?, db=None)`
- `seed.apply_seed() -> int`

## 불변식

1. 어댑터는 모델의 "호출 규격"만 안다. 라우팅·재시도·폴백은 어댑터 밖.
2. 어댑터 예외는 `StudioError` 14종으로 변환해서 던진다.
3. **새 모델 ≠ 새 파일.** HTTP 호환 모델은 seed JSON 1행으로 끝낸다.
4. 텍스트 LLM도 `modality='text'`로 레지스트리에 포함된다 (D-16).
5. `estimate()`는 네트워크 호출 없이 계산한다.

## 의존성

- `db`(세션·조회), `models`(Model 테이블), `core`. 도메인 `services`를 import하지 않는다.

## 확장 절차

- 새 HTTP 모델: `infra/seeds/models/*.json`에 1행 → `make seed`. 코드 생성 금지.
- 새 프로토콜 어댑터(예외적): `adapters/<프로토콜>.py` 1개 + 인터페이스 준수 테스트.
- 인터페이스 변경이 필요하면 ADR을 먼저 쓴다.

## 함정

- 설계서 §3.4의 인터페이스에는 `fetch_result`가 없지만, MODULES.md 카드에는 있다.
  산출물 바이트를 가져오는 방법이 없으면 태스크가 파일을 만들 수 없으므로,
  본 구현은 카드 버전을 따른다 (submit→poll→fetch_result).
- `registry.get_model(db=None)`은 seed 캐시를 쓴다. 운영에서는 DB 오버레이가 우선한다.
