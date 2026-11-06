"""Dependency-free S3 REST client (AWS Signature Version 4) for artifact storage.

Implements exactly what Paramfabric needs, against AWS S3 and S3-compatible
servers (MinIO, SeaweedFS, Ceph RGW, Wasabi, Cloudflare R2):

* `PutObject` (single request up to :data:`MULTIPART_THRESHOLD`) and
  multipart upload above it (`CreateMultipartUpload`/`UploadPart`/
  `CompleteMultipartUpload`, aborted on failure), streamed from disk;
* `GetObject` streamed to a temporary file and renamed atomically;
* `HeadObject`, `ListObjectsV2` (paginated), `DeleteObjects` (batches of
  1000, `Content-MD5`), `HeadBucket`/`CreateBucket` (with
  `LocationConstraint` outside us-east-1);
* every request is SigV4-signed with the payload SHA-256, retried with
  exponential backoff on connection errors, 5xx, `429` and `SlowDown`,
  and re-signed per attempt; S3 XML error documents become
  :class:`~paramfabric.server.storage.StorageError` with the S3 error code.

Addressing is path-style (`https://host/bucket/key`) for custom endpoints and
virtual-hosted (`https://bucket.s3.region.amazonaws.com/key`) for AWS.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import os
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

MULTIPART_THRESHOLD = 64 * 1024 * 1024
PART_SIZE = 16 * 1024 * 1024
_UNRESERVED = "-_.~"
_RETRY_STATUS = {429, 500, 502, 503, 504}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class S3Error(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"S3 {status} {code}: {message}")
        self.status, self.code = status, code


def _quote(value: str, safe: str = "") -> str:
    return urllib.parse.quote(value, safe=_UNRESERVED + safe)


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    return _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode("utf-8"), date), region), service), "aws4_request")


def sign_v4(method: str, host: str, path: str, query: dict[str, str], headers: dict[str, str], payload_hash: str,
            access_key: str, secret_key: str, region: str, service: str = "s3", now: datetime | None = None) -> dict[str, str]:
    """Return `headers` plus `x-amz-date`, `x-amz-content-sha256` (for s3) and `Authorization`"""
    now=now or datetime.now(timezone.utc)
    amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    signed = {key.lower(): " ".join(str(value).split()) for key, value in headers.items()}
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    if service == "s3":
        signed["x-amz-content-sha256"] = payload_hash
    names = sorted(signed)
    canonical_query = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
    canonical = "\n".join([
        method, _quote(path, "/") if service == "s3" else path, canonical_query,
        "".join(f"{name}:{signed[name]}\n" for name in names), ";".join(names), payload_hash,
    ])
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signature=hmac.new(signing_key(secret_key, date, region, service), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    result = dict(headers)
    result.update({"x-amz-date": amz_date, "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, Signature={signature}"})
    if service == "s3":
        result["x-amz-content-sha256"] = payload_hash
    return result


def _file_sha256(path: Path, start: int = 0, length: int | None = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        handle.seek(start)
        remaining = length if length is not None else float("inf")
        while remaining > 0:
            chunk = handle.read(int(min(1024 * 1024, remaining)))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


class _Slice:
    """File-like view of `length` bytes from `start` (for streamed part uploads)"""

    def __init__(self, handle: BinaryIO, start: int, length: int) -> None:
        handle.seek(start)
        self.handle, self.remaining = handle, length

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        size = self.remaining if size < 0 else min(size, self.remaining)
        data = self.handle.read(size)
        self.remaining -= len(data)
        return data


class NativeS3Client:
    """Minimal S3 client; one instance is safe to share (a connection per request)"""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, *, region: str = "us-east-1",
                 addressing: str | None = None, attempts: int = 5, backoff: float = 0.5, timeout: float = 60.0) -> None:
        if not access_key or not secret_key:
            raise ValueError("S3 access key and secret key are required")
        parsed = urllib.parse.urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
        self.scheme, self.netloc = parsed.scheme, parsed.netloc
        self.access_key, self.secret_key, self.region = access_key, secret_key, region or "us-east-1"
        self.addressing = addressing or ("virtual" if self.netloc.endswith("amazonaws.com") else "path")
        self.attempts, self.backoff, self.timeout = max(1, attempts), backoff, timeout

    # transport
    def _target(self, bucket: str, key: str = "") -> tuple[str, str]:
        if self.addressing == "virtual":
            return f"{bucket}.{self.netloc}", "/" + key
        return self.netloc, f"/{bucket}" + (f"/{key}" if key else "/")

    def request(self, method: str, bucket: str, key: str = "", *, query: dict[str, str] | None = None,
                headers: dict[str, str] | None = None, body: bytes | Path | tuple[Path, int, int] | None = None,
                sink: Path | None = None, expect: tuple[int, ...] = (200,)) -> tuple[int, dict[str, str], bytes]:
        query = query or {}
        host, path = self._target(bucket, key)
        for attempt in range(1, self.attempts + 1):
            payload_hash, length, opener = self._payload(body)
            signed = sign_v4(method, host, path, query, dict(headers or {}), payload_hash, self.access_key, self.secret_key, self.region)
            signed["Content-Length"] = str(length)
            connection_class = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            connection = connection_class(host, timeout=self.timeout)
            url = _quote(path, "/") + ("?" + "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items())) if query else "")
            try:
                stream = opener()
                try:
                    connection.request(method, url, body=stream, headers=signed)
                    response=connection.getresponse()
                    status, response_headers = response.status, {k.lower(): v for k, v in response.getheaders()}
                    if sink is not None and status in expect:
                        temporary = sink.with_suffix(sink.suffix + ".part")
                        sink.parent.mkdir(parents=True, exist_ok=True)
                        with open(temporary, "wb") as handle:
                            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                                handle.write(chunk)
                        temporary.replace(sink)
                        data = b""
                    else:
                        data = response.read()
                finally:
                    if hasattr(stream, "close"):
                        stream.close()
            except (OSError, http.client.HTTPException) as exc:
                if attempt == self.attempts:
                    raise S3Error(0, "ConnectionError", str(exc)) from exc
                time.sleep(self.backoff * 2 ** (attempt - 1))
                continue
            finally:
                connection.close()
            if status in expect:
                return status, response_headers, data
            code, message = self._error(data, status)
            if (status in _RETRY_STATUS or code in {"SlowDown", "RequestTimeout", "InternalError"}) and attempt < self.attempts:
                time.sleep(self.backoff * 2 ** (attempt - 1))
                continue
            raise S3Error(status, code, message)
        raise S3Error(0, "RetriesExhausted", f"{method} {path}")

    @staticmethod
    def _payload(body: bytes | Path | tuple[Path, int, int] | None) -> tuple[str, int, Any]:
        if body is None:
            return EMPTY_SHA256, 0, lambda: None
        if isinstance(body, bytes):
            return hashlib.sha256(body).hexdigest(), len(body), lambda: body
        if isinstance(body, tuple):
            path, start, length = body
            return _file_sha256(path, start, length), length, lambda: _Slice(open(path, "rb"), start, length)
        size = Path(body).stat().st_size
        return _file_sha256(Path(body)), size, lambda: open(body, "rb")

    @staticmethod
    def _error(data: bytes, status: int) -> tuple[str, str]:
        try:
            root = ET.fromstring(data)
            return root.findtext("Code") or str(status), root.findtext("Message") or ""
        except ET.ParseError:
            return {404: "NoSuchKey", 403: "AccessDenied"}.get(status, str(status)), data[:200].decode("utf-8", "replace")

    # operations
    def put_object(self, bucket: str, key: str, body: bytes | Path) -> None:
        if isinstance(body, Path) and body.stat().st_size > MULTIPART_THRESHOLD:
            self._multipart(bucket, key, body)
            return
        self.request("PUT", bucket, key, body=body, headers={"Content-Type": "application/octet-stream"})

    def _multipart(self, bucket: str, key: str, path: Path) -> None:
        _, _, data = self.request("POST", bucket, key, query={"uploads": ""})
        upload_id = _find(ET.fromstring(data), "UploadId")
        size = path.stat().st_size
        parts = []
        try:
            for number, start in enumerate(range(0, size, PART_SIZE), start=1):
                length = min(PART_SIZE, size - start)
                _, headers, _ = self.request("PUT", bucket, key, query={"partNumber": str(number), "uploadId": upload_id}, body=(path, start, length))
                parts.append((number, headers.get("etag", "")))
            document = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{etag}</ETag></Part>" for n, etag in parts) + "</CompleteMultipartUpload>"
            _, _, data = self.request("POST", bucket, key, query={"uploadId": upload_id}, body=document.encode("utf-8"))
            if b"<Error>" in data:  # S3 can report a failed completion with HTTP 200
                code, message = self._error(data, 200)
                raise S3Error(200, code, message)
        except Exception:
            self.request("DELETE", bucket, key, query={"uploadId": upload_id}, expect=(200, 204, 404))
            raise

    def get_object(self, bucket: str, key: str) -> bytes:
        return self.request("GET", bucket, key)[2]

    def download(self, bucket: str, key: str, destination: Path) -> None:
        self.request("GET", bucket, key, sink=Path(destination))

    def head_object(self, bucket: str, key: str) -> bool:
        try:
            self.request("HEAD", bucket, key)
            return True
        except S3Error as exc:
            if exc.status == 404:
                return False
            raise

    def list_objects(self, bucket: str, prefix: str) -> Iterator[str]:
        token = None
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            root = ET.fromstring(self.request("GET", bucket, query=query)[2])
            for element in root.iter():
                if element.tag.endswith("Contents"):
                    yield _find(element, "Key")
            truncated = (_find(root, "IsTruncated", "false") or "false").lower() == "true"
            token = _find(root, "NextContinuationToken", "")
            if not truncated or not token:
                return

    def delete_objects(self, bucket: str, keys: list[str]) -> None:
        from xml.sax.saxutils import escape

        for start in range(0, len(keys), 1000):
            chunk = keys[start:start + 1000]
            body = ("<Delete><Quiet>true</Quiet>" + "".join(f"<Object><Key>{escape(k)}</Key></Object>" for k in chunk) + "</Delete>").encode("utf-8")
            md5=base64.b64encode(hashlib.md5(body).digest()).decode()  # noqa: S324 - required by the S3 API
            self.request("POST", bucket, query={"delete": ""}, body=body, headers={"Content-MD5": md5, "Content-Type": "application/xml"})

    def ensure_bucket(self, bucket: str) -> None:
        try:
            self.request("HEAD", bucket)
            return
        except S3Error as exc:
            if exc.status not in (404, 400):
                raise
        body=None
        if self.region != "us-east-1":
            body = (f'<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    f"<LocationConstraint>{self.region}</LocationConstraint></CreateBucketConfiguration>").encode()
        try:
            self.request("PUT", bucket, body=body)
        except S3Error as exc:
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise


def _find(element: ET.Element, name: str, default: str | None = None) -> str:
    for child in element.iter():
        if child.tag == name or child.tag.endswith("}" + name):
            return child.text or ""
    if default is None:
        raise S3Error(0, "MalformedResponse", f"missing <{name}>")
    return default


def client_from_environment(endpoint: str) -> NativeS3Client:
    return NativeS3Client(
        endpoint, os.getenv("PARAMFABRIC_S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID", ""),
        os.getenv("PARAMFABRIC_S3_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        region=os.getenv("PARAMFABRIC_S3_REGION") or os.getenv("AWS_REGION", "us-east-1"),
        addressing=os.getenv("PARAMFABRIC_S3_ADDRESSING") or None,
    )
