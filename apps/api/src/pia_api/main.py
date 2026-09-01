"""FastAPI application factory and process entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pia_api.api.accounts import router as accounts_router
from pia_api.api.financial_picture import router as financial_picture_router
from pia_api.api.health import router as health_router
from pia_api.api.identity import router as identity_router
from pia_api.api.imports import router as imports_router
from pia_api.api.market import router as market_router
from pia_api.core.auth import SupabaseJWTVerifier
from pia_api.core.config import Settings
from pia_api.services.financial_snapshots import TrustedSnapshotGateway
from pia_api.services.manual_accounts import TrustedManualAccountGateway
from pia_api.services.market_analysis import TrustedMarketAnalysisGateway
from pia_api.services.market_watchlist import TrustedMarketWatchlistGateway
from pia_api.services.staged_imports import SupabaseStagedImportGateway


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the PIA API application with explicit non-secret settings."""
    app = FastAPI(title="PIA API")
    app.state.settings = settings or Settings()
    app.state.jwt_verifier = SupabaseJWTVerifier(app.state.settings)
    app.state.import_gateway = SupabaseStagedImportGateway(app.state.settings)
    app.state.manual_account_gateway = TrustedManualAccountGateway(app.state.settings)
    app.state.financial_picture_gateway = TrustedSnapshotGateway(app.state.settings)
    app.state.market_analysis_gateway = TrustedMarketAnalysisGateway(app.state.settings)
    app.state.market_watchlist_gateway = TrustedMarketWatchlistGateway(
        app.state.settings
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[app.state.settings.web_origin],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Import-Filename",
        ],
    )
    app.include_router(health_router)
    app.include_router(identity_router)
    app.include_router(imports_router)
    app.include_router(accounts_router)
    app.include_router(financial_picture_router)
    app.include_router(market_router)
    return app


app = create_app()
