"""Artifact object storage: local disk, S3 (boto3 and minio protocol doubles), and the task queue path"""

import io
from pathlib import Path

import pytest

from paramfabric.server import tasks
from paramfabric.server.storage import LocalStorage, S3Storage, StorageError, storage_from_environment


class FakeBoto3:
    """The subset of a boto3 S3 client that S3Storage uses"""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets: set[str] = set()

    def head_bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise RuntimeError("404")

    def create_bucket(self, Bucket):
        self.buckets.add(Bucket)

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError("404")

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys=sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page=keys[start:start + 2]  # tiny pages exercise pagination
        more = start + 2 < len(keys)
        return {"Contents": [{"Key": k} for k in page], "IsTruncated": more, "NextContinuationToken": str(start + 2) if more else None}

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop((Bucket, item["Key"]), None)


class FakeMinio:
    """The subset of minio.Minio that S3Storage uses"""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets: set[str] = set()

    def fput_object(self, *args, **kwargs):  # marks the client as minio
        raise NotImplementedError

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.buckets.add(bucket)

    def put_object(self, bucket, key, data, length):
        self.objects[(bucket, key)] = data.read(length)

    def get_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise KeyError("NoSuchKey")

        class Response(io.BytesIO):
            def release_conn(self):
                pass

        return Response(self.objects[(bucket, key)])

    def stat_object(self, bucket, key):
        if (bucket, key) not in self.objects:
            raise KeyError("404")

    def list_objects(self, bucket, prefix, recursive):
        class Item:
            def __init__(self, name):
                self.object_name = name

        return [Item(k) for b, k in sorted(self.objects) if b == bucket and k.startswith(prefix)]

    def remove_object(self, bucket, key):
        self.objects.pop((bucket, key), None)


def test_local_storage_round_trip_and_rejects_escaping_keys(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    storage.put_bytes("t1/result.vtk", b"vtk")
    source=tmp_path / "mesh.vtk"
    source.write_bytes(b"mesh")
    storage.put_file("t1/mesh.vtk", source)
    assert storage.list("t1") == ["t1/mesh.vtk", "t1/result.vtk"] and storage.exists("t1/mesh.vtk")
    assert storage.fetch("t1/result.vtk", tmp_path / "copy.vtk").read_bytes() == b"vtk"
    for key in ("../outside", "t1/../../x", ""):
        with pytest.raises(StorageError):
            storage.put_bytes(key, b"x")
    storage.delete_prefix("t1")
    assert storage.list("t1") == [] and not storage.exists("t1/mesh.vtk")
    in_place = tmp_path / "store" / "t2" / "a.json"
    in_place.parent.mkdir(parents=True)
    in_place.write_text("{}")
    storage.put_file("t2/a.json", in_place)  # same path: no copy, no error
    assert in_place.read_text() == "{}"


@pytest.mark.parametrize("client_type", [FakeBoto3, FakeMinio])
def test_s3_storage_with_boto3_and_minio_clients(tmp_path, client_type):
    client=client_type()
    storage = S3Storage("results", client, prefix="artifacts", endpoint="http://minio:9000", create_bucket=True)
    assert "results" in client.buckets
    storage.put_bytes("abc/flow.vtk", b"flow")
    path = tmp_path / "viewport.json"
    path.write_text('{"a": 1}')
    storage.put_file("abc/viewport.json", path)
    storage.put_bytes("abc/openfoam_case.zip", b"zip")
    assert ("results", "artifacts/abc/flow.vtk") in client.objects  # keys live under the prefix
    assert storage.list("abc") == ["abc/flow.vtk", "abc/openfoam_case.zip", "abc/viewport.json"]
    assert storage.get_bytes("abc/flow.vtk") == b"flow" and storage.exists("abc/flow.vtk")
    assert storage.fetch("abc/viewport.json", tmp_path / "cache" / "v.json").read_text() == '{"a": 1}'
    with pytest.raises(StorageError):
        storage.get_bytes("abc/missing")
    storage.delete_prefix("abc")
    assert storage.list("abc") == []
    assert storage.describe() == {"kind": "s3", "bucket": "results", "endpoint": "http://minio:9000", "prefix": "artifacts",
                                  "client": "minio" if client_type is FakeMinio else "boto3"}


@tasks.handler("storage-test")
def _storage_task(payload, context):
    context.artifact("result.vtk").write_text("# vtk DataFile\n" + payload["body"])
    context.artifact("viewport.json").write_text("{}")
    return {"ok": True}


def test_worker_artifacts_reach_an_api_server_without_shared_disk(tmp_path):
    bucket = S3Storage("paramfabric", FakeBoto3(), prefix="artifacts", create_bucket=True)
    worker = tasks.LocalTaskQueue(tmp_path / "worker-node", workers=1, storage=bucket)
    record = worker.submit("storage-test", {"body": "payload"}, owner="alice")
    worker.executor.shutdown(wait=True)
    assert worker.get(record.id).status == "completed"
    assert bucket.list(record.id) == [f"{record.id}/result.vtk", f"{record.id}/viewport.json"]
    # A different node with an empty artifact directory but the same task store and bucket
    api_node = tasks.LocalTaskQueue(tmp_path / "api-node", workers=1, storage=bucket)
    api_node.store = worker.store
    path = api_node.artifact_path(record.id, "result.vtk")
    assert path.is_relative_to(tmp_path / "api-node") and path.read_text().endswith("payload")
    with pytest.raises(KeyError):
        api_node.artifact_path(record.id, "not-registered.vtk")
    api_node.purge(record.id)
    assert bucket.list(record.id) == []


def test_default_local_storage_publishes_in_place(tmp_path):
    queue = tasks.LocalTaskQueue(tmp_path / "artifacts", workers=1)
    record = queue.submit("storage-test", {"body": "x"}, owner="bob")
    queue.executor.shutdown(wait=True)
    assert isinstance(queue.storage, LocalStorage)
    files = sorted(p.relative_to(tmp_path / "artifacts").as_posix() for p in (tmp_path / "artifacts").rglob("*") if p.is_file())
    assert files == [f"{record.id}/result.vtk", f"{record.id}/viewport.json"]  # no duplicate copies


def test_storage_from_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("PARAMFABRIC_S3_ENDPOINT", raising=False)
    assert isinstance(storage_from_environment(tmp_path), LocalStorage)
    monkeypatch.setenv("PARAMFABRIC_S3_ENDPOINT", "http://127.0.0.1:9")
    import builtins

    real_import=builtins.__import__

    def no_clients(name, *args, **kwargs):
        if name in {"boto3", "minio"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_clients)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("PARAMFABRIC_S3_ACCESS_KEY", raising=False)
    with pytest.raises(StorageError, match="ACCESS_KEY"):  # without boto3/minio the native client needs keys
        storage_from_environment(Path(tmp_path))
    monkeypatch.setenv("PARAMFABRIC_S3_CLIENT", "boto3")
    with pytest.raises(StorageError, match="boto3 is not installed"):
        storage_from_environment(Path(tmp_path))
    monkeypatch.setenv("PARAMFABRIC_S3_CLIENT", "native")
    monkeypatch.setenv("PARAMFABRIC_S3_ACCESS_KEY", "AKIDEXAMPLE")
    monkeypatch.setenv("PARAMFABRIC_S3_SECRET_KEY", "secret")
    monkeypatch.setenv("PARAMFABRIC_S3_ATTEMPTS", "1")
    with pytest.raises(StorageError, match="ConnectionError"):  # nothing listens on port 9: bucket check fails cleanly
        storage_from_environment(Path(tmp_path))
