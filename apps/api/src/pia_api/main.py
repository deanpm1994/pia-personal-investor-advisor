"""FastAPI application factory and process entry point."""

from fastapi import FastAPI

from pia_api.api.health import router as health_router
from pia_api.api.identity import router as identity_router
from pia_api.core.auth import SupabaseJWTVerifier
from pia_api.core.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the PIA API application with explicit non-secret settings."""
    app = FastAPI(title="PIA API")
    app.state.settings = settings or Settings()
    app.state.jwt_verifier = SupabaseJWTVerifier(app.state.settings)
    app.include_router(health_router)
    app.include_router(identity_router)
    return app


app = create_app()
