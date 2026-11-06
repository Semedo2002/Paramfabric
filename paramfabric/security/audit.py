"""Append-only hash-chained audit ledger authenticated with HMAC-SHA256"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

_LOCK = threading.Lock()


class AuditLedgerError(ValueError):
    """Raised when an audit ledger is malformed or has been modified"""


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@contextmanager
def _exclusive_file_lock(stream: Any) -> Generator[None, None, None]:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class AuditLedger:
    """Persist signed, linked audit records; any edit breaks verification"""

    def __init__(self, path: str | Path, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("audit key must contain at least 32 bytes")
        self.path = Path(path)
        self.key = key
        self.anchor_path = self.path.with_suffix(self.path.suffix + ".head")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            with lock_path.open("a+b") as lock_stream:
                with _exclusive_file_lock(lock_stream):
                    valid, previous = self.verify()
                    if not valid:
                        raise AuditLedgerError("audit ledger verification failed; refusing further writes")
                    record = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": event,
                        "previous_hash": previous,
                    }
                    digest = hashlib.sha256(_canonical(record)).hexdigest()
                    record["hash"] = digest
                    record["signature"] = hmac.new(
                        self.key, digest.encode("ascii"), hashlib.sha256
                    ).hexdigest()
                    with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                        stream.write(
                            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                            + "\n"
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    self._write_anchor(digest)
            return record

    def _write_anchor(self, head: str) -> None:
        signature = hmac.new(
            self.key, f"head:{head}".encode("ascii"), hashlib.sha256
        ).hexdigest()
        temporary = self.anchor_path.with_suffix(self.anchor_path.suffix + f".{os.getpid()}.tmp")
        with temporary.open("w", encoding="ascii", newline="\n") as stream:
            json.dump({"head": head, "signature": signature}, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.anchor_path)

    def verify(self) -> tuple[bool, str]:
        previous = "0" * 64
        try:
            with self.path.open(encoding="utf-8") as stream:
                for _, line in enumerate(stream, start=1):
                    record = json.loads(line)
                    digest = record.pop("hash")
                    signature=record.pop("signature")
                    if record.get("previous_hash") != previous:
                        return False, previous
                    expected_digest = hashlib.sha256(_canonical(record)).hexdigest()
                    expected_signature = hmac.new(
                        self.key, expected_digest.encode("ascii"), hashlib.sha256
                    ).hexdigest()
                    if not hmac.compare_digest(digest, expected_digest):
                        return False, previous
                    if not hmac.compare_digest(signature, expected_signature):
                        return False, previous
                    previous = digest
            if self.anchor_path.exists():
                anchor = json.loads(self.anchor_path.read_text(encoding="ascii"))
                anchor_head = anchor["head"]
                expected_anchor = hmac.new(
                    self.key, f"head:{anchor_head}".encode("ascii"), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(anchor_head, previous):
                    return False, previous
                if not hmac.compare_digest(anchor["signature"], expected_anchor):
                    return False, previous
            elif previous != "0" * 64:
                return False, previous
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return False, previous
        return True, previous

    def records(self) -> list[dict[str, Any]]:
        valid, _ = self.verify()
        if not valid:
            raise AuditLedgerError("audit ledger verification failed")
        with self.path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]
