"""Asymmetric (Ed25519) signatures for compliance reports and approval locks.

Signing uses a **private key that never leaves the signer** (the Paramfabric
server or a quality workstation). Verification needs only the **public key**,
so auditors, customers, and CI jobs can check a report seal or a
`paramfabric.lock` without holding any secret and without write access to
the server.

* Keys are Ed25519 (RFC 8032): 32-byte keys, deterministic 64-byte
  signatures, no parameter choices to get wrong.
* A key id is the first 16 hex digits of SHA-256 over the raw public key, so
  a signature names the key that made it and verifiers can pick it out of a
  key set (rotation keeps old ids verifiable).
* Private keys are PKCS#8 PEM files, optionally encrypted with
  `PARAMFABRIC_SIGNING_KEY_PASSPHRASE`; public keys are SubjectPublicKeyInfo PEM.

Key resolution for the signer (first match):
`PARAMFABRIC_SIGNING_KEY` (PEM text or a path), then
`PARAMFABRIC_SIGNING_KEYS_DIR/<organization>.pem` (per-organization keys on a
multi-tenant server), then `<workspace>/.paramfabric/signing-key.pem`,
created on first use for the local desktop.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ALGORITHM = "Ed25519"
LOCAL_KEY_FILE = Path(".paramfabric") / "signing-key.pem"


class SigningError(ValueError):
    """Raised for missing, malformed, or unsupported keys"""


def key_id(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def public_pem(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")


def load_public_key(value: str | bytes | Path) -> Ed25519PublicKey:
    """A public key from PEM text/bytes or a file path"""
    data = _pem_bytes(value)
    try:
        key = serialization.load_pem_public_key(data)
    except ValueError as exc:
        raise SigningError(f"not a PEM public key: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SigningError("only Ed25519 public keys are supported")
    return key


def load_public_keys(value: str | bytes | Path) -> list[Ed25519PublicKey]:
    """Every public key in a PEM bundle (key rotation: old and new keys in one file)"""
    text=_pem_bytes(value).decode("ascii", errors="replace")
    blocks=[block for block in text.split("-----END PUBLIC KEY-----") if "-----BEGIN PUBLIC KEY-----" in block]
    if not blocks:
        raise SigningError("no PEM public keys found")
    return [load_public_key(block[block.index("-----BEGIN"):] + "-----END PUBLIC KEY-----\n") for block in blocks]


def _pem_bytes(value: str | bytes | Path) -> bytes:
    if isinstance(value, bytes):
        return value
    text = str(value)
    if "-----BEGIN" in text:
        return text.encode("ascii")
    path = Path(text)
    if not path.is_file():
        raise SigningError(f"key file not found: {path}")
    return path.read_bytes()


@dataclass
class Signer:
    """A private signing key and its public identity"""

    private_key: Ed25519PrivateKey
    source: str = "memory"

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    @property
    def key_id(self) -> str:
        return key_id(self.public_key)

    @property
    def public_pem(self) -> str:
        return public_pem(self.public_key)

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self.private_key.sign(message)).decode("ascii")

    @classmethod
    def generate(cls) -> Signer:
        return cls(Ed25519PrivateKey.generate(), "generated")

    @classmethod
    def from_pem(cls, value: str | bytes | Path, passphrase: str | None = None) -> Signer:
        data = _pem_bytes(value)
        try:
            key = serialization.load_pem_private_key(data, password=passphrase.encode() if passphrase else None)
        except (ValueError, TypeError) as exc:
            raise SigningError(f"cannot load the private signing key: {exc}") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise SigningError("only Ed25519 private keys are supported (generate one with `paramfabric keys generate`)")
        return cls(key, str(value) if not isinstance(value, bytes) and "-----BEGIN" not in str(value) else "pem")

    def private_pem(self, passphrase: str | None = None) -> bytes:
        encryption = (serialization.BestAvailableEncryption(passphrase.encode()) if passphrase else serialization.NoEncryption())
        return self.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)

    def save(self, path: Path, passphrase: str | None = None) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.private_pem(passphrase))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows ACLs: the file stays in the user's profile/workspace
        path.with_suffix(".pub.pem").write_text(self.public_pem, encoding="ascii")


def verify(public_keys: list[Ed25519PublicKey], message: bytes, signature: str, expected_key_id: str | None = None) -> str | None:
    try:
        raw = base64.b64decode(signature, validate=True)
    except (ValueError, TypeError):
        return None
    for public_key in public_keys:
        identifier = key_id(public_key)
        if expected_key_id and identifier != expected_key_id:
            continue
        try:
            public_key.verify(raw, message)
            return identifier
        except InvalidSignature:
            continue
    return None


def resolve_signer(root: Path | None = None, organization: str = "default", *, create_local: bool = True) -> Signer:
    """The signing key for `organization` (see the module docstring for the lookup order)"""
    passphrase = os.getenv("PARAMFABRIC_SIGNING_KEY_PASSPHRASE") or None
    explicit = os.getenv("PARAMFABRIC_SIGNING_KEY")
    if explicit:
        return Signer.from_pem(explicit, passphrase)
    directory = os.getenv("PARAMFABRIC_SIGNING_KEYS_DIR")
    if directory:
        candidate = Path(directory) / f"{organization}.pem"
        if candidate.is_file():
            return Signer.from_pem(candidate, passphrase)
    if root is not None:
        local = Path(root) / LOCAL_KEY_FILE
        if local.is_file():
            return Signer.from_pem(local, passphrase)
        if create_local:
            signer = Signer.generate()
            signer.save(local, passphrase)
            signer.source = str(local)
            return signer
    raise SigningError("no signing key: set PARAMFABRIC_SIGNING_KEY or PARAMFABRIC_SIGNING_KEYS_DIR")
