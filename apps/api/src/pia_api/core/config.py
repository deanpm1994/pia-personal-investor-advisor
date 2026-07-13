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
