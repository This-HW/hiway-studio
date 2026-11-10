# OSS 공개 검토서 — AI 이미지·영상 스튜디오

> 작성일: 2026-09-30. marketing 자동화 시스템의 OSS 검토(별도 메모리)와 같은 관점으로 분석.
> 결론부터: **OSS 적합도가 높다.** 로컬-퍼스트 1인용 설계라 멀티테넌트 부담이 없고,
> "각자 자기 머신에서 자기 키로 돌린다"는 모델이 자연스럽다.

---

## 1. OSS 적합성 판정

| 관점 | 판정 | 근거 |
|---|---|---|
| 아키텍처 | ✅ 적합 | 로컬 우선, 인증 없음, 결제 없음, docker-compose 단일 배포. SaaS 전환 압력이 구조에 없음. 인증이 없으므로 compose는 API를 `127.0.0.1`에만 게시한다(`tools/check_compose_loopback.py`, CI G7) |
| 실행 모델 | ✅ 적합 | 사용자=운영자 1인. "내 머신에서 내 API 키로"가 기본 사용법이라 계정·과금 분쟁 소지가 없음 |
| 확장 표면 | ✅ 적합 | Provider Adapter가 명확한 확장점. "새 모델 붙이기"가 기여의 단위가 되기 좋음 |
| 데모 가능성 | ✅ 적합 | stub 어댑터가 이미 설계에 있음 → API 키 없이 `make demo`로 전체 플로우 체험 가능 |
| 민감 정보 | ⚠️ 분리 필요 | API 키·개인 캐릭터/에셋은 런타임 파일로만 존재. 공개 전 분리 규칙 확정 필요 (§2) |

## 2. 공개 / 비공개 분리

### 공개 (public repo)
- 전체 코드 (`apps/web`, `services/api`, `packages/contracts`)
- `infra/seeds/` — 모델 레지스트리 템플릿, 카테고리 팩 3종, 출력 프리셋, 프롬프트 스니펫(네거티브 라이브러리 기본 세트)
- `docs/` — 설계서 요약, AGENT_GUIDE, MODULES, ADR
- `docker-compose.yml`, `.env.example`, `Makefile`
- stub 어댑터 기반 데모 시나리오

### 비공개 (사용자 로컬, git 제외)
| 항목 | 위치 | 비고 |
|---|---|---|
| API 키·시크릿 | `.env` | `.env.example`만 공개 |
| 개인 프로젝트 산출물 | `projects/` | 캐릭터·영상 전부 |
| 전역 라이브러리 | `library/` | 개인 캐릭터·에셋 |
| 백업 | `infra/backups/` | DB 덤프 포함 |
| ComfyUI 모델 가중치 | 볼륨 마운트 | 용량·라이선스 이슈 |

**단일 public repo로 충분하다.** marketing 시스템처럼 "public core + private per-business overlay" 2단 분리가 필요 없는 이유:
marketing은 업체별 지식·설정이 코드에 준하는 자산이지만, 스튜디오는 개인 산출물이 전부 런타임 파일이라 `.gitignore`로 끝난다.

## 3. 라이선스 분석

| 선택지 | 평가 |
|---|---|
| **MIT** (권장) | 가장 단순. 어댑터 기여·포크·상용 활용에 장벽 없음. 1인 창작 도구定位에 적합 |
| Apache-2.0 | 특허 조항이 있어 기업 기여에 유리. MIT와 체감 차이 작음. 둘 중 하나면 충분 |
| AGPL | SaaS 봉쇄 목적. 우리는 SaaS를 안 하기로 했으므로 불필요한 제약 |
| GPL 계열 | 비권장. 아래 주의사항과 충돌 |

