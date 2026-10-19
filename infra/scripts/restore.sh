#!/usr/bin/env bash
# restore.sh — restore files and/or DB from a backup.sh snapshot.
#
# Safety first: before touching live data, the script takes a fresh
# pre-restore snapshot (files) and copies the current DB aside, so a bad
# restore can itself be undone. Restoring files requires explicit
# confirmation (interactive prompt or --yes).
#
# Usage:
#   infra/scripts/restore.sh --list
#   infra/scripts/restore.sh --snapshot <STAMP|latest> [--db-dump FILE]
#                            [--target-root DIR] [--yes] [--dry-run]
#                          (omit --db-dump to restore files only)
#   infra/scripts/restore.sh --db-dump FILE [--yes] [--dry-run]
#                          (DB only; FILE defaults to the newest dump)
#
# Env (or .env in the repo root): DATABASE_URL, PROJECTS_ROOT.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKUP_ROOT="$REPO_ROOT/infra/backups"
SNAPSHOT=""
DB_DUMP=""
TARGET_ROOT=""
YES=0
DRY_RUN=0
LIST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list) LIST=1; shift ;;
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --db-dump) DB_DUMP="$2"; shift 2 ;;
    --target-root) TARGET_ROOT="$2"; shift 2 ;;
    --backup-root) BACKUP_ROOT="$2"; shift 2 ;;
    --yes) YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"; set +a
fi
FILES_DIR="$BACKUP_ROOT/files"
DB_DIR="$BACKUP_ROOT/db"
PROJECTS_ROOT="${PROJECTS_ROOT:-$REPO_ROOT/projects}"
DATABASE_URL="${DATABASE_URL:-}"
TARGET_ROOT="${TARGET_ROOT:-$PROJECTS_ROOT}"

log()  { echo "[restore] $*"; }
fail() { echo "[restore] ERROR: $*" >&2; exit 1; }

if [[ $LIST -eq 1 ]]; then
  echo "file snapshots:"
  ls -1 "$FILES_DIR" 2>/dev/null | grep -E '^[0-9]{8}-[0-9]{6}(-[0-9]+)?$' | sort || echo "  (none)"
  if [[ -L "$FILES_DIR/latest" ]]; then
    echo "latest -> $(readlink "$FILES_DIR/latest")"
  fi
  echo "db dumps:"
  ls -1 "$DB_DIR" 2>/dev/null | grep -E '^[0-9]{8}-[0-9]{6}(-[0-9]+)?\.(dump|sqlite3)$' | sort || echo "  (none)"
  exit 0
fi

[[ -n "$SNAPSHOT" || -n "$DB_DUMP" ]] || fail "nothing to do: pass --snapshot and/or --db-dump (see --help)"

confirm() {
  [[ $YES -eq 1 || $DRY_RUN -eq 1 ]] && return 0
  read -r -p "[restore] $1 Type 'yes' to continue: " ans
  [[ "$ans" == "yes" ]] || fail "aborted by user"
}

# --- 1. files ------------------------------------------------------------------
if [[ -n "$SNAPSHOT" ]]; then
  [[ "$SNAPSHOT" == "latest" ]] && SNAPSHOT="$(readlink "$FILES_DIR/latest")"
  src="$FILES_DIR/$SNAPSHOT"
  [[ -d "$src" ]] || fail "snapshot not found: $src (see --list)"

  confirm "restore files from snapshot $SNAPSHOT into $TARGET_ROOT (current files will be replaced)?"

  # Pre-restore safety snapshot of the live data (skipped in dry-run).
  if [[ $DRY_RUN -eq 0 ]]; then
    log "taking pre-restore safety snapshot of live data"
    "$REPO_ROOT/infra/scripts/backup.sh" --backup-root "$BACKUP_ROOT" \
      --keep-files 40 >/dev/null
  fi

  for sub in projects library; do
    if [[ -d "$src/$sub" ]]; then
      log "restoring $src/$sub -> $TARGET_ROOT/$sub"
      if [[ $DRY_RUN -eq 0 ]]; then
        mkdir -p "$TARGET_ROOT/$sub"
        rsync -a --delete "$src/$sub/" "$TARGET_ROOT/$sub/"
      else
        rsync -a --delete --dry-run "$src/$sub/" "$TARGET_ROOT/$sub/"
      fi
    else
      log "snapshot has no $sub/; skipping"
    fi
  done
  log "file restore complete"
fi

# --- 2. database ---------------------------------------------------------------
pg_plain_url() {
  sed -E 's#^postgres(ql)?\+[^:]+://#postgres\1://#' <<<"$1"
}

do_db_restore=0
dump_file=""
if [[ -n "$DB_DUMP" ]]; then
  dump_file="$DB_DUMP"
  [[ -f "$dump_file" ]] || fail "db dump not found: $dump_file"
  do_db_restore=1
elif [[ -n "$SNAPSHOT" ]]; then
  # No explicit dump: use the dump from the same generation if it exists.
  for ext in dump sqlite3; do
    if [[ -f "$DB_DIR/${SNAPSHOT}.${ext}" ]]; then
      dump_file="$DB_DIR/${SNAPSHOT}.${ext}"
      do_db_restore=1
      break
    fi
  done
  [[ $do_db_restore -eq 0 ]] && log "no db dump for generation $SNAPSHOT; files only"
fi

if [[ $do_db_restore -eq 1 ]]; then
  [[ -z "$DATABASE_URL" ]] && fail "DATABASE_URL not set; cannot restore DB"
  confirm "restore database from $dump_file (current DB will be replaced)?"

  if [[ "$DATABASE_URL" == postgresql* ]]; then
    command -v pg_restore >/dev/null || fail "pg_restore not found in PATH"
    url="$(pg_plain_url "$DATABASE_URL")"
    if [[ $DRY_RUN -eq 1 ]]; then
      log "dry-run: would pg_restore -c -d <db> $dump_file"
    else
      log "pg_restore -c -> database (pre-restore dump taken first)"
      pre="$DB_DIR/pre-restore-$(date +%Y%m%d-%H%M%S).dump"
      pg_dump "$url" -Fc -f "$pre" && log "pre-restore dump: $pre"
      pg_restore -c -d "$url" "$dump_file"
    fi
  elif [[ "$DATABASE_URL" == sqlite:* ]]; then
    dbfile="${DATABASE_URL#sqlite:///}"
    if [[ $DRY_RUN -eq 1 ]]; then
      log "dry-run: would replace $dbfile with $dump_file"
    else
      if [[ -f "$dbfile" ]]; then
        cp -p "$dbfile" "$dbfile.pre-restore.bak"
        log "current db backed up to $dbfile.pre-restore.bak"
      fi
      cp -p "$dump_file" "$dbfile"
    fi
  else
    fail "unsupported DATABASE_URL scheme: ${DATABASE_URL%%://*}://..."
  fi
  log "db restore complete"
fi

log "restore finished (dry_run=$DRY_RUN)"
