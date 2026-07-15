"""Supabase JWKS authentication dependency."""

from dataclasses import dataclass
from typing import Any, cast

import httpx
import jwt
from fastapi import Header, HTTPException, Request, status
from jwt import InvalidTokenError, PyJWKSet

from pia_api.core.config import Settings


@dataclass(frozen=True)
class AuthenticatedUser:
    """Identity claims accepted at the API boundary."""

    id: str
    email: str | None


class SupabaseJWTVerifier:
    """Verify Supabase-issued access tokens with the project's public JWKS."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def verify(self, token: str) -> AuthenticatedUser:
        """Verify signature and required identity claims before accepting a token."""
        try:
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            algorithm = header.get("alg")
            if not isinstance(key_id, str) or algorithm not in {"ES256", "RS256"}:
                raise InvalidTokenError("Unsupported token header")

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self._settings.supabase_jwks_url)
                response.raise_for_status()
            jwks = PyJWKSet.from_dict(cast(dict[str, Any], response.json()))
            key = next(
                (
                    candidate.key
                    for candidate in jwks.keys
                    if candidate.key_id == key_id
                ),
                None,
            )
            if key is None:
                raise InvalidTokenError("Unknown signing key")
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self._settings.supabase_jwt_audience,
                issuer=self._settings.supabase_jwt_issuer,
                options={"require": ["sub", "exp", "aud", "iss"]},
            )
            subject = claims.get("sub")
            if not isinstance(subject, str) or not subject:
                raise InvalidTokenError("Missing subject")
            email = claims.get("email")
            return AuthenticatedUser(
                id=subject, email=email if isinstance(email, str) else None
            )
        except (
            httpx.HTTPError,
            InvalidTokenError,
            jwt.PyJWTError,
            ValueError,
        ) as error:
            raise InvalidTokenError("Invalid Supabase access token") from error


async def get_authenticated_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    """Require a syntactically valid Bearer token at protected API routes."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    verifier = cast(SupabaseJWTVerifier, request.app.state.jwt_verifier)
    try:
        return await verifier.verify(token)
    except InvalidTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
