"""Object storage for task artifacts: local disk or S3-compatible buckets.

Handlers always write artifacts into the task's local working directory.
When a task finishes, the queue publishes every registered artifact to the
configured storage under `<prefix>/<task id>/<name>` (prefix `PARAMFABRIC_S3_PREFIX`, default `artifacts`); API servers fetch them
back on demand into their own local cache. With :class:`LocalStorage` rooted
at the artifact directory, publishing is a no-op and nothing is copied.

`PARAMFABRIC_S3_ENDPOINT` selects :class:`S3Storage` (AWS S3, MinIO,
SeaweedFS, Ceph RGW, R2, …) with `PARAMFABRIC_S3_BUCKET` (default
`paramfabric`), `PARAMFABRIC_S3_ACCESS_KEY`/`PARAMFABRIC_S3_SECRET_KEY`
(or `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`),
`PARAMFABRIC_S3_REGION`, `PARAMFABRIC_S3_PREFIX`, and
`PARAMFABRIC_S3_ADDRESSING` (`path` | `virtual`).

`PARAMFABRIC_S3_CLIENT` chooses the client: `boto3` (multipart transfers,
standard retry mode), `minio`, or `native` (built-in SigV4 client, no
extra dependency). `auto` (default) prefers boto3, then minio, then native.
Uploads stream from disk and downloads stream to a temporary file that is
renamed into place, so large VTK results and case archives never load into
memory.
"""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Protocol


class StorageError(RuntimeError):
    """Raised when an object is missing or the backend is misconfigured"""


def _clean_key(key: str) -> str:
    parts = PurePosixPath(key.replace("\\", "/")).parts
    if not parts or any(part in {"..", ""} or part.startswith("/") for part in parts):
        raise StorageError(f"invalid storage key {key!r}")
    return "/".join(parts)


class ObjectStorage(Protocol):
    def put_file(self, key: str, path: Path) -> None: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def fetch(self, key: str, destination: Path) -> Path: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete_prefix(self, prefix: str) -> None: ...
    def describe(self) -> dict[str, Any]: ...


class LocalStorage:
    """Objects as files under `root` (key `a/b` -> `root/a/b`)"""

    kind = "local"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / _clean_key(key)).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise StorageError(f"key {key!r} escapes the storage root")
        return path

    def put_file(self, key: str, path: Path) -> None:
        target = self._path(key)
        if Path(path).resolve() == target:
            return  # already in place (artifact directory is the storage root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)

    def put_bytes(self, key: str, data: bytes) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"no object {key!r}")
        return path.read_bytes()

    def fetch(self, key: str, destination: Path) -> Path:
        source = self._path(key)
        if not source.is_file():
            raise StorageError(f"no object {key!r}")
        if source != Path(destination).resolve():
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        return Path(destination)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix) if prefix.strip("/") else self.root.resolve()
        if not base.exists():
            return []
        root = self.root.resolve()
        return sorted(str(path.relative_to(root)).replace("\\", "/") for path in base.rglob("*") if path.is_file())

    def delete_prefix(self, prefix: str) -> None:
        target=self._path(prefix)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.is_file():
            target.unlink()

    def describe(self) -> dict[str, Any]:
        return {"kind": "local", "root": str(self.root)}


class _Boto3:
    """Adapter over a `boto3` S3 client"""

    def __init__(self, client: Any) -> None:
        self.client = client

    def put_file(self, bucket: str, key: str, path: Path) -> None:
        if hasattr(self.client, "upload_file"):
            self.client.upload_file(str(path), bucket, key)  # managed multipart transfer
        else:
            self.put(bucket, key, Path(path).read_bytes())

    def download(self, bucket: str, key: str, destination: Path) -> None:
        if hasattr(self.client, "download_file"):
            try:
                self.client.download_file(bucket, key, str(destination))
            except Exception as exc:  # noqa: BLE001 - botocore ClientError (404)
                raise StorageError(f"no object {key!r}: {exc}") from exc
        else:
            Path(destination).write_bytes(self.get(bucket, key))

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=bucket, Key=key, Body=data)

    def get(self, bucket: str, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001 - botocore ClientError / NoSuchKey
            raise StorageError(f"no object {key!r}: {exc}") from exc

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def list(self, bucket: str, prefix: str) -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            arguments = {"Bucket": bucket, "Prefix": prefix} | ({"ContinuationToken": token} if token else {})
            page = self.client.list_objects_v2(**arguments)
            keys += [item["Key"] for item in page.get("Contents", [])]
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                return keys

    def delete(self, bucket: str, keys: list[str]) -> None:
        for start in range(0, len(keys), 1000):
            chunk = keys[start:start + 1000]
            self.client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in chunk]})

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self.client.head_bucket(Bucket=bucket)
        except Exception:  # noqa: BLE001
            self.client.create_bucket(Bucket=bucket)


