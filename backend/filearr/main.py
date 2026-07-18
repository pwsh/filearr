"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from filearr import __version__
from filearr.api import v1_router
from filearr.config import get_settings
from filearr.profiles import seed_profiles_to_db
from filearr.search import ensure_index


@asynccontextmanager
async def lifespan(app: FastAPI):
    # P4-T1: register the code-shipped metadata profiles (idempotent upsert).
    await seed_profiles_to_db()
    await ensure_index()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Filearr",
        version=__version__,
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
    )
    if settings.environment == "development":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(v1_router, prefix="/api/v1")
    # Built SPA is copied to /app/static in the Docker image
    try:
        app.mount("/", StaticFiles(directory="static", html=True), name="spa")
    except RuntimeError:
        pass  # dev mode: frontend served by Vite
    return app


app = create_app()
