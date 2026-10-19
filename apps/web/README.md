# AI Studio — Web (Next.js)

Phase 1 프론트엔드: 프로젝트 목록·워크스페이스 셸·스토리·캐릭터·캐릭터 시트·전역
라이브러리 화면. 보드/리뷰/타임라인/QC·출력/비용/설정은 Phase 2 플레이스홀더.

백엔드는 FastAPI이며, 호출 규격은 `docs/P0_SCREENS.md` + `docs/adr/0002` 기준
(설계서 §8.1). 백엔드가 아직 없으므로 API 호출 코드는 명세대로 작성되어 있고,
동작 확인은 `npm run typecheck` + `npm run build`까지 수행됨.

## 구조

- `app/` — Next.js App Router
  - `app/layout.tsx` — 루트 레이아웃 (`lang="ko"` + `globals.css`)
  - `app/page.tsx` — `/projects`로 리다이렉트
  - `app/projects/` — 프로젝트 목록 (카드 그리드·생성 다이얼로그·복제·아카이브·삭제)
  - `app/projects/[id]/layout.tsx` — 워크스페이스 셸 (좌측 8개 탭 내비 + 상단 헤더:
    프로젝트 제목·잡 뱃지·SSE 상태 점). SSE 단일 구독을 소유하고 이벤트를
    `stores/workspace.ts`로 흘려보낸다.
  - `app/projects/[id]/story/` — 스토리 카드·비트 리스트·비트 상세·스크립트 임포트
  - `app/projects/[id]/characters/` — 캐릭터 랩 (스코프 탭·상세 패널·마스터 생성/업로드)
  - `app/projects/[id]/characters/[cid]/sheet/` — 캐릭터 시트 (섹션 그리드·주 레퍼런스 지정)
  - `app/projects/[id]/{board,review,timeline,qc,costs,settings}/` — Phase 2 플레이스홀더
  - `app/library/` — 전역 라이브러리 (에셋 탭·전역 캐릭터 탭)
  - `app/globals.css` — 공용 스타일
- `components/` — 여러 화면에서 쓰는 것만
  - `Modal.tsx`, `ConfirmModal.tsx` (제목 입력 확인 지원), `Toast.tsx`,
    `JobBadge.tsx` (잡 뱃지+드롭다운+SSE 점), `PhasePlaceholder.tsx`
- `lib/` — 공통 유틸리티
  - `lib/api-client.ts` — 중앙 API 클라이언트
    (`apiGet`/`apiPost`/`apiPatch`/`apiPut`/`apiDelete`/`apiUpload`)
  - `lib/errors.ts` — 백엔드 에러 포맷(§8.4) 파싱용 `ApiError`
  - `lib/types.ts` — 백엔드 DTO 타입 (코드젠 전까지 수동 유지, 정본은 packages/contracts)
  - `lib/format.ts` — 비용 표시 규칙(§0.3)·에러 한글 메시지
  - `lib/sse.ts` — `useSse` React hook (단일 EventSource 연결)
  - `lib/sse-events.ts` — SSE 이벤트명 7종 상수
  - `lib/query-keys.ts` — React Query 키 정의 (Phase 1+ 확장용)
- `stores/` — 화면별 zustand 스토어 co-location
  - `toast.ts` (공용 토스트), `workspace.ts` (프로젝트·잡·SSE),
    `projects.ts`, `story.ts`, `characters.ts`, `character-sheet.ts`, `library.ts`

## 규칙

1. **모든 API 호출은 `lib/api-client.ts`를 경유**합니다.
   컴포넌트·스토어에서 `fetch()`를 직접 호출하지 마세요.
2. **SSE 연결은 `lib/sse.ts`의 `useSse` 훅만 사용**합니다.
   워크스페이스 레이아웃이 유일하게 호출하며, 화면은 `stores/workspace.ts`의
   잡 상태를 읽습니다. 화면마다 `EventSource`를 만들지 마세요.
3. **타입은 contracts가 정본**입니다. `packages/contracts`에서
   `make gen-types`로 코드젠할 예정이므로, 그때까지 `lib/types.ts`를
   contracts와 수동으로 맞춰 유지합니다.
4. UI 텍스트는 한국어, 코드 식별자·주석은 영어.

## 실행 방법

```bash
# 백엔드 주소 설정
cp .env.example .env.local   # 필요하면 NEXT_PUBLIC_API_URL 수정

npm install
npm run dev
```

`http://localhost:3000`에서 프로젝트 목록이 열립니다.

## 스크립트

| 명령어          | 설명                  |
| --------------- | --------------------- |
| `npm run dev`   | 개발 서버 실행        |
| `npm run build` | 프로덕션 빌드         |
| `npm run typecheck` | 타입 체크 (`tsc --noEmit`) |
