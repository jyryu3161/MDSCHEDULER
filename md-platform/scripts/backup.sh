#!/usr/bin/env bash
# =============================================================================
# backup.sh - back up the MD Platform: storage tree + PostgreSQL dump.
#
# Produces a single timestamped tarball under backups/ containing:
#   - db.sql        : pg_dump of the Postgres database (via the `db` service)
#   - storage/      : the storage/ tree (uploads, jobs, results)
#   - .env          : the environment file (contains secrets; keep the backup safe)
#
# Usage:
#   ./scripts/backup.sh                 # write to ./backups/
#   BACKUP_DIR=/mnt/nas ./scripts/backup.sh
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

BACKUP_DIR="${BACKUP_DIR:-${ROOT_DIR}/backups}"
# Timestamp with nanoseconds + PID so concurrent/same-second runs never collide.
TS="$(date -u +%Y%m%d_%H%M%S)_$(date -u +%N)_$$"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

log()  { printf '\033[1;34m[backup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }

mkdir -p "${BACKUP_DIR}"

# Resolve compose command (v2 preferred).
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  COMPOSE=()
fi

# Pull DB credentials from .env (fall back to contract defaults).
get_env() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
PG_USER="$(get_env POSTGRES_USER)"; PG_USER="${PG_USER:-mduser}"
PG_DB="$(get_env POSTGRES_DB)";     PG_DB="${PG_DB:-mdplatform}"

# ---------------------------------------------------------------------------
# 1. Database dump (best-effort: only if the db service is running).
# ---------------------------------------------------------------------------
if [ "${#COMPOSE[@]}" -gt 0 ] && "${COMPOSE[@]}" ps db 2>/dev/null | grep -q db; then
  log "Dumping PostgreSQL database '${PG_DB}' ..."
  if "${COMPOSE[@]}" exec -T db pg_dump -U "${PG_USER}" "${PG_DB}" > "${WORK}/db.sql" 2>/dev/null; then
    log "Database dump written ($(wc -c < "${WORK}/db.sql") bytes)."
  else
    warn "pg_dump failed; the backup will omit db.sql."
    rm -f "${WORK}/db.sql"
  fi
else
  warn "db service not running (or compose unavailable); skipping database dump."
  warn "If using SQLite (local dev), the DB file lives under storage/ and is included below."
fi

# ---------------------------------------------------------------------------
# 2. Assemble the archive (storage + .env + db.sql).
# ---------------------------------------------------------------------------
ARCHIVE="${BACKUP_DIR}/md-platform-backup-${TS}.tar.gz"
if [ -e "${ARCHIVE}" ]; then
  warn "Archive ${ARCHIVE} already exists; refusing to overwrite."
  exit 1
fi
log "Creating archive ${ARCHIVE} ..."

# Build a file list that exists, to keep tar happy.
TAR_ARGS=()
[ -d storage ] && TAR_ARGS+=(-C "${ROOT_DIR}" storage)
[ -f .env ]    && TAR_ARGS+=(-C "${ROOT_DIR}" .env)
[ -f "${WORK}/db.sql" ] && TAR_ARGS+=(-C "${WORK}" db.sql)

if [ "${#TAR_ARGS[@]}" -eq 0 ]; then
  warn "Nothing to back up (no storage/, .env, or db.sql)."
  exit 1
fi

tar -czf "${ARCHIVE}" "${TAR_ARGS[@]}"
log "Backup complete: ${ARCHIVE} ($(du -h "${ARCHIVE}" | cut -f1))"
echo "${ARCHIVE}"
