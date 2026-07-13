"""Tests for Supabase JWKS access-token verification."""

import asyncio
import json
import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt import InvalidTokenError
from jwt.algorithms import ECAlgorithm

from pia_api.core.auth import SupabaseJWTVerifier
from pia_api.core.config import Settings


class _JWKSResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._body


class _JWKSClient:
    def __init__(self, body: dict[str, object], **_: object) -> None:
        self._body = body

    async def __aenter__(self) -> "_JWKSClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, _: str) -> _JWKSResponse:
        return _JWKSResponse(self._body)


def _make_token(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    audience: str = "authenticated",
    expires_at: int | None = None,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": "a9cf8ef7-40bc-46b4-9736-6b6a6340c4df",
            "email": "investor@example.test",
            "aud": audience,
            "iss": "http://supabase.test/auth/v1",
            "exp": expires_at if expires_at is not None else now + 300,
        },
        private_key,
        algorithm="ES256",
        headers={"kid": "local-test-key"},
    )


def _verifier_with_jwks(
    private_key: ec.EllipticCurvePrivateKey,
) -> tuple[SupabaseJWTVerifier, dict[str, object]]:
    public_jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "local-test-key", "alg": "ES256", "use": "sig"})
    return (
        SupabaseJWTVerifier(Settings(supabase_url="http://supabase.test")),
        {"keys": [public_jwk]},
    )


def test_verifier_accepts_a_valid_asymmetric_supabase_token() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    verifier, jwks = _verifier_with_jwks(private_key)

    with patch(
        "pia_api.core.auth.httpx.AsyncClient",
        lambda **kwargs: _JWKSClient(jwks, **kwargs),
    ):
        user = asyncio.run(verifier.verify(_make_token(private_key)))

    assert user.id == "a9cf8ef7-40bc-46b4-9736-6b6a6340c4df"
    assert user.email == "investor@example.test"


@pytest.mark.parametrize(
    ("audience", "expires_at"),
    [("another-audience", None), ("authenticated", 0)],
)
def test_verifier_rejects_invalid_audience_and_expired_tokens(
    audience: str, expires_at: int | None
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    verifier, jwks = _verifier_with_jwks(private_key)

    with (
        patch(
            "pia_api.core.auth.httpx.AsyncClient",
            lambda **kwargs: _JWKSClient(jwks, **kwargs),
        ),
        pytest.raises(InvalidTokenError),
    ):
        asyncio.run(
            verifier.verify(
                _make_token(private_key, audience=audience, expires_at=expires_at)
            )
        )
