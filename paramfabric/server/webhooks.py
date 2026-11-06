"""Asynchronous webhook delivery for platform events.

Configure one or more endpoints with `PARAMFABRIC_WEBHOOK_URL`
(comma-separated). Events are queued in memory and delivered by a background
thread, so request handlers and solver tasks never wait on the network.

Events
    `parameter.merged`      a reviewed change request was merged
    `simulation.completed`  a mesh or solve task finished
    `simulation.failed`     a mesh or solve task failed
    `drift.detected`        source code disagrees with the fabric (new drift only)

Envelope (JSON, `Content-Type: application/json`)::

    {"id": "evt_…", "type": "simulation.completed", "created_at": "…Z",
     "organization": "acme", "project": "wing", "data": {…}}

Security and delivery
    * `X-Paramfabric-Signature: t=<unix time>,v1=<hex>` where `v1` is
      HMAC-SHA256 over `"<t>.<raw body>"` with `PARAMFABRIC_WEBHOOK_SECRET`.
      Receivers recompute it and reject stale timestamps (replay protection).
    * `X-Paramfabric-Event` and `X-Paramfabric-Delivery` (event id) headers;
      the id is stable across retries, so receivers can deduplicate.
    * Up to `PARAMFABRIC_WEBHOOK_ATTEMPTS` (default 5) attempts with
      exponential backoff on network errors, 5xx, and 429; other 4xx responses
      are not retried. Undelivered events are kept in :attr:`dead_letters`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from paramfabric.governance.tenancy import current_tenant

EVENTS = ("parameter.merged", "simulation.completed", "simulation.failed", "drift.detected")


@dataclass
class Delivery:
    event: dict[str, Any]
    url: str
    attempts: int = 0
    status: int | None = None
    error: str | None = None
    delivered: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, header: str, body: bytes, tolerance: int = 300, now: float | None = None) -> bool:
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now or time.time()) - timestamp) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), parts.get("v1", ""))


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-configured URL
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class WebhookDispatcher:
    """Queue events and deliver them from a daemon thread with retries"""

    def __init__(self, urls: list[str], secret: str | None = None, *, attempts: int = 5, backoff: float = 1.0,
                 timeout: float = 10.0, transport: Callable[[str, bytes, dict[str, str], float], int] = _post,
                 events: tuple[str, ...] = EVENTS) -> None:
        self.urls = [url for url in urls if url]
        self.secret = secret
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self.timeout = timeout
        self.transport = transport
        self.events = set(events)
        self.deliveries: deque[Delivery] = deque(maxlen=500)
        self.dead_letters: deque[Delivery] = deque(maxlen=500)
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> WebhookDispatcher | None:
        urls=[url.strip() for url in os.getenv("PARAMFABRIC_WEBHOOK_URL", "").split(",") if url.strip()]
        if not urls:
            return None
        return cls(urls, os.getenv("PARAMFABRIC_WEBHOOK_SECRET") or None,
                   attempts=int(os.getenv("PARAMFABRIC_WEBHOOK_ATTEMPTS", "5")),
                   timeout=float(os.getenv("PARAMFABRIC_WEBHOOK_TIMEOUT", "10")))

    @property
    def enabled(self) -> bool:
        return bool(self.urls)

    def emit(self, event_type: str, data: dict[str, Any], *, organization: str | None = None, project: str | None = None) -> dict[str, Any] | None:
        """Queue an event (returns the envelope immediately; delivery is asynchronous)"""
        if event_type not in self.events or not self.urls:
            return None
        tenant = current_tenant()
        envelope = {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": event_type,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "organization": organization or tenant.organization,
            "project": project or tenant.project,
            "data": data,
        }
        self._ensure_worker()
        self._queue.put(envelope)
        return envelope

    def flush(self, timeout: float = 30.0) -> bool:
        deadline=time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    def close(self) -> None:
        if self._thread and self._thread.is_alive():
            self._queue.put(None)
            self._thread.join(timeout=5)

    # delivery
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="paramfabric-webhooks", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            envelope = self._queue.get()
            try:
                if envelope is None:
                    return
                for url in self.urls:
                    self._deliver(envelope, url)
            finally:
                self._queue.task_done()

    def _deliver(self, envelope: dict[str, Any], url: str) -> Delivery:
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
        delivery = Delivery(envelope, url)
        for attempt in range(1, self.attempts + 1):
            timestamp = int(time.time())
            headers={
                "Content-Type": "application/json", "User-Agent": "Paramfabric-Webhooks/0.5",
                "X-Paramfabric-Event": envelope["type"], "X-Paramfabric-Delivery": envelope["id"],
                "X-Paramfabric-Attempt": str(attempt),
            }
            if self.secret:
                headers["X-Paramfabric-Signature"] = f"t={timestamp},v1={sign(self.secret, timestamp, body)}"
            delivery.attempts = attempt
            try:
                status = self.transport(url, body, headers, self.timeout)
                delivery.status, delivery.error = status, None
            except (OSError, ValueError) as exc:  # connection refused, DNS, timeout
                status, delivery.error = None, str(exc)
            delivery.history.append({"attempt": attempt, "status": status, "error": delivery.error})
            if status is not None and 200 <= status < 300:
                delivery.delivered = True
                break
            if status is not None and 400 <= status < 500 and status != 429:
                break  # the receiver rejected the event: retrying will not help
            if attempt < self.attempts:
                time.sleep(self.backoff * 2 ** (attempt - 1))
        self.deliveries.append(delivery)
        if not delivery.delivered:
            self.dead_letters.append(delivery)
        return delivery


_DISPATCHER: list[WebhookDispatcher | None] = []


def dispatcher() -> WebhookDispatcher | None:
    if not _DISPATCHER:
        _DISPATCHER.append(WebhookDispatcher.from_environment())
    return _DISPATCHER[0]


def configure(instance: WebhookDispatcher | None) -> None:
    _DISPATCHER[:] = [instance]


def emit(event_type: str, data: dict[str, Any], **tenant: str | None) -> dict[str, Any] | None:
    instance = dispatcher()
    return instance.emit(event_type, data, **tenant) if instance else None
