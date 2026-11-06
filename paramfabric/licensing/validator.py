"""Air-gapped RSA-signed enterprise license validation"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import jwt
from jwt import InvalidTokenError
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class LicenseError(ValueError):
    """Raised for invalid, expired, or insufficient licenses"""


class LicenseClaims(BaseModel):
    """Signed commercial license payload"""

    model_config=ConfigDict(extra="forbid")
    license_id: str = Field(min_length=1)
    seats: int = Field(gt=0)
    features: list[str] = Field(default_factory=list)
    exp: int
    iat: int
    iss: str = "paramfabric-licensing"


def verify_license(
    token: str,
    public_key_pem: str | bytes,
    *,
    required_feature: str | None = None,
    required_seats: int = 1,
    now: datetime | None = None,
) -> LicenseClaims:
    """Verify an RS256-signed compact license entirely offline"""
    try:
        public_key = load_pem_public_key(
            public_key_pem.encode("utf-8") if isinstance(public_key_pem, str) else public_key_pem
        )
        if not isinstance(public_key, RSAPublicKey) or public_key.key_size < 2048:
            raise LicenseError("license verification requires an RSA public key of at least 2048 bits")
        payload: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            issuer="paramfabric-licensing",
            options={"require": ["license_id", "seats", "features", "exp", "iat", "iss"]},
            leeway=0,
        )
        claims = LicenseClaims.model_validate(payload)
    except (InvalidTokenError, ValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, LicenseError):
            raise
        raise LicenseError(f"license validation failed: {exc}") from exc
    current = now or datetime.now(timezone.utc)
    if claims.exp <= int(current.timestamp()):
        raise LicenseError("license has expired")
    if claims.seats < required_seats:
        raise LicenseError(f"license provides {claims.seats} seat(s), {required_seats} required")
    if required_feature and required_feature not in claims.features:
        raise LicenseError(f"license does not include feature {required_feature!r}")
    return claims
