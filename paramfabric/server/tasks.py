"""Background task queue for CAD regeneration, meshing, and solver runs.

Two interchangeable backends share one record format and API:

`LocalTaskQueue`  thread pool inside the API process (desktop and single-node servers)
`RedisTaskQueue`  Redis-backed queue drained by `paramfabric worker` processes on
                    remote compute nodes; task state, residual streams, and logs live in
                    Redis

Artifacts (VTK results, meshes, OpenFOAM case archives, viewport payloads) are
written to the task's local working directory and published through an
:mod:`paramfabric.server.storage` backend when the task ends: local disk by
default, or an S3-compatible bucket (`PARAMFABRIC_S3_ENDPOINT`) so workers
and API servers need no shared filesystem.

Handlers are plain functions `handler(payload, context) -> result` registered
by task kind. The context streams progress and log lines, exposes a
cancellation flag, and owns the task's artifact directory.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from paramfabric.server.storage import LocalStorage, ObjectStorage, StorageError, storage_from_environment

TERMINAL = {"completed", "failed", "cancelled"}
_LOG_LIMIT = 5000
Handler = Callable[[dict[str, Any], "TaskContext"], dict[str, Any]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(function: Handler) -> Handler:
        HANDLERS[kind] = function
        return function

    return register


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskCancelled(RuntimeError):
    """Raised inside a handler when cancellation was requested"""


@dataclass
class TaskRecord:
    id: str
    kind: str
    owner: str
    payload: dict[str, Any]
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    worker: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)
    organization: str = "default"
    project: str = "default"

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("payload")
        return data


class TaskStore(Protocol):
    """Storage contract shared by both backends"""

    def save(self, record: TaskRecord) -> None: ...
    def load(self, task_id: str) -> TaskRecord | None: ...
    def all(self) -> list[TaskRecord]: ...
    def append_progress(self, task_id: str, entry: dict[str, Any]) -> None: ...
    def append_log(self, task_id: str, line: str) -> None: ...
    def progress(self, task_id: str, offset: int) -> tuple[list[dict[str, Any]], int]: ...
    def log(self, task_id: str, offset: int) -> tuple[list[str], int]: ...
    def request_cancel(self, task_id: str) -> None: ...
    def cancel_requested(self, task_id: str) -> bool: ...


class TaskContext:
    """What a running handler sees"""

    def __init__(self, store: TaskStore, record: TaskRecord, artifact_dir: Path, storage: ObjectStorage | None = None) -> None:
        self.store = store
        self.record = record
        self.artifact_dir = artifact_dir
        self.storage = storage  # where synced workspaces come from (see workspace_sync)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self._last_cancel_check = 0.0
        self._cancelled = False

    def progress(self, iteration: int, fields: dict[str, float]) -> None:
        self.store.append_progress(self.record.id, {"iteration": int(iteration), "fields": {k: float(v) for k, v in fields.items()}})
        self.check()

    def log(self, line: str) -> None:
        for part in str(line).rstrip().splitlines() or [""]:
            self.store.append_log(self.record.id, part)

    @property
    def cancelled(self) -> bool:
        now = time.monotonic()
        if not self._cancelled and now - self._last_cancel_check > 0.2:
            self._last_cancel_check = now
            self._cancelled = self.store.cancel_requested(self.record.id)
        return self._cancelled

    def check(self) -> None:
        if self.cancelled:
            raise TaskCancelled("task cancelled")

    def artifact(self, name: str) -> Path:
        safe = Path(name).name
        if safe not in self.record.artifacts:
            self.record.artifacts.append(safe)
        return self.artifact_dir / safe


class CancelEvent:
    """`threading.Event`-compatible view of a task's cancellation flag"""

    def __init__(self, context: TaskContext) -> None:
        self.context = context

    def is_set(self) -> bool:
        return self.context.cancelled


def artifact_key(task_id: str, name: str) -> str:
    return f"{task_id}/{name}"  # LocalStorage at the artifact root: objects are the files themselves


def publish_artifacts(storage: ObjectStorage, record: TaskRecord, directory: Path) -> list[str]:
    failed = []
    for name in record.artifacts:
        path = directory / name
        if not path.is_file():
            continue
        try:
            storage.put_file(artifact_key(record.id, name), path)
        except Exception:  # noqa: BLE001 - reported in the task log, the result stays valid
            failed.append(name)
    return failed


