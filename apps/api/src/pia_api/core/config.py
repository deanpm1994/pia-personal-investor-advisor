"""Non-secret runtime configuration."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    """Settings loaded only from explicit non-secret environment variables."""

    model_config = SettingsConfigDict(env_prefix="PIA_", extra="ignore")

    environment: Environment = "development"
