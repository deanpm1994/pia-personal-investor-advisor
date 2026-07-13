"""Tests for the public health endpoint."""

from fastapi.testclient import TestClient

from pia_api.core.config import Settings
from pia_api.main import create_app


def test_health_returns_default_contract(monkeypatch) -> None:
    monkeypatch.delenv("PIA_ENVIRONMENT", raising=False)

    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "development"}


def test_health_returns_configured_environment(monkeypatch) -> None:
    monkeypatch.setenv("PIA_ENVIRONMENT", "test")

    response = TestClient(create_app(Settings())).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test"}