class _Minio:
    """Adapter over a `minio.Minio` client"""

    def __init__(self, client: Any) -> None:
        self.client = client

    def put_file(self, bucket: str, key: str, path: Path) -> None:
        try:
            self.client.fput_object(bucket, key, str(path))
        except NotImplementedError:
            self.put(bucket, key, Path(path).read_bytes())

    def download(self, bucket: str, key: str, destination: Path) -> None:
        try:
            self.client.fget_object(bucket, key, str(destination))
        except (AttributeError, NotImplementedError):
            Path(destination).write_bytes(self.get(bucket, key))
        except Exception as exc:  # noqa: BLE001 - minio.error.S3Error
            raise StorageError(f"no object {key!r}: {exc}") from exc

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self.client.put_object(bucket, key, io.BytesIO(data), len(data))

    def get(self, bucket: str, key: str) -> bytes:
        try:
            response = self.client.get_object(bucket, key)
        except Exception as exc:  # noqa: BLE001 - minio.error.S3Error
            raise StorageError(f"no object {key!r}: {exc}") from exc
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self.client.stat_object(bucket, key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def list(self, bucket: str, prefix: str) -> list[str]:
        return [item.object_name for item in self.client.list_objects(bucket, prefix=prefix, recursive=True)]

    def delete(self, bucket: str, keys: list[str]) -> None:
        for key in keys:
            self.client.remove_object(bucket, key)

    def ensure_bucket(self, bucket: str) -> None:
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)


class _Native:
    """Adapter over the built-in SigV4 client (:mod:`paramfabric.server.s3native`)"""

    def __init__(self, client: Any) -> None:
        self.client = client

    def _call(self, function: Any, *args: Any) -> Any:
        from paramfabric.server.s3native import S3Error

        try:
            return function(*args)
        except S3Error as exc:
            raise StorageError(str(exc)) from exc

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self._call(self.client.put_object, bucket, key, data)

    def put_file(self, bucket: str, key: str, path: Path) -> None:
        self._call(self.client.put_object, bucket, key, Path(path))

    def get(self, bucket: str, key: str) -> bytes:
        return self._call(self.client.get_object, bucket, key)

    def download(self, bucket: str, key: str, destination: Path) -> None:
        self._call(self.client.download, bucket, key, Path(destination))

    def exists(self, bucket: str, key: str) -> bool:
        return self._call(self.client.head_object, bucket, key)

    def list(self, bucket: str, prefix: str) -> list[str]:
        return self._call(lambda: list(self.client.list_objects(bucket, prefix)))

    def delete(self, bucket: str, keys: list[str]) -> None:
        if keys:
            self._call(self.client.delete_objects, bucket, keys)

    def ensure_bucket(self, bucket: str) -> None:
        self._call(self.client.ensure_bucket, bucket)


