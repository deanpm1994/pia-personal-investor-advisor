"""Tests for non-secret API settings."""

from pia_api.core.config import Settings


def test_settings_default_to_development(monkeypatch) -> None:
    monkeypatch.delenv("PIA_ENVIRONMENT", raising=False)

    assert Settings().environment == "development"


def test_settings_load_environment(monkeypatch) -> None:
    monkeypatch.setenv("PIA_ENVIRONMENT", "production")

    assert Settings().environment == "production"


def test_settings_default_to_local_supabase_database(monkeypatch) -> None:
    monkeypatch.delenv("PIA_DATABASE_URL", raising=False)

    assert (
        Settings().database_url
        == "postgresql+psycopg://postgres:postgres@localhost:54322/postgres"
    )


def test_settings_load_database_url_from_server_environment(monkeypatch) -> None:
    database_url = "postgresql+psycopg://postgres:example@db.example.test:5432/postgres"
    monkeypatch.setenv("PIA_DATABASE_URL", database_url)

    assert Settings().database_url == database_url


def test_settings_derive_jwks_and_issuer_from_server_url(monkeypatch) -> None:
    monkeypatch.setenv("PIA_SUPABASE_URL", "https://project.example.test/")

    settings = Settings()

    assert (
        settings.supabase_jwks_url
        == "https://project.example.test/auth/v1/.well-known/jwks.json"
    )
    assert settings.supabase_jwt_issuer == "https://project.example.test/auth/v1"
