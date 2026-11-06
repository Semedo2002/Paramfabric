"""OpenID Connect bearer-token validation for enterprise identity providers.

Works with any standards-compliant OIDC provider (Okta, Microsoft Entra ID /
Azure AD, Google Workspace, Auth0, Keycloak, Ping): the API receives the
provider-issued **ID token or JWT access token** as `Authorization: Bearer`,
and this module validates it against the provider's published keys.

Validation
    * signing keys from the issuer's JWKS (`jwks_uri` from
      `<issuer>/.well-known/openid-configuration`, or set explicitly), cached
      and refreshed on unknown `kid` (key rotation);
    * asymmetric algorithms only (RS256/384/512, PS*, ES*; `none` and HMAC
      are rejected so a public key can never be used as an HMAC secret);
    * `iss` must equal the configured issuer, `aud` must contain the
      configured audience (client id or API identifier), `exp`/`nbf`/`iat`
      with a small clock-skew leeway.

Claim mapping (configurable)
    * subject: `sub` (or `PARAMFABRIC_OIDC_SUBJECT_CLAIM`, e.g. `email`);
    * roles: values of `PARAMFABRIC_OIDC_ROLES_CLAIM` (`groups` for Okta,
      `roles` for Entra app roles; dotted paths such as
      `realm_access.roles` work for Keycloak) mapped through
      `PARAMFABRIC_OIDC_ROLE_MAP` (JSON `{"idp-group": "engineer"}`);
      unmapped users get `PARAMFABRIC_OIDC_DEFAULT_ROLE` (default `viewer`);
    * organization: `PARAMFABRIC_OIDC_ORG_CLAIM` (`tid` for Entra, `hd`
      for Google Workspace, `org_id` for Okta/Auth0 organizations), mapped
      through `PARAMFABRIC_OIDC_ORG_MAP` or slugified; with no claim
      configured, `PARAMFABRIC_OIDC_ORGANIZATION` (default `default`).

Configuration: `PARAMFABRIC_OIDC_ISSUER` and `PARAMFABRIC_OIDC_AUDIENCE`
are required to enable OIDC; `PARAMFABRIC_OIDC_JWKS_URI` skips discovery.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import InvalidTokenError

from paramfabric.governance.tenancy import slugify
from paramfabric.security.auth import AuthenticationError, Principal

ASYMMETRIC_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]


@dataclass
class OIDCConfig:
    issuer: str
    audience: str
    jwks_uri: str | None = None
    roles_claim: str = "groups"
    role_map: dict[str, str] = field(default_factory=dict)
    default_role: str | None = "viewer"
    subject_claim: str = "sub"
    org_claim: str | None = None
    org_map: dict[str, str] = field(default_factory=dict)
    organization: str = "default"
    leeway: int = 60
    algorithms: list[str] = field(default_factory=lambda: list(ASYMMETRIC_ALGORITHMS))

    @classmethod
    def from_environment(cls) -> OIDCConfig | None:
        issuer = os.getenv("PARAMFABRIC_OIDC_ISSUER", "").strip()
        audience = os.getenv("PARAMFABRIC_OIDC_AUDIENCE", "").strip()
        if not issuer or not audience:
            return None
        return cls(
            issuer=issuer,  # compared exactly with the token's iss claim
            audience=audience,
            jwks_uri=os.getenv("PARAMFABRIC_OIDC_JWKS_URI") or None,
            roles_claim=os.getenv("PARAMFABRIC_OIDC_ROLES_CLAIM", "groups"),
            role_map=json.loads(os.getenv("PARAMFABRIC_OIDC_ROLE_MAP", "{}")),
            default_role=os.getenv("PARAMFABRIC_OIDC_DEFAULT_ROLE", "viewer") or None,
            subject_claim=os.getenv("PARAMFABRIC_OIDC_SUBJECT_CLAIM", "sub"),
            org_claim=os.getenv("PARAMFABRIC_OIDC_ORG_CLAIM") or None,
            org_map=json.loads(os.getenv("PARAMFABRIC_OIDC_ORG_MAP", "{}")),
            organization=os.getenv("PARAMFABRIC_OIDC_ORGANIZATION", "default"),
            leeway=int(os.getenv("PARAMFABRIC_OIDC_LEEWAY", "60")),
        )


def _fetch_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    request=urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "paramfabric-oidc"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured https issuer
        return json.loads(response.read().decode("utf-8"))


def _claim(claims: dict[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


class OIDCValidator:
    """Validates bearer tokens from one OIDC issuer and maps them to a :class:`Principal`"""

    def __init__(self, config: OIDCConfig, fetch: Any = _fetch_json, cache_seconds: float = 3600.0) -> None:
        self.config = config
        self._fetch = fetch
        self._cache_seconds = cache_seconds
        self._keys: dict[str, Any] = {}
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    # keys
    def jwks_uri(self) -> str:
        if self.config.jwks_uri:
            return self.config.jwks_uri
        discovery = self._fetch(self.config.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if discovery.get("issuer", "").rstrip("/") != self.config.issuer.rstrip("/"):
            raise AuthenticationError("OIDC discovery document issuer does not match the configured issuer")
        self.config.jwks_uri = discovery["jwks_uri"]
        return self.config.jwks_uri

    def _load_keys(self) -> None:
        document = self._fetch(self.jwks_uri())
        keys = {}
        for entry in document.get("keys", []):
            if entry.get("use", "sig") != "sig" or "kid" not in entry:
                continue
            try:
                keys[entry["kid"]] = jwt.PyJWK(entry).key
            except (jwt.PyJWKError, ValueError, KeyError):
                continue  # unsupported key types are ignored, not fatal
        self._keys = keys
        self._loaded_at = time.monotonic()

    def signing_key(self, kid: str) -> Any:
        with self._lock:
            stale=time.monotonic() - self._loaded_at > self._cache_seconds
            if stale or kid not in self._keys:
                self._load_keys()  # refresh on rotation or cache expiry
            if kid not in self._keys:
                raise AuthenticationError(f"no signing key with kid {kid!r} at the issuer")
            return self._keys[kid]

    # tokens
    def validate(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise AuthenticationError(f"malformed token: {exc}") from exc
        algorithm = header.get("alg")
        if algorithm not in self.config.algorithms:
            raise AuthenticationError(f"token algorithm {algorithm!r} is not allowed")
        if "kid" not in header:
            raise AuthenticationError("token header has no key id (kid)")
        key = self.signing_key(header["kid"])
        try:
            claims = jwt.decode(
                token, key, algorithms=[algorithm], audience=self.config.audience, issuer=self.config.issuer,
                leeway=self.config.leeway, options={"require": ["iss", "aud", "exp", "iat", "sub"]},
            )
        except InvalidTokenError as exc:
            raise AuthenticationError(f"invalid OIDC token: {exc}") from exc
        return self.principal(claims)

    def principal(self, claims: dict[str, Any]) -> Principal:
        config = self.config
        subject = _claim(claims, config.subject_claim) or claims["sub"]
        raw_roles = _claim(claims, config.roles_claim) or []
        if isinstance(raw_roles, str):
            raw_roles = raw_roles.split()
        roles = sorted({config.role_map[value] for value in raw_roles if value in config.role_map})
        if not roles and config.default_role:
            roles = [config.default_role]
        organization = config.organization
        if config.org_claim:
            value = _claim(claims, config.org_claim)
            if not value:
                raise AuthenticationError(f"token has no {config.org_claim!r} claim to identify the organization")
            organization = config.org_map.get(str(value), slugify(str(value)))
        return Principal(
            subject=str(subject), roles=roles, issuer=claims["iss"], organization=organization,
            email=claims.get("email") or claims.get("preferred_username"), name=claims.get("name"),
        )


def is_oidc_token(token: str) -> bool:
    try:
        return jwt.get_unverified_header(token).get("alg") in ASYMMETRIC_ALGORITHMS
    except InvalidTokenError:
        return False
