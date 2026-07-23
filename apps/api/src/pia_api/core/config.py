"""Server-side runtime configuration."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    """Settings loaded only by Python processes from explicit environment variables."""

    model_config = SettingsConfigDict(env_prefix="PIA_", extra="ignore")

    environment: Environment = "development"
    database_url: str = (
        "postgresql+psycopg://postgres:postgres@localhost:54322/postgres"
    )
    supabase_url: str = "http://localhost:54321"
    supabase_anon_key: str = ""
    supabase_jwt_audience: str = "authenticated"
    web_origin: str = "http://localhost:3000"

    @property
    def supabase_jwks_url(self) -> str:
        """Return the server-side JWKS discovery URL for the configured project."""
        return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def supabase_jwt_issuer(self) -> str:
        """Return the only accepted issuer for Supabase access tokens."""
        return f"{self.supabase_url.rstrip('/')}/auth/v1"
