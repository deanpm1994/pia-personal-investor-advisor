"""FastAPI application factory and process entry point."""

from fastapi import FastAPI

from pia_api.api.health import router as health_router
from pia_api.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the PIA API application with explicit non-secret settings."""
    app = FastAPI(title="PIA API")
    app.state.settings = settings or Settings()
    app.include_router(health_router)
    return app


app = create_app()
