"""FastAPI application factory."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from webapp.config import WEBAPP_ROOT
from webapp.server.routers import meta, sessions, skills
from webapp.store.db import init_db

logger = logging.getLogger(__name__)


def _safe_static_candidate(static_dir: Path, request_path: str) -> Path | None:
    """Return an existing file below ``static_dir``; reject path traversal."""
    root = static_dir.resolve()
    candidate = (root / request_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="TradingAgents 回测模拟器", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(meta.router)
    app.include_router(sessions.router)
    app.include_router(skills.router)

    # Frontend: prefer the npm build output (webapp/static), fall back to the
    # zero-build CDN single-page app (webapp/server/static_fallback).
    static_dir = WEBAPP_ROOT / "static"
    index_file = static_dir / "index.html"
    if not index_file.exists():
        static_dir = WEBAPP_ROOT / "server" / "static_fallback"
        index_file = static_dir / "index.html"
    if index_file.exists():
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():  # only the npm build has an assets/ dir
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            candidate = _safe_static_candidate(static_dir, full_path) if full_path else None
            if candidate is not None:
                return FileResponse(candidate)
            return FileResponse(index_file)
    else:
        @app.get("/", include_in_schema=False)
        def no_frontend():
            return {"hint": "前端未构建：请访问 /docs 使用 API，或 npm run build 后重启"}

    return app
