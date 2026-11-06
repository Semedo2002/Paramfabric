"""Signed approval records: the bridge between server-side review and repository CI.

When a change request is merged, the server issues an approval record (the
merged fabric's content hash, the approvers, the request id) **signed with the
organization's Ed25519 private key**. Committed to the repository as
`paramfabric.lock`, it lets `paramfabric ci-check` prove that the
`params.yaml` in a pull request is exactly a reviewed and merged state using
only the organization's **public key** (`PARAMFABRIC_APPROVAL_PUBLIC_KEY`):
CI holds no secret that could forge an approval.

Records from v0.5 (`hmac-sha256:` signatures) still verify when the legacy
shared secret is supplied.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paramfabric.security.signing import ALGORITHM, Signer, verify

LOCK_FILE = "paramfabric.lock"
_UNSIGNED = {"signature"}


def _canonical(record: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in record.items() if key not in _UNSIGNED}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_approval(payload: dict[str, Any], signer: Signer) -> dict[str, Any]:
    record = {
        "kind": "paramfabric.approval", "version": 2, "issued_at": datetime.now(timezone.utc).isoformat(),
        "algorithm": ALGORITHM, "key_id": signer.key_id, **payload,
    }
    record["signature"] = signer.sign(_canonical(record))
    return record


def verify_approval(record: dict[str, Any], public_keys: list[Any] | None = None, *, legacy_secret: str | None = None) -> bool:
    """True if `record` is signed by one of `public_keys` (or, for v1 records, by the legacy HMAC secret)"""
    signature = str(record.get("signature", ""))
    if signature.startswith("hmac-sha256:"):
        if not legacy_secret:
            return False
        expected = hmac.new(legacy_secret.encode("utf-8"), _canonical(record), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature.split(":", 1)[1], expected)
    if record.get("algorithm") != ALGORITHM or not public_keys:
        return False
    return verify(public_keys, _canonical(record), signature, record.get("key_id")) is not None


def load_lock(path: Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def write_lock(path: Path, record: dict[str, Any], keep_history: int = 20) -> None:
    existing: list[dict[str, Any]] = []
    if Path(path).is_file():
        try:
            existing = load_lock(path)
        except (ValueError, OSError):
            existing = []
    records=[record, *[item for item in existing if item.get("fabric_hash") != record.get("fabric_hash")]][:keep_history]
    Path(path).write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
