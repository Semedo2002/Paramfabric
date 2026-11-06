"""Built-in SigV4 S3 client: AWS reference vector, a signature-checking S3 server over real HTTP, and an opt-in live test"""

import hashlib
import hmac
import os
import re
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from paramfabric.server import s3native
from paramfabric.server.s3native import EMPTY_SHA256, NativeS3Client, S3Error, sign_v4
from paramfabric.server.storage import S3Storage, StorageError

ACCESS, SECRET = "AKIDPARAMFABRIC", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def test_signature_matches_the_aws_documentation_example():
    # "Create a signed AWS API request" (IAM ListUsers), AWS General Reference
    headers = sign_v4("GET", "iam.amazonaws.com", "/", {"Action": "ListUsers", "Version": "2010-05-08"},
                      {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, EMPTY_SHA256,
                      "AKIDEXAMPLE", SECRET, "us-east-1", "iam", datetime(2015, 8, 30, 12, 36, tzinfo=timezone.utc))
    assert headers["Authorization"].endswith("Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")
    assert s3native.signing_key(SECRET, "20150830", "us-east-1", "iam").hex() == "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"


def _expected_signature(method, raw_path, query_string, headers, secret):
    auth = headers["authorization"]
    credential=re.search(r"Credential=([^,]+)", auth).group(1)
    signed_names = re.search(r"SignedHeaders=([^,]+)", auth).group(1).split(";")
    _, date, region, service, _ = credential.split("/")
    pairs = urllib.parse.parse_qsl(query_string, keep_blank_values=True)
    encode = lambda v: urllib.parse.quote(v, safe="-_.~")  # noqa: E731
    canonical_query = "&".join(f"{encode(k)}={encode(v)}" for k, v in sorted(pairs))
    canonical_headers = "".join(f"{name}:{' '.join(headers[name].split())}\n" for name in signed_names)
    canonical = "\n".join([method, raw_path, canonical_query, canonical_headers, ";".join(signed_names), headers["x-amz-content-sha256"]])
    scope=f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", headers["x-amz-date"], scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret).encode()
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest(), auth.rsplit("Signature=", 1)[1]


class FakeS3:
    """Path-style S3 subset with SigV4 verification, payload hash checks, multipart, and fault injection"""

    def __init__(self, page_size=2):
        self.buckets: dict[str, dict[str, bytes]] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.faults: list[int] = []
        self.requests: list[str] = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self, status, body=b"", headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _error(self, status, code):
                self._reply(status, f"<Error><Code>{code}</Code><Message>{code}</Message></Error>".encode())

            def _handle(self):
                raw_path, _, query_string = self.path.partition("?")
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                headers = {k.lower(): v for k, v in self.headers.items()}
                state.requests.append(f"{self.command} {raw_path}?{query_string}")
                if state.faults:
                    return self._error(state.faults.pop(0), "SlowDown")
                expected, given = _expected_signature(self.command, raw_path, query_string, headers, SECRET)
                if not hmac.compare_digest(expected, given):
                    return self._error(403, "SignatureDoesNotMatch")
                if hashlib.sha256(body).hexdigest() != headers["x-amz-content-sha256"]:
                    return self._error(400, "XAmzContentSHA256Mismatch")
                query = dict(urllib.parse.parse_qsl(query_string, keep_blank_values=True))
                bucket, _, key = urllib.parse.unquote(raw_path).lstrip("/").partition("/")
                objects = state.buckets.get(bucket)
                if not key:
                    if self.command == "HEAD":
                        return self._reply(200 if objects is not None else 404)
                    if self.command == "PUT":
                        state.buckets.setdefault(bucket, {})
                        return self._reply(200)
                    if objects is None:
                        return self._error(404, "NoSuchBucket")
                    if self.command == "POST" and "delete" in query:
                        for name in re.findall(rb"<Key>(.*?)</Key>", body):
                            objects.pop(name.decode().replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"), None)
                        return self._reply(200, b"<DeleteResult/>")
                    if self.command == "GET":
                        keys = sorted(k for k in objects if k.startswith(query.get("prefix", "")))
                        start = int(query.get("continuation-token") or 0)
                        page = keys[start:start + page_size]
                        more = start + page_size < len(keys)
                        xml = '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">' + "".join(
                            f"<Contents><Key>{escape(k)}</Key></Contents>" for k in page) + f"<IsTruncated>{str(more).lower()}</IsTruncated>"
                        xml += (f"<NextContinuationToken>{start + page_size}</NextContinuationToken>" if more else "") + "</ListBucketResult>"
                        return self._reply(200, xml.encode())
                if objects is None:
                    return self._error(404, "NoSuchBucket")
                if self.command == "POST" and "uploads" in query:
                    upload = uuid.uuid4().hex
                    state.uploads[upload] = {}
                    return self._reply(200, f"<InitiateMultipartUploadResult><UploadId>{upload}</UploadId></InitiateMultipartUploadResult>".encode())
                if self.command == "PUT" and "partNumber" in query:
                    state.uploads[query["uploadId"]][int(query["partNumber"])] = body
                    return self._reply(200, headers={"ETag": f'"{hashlib.md5(body).hexdigest()}"'})  # noqa: S324
                if self.command == "POST" and "uploadId" in query:
                    parts = state.uploads.pop(query["uploadId"])
                    objects[key] = b"".join(parts[n] for n in sorted(parts))
                    return self._reply(200, b"<CompleteMultipartUploadResult/>")
                if self.command == "PUT":
                    objects[key] = body
                    return self._reply(200)
                if key not in objects:
                    return self._error(404, "NoSuchKey") if self.command != "HEAD" else self._reply(404)
                return self._reply(200, objects[key] if self.command == "GET" else b"", {"Content-Length": str(len(objects[key]))} if self.command == "HEAD" else None)

            do_GET=do_PUT = do_POST = do_HEAD = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture()
def fake():
    server = FakeS3()
    yield server
    server.server.shutdown()


def test_storage_round_trip_over_http_with_signature_checks(fake, tmp_path):
    client = NativeS3Client(fake.endpoint, ACCESS, SECRET, backoff=0.01)
    storage = S3Storage("results", client, prefix="artifacts", endpoint=fake.endpoint, create_bucket=True)
    assert storage.describe()["client"] == "native" and "results" in fake.buckets
    vtk = tmp_path / "flow.vtk"
    vtk.write_bytes(b"# vtk DataFile\n" + bytes(range(256)) * 50)
    storage.put_file("task 1/flow.vtk", vtk)  # space in the key: exercises URI encoding on both sides
    storage.put_bytes("task 1/viewport.json", b'{"a": 1}')
    storage.put_bytes("task 1/üñí & <x>.txt", "unicode".encode())
    storage.put_bytes("task 2/mesh.vtk", b"mesh")
    assert storage.list("task 1") == ["task 1/flow.vtk", "task 1/viewport.json", "task 1/üñí & <x>.txt"]  # paginated (page size 2)
    assert storage.exists("task 1/flow.vtk") and not storage.exists("task 1/missing.vtk")
    fetched = storage.fetch("task 1/flow.vtk", tmp_path / "cache" / "flow.vtk")
    assert fetched.read_bytes() == vtk.read_bytes() and not list((tmp_path / "cache").glob("*.download"))
    with pytest.raises(StorageError, match="NoSuchKey"):
        storage.fetch("task 1/missing.vtk", tmp_path / "cache" / "missing.vtk")
    assert not (tmp_path / "cache" / "missing.vtk").exists()
    storage.delete_prefix("task 1")
    assert storage.list("task 1") == [] and storage.list("task 2") == ["task 2/mesh.vtk"]


def test_multipart_upload_retries_and_wrong_credentials(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(s3native, "MULTIPART_THRESHOLD", 1000)
    monkeypatch.setattr(s3native, "PART_SIZE", 400)
    client = NativeS3Client(fake.endpoint, ACCESS, SECRET, backoff=0.01)
    client.ensure_bucket("cases")
    archive = tmp_path / "openfoam_case.zip"
    archive.write_bytes(os.urandom(2500))
    client.put_object("cases", "t/openfoam_case.zip", archive)
    assert fake.buckets["cases"]["t/openfoam_case.zip"] == archive.read_bytes()
    assert sum("partNumber" in r for r in fake.requests) == 7  # 2500 bytes in 400-byte parts
    fake.faults = [503, 500]  # transient failures are retried and re-signed
    client.put_object("cases", "t/after-retry", b"ok")
    assert fake.buckets["cases"]["t/after-retry"] == b"ok"
    fake.faults = [503] * 5
    with pytest.raises(S3Error, match="SlowDown"):
        client.put_object("cases", "t/never", b"x")
    wrong = NativeS3Client(fake.endpoint, ACCESS, "not-the-secret", backoff=0.01)
    with pytest.raises(S3Error) as error:
        wrong.put_object("cases", "t/forged", b"x")
    assert error.value.status == 403 and error.value.code == "SignatureDoesNotMatch" and "t/forged" not in fake.buckets["cases"]


def test_artifacts_move_between_nodes_through_the_bucket(fake, tmp_path):
    from paramfabric.server import tasks

    @tasks.handler("s3-native-test")
    def _task(payload, context):
        context.artifact("flow.vtk").write_text("vtk " * 100)
        return {}

    bucket = S3Storage("paramfabric", NativeS3Client(fake.endpoint, ACCESS, SECRET, backoff=0.01), prefix="artifacts", create_bucket=True)
    worker = tasks.LocalTaskQueue(tmp_path / "worker", workers=1, storage=bucket)
    record = worker.submit("s3-native-test", {}, "alice")
    worker.executor.shutdown(wait=True)
    api_node = tasks.LocalTaskQueue(tmp_path / "api", workers=1, storage=bucket)
    api_node.store = worker.store
    assert api_node.artifact_path(record.id, "flow.vtk").read_text().startswith("vtk vtk")
    assert f"artifacts/{record.id}/flow.vtk" in fake.buckets["paramfabric"]


LIVE = os.getenv("PARAMFABRIC_TEST_S3_ENDPOINT")


@pytest.mark.skipif(not LIVE, reason="set PARAMFABRIC_TEST_S3_ENDPOINT (+ _ACCESS_KEY, _SECRET_KEY, _BUCKET) to test a real bucket")
def test_live_bucket_round_trip(tmp_path):
    client=NativeS3Client(LIVE, os.environ["PARAMFABRIC_TEST_S3_ACCESS_KEY"], os.environ["PARAMFABRIC_TEST_S3_SECRET_KEY"],
                            region=os.getenv("PARAMFABRIC_TEST_S3_REGION", "us-east-1"))
    storage = S3Storage(os.getenv("PARAMFABRIC_TEST_S3_BUCKET", "paramfabric-test"), client, prefix=f"pytest-{uuid.uuid4().hex[:8]}", create_bucket=True)
    source = tmp_path / "result.vtk"
    source.write_bytes(os.urandom(200_000))
    storage.put_file("t/result.vtk", source)
    assert storage.list("t") == ["t/result.vtk"]
    assert storage.fetch("t/result.vtk", tmp_path / "copy.vtk").read_bytes() == source.read_bytes()
    storage.delete_prefix("t")
    assert storage.list("t") == []
