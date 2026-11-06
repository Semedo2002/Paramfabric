"""S3 workspace sync: remote workers mesh/solve from a content-addressed snapshot and publish results back"""

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from paramfabric.server import tasks
from paramfabric.server.app import create_app
from paramfabric.server.storage import S3Storage
from paramfabric.server.workspace_sync import WorkspaceSyncError, download_workspace, upload_workspace, workspace_files
from tests.test_storage import FakeBoto3


def _workspace(tmp_path, monkeypatch, name="api"):
    root = tmp_path / name
    root.mkdir()
    monkeypatch.chdir(root)
    (root / "params.yaml").write_text("parameters: []\n", encoding="utf-8")
    app = create_app(root / "params.yaml", database_url=f"sqlite:///{(root / 'wb.db').as_posix()}", auth_required=False,
                     audit_path=root / ".paramfabric" / "audit.jsonl")
    client = TestClient(app, client=("127.0.0.1", 40000))
    assert client.post("/api/workbench/geometry/demo").status_code == 200
    assert client.post("/api/workbench/physics", json={"analysis": "fea"}).status_code == 200
    return root, client


def test_snapshot_is_content_addressed_and_verified(tmp_path, monkeypatch):
    root, client = _workspace(tmp_path, monkeypatch)
    bucket = S3Storage("paramfabric", FakeBoto3(), prefix="artifacts", create_bucket=True)
    files = {path.as_posix() for path in workspace_files(root)}
    assert ".paramfabric/workbench.json" in files and any(f.startswith(".paramfabric/geometry/") for f in files)
    assert not any("regenerated" in f or "audit" in f or f.endswith(".db") for f in files)  # inputs only

    first = upload_workspace(bucket, root, "acme", "bracket", {"parameters": []})
    again = upload_workspace(bucket, root, "acme", "bracket", {"parameters": []})
    assert first["uploaded"] == first["files"] > 0 and again["uploaded"] == 0 and again["sha256"] == first["sha256"]
    assert first["manifest"] == f"workspaces/acme/bracket/manifests/{first['sha256']}.json"

    copy = download_workspace(bucket, first, tmp_path / "cache")
    for relative in files:
        assert (copy / relative).read_bytes() == (root / relative).read_bytes()
    assert download_workspace(bucket, first, tmp_path / "cache") == copy  # cached: no second download

    blob=next(key for key in bucket.list("workspaces/acme/bracket/blobs"))
    bucket.put_bytes(blob, b"tampered")
    with pytest.raises(WorkspaceSyncError, match="does not match its hash"):
        download_workspace(bucket, first, tmp_path / "other-cache")
    with pytest.raises(WorkspaceSyncError):
        upload_workspace(bucket, root, "../escape", "x")


def test_remote_worker_meshes_from_the_bucket_and_uploads_results(tmp_path, monkeypatch):
    root, client = _workspace(tmp_path, monkeypatch)
    fabric = client.get("/api/local/fabric").json()
    bucket = S3Storage("paramfabric", FakeBoto3(), prefix="artifacts", create_bucket=True)
    snapshot = upload_workspace(bucket, root, "default", "default", fabric)
    payload = {"root": str(root), "fabric": fabric, "analysis": "fea", "mesher": "cartesian", "workspace": snapshot}
    shutil.rmtree(root / ".paramfabric")  # the worker cannot see the API's disk

    monkeypatch.setenv("PARAMFABRIC_WORKSPACE_CACHE", str(tmp_path / "worker-cache"))
    worker = tasks.LocalTaskQueue(tmp_path / "worker" / "artifacts", workers=1, storage=bucket)
    record = worker.submit("mesh", payload, owner="alice")
    worker.executor.shutdown(wait=True)
    done = worker.get(record.id)
    assert done.status == "completed", done.error
    assert done.result["quality"]["solid_cells"] > 0
    published = set(bucket.list(record.id))
    assert {f"{record.id}/log.txt", f"{record.id}/viewport.json"} <= published
    log = bucket.get_bytes(f"{record.id}/log.txt").decode()
    assert f"Workspace snapshot {snapshot['sha256'][:12]}" in log and "Task completed." in log
    assert (tmp_path / "worker-cache" / snapshot["sha256"] / ".paramfabric" / "workbench.json").is_file()


def test_api_ships_a_snapshot_when_workers_are_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("PARAMFABRIC_WORKSPACE_SYNC", "always")
    root, client = _workspace(tmp_path, monkeypatch)
    mesh = client.post("/api/workbench/mesh", json={"mesher": "cartesian", "wait": True})
    assert mesh.status_code == 200, mesh.json()
    manifests = list(root.rglob("workspaces/default/default/manifests/*.json"))
    assert len(manifests) == 1 and json.loads(manifests[0].read_text())["files"]
    # the task read the downloaded snapshot (marker written by the worker), not the live folder
    assert list(root.rglob("workspace-cache/*/.paramfabric/.snapshot"))