FINISH_LISTENERS: list[Callable[[TaskRecord], None]] = []
SIMULATION_KINDS = {"mesh", "solve"}


def on_finish(listener: Callable[[TaskRecord], None]) -> Callable[[TaskRecord], None]:
    FINISH_LISTENERS.append(listener)
    return listener


def _persist_progress(store: TaskStore, record: TaskRecord, context: TaskContext) -> None:
    progress, _ = store.progress(record.id, 0)
    if progress:
        context.artifact("progress.json").write_text(json.dumps(progress), encoding="utf-8")


def _persist_log(store: TaskStore, record: TaskRecord, context: TaskContext) -> None:
    if record.kind in SIMULATION_KINDS:
        lines, _ = store.log(record.id, 0)
        context.artifact("log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _emit_simulation_event(record: TaskRecord) -> None:
    if record.kind not in SIMULATION_KINDS or record.status not in {"completed", "failed"}:
        return
    from paramfabric.server import webhooks

    result = record.result or {}
    webhooks.emit(
        f"simulation.{record.status}",
        {
            "task_id": record.id, "kind": record.kind, "owner": record.owner, "worker": record.worker,
            "analysis": record.payload.get("analysis"), "engine": result.get("engine"),
            "converged": result.get("converged"), "iterations": result.get("iterations"),
            "summary": {k: v for k, v in (result.get("summary") or {}).items() if isinstance(v, (int, float, str, bool)) or v is None},
            "error": record.error, "artifacts": list(record.artifacts),
            "started_at": record.started_at, "finished_at": record.finished_at,
        },
        organization=record.organization, project=record.project,
    )


def run_task(store: TaskStore, record: TaskRecord, artifact_root: Path, worker: str, storage: ObjectStorage | None = None) -> None:
    """Execute one task to a terminal state (shared by both backends)"""
    if store.cancel_requested(record.id):
        record.status, record.finished_at = "cancelled", _now()
        store.save(record)
        return
    record.status, record.started_at, record.worker = "running", _now(), worker
    store.save(record)
    context = TaskContext(store, record, artifact_root / record.id, storage)
    try:
        function = HANDLERS.get(record.kind)
        if function is None:
            raise ValueError(f"no handler registered for task kind {record.kind!r}")
        record.result = function(record.payload, context)
        record.status = "completed"
        context.log("Task completed.")
    except TaskCancelled:
        record.status = "cancelled"
        context.log("Task cancelled.")
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as task error
        record.status = "failed"
        record.error = str(exc) or type(exc).__name__
        context.log(f"ERROR: {record.error}")
        context.log(traceback.format_exc(limit=4))
    finally:
        try:
            _persist_progress(store, record, context)
            _persist_log(store, record, context)
        except OSError:
            pass
        if storage is not None:
            failed=publish_artifacts(storage, record, context.artifact_dir)
            if failed:
                context.log(f"WARNING: could not upload artifact(s) {', '.join(failed)} to {storage.describe()['kind']} storage")
        record.finished_at = _now()
        store.save(record)
        _emit_simulation_event(record)
        for listener in FINISH_LISTENERS:
            try:
                listener(record)
            except Exception:  # noqa: BLE001 - observers must never fail a task
                pass


class MemoryStore:
    """Thread-safe in-process task store"""

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._progress: dict[str, list[dict[str, Any]]] = {}
        self._logs: dict[str, list[str]] = {}
        self._cancel: set[str] = set()
        self._lock = threading.Lock()

    def save(self, record: TaskRecord) -> None:
        with self._lock:
            self._records[record.id] = record

    def load(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    def all(self) -> list[TaskRecord]:
        return list(self._records.values())

    def append_progress(self, task_id: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self._progress.setdefault(task_id, []).append(entry)

    def append_log(self, task_id: str, line: str) -> None:
        with self._lock:
            log = self._logs.setdefault(task_id, [])
            log.append(line)
            if len(log) > _LOG_LIMIT:
                del log[: len(log) - _LOG_LIMIT]

    def progress(self, task_id: str, offset: int) -> tuple[list[dict[str, Any]], int]:
        entries = self._progress.get(task_id, [])
        return entries[offset:], len(entries)

    def log(self, task_id: str, offset: int) -> tuple[list[str], int]:
        entries = self._logs.get(task_id, [])
        return entries[offset:], len(entries)

    def request_cancel(self, task_id: str) -> None:
        self._cancel.add(task_id)

    def cancel_requested(self, task_id: str) -> bool:
        return task_id in self._cancel


class TaskQueue:
    """Common front-end: submission, status snapshots, cancellation, artifacts"""

    backend = "base"

    def __init__(self, store: TaskStore, artifact_root: Path, storage: ObjectStorage | None = None) -> None:
        self.store = store
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.storage: ObjectStorage = storage if storage is not None else LocalStorage(self.artifact_root)

    def submit(self, kind: str, payload: dict[str, Any], owner: str, *, organization: str = "default", project: str = "default") -> TaskRecord:
        if kind not in HANDLERS:
            raise ValueError(f"unknown task kind {kind!r}")
        json.dumps(payload)  # payloads must be JSON-serializable to cross process boundaries
        record = TaskRecord(id=uuid.uuid4().hex[:16], kind=kind, owner=owner, payload=payload,
                            organization=organization, project=project)
        self.store.save(record)
        self._dispatch(record)
        return record

    def _dispatch(self, record: TaskRecord) -> None:
        raise NotImplementedError

    @property
    def remote_storage(self) -> bool:
        mode = os.environ.get("PARAMFABRIC_WORKSPACE_SYNC", "auto").lower()
        if mode in {"always", "never"}:
            return mode == "always"
        return not (isinstance(self.storage, LocalStorage) and self.storage.root.resolve() == self.artifact_root.resolve())

    def get(self, task_id: str) -> TaskRecord:
        record = self.store.load(task_id)
        if record is None:
            raise KeyError(f"unknown task {task_id!r}")
        return record

    def list(self, owner: str | None = None, *, organization: str | None = None, project: str | None = None) -> list[TaskRecord]:
        records=[
            record for record in self.store.all()
            if (owner is None or record.owner == owner)
            and (organization is None or record.organization == organization)
            and (project is None or record.project == project)
        ]
        return sorted(records, key=lambda record: record.created_at, reverse=True)

    def active(self, kinds: set[str] | None = None) -> list[TaskRecord]:
        return [r for r in self.store.all() if r.status not in TERMINAL and (kinds is None or r.kind in kinds)]

    def cancel(self, task_id: str) -> TaskRecord:
        record = self.get(task_id)
        if record.status not in TERMINAL:
            self.store.request_cancel(task_id)
        return record

    def snapshot(self, task_id: str, progress_offset: int = 0, log_offset: int = 0, include_result: bool = True) -> dict[str, Any]:
        record = self.get(task_id)
        progress, progress_count = self.store.progress(task_id, max(0, progress_offset))
        log, log_count = self.store.log(task_id, max(0, log_offset))
        data = record.public()
        if not include_result or record.status != "completed":
            data["result"] = None
        data.update(progress=progress, progress_count=progress_count, log=log, log_count=log_count, backend=self.backend)
        return data

    def artifact_path(self, task_id: str, name: str) -> Path:
        record = self.get(task_id)
        safe = Path(name).name
        if safe not in record.artifacts:
            raise KeyError(f"task {task_id} has no artifact {name!r}")
        path = (self.artifact_root / task_id / safe).resolve()
        if not path.is_relative_to(self.artifact_root.resolve()):
            raise KeyError(f"artifact {name!r} is not available")
        if not path.is_file():
            # produced on another node: fetch it from object storage into the local cache
            try:
                self.storage.fetch(artifact_key(task_id, safe), path)
            except StorageError as exc:
                raise KeyError(f"artifact {name!r} is not available") from exc
        return path

    def purge(self, task_id: str) -> None:
        shutil.rmtree(self.artifact_root / task_id, ignore_errors=True)
        self.storage.delete_prefix(task_id)


class LocalTaskQueue(TaskQueue):
    """Runs tasks on an in-process thread pool"""

    backend = "local"

    def __init__(self, artifact_root: Path, workers: int = 2, storage: ObjectStorage | None = None) -> None:
        super().__init__(MemoryStore(), artifact_root, storage)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="paramfabric-task")

    def _dispatch(self, record: TaskRecord) -> None:
        self.executor.submit(run_task, self.store, record, self.artifact_root, "local", self.storage)


class RedisStore:
    """Task state in Redis: one JSON hash field per record plus progress/log lists"""

    def __init__(self, client: Any, prefix: str = "paramfabric") -> None:
        self.r = client
        self.prefix = prefix

    def _key(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts))

    def save(self, record: TaskRecord) -> None:
        self.r.hset(self._key("tasks"), record.id, json.dumps(asdict(record)))

    def load(self, task_id: str) -> TaskRecord | None:
        raw = self.r.hget(self._key("tasks"), task_id)
        return TaskRecord(**json.loads(raw)) if raw else None

    def all(self) -> list[TaskRecord]:
        return [TaskRecord(**json.loads(raw)) for raw in self.r.hgetall(self._key("tasks")).values()]

    def append_progress(self, task_id: str, entry: dict[str, Any]) -> None:
        self.r.rpush(self._key("progress", task_id), json.dumps(entry))

    def append_log(self, task_id: str, line: str) -> None:
        key = self._key("log", task_id)
        self.r.rpush(key, line)
        self.r.ltrim(key, -_LOG_LIMIT, -1)

    def progress(self, task_id: str, offset: int) -> tuple[list[dict[str, Any]], int]:
        key = self._key("progress", task_id)
        return [json.loads(item) for item in self.r.lrange(key, offset, -1)], int(self.r.llen(key))

    def log(self, task_id: str, offset: int) -> tuple[list[str], int]:
        key = self._key("log", task_id)
        return [item if isinstance(item, str) else item.decode() for item in self.r.lrange(key, offset, -1)], int(self.r.llen(key))

    def request_cancel(self, task_id: str) -> None:
        self.r.sadd(self._key("cancel"), task_id)

    def cancel_requested(self, task_id: str) -> bool:
        return bool(self.r.sismember(self._key("cancel"), task_id))


class RedisTaskQueue(TaskQueue):
    """Enqueues task ids in Redis for remote `paramfabric worker` processes"""

    backend = "redis"

    def __init__(self, client: Any, artifact_root: Path, prefix: str = "paramfabric", storage: ObjectStorage | None = None) -> None:
        super().__init__(RedisStore(client, prefix), artifact_root, storage)
        self.client = client
        self.queue_key = f"{prefix}:queue"

    def _dispatch(self, record: TaskRecord) -> None:
        self.client.lpush(self.queue_key, record.id)

    def work(self, worker: str, *, once: bool = False, timeout: int = 5, stop: threading.Event | None = None) -> int:
        executed = 0
        while stop is None or not stop.is_set():
            item=self.client.brpop(self.queue_key, timeout=timeout)
            if item is None:
                if once:
                    break
                continue
            task_id = item[1] if isinstance(item[1], str) else item[1].decode()
            record = self.store.load(task_id)
            if record is not None:
                run_task(self.store, record, self.artifact_root, worker, self.storage)
                executed += 1
            if once:
                break
        return executed


def queue_from_environment(artifact_root: Path) -> TaskQueue:
    """`PARAMFABRIC_REDIS_URL` selects the Redis backend; otherwise local threads.

    Artifact storage follows :func:`storage_from_environment` (S3 when
    `PARAMFABRIC_S3_ENDPOINT` is set)."""
    import os

    url = os.getenv("PARAMFABRIC_REDIS_URL")
    root = Path(os.getenv("PARAMFABRIC_ARTIFACT_DIR", str(artifact_root)))
    if url:
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise RuntimeError("PARAMFABRIC_REDIS_URL is set but the 'redis' package is not installed") from exc
        return RedisTaskQueue(redis.Redis.from_url(url, decode_responses=True), root, storage=storage_from_environment(root))
    return LocalTaskQueue(root, workers=int(os.getenv("PARAMFABRIC_TASK_WORKERS", "2")), storage=storage_from_environment(root))
