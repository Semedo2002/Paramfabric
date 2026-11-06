"""Workspace snapshots in object storage, so remote workers mesh and solve without a shared disk.

The API uploads the files a mesh/solve task reads (`.paramfabric/workbench.json`
and the imported geometry: model records, source STEP/IGES, B-Rep) as
content-addressed blobs plus a manifest::

    workspaces/<org>/<project>/blobs/<sha256>
    workspaces/<org>/<project>/manifests/<manifest sha256>.json

and puts the manifest key in the task payload. A worker downloads the manifest
into a local cache (verifying every hash), points the task at that copy, and
reuses it for later tasks with the same snapshot. Unchanged CAD files are never
uploaded twice. The fabric itself travels in the payload; regenerated B-Rep
caches are rebuilt on the worker. Results go back the usual way: every task
artifact (case archive, VTK, `log.txt`) is published when the task ends.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from paramfabric.server.storage import ObjectStorage

MANIFEST_VERSION = 1
PREFIX = "workspaces"


class WorkspaceSyncError(RuntimeError):
    """The snapshot could not be uploaded, downloaded, or verified"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workspace_files(root: Path) -> list[Path]:
    """Files a mesh/solve task reads from a workspace (relative to `root`)"""
    meta = Path(root) / ".paramfabric"
    files = [meta / "workbench.json"] if (meta / "workbench.json").is_file() else []
    geometry = meta / "geometry"
    if geometry.is_dir():
        files += sorted(path for path in geometry.iterdir() if path.is_file())
    return [path.relative_to(root) for path in files]


def _scope(organization: str, project: str) -> str:
    for value in (organization, project):
        if not value or "/" in value or value in {".", ".."}:
            raise WorkspaceSyncError(f"invalid tenant name {value!r}")
    return f"{PREFIX}/{organization}/{project}"


def upload_workspace(storage: ObjectStorage, root: Path, organization: str = "default", project: str = "default",
                     fabric: dict[str, Any] | None = None) -> dict[str, Any]:
    """Upload a snapshot; returns `{"manifest", "sha256", "files", "uploaded", "bytes"}` for the task payload"""
    root = Path(root)
    scope = _scope(organization, project)
    entries: dict[str, dict[str, Any]] = {}
    uploaded = 0
    total = 0
    for relative in workspace_files(root):
        path = root / relative
        digest = _sha256(path)
        size = path.stat().st_size
        entries[relative.as_posix()] = {"sha256": digest, "size": size}
        total += size
        key = f"{scope}/blobs/{digest}"
        try:
            if not storage.exists(key):
                storage.put_file(key, path)
                uploaded += 1
        except Exception as exc:  # noqa: BLE001 - every backend raises its own errors
            raise WorkspaceSyncError(f"could not upload {relative.as_posix()}: {exc}") from exc
    manifest = {
        "version": MANIFEST_VERSION, "organization": organization, "project": project, "files": entries,
        "fabric_sha256": hashlib.sha256(json.dumps(fabric, sort_keys=True).encode()).hexdigest() if fabric is not None else None,
    }
    body = json.dumps(manifest, sort_keys=True, indent=1).encode()
    digest = hashlib.sha256(body).hexdigest()
    key = f"{scope}/manifests/{digest}.json"
    try:
        if not storage.exists(key):
            storage.put_bytes(key, body)
    except Exception as exc:  # noqa: BLE001
        raise WorkspaceSyncError(f"could not upload the workspace manifest: {exc}") from exc
    return {"manifest": key, "sha256": digest, "files": len(entries), "uploaded": uploaded, "bytes": total}


def download_workspace(storage: ObjectStorage, snapshot: dict[str, Any], cache_root: Path, log: Any = None) -> Path:
    """Materialize a snapshot under `cache_root/<manifest sha256>` and return that workspace root"""
    key, expected = snapshot["manifest"], snapshot["sha256"]
    target = Path(cache_root) / expected
    if (target / ".paramfabric" / ".snapshot").is_file():
        if log:
            log(f"Workspace snapshot {expected[:12]} already cached on this worker")
        return target
    try:
        body = storage.get_bytes(key)
    except Exception as exc:  # noqa: BLE001 - StorageError or a backend's own error
        raise WorkspaceSyncError(f"could not download workspace manifest {key}: {exc}") from exc
    if hashlib.sha256(body).hexdigest() != expected:
        raise WorkspaceSyncError(f"workspace manifest {key} does not match its hash")
    manifest=json.loads(body)
    scope = key.rsplit("/manifests/", 1)[0]
    staging = Path(cache_root) / f".{expected}.{uuid.uuid4().hex[:8]}"
    try:
        fetched=0
        for relative, entry in manifest["files"].items():
            path = (staging / relative).resolve()
            if not path.is_relative_to(staging.resolve()) or Path(relative).is_absolute():
                raise WorkspaceSyncError(f"manifest path {relative!r} escapes the workspace")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                storage.fetch(f"{scope}/blobs/{entry['sha256']}", path)
            except Exception as exc:  # noqa: BLE001
                raise WorkspaceSyncError(f"could not download {relative}: {exc}") from exc
            if _sha256(path) != entry["sha256"]:
                raise WorkspaceSyncError(f"{relative} does not match its hash (corrupted upload?)")
            fetched += entry["size"]
        (staging / ".paramfabric").mkdir(parents=True, exist_ok=True)
        (staging / ".paramfabric" / ".snapshot").write_text(json.dumps({"manifest": key}), encoding="utf-8")
        try:
            os.replace(staging, target)  # atomic: concurrent workers either win or find it complete
        except OSError:
            if not (target / ".paramfabric" / ".snapshot").is_file():
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if log:
        log(f"Workspace snapshot {expected[:12]}: {len(manifest['files'])} file(s), {fetched / 1e6:.3g} MB downloaded")
    return target


def task_root(payload: dict[str, Any], context: Any) -> Path:
    """The workspace a handler should read: the synced snapshot when there is one, else the shared path"""
    snapshot = payload.get("workspace")
    storage=getattr(context, "storage", None)
    if not snapshot or storage is None:
        return Path(payload["root"])
    cache=Path(os.environ.get("PARAMFABRIC_WORKSPACE_CACHE") or context.artifact_dir.parent.parent / "workspace-cache")
    cache.mkdir(parents=True, exist_ok=True)
    return download_workspace(storage, snapshot, cache, log=context.log)