**주의 — 의존성 라이선스:**
- **ComfyUI는 GPL-3.0.** 우리 어댑터(seed 기반 `http_polling`)가 별도 프로세스에 HTTP REST로 붙으므로 "결합"이 아니라 "집합"으로 본다. ComfyUI 코드를 레포에 넣거나 임포트하지 않는 한 MIT 유지 가능. `docker-compose.yml`에도 넣지 않고 `COMFYUI_URL`로만 참조한다.
- **ffmpeg:** 빌드에 따라 LGPL/GPL. 문서에 "LGPL 빌드 사용"을 명시하거나, 사용자에게 선택을 위임한다. (현재 API 이미지는 Debian 패키지 `ffmpeg`를 설치한다.)
- PaddleOCR(Apache-2.0), Next.js·FastAPI(MIT)는 문제없음. API 이미지에는 `fonts-noto-cjk`(SIL OFL 1.1)와 빌드 시 받는 PP-OCR 한국어 인식 모델(`docs/licenses.md` Unit B)이 들어간다. 폰트 고지는 `docs/licenses.md`에 있다. 이미지의 ffmpeg는 Debian 빌드(GPL 옵션 포함)이므로, 이미지를 재배포하면 GPL 조건을 따르거나 LGPL 빌드로 바꾼다.
- 모델 가중치(LoRA 등)는 사용자가 직접 구하는 것으로. 레포에 가중치를 포함하지 않는다.

## 4. 공개 전 체크리스트

- [ ] `.env.example` 완성 (모든 변수 문서화, 값 없음)
- [ ] 시크릿 스캔 (히스토리에 키가 들어간 적 없는지)
- [ ] `make demo` 동작 확인 (stub 어댑터로 키 없이 전체 플로우). CI(`.github/workflows/ci.yml`)도 키·유료 provider 없이 stub으로 돈다
- [ ] README: 5분 퀵스타트 (docker-compose up → 데모 프로젝트 생성까지)
- [ ] LICENSE 파일 (MIT 권장) + 의존성 라이선스 고지 (`docs/licenses.md`)
- [ ] `projects/`, `library/`, `*.env`, `infra/backups/` gitignore 확인
- [ ] 썸네일·샘플 에셋의 초상권·저작권 확인 (기본 캐릭터는 AI 생성분으로)
- [ ] ComfyUI 연동 문서에 "별도 프로세스·별도 라이선스" 명시
- [ ] 이슈 템플릿에 "API 키·개인 에셋을 첨부하지 마세요" 경고

## 5. 리스크와 대응

| 리스크 | 대응 |
|---|---|
| API 키 노출 (이슈·스크린샷) | 템플릿 경고 + 키는 `.env`에서만 읽는 구조라 코드에 박을 자리가 없음 |
| 모델 제공자 ToS 위반 사용 | 어댑터는 정상 API 호출만. 우회·스크래핑 어댑터는 기여 가이드에서 거부 |
| 생성물 저작권 분쟁 | "출력물의 권리는 사용자와 모델 제공자 약관에 따름"을 README에 명시 |
| 유지보수 부담 | 코어 작게 유지. 모델 지원은 커뮤니티 어댑터 기여로 분산 (인터페이스가 고정돼 있어 가능) |
| 한글 생태계 한계 | 오히려 기회. CJK 자막·썸네일 오버레이(D-17) 같은 차별점이 문서화돼 있음 |

## 6. marketing 시스템 OSS 계획과의 관계

- marketing: public core + private per-business overlay 2단 구조 (업체별 지식 자산 때문).
- studio: 단일 public repo + 로컬 런타임 파일 (개인 산출물은 코드가 아님).
- 공통점: 둘 다 "SaaS 아님. 각자 자기 환경에서 자기 에이전트/키로 실행" 모델. Naver 자동화의 계정 리스크 같은 구조적 부담이 studio에는 없다.
- 합칠 필요는 없다. 레포 2개로 독립 운영이 깔끔하다.

## 7. 권장 순서

1. **지금**: 본 문서 확정 + `.gitignore`·`LICENSE`·`make demo`를 아키텍처에 반영 (구현 Phase 0에서 처리)
2. **Phase 0 완료 후**: stub 데모가 도는 시점에 private repo로 1차 정리
3. **Phase 4 완료 후** (QC·비용까지 돌아가는 시점): public 전환 검토. 이 시점이 "남이 써볼 만한 최소 제품"이다
4. 공개 후: 어댑터 기여 가이드(`CONTRIBUTING.md`)를 별도 작성

---

* marketing OSS와 마찬가지로, OSS 전환은 HW의 명시적 승인 후에 진행한다. 본 문서는 검토용이다.*
