from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from paramfabric.licensing.validator import LicenseError, verify_license


def keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def test_air_gapped_rsa_license_verification():
    private_key, public_key = keys()
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "license_id": "test-license",
            "seats": 8,
            "features": ["studio", "governance"],
            "iss": "paramfabric-licensing",
            "iat": now,
            "exp": now + timedelta(days=30),
        },
        private_key,
        algorithm="RS256",
    )
    claims = verify_license(token, public_key, required_feature="studio", required_seats=4)
    assert claims.license_id == "test-license"
    with pytest.raises(LicenseError, match="does not include"):
        verify_license(token, public_key, required_feature="cad-enterprise")


def test_expired_license_is_rejected():
    private_key, public_key = keys()
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "license_id": "expired",
            "seats": 1,
            "features": [],
            "iss": "paramfabric-licensing",
            "iat": now - timedelta(days=2),
            "exp": now - timedelta(days=1),
        },
        private_key,
        algorithm="RS256",
    )
    with pytest.raises(LicenseError, match="expired"):
        verify_license(token, public_key)


def test_license_feature_does_not_get_inferred_from_another_capability():
    private_key, public_key = keys()
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "license_id": "governance-only",
            "seats": 3,
            "features": ["governance"],
            "iss": "paramfabric-licensing",
            "iat": now,
            "exp": now + timedelta(days=5),
        },
        private_key,
        algorithm="RS256",
    )
    claims = verify_license(token, public_key)
    assert "studio" not in claims.features


def test_license_verifier_rejects_rsa_keys_smaller_than_2048_bits():
    private_key=rsa.generate_private_key(public_exponent=65537, key_size=1024)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with pytest.raises(LicenseError, match="at least 2048"):
        verify_license("not-a-token", public_key)
