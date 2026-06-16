#!/usr/bin/env bash
# =============================================================================
# install.sh - one-shot installer for the MD Platform (CONTRACT §11 / PDR §21.3).
#
# Checks prerequisites (Docker, Docker Compose v2, NVIDIA Container Toolkit),
# prepares .env, builds the md-env scientific base image, brings the stack up,
# and prints the access URL.
#
# Usage:
#   ./scripts/install.sh                 # full install
#   SKIP_MDENV_BUILD=1 ./scripts/install.sh   # skip the heavy md-env build
#   GROMACS_BUILD=OFF ./scripts/install.sh    # build md-env without GROMACS (mock)
# =============================================================================
set -euo pipefail

# Resolve project root (parent of this scripts/ dir) regardless of CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

MDENV_IMAGE="md-platform-mdenv:latest"
CUDA_TEST_IMAGE="nvidia/cuda:12.4.1-base-ubuntu22.04"

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# 1. Prerequisite checks.
# ---------------------------------------------------------------------------
log "Checking prerequisites ..."

if ! command -v docker >/dev/null 2>&1; then
  err "Docker is not installed. Install Docker Engine: https://docs.docker.com/engine/install/"
  exit 1
fi
log "Docker found: $(docker --version)"

# Docker Compose v2 is the `docker compose` subcommand.
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  warn "Using legacy docker-compose v1. Docker Compose v2 (\`docker compose\`) is recommended."
  COMPOSE=(docker-compose)
else
  err "Docker Compose not found. Install Compose v2: https://docs.docker.com/compose/install/"
  exit 1
fi
log "Docker Compose found: $("${COMPOSE[@]}" version | head -1)"

# NVIDIA Container Toolkit check (PDR §21.4). Non-fatal: the platform still runs
# CPU/mock-only without GPUs, so warn rather than abort.
log "Checking NVIDIA Container Toolkit (GPU passthrough) ..."
if command -v nvidia-smi >/dev/null 2>&1; then
  if docker run --rm --gpus all "${CUDA_TEST_IMAGE}" nvidia-smi >/dev/null 2>&1; then
    log "GPU passthrough OK (docker --gpus all nvidia-smi succeeded)."
  else
    warn "Host has nvidia-smi but \`docker run --gpus all\` failed."
    warn "Install/configure the NVIDIA Container Toolkit:"
    warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
    warn "Verify with: docker run --rm --gpus all ${CUDA_TEST_IMAGE} nvidia-smi"
    warn "Continuing; GPU workers will fail to start until this is fixed."
  fi
else
  warn "No nvidia-smi on host: no GPUs detected. The stack will run, but GPU"
  warn "workers need NVIDIA hardware + toolkit. Set MD_ENGINE=mock in .env to run"
  warn "the full pipeline without GROMACS/GPU."
fi

# ---------------------------------------------------------------------------
# 2. Prepare .env (copy from .env.example if absent) and rotate placeholder
#    secrets so a fresh install never starts with the committed example values.
# ---------------------------------------------------------------------------

# Generate a strong random secret (URL-safe, no shell-special chars).
gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 64
  fi
}

# Set KEY=value in .env, replacing any existing line; appends KEY if absent.
set_env_var() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" .env; then
    # Use a non-/ delimiter; secrets are hex/base-safe so | is safe.
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "${key}" "${value}" >> .env
  fi
}

# Rotate a secret only if it is unset or still the known placeholder value.
rotate_if_placeholder() {
  local key="$1" placeholder="$2"
  local current
  current="$(grep -E "^${key}=" .env | tail -1 | cut -d= -f2- || true)"
  if [ -z "${current}" ] || [ "${current}" = "${placeholder}" ]; then
    local newval
    newval="$(gen_secret)"
    set_env_var "${key}" "${newval}"
    log "Rotated ${key}: was placeholder/empty, set to a strong random value."
    return 0
  fi
  return 1
}

FRESH_ENV=0
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    FRESH_ENV=1
    log "Created .env from .env.example."
  else
    err ".env.example not found; cannot create .env."
    exit 1
  fi
else
  log ".env already exists; leaving existing values intact (only placeholders rotated)."
fi

# Always rotate any secret still left at its committed placeholder, on fresh
# or existing .env. Admin login (csbl/csbl) is intentionally left as the
# documented default; first login forces a password change.
ROTATED=0
rotate_if_placeholder "JWT_SECRET"         "change-me-in-production"        && ROTATED=1 || true
rotate_if_placeholder "INTERNAL_API_TOKEN" "internal-worker-token-change-me" && ROTATED=1 || true
rotate_if_placeholder "POSTGRES_PASSWORD"  "mdpass"                          && ROTATED=1 || true

# Ensure POSTGRES_* exist so compose interpolation and the db service agree.
grep -qE '^POSTGRES_USER=' .env || set_env_var "POSTGRES_USER" "mduser"
grep -qE '^POSTGRES_DB='   .env || set_env_var "POSTGRES_DB" "mdplatform"
if ! grep -qE '^POSTGRES_PASSWORD=' .env; then
  set_env_var "POSTGRES_PASSWORD" "$(gen_secret)"
  log "Set POSTGRES_PASSWORD to a strong random value."
  ROTATED=1
fi

if [ "${ROTATED}" = "1" ]; then
  warn "One or more secrets were rotated in .env. The admin login remains csbl/csbl"
  warn "(forced password change on first login). Keep .env private (it is gitignored)."
fi

# ---------------------------------------------------------------------------
# 3. Build the md-env scientific base image (heavy; the worker image FROMs it).
# ---------------------------------------------------------------------------
if [ "${SKIP_MDENV_BUILD:-0}" = "1" ]; then
  warn "SKIP_MDENV_BUILD=1: skipping md-env image build. The worker build will"
  warn "fail unless ${MDENV_IMAGE} already exists locally."
else
  log "Building ${MDENV_IMAGE} (this is heavy: GROMACS GPU compile + conda stack) ..."
  docker build \
    --build-arg "GROMACS_BUILD=${GROMACS_BUILD:-ON}" \
    -t "${MDENV_IMAGE}" \
    ./md-env
  log "Built ${MDENV_IMAGE}."
fi

# ---------------------------------------------------------------------------
# 4. Build + start the stack.
# ---------------------------------------------------------------------------
log "Building and starting services (docker compose up -d) ..."
"${COMPOSE[@]}" up -d --build

# ---------------------------------------------------------------------------
# 5. Report the access URL.
# ---------------------------------------------------------------------------
APP_PORT="$(grep -E '^APP_PORT=' .env | tail -1 | cut -d= -f2 || true)"
APP_PORT="${APP_PORT:-8888}"
# Best-effort primary IP for a shareable URL.
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-localhost}"

log "Stack is up."
echo
echo "  Open the platform at:  http://${HOST_IP}:${APP_PORT}"
echo "  (or http://localhost:${APP_PORT} on this host)"
echo "  Default login:         csbl / csbl  (you will be required to change the password)"
echo
log "Check health with:  ./scripts/healthcheck.sh"
log "Tail logs with:     ${COMPOSE[*]} logs -f"
