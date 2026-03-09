"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI

logging.getLogger("devops_agent").setLevel(logging.INFO)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import apply_db_overrides
from ..database import get_all_system_settings, init_db
from ..poller import poll_loop
from .routes.api import router as api_router
from .routes.auth import router as auth_router
from .routes.chat import router as chat_router
from .routes.terminal import router as terminal_router
from .routes.webhook import router as webhook_router

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="DevOps Agent")

    @app.on_event("startup")
    async def startup():
        init_db()
        apply_db_overrides(get_all_system_settings())
        asyncio.create_task(poll_loop())

    # Routers
    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(terminal_router)
    app.include_router(webhook_router)
    app.include_router(api_router)

    # Static files
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
