"""JWT issuance and verification for trusted identity providers"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from jwt import InvalidTokenError
from pydantic import BaseModel, ConfigDict, Field


class Principal(BaseModel):
    """Authenticated identity and role claims"""

    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)
    issuer: str
    organization: str = "default"
    email: str | None = None
    name: str | None = None


class AuthenticationError(ValueError):
    """Raised when a token cannot be trusted"""


def issue_access_token(
    subject: str,
    roles: list[str],
    secret: str,
    *,
    issuer: str = "paramfabric",
    audience: str = "paramfabric-api",
    lifetime: timedelta = timedelta(minutes=30),
    organization: str = "default",
) -> str:
    if len(secret.encode("utf-8")) < 32:
        raise ValueError("JWT signing secret must contain at least 32 bytes")
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": subject,
            "roles": roles,
            "org": organization,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "nbf": now,
            "exp": now + lifetime,
        },
        secret,
        algorithm="HS256",
    )


def decode_access_token(
    token: str,
    secret: str,
    *,
    issuer: str = "paramfabric",
    audience: str = "paramfabric-api",
) -> Principal:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=issuer,
            audience=audience,
            options={"require": ["sub", "iss", "aud", "exp", "iat", "nbf"]},
        )
        return Principal(subject=claims["sub"], roles=claims.get("roles", []), issuer=claims["iss"],
                         organization=claims.get("org", "default"))
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError(f"invalid access token: {exc}") from exc