class S3Storage:
    """S3-compatible object storage (AWS S3, MinIO, SeaweedFS, Ceph RGW, …)"""

    kind="s3"

    def __init__(self, bucket: str, client: Any, *, prefix: str = "", endpoint: str = "", create_bucket: bool = False) -> None:
        from paramfabric.server.s3native import NativeS3Client

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint = endpoint
        if isinstance(client, NativeS3Client):
            self.backend: Any = _Native(client)
        elif hasattr(client, "fput_object"):
            self.backend = _Minio(client)
        else:
            self.backend = _Boto3(client)
        self.client_kind = type(self.backend).__name__.strip("_").lower()
        if create_bucket:
            self.backend.ensure_bucket(bucket)

    def _key(self, key: str) -> str:
        key = _clean_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def _strip(self, key: str) -> str:
        return key[len(self.prefix) + 1:] if self.prefix and key.startswith(self.prefix + "/") else key

    def put_file(self, key: str, path: Path) -> None:
        self.backend.put_file(self.bucket, self._key(key), Path(path))

    def put_bytes(self, key: str, data: bytes) -> None:
        self.backend.put(self.bucket, self._key(key), data)

    def get_bytes(self, key: str) -> bytes:
        return self.backend.get(self.bucket, self._key(key))

    def fetch(self, key: str, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".download")
        try:
            self.backend.download(self.bucket, self._key(key), temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def exists(self, key: str) -> bool:
        return self.backend.exists(self.bucket, self._key(key))

    def list(self, prefix: str) -> list[str]:
        return sorted(self._strip(key) for key in self.backend.list(self.bucket, self._key(prefix)))

    def delete_prefix(self, prefix: str) -> None:
        self.backend.delete(self.bucket, self.backend.list(self.bucket, self._key(prefix)))

    def describe(self) -> dict[str, Any]:
        return {"kind": "s3", "bucket": self.bucket, "endpoint": self.endpoint, "prefix": self.prefix, "client": self.client_kind}


def storage_from_environment(local_root: Path) -> ObjectStorage:
    """S3 when `PARAMFABRIC_S3_ENDPOINT` is set, otherwise local disk at `local_root`"""
    endpoint = os.getenv("PARAMFABRIC_S3_ENDPOINT", "").strip()
    if not endpoint:
        return LocalStorage(local_root)
    bucket = os.getenv("PARAMFABRIC_S3_BUCKET", "paramfabric")
    access = os.getenv("PARAMFABRIC_S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID") or None
    secret = os.getenv("PARAMFABRIC_S3_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY") or None
    region=os.getenv("PARAMFABRIC_S3_REGION") or os.getenv("AWS_REGION") or "us-east-1"
    prefix = os.getenv("PARAMFABRIC_S3_PREFIX", "artifacts")
    addressing = os.getenv("PARAMFABRIC_S3_ADDRESSING") or None
    choice = os.getenv("PARAMFABRIC_S3_CLIENT", "auto").lower()
    if choice not in {"auto", "boto3", "minio", "native"}:
        raise StorageError("PARAMFABRIC_S3_CLIENT must be auto, boto3, minio, or native")
    if choice in {"auto", "boto3"}:
        try:
            import boto3  # type: ignore[import-not-found]
            from botocore.config import Config  # type: ignore[import-not-found]
        except ImportError as exc:
            if choice == "boto3":
                raise StorageError("PARAMFABRIC_S3_CLIENT=boto3 but boto3 is not installed (pip install paramfabric-studio[s3])") from exc
        else:
            config = Config(retries={"max_attempts": 5, "mode": "standard"}, signature_version="s3v4",
                            s3={"addressing_style": addressing or ("virtual" if "amazonaws.com" in endpoint else "path")})
            client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access, aws_secret_access_key=secret,
                                  region_name=region, config=config)
            return S3Storage(bucket, client, prefix=prefix, endpoint=endpoint, create_bucket=True)
    if choice in {"auto", "minio"}:
        try:
            from minio import Minio  # type: ignore[import-not-found]
        except ImportError as exc:
            if choice == "minio":
                raise StorageError("PARAMFABRIC_S3_CLIENT=minio but the minio package is not installed") from exc
        else:
            secure = endpoint.startswith("https://")
            host = endpoint.split("://", 1)[-1].rstrip("/")
            client=Minio(host, access_key=access, secret_key=secret, secure=secure, region=region)
            return S3Storage(bucket, client, prefix=prefix, endpoint=endpoint, create_bucket=True)
    from paramfabric.server.s3native import NativeS3Client

    if not access or not secret:
        raise StorageError("PARAMFABRIC_S3_ACCESS_KEY and PARAMFABRIC_S3_SECRET_KEY are required for S3 storage")
    client = NativeS3Client(endpoint, access, secret, region=region, addressing=addressing,
                            attempts=int(os.getenv("PARAMFABRIC_S3_ATTEMPTS", "5")))
    return S3Storage(bucket, client, prefix=prefix, endpoint=endpoint, create_bucket=True)
