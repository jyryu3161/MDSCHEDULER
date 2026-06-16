# =============================================================================
# MD Platform - Makefile
#
# Two ways to run the platform:
#   1. Docker (production-like, full GPU pipeline):   make up
#   2. Local dev (no Docker, SQLite + mock engine):   make install-local
#                                                     make dev-backend   (one shell)
#                                                     make dev-frontend  (another shell)
#
# In LOCAL DEV the queue backend is `local`: the backend runs jobs in-process via
# its ThreadPoolExecutor + DbReporter (CONTRACT §5), so NO separate worker process
# is required. `make dev-worker` is provided only for exercising the RQ path
# (QUEUE_BACKEND=rq) against a local Redis.
#
# Targets:
#   install-local  pip-install backend reqs + editable mdworker + npm install
#   dev-backend    uvicorn (reload) with SQLite + local queue + mock engine
#   dev-worker     RQ worker on the 'md' queue (optional; needs Redis + rq extra)
#   dev-frontend   Vite dev server (proxies /api -> http://localhost:8000)
#   build          build the heavy md-env scientific base image
#   up / down      docker compose up -d --build / down
#   logs           follow docker compose logs
#   health         run scripts/healthcheck.sh --gpus
#   backup         run scripts/backup.sh (storage tar + pg_dump)
#   test           backend pytest + a local mock smoke run
#   seed           reset the local SQLite DB so startup re-seeds the admin user
#   clean          remove local virtualenv, build caches, node_modules
# =============================================================================

# Resolve the directory this Makefile lives in (project root) so targets work
# regardless of the caller's CWD.
ROOT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# Tooling (override on the command line, e.g. `make PYTHON=python3.11 dev-backend`).
PYTHON  ?= python3
PIP     ?= $(PYTHON) -m pip
NPM     ?= npm
VENV    ?= $(ROOT_DIR)/.venv
VENV_PY := $(VENV)/bin/python
VENV_BIN := $(VENV)/bin

# Compose command: prefer Docker Compose v2 (`docker compose`).
COMPOSE ?= docker compose

# The scientific base image the worker image is built FROM (see md-env/Dockerfile).
MDENV_IMAGE ?= md-platform-mdenv:latest

# Local-dev environment (CONTRACT §1 "local dev without Docker"):
#   SQLite DB under ./storage, in-process local queue, mock MD engine.
LOCAL_ENV := \
	STORAGE_ROOT=$(ROOT_DIR)/storage \
	DATABASE_URL=sqlite:///$(ROOT_DIR)/storage/md_platform.db \
	REDIS_URL=redis://localhost:6379/0 \
	QUEUE_BACKEND=local \
	MD_ENGINE=mock \
	MDP_TEMPLATE_DIR=$(ROOT_DIR)/md-env/templates/gromacs \
	JWT_SECRET=dev-secret-not-for-production \
	INTERNAL_API_TOKEN=dev-internal-token

# RQ worker variant of the local env (queue backend = rq, talks to local backend).
WORKER_ENV := \
	STORAGE_ROOT=$(ROOT_DIR)/storage \
	DATABASE_URL=sqlite:///$(ROOT_DIR)/storage/md_platform.db \
	REDIS_URL=redis://localhost:6379/0 \
	QUEUE_BACKEND=rq \
	MD_ENGINE=mock \
	MDP_TEMPLATE_DIR=$(ROOT_DIR)/md-env/templates/gromacs \
	BACKEND_URL=http://localhost:8000 \
	INTERNAL_API_TOKEN=dev-internal-token

.DEFAULT_GOAL := help
.PHONY: help install-local dev-backend dev-worker dev-frontend build up down logs health backup test seed clean

