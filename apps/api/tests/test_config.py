"""Tests for non-secret API settings."""

from pia_api.core.config import Settings


def test_settings_default_to_development(monkeypatch) -> None:
    monkeypatch.delenv("PIA_ENVIRONMENT", raising=False)

    assert Settings().environment == "development"


def test_settings_load_environment(monkeypatch) -> None:
    monkeypatch.setenv("PIA_ENVIRONMENT", "production")

    assert Settings().environment == "production"
