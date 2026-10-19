#!/usr/bin/env bash
# backup.sh — ADR-0001 backup: hardlink snapshots + DB dumps.
#
# Files (projects/, library/): rsync --link-dest snapshots under
#   <backup-root>/files/<YYYYMMDD-HHMMSS>/, newest 30 kept.
#   Unchanged files are hardlinked, so each snapshot costs ~only the delta.
# DB: pg_dump (postgres) or file copy (sqlite) under
#   <backup-root>/db/, newest 14 kept.
#
# Idempotent: safe to re-run (each run adds one snapshot; retention is
# enforced every run). Supports --dry-run.
#
# Usage:
#   infra/scripts/backup.sh [--dry-run] [--backup-root DIR]
#                           [--keep-files N] [--keep-db N]
# Env (or .env in the repo root): DATABASE_URL, PROJECTS_ROOT.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP_ROOT="$REPO_ROOT/infra/backups"
KEEP_FILES=30
KEEP_DB=14
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --backup-root) BACKUP_ROOT="$2"; shift 2 ;;
    --keep-files) KEEP_FILES="$2"; shift 2 ;;
    --keep-db) KEEP_DB="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# Load .env (layer 2 config) if present; real env vars win.
if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"; set +a
fi

PROJECTS_ROOT="${PROJECTS_ROOT:-$REPO_ROOT/projects}"
DATABASE_URL="${DATABASE_URL:-}"

STAMP="$(date +%Y%m%d-%H%M%S)"
FILES_DIR="$BACKUP_ROOT/files"
DB_DIR="$BACKUP_ROOT/db"
# Guard against two runs within the same second.
n=0
while [[ -e "$FILES_DIR/$STAMP" && $n -lt 100 ]]; do
  n=$((n + 1))
  STAMP="$(date +%Y%m%d-%H%M%S)-$n"
done
SNAP_DIR="$FILES_DIR/$STAMP"
RSYNC_OPTS=(-a --delete)
[[ $DRY_RUN -eq 1 ]] && RSYNC_OPTS+=(--dry-run)

log()  { echo "[backup] $*"; }
fail() { echo "[backup] ERROR: $*" >&2; exit 1; }

command -v rsync >/dev/null || fail "rsync not found in PATH"

mkdir -p "$FILES_DIR" "$DB_DIR"

# Serialize concurrent runs.
exec 9>"$BACKUP_ROOT/.backup.lock"
flock -n 9 || fail "another backup is running (lock: $BACKUP_ROOT/.backup.lock)"

# --- 1. file snapshots -------------------------------------------------------
backed_up_any=0
for sub in projects library; do
  src="$PROJECTS_ROOT/$sub"
  if [[ -d "$src" ]]; then
    link=()
    if [[ -d "$FILES_DIR/latest/$sub" ]]; then
      link=(--link-dest="$FILES_DIR/latest/$sub")
    fi
    log "snapshotting $src -> $SNAP_DIR/$sub ${link[*]:-(full copy)}"
    if [[ $DRY_RUN -eq 0 ]]; then
      mkdir -p "$SNAP_DIR/$sub"
      if ((${#link[@]})); then
        rsync "${RSYNC_OPTS[@]}" "${link[@]}" "$src/" "$SNAP_DIR/$sub/"
      else
        rsync "${RSYNC_OPTS[@]}" "$src/" "$SNAP_DIR/$sub/"
      fi
    else
      rsync "${RSYNC_OPTS[@]}" "$src/" "$SNAP_DIR/$sub/"
    fi
    backed_up_any=1
  else
    log "skip: $src does not exist"
  fi
done

if [[ $backed_up_any -eq 1 && $DRY_RUN -eq 0 ]]; then
  ln -sfn "$STAMP" "$FILES_DIR/latest"
  log "snapshot complete: $SNAP_DIR"
fi

# Retention: keep newest $KEEP_FILES generations (+ the 'latest' symlink).
if [[ $DRY_RUN -eq 0 ]]; then
  mapfile -t old < <(ls -1 "$FILES_DIR" | grep -E '^[0-9]{8}-[0-9]{6}(-[0-9]+)?$' | sort | head -n "-$KEEP_FILES" || true)
  for d in "${old[@]:-}"; do
    [[ -n "$d" ]] && { log "pruning old snapshot: $d"; rm -rf "$FILES_DIR/$d"; }
  done
fi

# --- 2. database dump ----------------------------------------------------------
pg_plain_url() {
  # pg_dump/pg_restore need a plain postgres:// URI; strip SQLAlchemy
  # dialect suffixes like +psycopg / +psycopg2.
  sed -E 's#^postgres(ql)?\+[^:]+://#postgres\1://#' <<<"$1"
}

if [[ -z "$DATABASE_URL" ]]; then
  log "WARN: DATABASE_URL not set; skipping DB dump (files were still snapshotted)"
elif [[ "$DATABASE_URL" == postgresql* ]]; then
  command -v pg_dump >/dev/null || fail "pg_dump not found in PATH (needed for postgres DATABASE_URL)"
  dump="$DB_DIR/${STAMP}.dump"
  if [[ $DRY_RUN -eq 1 ]]; then
    log "dry-run: would pg_dump to $dump"
  else
    log "pg_dump -> $dump"
    pg_dump "$(pg_plain_url "$DATABASE_URL")" -Fc -f "$dump"
    log "db dump complete: $(du -h "$dump" | cut -f1)"
  fi
elif [[ "$DATABASE_URL" == sqlite:* ]]; then
  dbfile="${DATABASE_URL#sqlite:///}"
  if [[ -f "$dbfile" ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "dry-run: would copy sqlite db $dbfile -> $DB_DIR/${STAMP}.sqlite3"
    else
      log "copying sqlite db -> $DB_DIR/${STAMP}.sqlite3"
      cp -p "$dbfile" "$DB_DIR/${STAMP}.sqlite3"
    fi
  else
    log "WARN: sqlite db file not found: $dbfile; skipping DB dump"
  fi
else
  log "WARN: unsupported DATABASE_URL scheme; skipping DB dump: ${DATABASE_URL%%://*}://..."
fi

# DB retention: newest $KEEP_DB dumps.
if [[ $DRY_RUN -eq 0 ]]; then
  mapfile -t old_db < <(ls -1 "$DB_DIR" | grep -E '^[0-9]{8}-[0-9]{6}(-[0-9]+)?\.(dump|sqlite3)$' | sort | head -n "-$KEEP_DB" || true)
  for f in "${old_db[@]:-}"; do
    [[ -n "$f" ]] && { log "pruning old db dump: $f"; rm -f "$DB_DIR/$f"; }
  done
fi

log "backup finished (dry_run=$DRY_RUN)"