help: ## Show this help.
	@printf 'MD Platform - make targets:\n\n'
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[1;34m%-14s\033[0m %s\n", $$1, $$2}'
	@printf '\nLocal dev: make install-local  ->  make dev-backend  ->  make dev-frontend\n'
	@printf 'Docker:    make up  (then open http://localhost:8888, login csbl/csbl)\n'

# ---------------------------------------------------------------------------
# Local development (no Docker).
# ---------------------------------------------------------------------------

install-local: ## Create venv, install backend reqs + editable mdworker, npm install frontend.
	@echo "==> Creating virtualenv at $(VENV)"
	$(PYTHON) -m venv $(VENV)
	$(VENV_PY) -m pip install --upgrade pip
	@echo "==> Installing backend requirements"
	$(VENV_PY) -m pip install -r $(ROOT_DIR)/backend/requirements.txt
	@echo "==> Installing the mdworker package (editable, with RQ extra)"
	$(VENV_PY) -m pip install -e "$(ROOT_DIR)/worker[rq]"
	@echo "==> Installing frontend dependencies"
	$(NPM) --prefix $(ROOT_DIR)/frontend install
	@mkdir -p $(ROOT_DIR)/storage/uploads $(ROOT_DIR)/storage/jobs $(ROOT_DIR)/storage/results
	@echo "==> install-local complete. Next: make dev-backend  (and make dev-frontend)"

dev-backend: ## Run the backend with uvicorn --reload (SQLite + local queue + mock engine).
	@mkdir -p $(ROOT_DIR)/storage/uploads $(ROOT_DIR)/storage/jobs $(ROOT_DIR)/storage/results
	cd $(ROOT_DIR)/backend && $(LOCAL_ENV) $(VENV_BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-worker: ## (Optional) Run an RQ worker on the 'md' queue against local Redis.
	@echo "Requires Redis at $${REDIS_URL:-redis://localhost:6379/0} and QUEUE_BACKEND=rq."
	cd $(ROOT_DIR) && $(WORKER_ENV) $(VENV_BIN)/rq worker md

dev-frontend: ## Run the Vite dev server (proxies /api -> http://localhost:8000).
	$(NPM) --prefix $(ROOT_DIR)/frontend run dev

# ---------------------------------------------------------------------------
# Docker stack.
# ---------------------------------------------------------------------------

build: ## Build the md-env scientific base image (heavy; GROMACS GPU compile + conda).
	docker build -t $(MDENV_IMAGE) $(ROOT_DIR)/md-env

up: ## Build and start the Docker stack in the background.
	$(COMPOSE) up -d --build

down: ## Stop and remove the Docker stack containers.
	$(COMPOSE) down

logs: ## Follow logs from all Docker services.
	$(COMPOSE) logs -f

health: ## Run the health check (includes GPU diagnostics).
	$(ROOT_DIR)/scripts/healthcheck.sh --gpus

backup: ## Back up storage/ + the database (scripts/backup.sh).
	$(ROOT_DIR)/scripts/backup.sh

# ---------------------------------------------------------------------------
# Quality + data.
# ---------------------------------------------------------------------------

test: ## Run backend pytest and a local mock smoke run.
	@echo "==> Backend pytest"
	@cd $(ROOT_DIR)/backend && $(LOCAL_ENV) $(VENV_BIN)/python -m pytest -q; \
		rc=$$?; \
		if [ $$rc -eq 5 ]; then \
			echo "(pytest collected no tests; treating as pass)"; \
		elif [ $$rc -ne 0 ]; then \
			echo "backend pytest failed (exit $$rc)"; exit $$rc; \
		fi
	@echo "==> Worker mock smoke (import + parse the sample PDBQT poses)"
	$(LOCAL_ENV) $(VENV_PY) -c "import mdworker; from mdworker.io import pdbqt; \
		poses = pdbqt.parse_pdbqt_models('$(ROOT_DIR)/samples/3-HDC_KCCIVYP.pdbqt'); \
		assert len(poses) >= 1, 'no poses parsed'; \
		print('smoke OK: parsed', len(poses), 'poses from the sample PDBQT')"

seed: ## Reset the local SQLite DB so the backend re-seeds the admin (csbl/csbl) on next start.
	@rm -f $(ROOT_DIR)/storage/md_platform.db
	@echo "Removed local SQLite DB. Start the backend to re-seed admin user csbl/csbl."

clean: ## Remove the local virtualenv, Python caches, and frontend node_modules.
	rm -rf $(VENV)
	rm -rf $(ROOT_DIR)/frontend/node_modules $(ROOT_DIR)/frontend/dist
	find $(ROOT_DIR) -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	find $(ROOT_DIR) -type d -name '*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned local dev artifacts (storage/ and .env left intact)."
