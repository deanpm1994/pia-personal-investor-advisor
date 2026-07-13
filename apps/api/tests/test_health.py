"""Tests for the public health endpoint."""

from fastapi.testclient import TestClient

from pia_api.core.auth import AuthenticatedUser
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


class AcceptedTokenVerifier:
    async def verify(self, token: str) -> AuthenticatedUser:
        if token == "valid-token":
            return AuthenticatedUser(id="test-user", email="user@example.test")
        from jwt import InvalidTokenError

        raise InvalidTokenError("invalid")


def test_identity_requires_a_valid_bearer_token() -> None:
    app = create_app()
    app.state.jwt_verifier = AcceptedTokenVerifier()
    client = TestClient(app)

    assert client.get("/health").status_code == 200
    assert client.get("/v1/identity").status_code == 401
    assert (
        client.get(
            "/v1/identity", headers={"Authorization": "Bearer malformed"}
        ).status_code
        == 401
    )
    response = client.get(
        "/v1/identity", headers={"Authorization": "Bearer valid-token"}
    )
    assert response.status_code == 200
    assert response.json() == {"id": "test-user", "email": "user@example.test"}
