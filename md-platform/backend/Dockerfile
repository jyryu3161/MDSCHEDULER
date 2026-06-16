# MD Platform — Backend (FastAPI) image.
#
# Build context: this Dockerfile expects the docker-compose build to use the
# PROJECT ROOT (md-platform/) as the build context so that BOTH ./backend and
# ./worker are visible, e.g. in docker-compose.yml:
#
#     backend:
#       build:
#         context: .                      # md-platform/ (project root)
#         dockerfile: backend/Dockerfile
#
# The backend imports the `mdworker` package (Reporter Protocol type + the
# validate_input entrypoint + the in-process runner used by QUEUE_BACKEND=local),
# so the worker source is copied in and installed. The backend is import-guarded:
# if rdkit (a worker dependency) is unavailable it still starts and serves
# health/auth, returning a clear error from validation/enqueue endpoints.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps: build tools are needed only if a wheel must be compiled; keep slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 1) Install backend Python dependencies first (better layer caching).
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/backend/requirements.txt

# 2) Install the worker package so `import mdworker` works for the Reporter
#    Protocol / validate_input / runner. The worker pulls its own (possibly heavy)
#    scientific deps from its pyproject.
#
#    By default the build FAILS if the worker install fails — the backend genuinely
#    depends on `mdworker`, so a broken install must not ship as a green image.
#    Set --build-arg REQUIRE_WORKER=0 to build a deliberately backend-only image
#    (e.g. a CI image that exercises only auth/health); in that mode the backend's
#    runtime import-guards keep it serving while validation/enqueue return a clear
#    "worker unavailable" error.
ARG REQUIRE_WORKER=1
COPY worker /app/worker
RUN if [ "$REQUIRE_WORKER" = "1" ]; then \
        pip install ./worker; \
    else \
        pip install ./worker || echo "WARNING: REQUIRE_WORKER=0 — mdworker install skipped/failed; backend will import-guard it."; \
    fi

# 3) Backend application code.
COPY backend/app /app/app

# Storage volume mount point (compose mounts ./storage:/app/storage).
RUN mkdir -p /app/storage

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
