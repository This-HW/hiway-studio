# 백업과 복원 (ADR-0001)

매일 실행을 가정한 백업 체계다. 크론 예: `0 3 * * * <repo>/infra/scripts/backup.sh` (`<repo>`는 저장소 루트).

## 무엇을 백업하는가

| 대상 | 방식 | 보관 | 위치 |
|---|---|---|---|
| `projects/`, `library/` | `rsync --link-dest` 하드링크 스냅샷 (매일 1세대) | 30세대 | `infra/backups/files/<YYYYMMDD-HHMMSS>/` |
| DB | `pg_dump -Fc` (postgres) 또는 파일 복사 (sqlite) | 14일 | `infra/backups/db/<stamp>.dump` |

스냅샷은 매일 "전체처럼 보이는" 디렉토리지만, 변경되지 않은 파일은 하드링크로
공유되므로 실제 용량은 **full 1개 + 일일 신규분**이다. 산출물이 불변(immutable)이므로
증분 체인이 깨질 염려가 없다 (ADR-0001).

`infra/backups/`는 `.gitignore`에 등록되어 있어 공개 저장소에 들어가지 않는다.

## 백업 실행

```bash
infra/scripts/backup.sh [--dry-run] [--backup-root DIR] [--keep-files N] [--keep-db N]
```

- `.env`의 `DATABASE_URL`·`PROJECTS_ROOT`를 읽는다 (환경변수가 우선).
- postgres URL은 SQLAlchemy 접미사(`+psycopg`)를 제거하고 `pg_dump`에 전달한다.
- `pg_dump`가 없으면 postgres 백업은 실패한다 (조용히 넘어가지 않음).
- 동시 실행은 `flock`으로 방지한다. 재실행은 안전하다 (새 스냅샷 1세대 추가).

## 스냅샷 확인

```bash
infra/scripts/restore.sh --list
```

## 복원 절차

**원칙**: 복원 전에 현재 데이터를 먼저 백업한다. 스크립트가 자동으로 수행한다.

1. 파일 복원 (스냅샷 날짜 지정):
   ```bash
   infra/scripts/restore.sh --snapshot 20260930-030000 --yes
   # --snapshot latest  → 가장 최근 스냅샷
   # --target-root DIR  → 다른 위치로 복원 (기본값: PROJECTS_ROOT)
   # --dry-run          → 실제 변경 없이 미리보기
   ```
   - 실행 전 현재 `projects/`·`library/`의 안전 스냅샷을 자동으로 1세대 추가한다.
   - `--yes` 없이 실행하면 `yes` 입력 확인을 요구한다.

2. DB 복원:
   ```bash
   infra/scripts/restore.sh --db-dump infra/backups/db/20260930-030000.dump --yes
   ```
   - `--snapshot`만 주고 `--db-dump`를 생략하면 같은 세대의 덤프가 있으면 함께 복원한다.
   - postgres: 복원 전 `pre-restore-<stamp>.dump`를 먼저 뜬다 (`pg_restore -c`).
   - sqlite: 기존 파일을 `<db>.pre-restore.bak`으로 복사해 둔다.

3. 복원 후 확인:
   - API 기동 후 `GET /healthz`, 프로젝트 목록 조회로 DB 정합성을 확인한다.
   - 파일은 `GET /files/<relative-path>`로 1~2개 샘플을 열어 본다.

## 복원이 잘못되었을 때

- 파일: 복원 직전에 뜬 안전 스냅샷(`--list`에서 가장 최근)으로 다시 복원한다.
- DB: `pre-restore-<stamp>.dump` (postgres) 또는 `<db>.pre-restore.bak` (sqlite)로 되돌린다.

## 알려진 제약

- rsync 변경 감지는 기본적으로 "크기+mtime"이다. 같은 초 안에 크기가 같은 채로
  바뀐 파일은 감지하지 못할 수 있다 (일 1회 크론 운용에서는 사실상 발생하지 않음).
- 스냅샷 디렉토리를 직접 수정하지 마라. 하드링크로 공유되므로 다른 세대의
  파일까지 바뀌는 것처럼 보일 수 있다 (실제로는 COW가 아니라 공유 inode를
  직접 건드리는 것이므로 위험).
