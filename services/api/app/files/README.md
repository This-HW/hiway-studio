# files/ — 저장 규칙 구현 (설계서 §11)

> MODULES.md 카드 형식.

## 목적 (1줄)

파일시스템 저장 규칙을 코드로 강제한다. DB에는 상대경로만 들어간다.

## 책임

- `paths.py`: `PROJECTS_ROOT` 해결, 상대↔절대 변환, 경로 탈출 방지, private 경로 접근의 유일한 창구.
- `naming.py`: `{uuid}[_{batch}].{ext}` 명명. 토큰 실행(ADR-0013) 워커의 산출물은 시도 태그(실행 토큰 앞 8자)를 더한 이름을 쓴다. 예외(안정 경로): 캐릭터의 첫 `master.png`, LoRA의 `lora/{lora.id}.safetensors`.
- `immutability.py`: 성공 산출물 덮어쓰기 방지 가드. 임시 파일 → 하드링크(불가하면 배타 생성)로 게시해 경쟁 중에도 덮지 않는다.
- `orphans.py`: 고아 파일 검출 (리포트만, 자동 삭제 금지).

## 공개 인터페이스

- `paths.projects_root()`, `paths.to_relative(abs) -> str`, `paths.resolve(rel) -> Path`,
  `paths.assert_relative(p)`, `paths.project_dir(project_id)`, `paths.ensure_parent(p)`
- `naming.frame_filename(generation_id, batch_index, ext)`
- `immutability.write_new(abs_path, data)` — 존재하면 `ImmutableViolation`
- `orphans.find_orphans(known_relative_paths) -> [str]`

## 불변식

1. DB에 절대경로를 저장하지 않는다 (`assert_relative`로 강제).
2. `succeeded` 산출물은 덮어쓰지 않는다. 수정·리테이크는 항상 새 파일.
3. `projects/`, `library/`에는 이 모듈을 통해서만 접근한다.

## 의존성

- `core`만 (설정·에러). `services`를 import하지 않는다.

## 함정

- Phase 0의 프레임 저장 위치는 `projects/{project_id}/frames/`이다.
  설계서 §11.1의 정식 위치(`scenes/{scene}/shots/{shot}/frames/`)는 Phase 2에서 씬·샷 테이블과 함께 적용한다.
- `resolve()`는 절대경로·`..` 탈출을 거부하고, symlink까지 따라간 실제 위치가 `PROJECTS_ROOT` 안에 있어야 한다. 사용자 입력 경로를 그대로 믿지 않는다.
- 파일명에 사용자 입력을 넣지 않는다. 예: LoRA 가중치는 `lora/{lora_id}.safetensors`(UUID)다.
