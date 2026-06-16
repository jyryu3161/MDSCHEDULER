#!/usr/bin/env bash
# =============================================================================
# healthcheck.sh - verify the MD Platform is serving (CONTRACT §11 / PDR §21).
#
# Probes, through the frontend's nginx proxy (the same path a browser uses):
#   GET /api/health  -> must return HTTP 2xx (liveness; unauthenticated).
#   GET /api/gpus    -> must be reachable (the backend is routing /api). This
#                       endpoint requires auth (CONTRACT §5), so HTTP 401 is an
#                       EXPECTED "server is up" response and is treated as pass;
#                       a connection failure or 5xx is a failure.
#
# Exits 0 only when both probes pass; nonzero otherwise (usable as a CI gate or
# a container/orchestrator healthcheck).
#
# Usage:
#   ./scripts/healthcheck.sh
#   ./scripts/healthcheck.sh --gpus        # also print GPU diagnostics (host + workers)
#   APP_PORT=8080 ./scripts/healthcheck.sh
#   HEALTH_HOST=my-host ./scripts/healthcheck.sh
#   HEALTH_CHECK_GPUS=1 ./scripts/healthcheck.sh
#
# GPU diagnostics are opt-in (--gpus / HEALTH_CHECK_GPUS=1) and are purely
# informational: they never change the pass/fail result of the liveness probes.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

log()  { printf '\033[1;34m[health]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; }

# Resolve APP_PORT: explicit env > .env > contract default (8888).
if [ -z "${APP_PORT:-}" ] && [ -f "${ROOT_DIR}/.env" ]; then
  APP_PORT="$(grep -E '^APP_PORT=' "${ROOT_DIR}/.env" | tail -1 | cut -d= -f2- || true)"
fi
APP_PORT="${APP_PORT:-8888}"
HEALTH_HOST="${HEALTH_HOST:-localhost}"
BASE="http://${HEALTH_HOST}:${APP_PORT}"

if ! command -v curl >/dev/null 2>&1; then
  err "curl is not installed; cannot run health checks."
  exit 2
fi

FAILED=0

# --- /api/health : strict 2xx required ---------------------------------------
log "GET ${BASE}/api/health"
if curl -fsS --max-time 10 "${BASE}/api/health" >/dev/null 2>&1; then
  ok "/api/health responded 2xx"
else
  err "/api/health did not return success (backend down or not proxied)."
  FAILED=1
fi

# --- /api/gpus : reachability (401 is an expected authenticated-route reply) -
log "GET ${BASE}/api/gpus"
GPU_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${BASE}/api/gpus" || echo 000)"
case "${GPU_CODE}" in
  2??|401|403)
    ok "/api/gpus reachable (HTTP ${GPU_CODE})"
    ;;
  000)
    err "/api/gpus unreachable (connection failed)."
    FAILED=1
    ;;
  *)
    err "/api/gpus returned HTTP ${GPU_CODE} (expected 2xx or 401/403)."
    FAILED=1
    ;;
esac

# --- GPU visibility (opt-in; informational; never affects pass/fail) ---------
# Enabled by `--gpus` or HEALTH_CHECK_GPUS=1. Kept opt-in so the core liveness
# result is not diluted by GPU diagnostics. No bash arrays are used so the
# block is safe even if invoked under a non-bash /bin/sh.
WANT_GPUS="${HEALTH_CHECK_GPUS:-0}"
for arg in "$@"; do
  [ "${arg}" = "--gpus" ] && WANT_GPUS=1
done

if [ "${WANT_GPUS}" = "1" ]; then
  # Pick a compose command without arrays (portable).
  COMPOSE_CMD=""
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
  fi

  log "Host GPUs (nvidia-smi)"
  if command -v nvidia-smi >/dev/null 2>&1; then
    if nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu \
         --format=csv,noheader 2>/dev/null; then
      ok "host nvidia-smi reported GPUs above"
    else
      log "nvidia-smi present but query failed"
    fi
  else
    log "no nvidia-smi on host (mock-engine path; GPU workers need NVIDIA hardware)"
  fi

  if [ -n "${COMPOSE_CMD}" ]; then
    log "GPU workers' in-container GPU access"
    for svc in worker-gpu-0 worker-gpu-1; do
      if (cd "${ROOT_DIR}" && ${COMPOSE_CMD} ps "${svc}" 2>/dev/null | grep -q "${svc}"); then
        if (cd "${ROOT_DIR}" && ${COMPOSE_CMD} exec -T "${svc}" nvidia-smi -L >/dev/null 2>&1); then
          GPUS="$(cd "${ROOT_DIR}" && ${COMPOSE_CMD} exec -T "${svc}" nvidia-smi -L 2>/dev/null | head -1)"
          ok "${svc}: GPU visible (${GPUS})"
        else
          log "${svc}: running but in-container nvidia-smi failed (toolkit/reservation issue)"
        fi
      else
        log "${svc}: not running"
      fi
    done
  fi
fi

if [ "${FAILED}" -ne 0 ]; then
  err "Health check FAILED. Inspect logs with: docker compose logs -f backend frontend"
  exit 1
fi

ok "All health checks passed: ${BASE}"
