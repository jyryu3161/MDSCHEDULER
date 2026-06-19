"""FastAPI application entrypoint (CONTRACT §5, §11).

Wires all routers under ``/api``, creates tables + seeds on startup, launches the
background GPU poller and resource sampler, captures the running event loop for the
cross-thread realtime bus, and optionally serves a built frontend from STORAGE.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import psutil
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import realtime
from .config import get_settings
from .database import init_db, session_scope
from .models import ResourceUsage, utcnow
from .routers import (
    auth,
    dashboard,
    design,
    events,
    gpus,
    internal,
    jobs,
    queue,
    results,
    settings as settings_router,
    uploads,
    ws,
)
from .seed import seed_all
from .services import gpu_manager, storage

logger = logging.getLogger("mdplatform.backend")

_GPU_POLL_SECONDS = 5.0
_RESOURCE_SAMPLE_SECONDS = 15.0


async def _gpu_poller() -> None:
    """Periodically refresh GPU live metrics and publish a dashboard tick."""
    while True:
        try:
            with session_scope() as db:
                gpu_manager.poll_and_update(db)
            await realtime.bus.publish(realtime.dashboard_topic(), {"trigger": "gpu_poll"})
        except Exception as exc:  # noqa: BLE001
            logger.debug("GPU poll failed: %s", exc)
        await asyncio.sleep(_GPU_POLL_SECONDS)


async def _resource_sampler() -> None:
    """Periodically record host CPU/memory/disk into resourceusage."""
    # Prime cpu_percent (first call returns 0.0).
    psutil.cpu_percent(interval=None)
    while True:
        await asyncio.sleep(_RESOURCE_SAMPLE_SECONDS)
        try:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().used / (1024 * 1024)
            used_gb, _ = storage.disk_usage_gb()
            with session_scope() as db:
                db.add(
                    ResourceUsage(
                        cpu_percent=float(cpu),
                        memory_used=float(mem),
                        disk_used=float(used_gb * 1024.0),
                        sampled_at=utcnow(),
                    )
                )
                db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Resource sample failed: %s", exc)


def _security_preflight(settings) -> None:
    """Warn loudly (PDR §22) when secrets are left at their shipped defaults.

    Non-fatal so local/dev (sqlite) and first-boot still work, but operators get a clear,
    actionable warning to rotate JWT_SECRET / INTERNAL_API_TOKEN before exposing the service.
    The default admin (csbl/csbl) is mitigated by the forced first-login password change.
    """
    defaults = {
        "JWT_SECRET": "change-me-in-production",
        "INTERNAL_API_TOKEN": "internal-worker-token-change-me",
    }
    at_default = [k for k, v in defaults.items() if getattr(settings, k, None) == v]
    if not at_default:
        return
    # Heuristic for a non-local deployment: a non-sqlite database (compose/prod uses
    # PostgreSQL). There, refuse to start with default secrets (PDR §22); local sqlite dev
    # only warns so first-boot still works.
    is_local_sqlite = str(getattr(settings, "DATABASE_URL", "")).startswith("sqlite")
    msg = (
        f"{', '.join(at_default)} still set to the shipped default(s). "
        "Set strong values in .env before exposing the platform (PDR §22)."
    )
    if is_local_sqlite:
        logger.warning("SECURITY: %s", msg)
    else:
        raise RuntimeError(
            "Refusing to start a non-local deployment with default secrets — " + msg
        )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup.
    init_db()
    storage.ensure_base_tree()
    with session_scope() as db:
        seed_all(db)
    _security_preflight(get_settings())

    realtime.set_main_loop(asyncio.get_running_loop())

    tasks = [
        asyncio.create_task(_gpu_poller(), name="gpu-poller"),
        asyncio.create_task(_resource_sampler(), name="resource-sampler"),
    ]
    try:
        yield
    finally:
        # Shutdown.
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        realtime.set_main_loop(None)
        from .services.queue_manager import get_queue_manager

        with contextlib.suppress(Exception):
            get_queue_manager().shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="MD Platform API",
        version="1.0.0",
        description="Docking-to-MD platform backend.",
        lifespan=lifespan,
    )

    # Gzip responses (SPA JS/CSS + JSON). The hashed bundles are served uncompressed otherwise
    # (~140 KB index + ~165 KB react-vendor), which dominates first-load time for a remote
    # client; gzip cuts them ~3x and also shrinks the polled JSON. minimum_size avoids
    # compressing tiny payloads. Skips already-compressed assets (Plotly/NGL chunks gzip well too).
    from starlette.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=512)

    # CORS for dev (Vite on 5173/3000 and the compose frontend). Allow all in dev so
    # the SPA proxy and direct calls both work; tighten via reverse proxy in prod.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trajectory-Format", "Content-Disposition"],
    )

    api_routers = [
        auth.router,
        uploads.router,
        jobs.router,
        results.router,
        queue.router,
        gpus.router,
        dashboard.router,
        design.router,
        events.router,
        ws.router,
        internal.router,
        settings_router.router,
    ]
    for r in api_routers:
        app.include_router(r, prefix="/api")

    @app.get("/api/health", tags=["health"])
    def health() -> JSONResponse:
        s = get_settings()
        return JSONResponse(
            {
                "status": "ok",
                "version": app.version,
                "engine": s.resolved_md_engine(),
                "queue_backend": s.resolved_queue_backend(),
            }
        )

    _maybe_mount_frontend(app, settings)
    return app


def _maybe_mount_frontend(app: FastAPI, settings) -> None:
    """Serve a built SPA in single-origin mode.

    Looks for the built bundle at ``FRONTEND_DIST`` (if set — used by the all-in-one image,
    where the dist is baked at a fixed path that a STORAGE_ROOT volume mount can't shadow),
    else at ``STORAGE_ROOT/frontend``. Hashed assets are served from /assets; every other
    non-/api path falls back to index.html so client-side deep links resolve (try_files-style).
    """
    import os
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    configured = os.environ.get("FRONTEND_DIST", "").strip()
    dist = Path(configured) if configured else Path(settings.STORAGE_ROOT) / "frontend"
    index = dist / "index.html"
    if not index.exists():
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        # Never hijack the API namespace — return a JSON 404 for unknown /api paths.
        if full_path.startswith("api/") or full_path.startswith("api"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (dist / full_path).resolve()
        # Serve a real static file when it exists AND is genuinely contained in dist
        # (relative_to rejects ../ traversal and sibling dirs sharing a name prefix); else
        # fall back to index.html for SPA routing.
        if full_path and candidate.is_file():
            try:
                candidate.relative_to(dist.resolve())
                return FileResponse(str(candidate))
            except ValueError:
                pass
        return FileResponse(str(index))


app = create_app()
