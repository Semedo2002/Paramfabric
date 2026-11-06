"""Webhook delivery: signatures, retries, and the four platform events end to end"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from paramfabric.server import tasks, webhooks
from paramfabric.server.webhooks import WebhookDispatcher, verify_signature

SECRET = "webhook signing secret"


class Receiver:
    """A real HTTP endpoint that records deliveries and can answer with scripted status codes"""

    def __init__(self, statuses=None):
        self.events, self.statuses = [], list(statuses or [])
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body=self.rfile.read(int(self.headers["Content-Length"]))
                receiver.events.append({"headers": dict(self.headers), "body": body, "json": json.loads(body)})
                status = receiver.statuses.pop(0) if receiver.statuses else 204
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hooks"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def receiver():
    instance = Receiver()
    yield instance
    instance.close()


@pytest.fixture()
def dispatcher(receiver):
    instance = WebhookDispatcher([receiver.url], SECRET, backoff=0.01)
    webhooks.configure(instance)
    yield instance
    instance.close()
    webhooks.configure(None)


def test_signed_envelope_and_receiver_verification(receiver, dispatcher):
    envelope = dispatcher.emit("simulation.completed", {"task_id": "t1"}, organization="acme", project="wing")
    assert dispatcher.flush()
    (event,) = receiver.events
    assert event["json"] == envelope and envelope["id"].startswith("evt_") and envelope["organization"] == "acme"
    headers = {k.lower(): v for k, v in event["headers"].items()}
    assert headers["x-paramfabric-event"] == "simulation.completed" and headers["x-paramfabric-delivery"] == envelope["id"]
    assert verify_signature(SECRET, headers["x-paramfabric-signature"], event["body"])
    assert not verify_signature("wrong secret", headers["x-paramfabric-signature"], event["body"])
    assert not verify_signature(SECRET, headers["x-paramfabric-signature"], event["body"] + b" ")
    assert not verify_signature(SECRET, headers["x-paramfabric-signature"], event["body"], now=time.time() + 3600)  # replay window
    assert dispatcher.emit("unknown.event", {}) is None  # only the documented events are sent


def test_retries_server_errors_but_not_client_errors():
    flaky = Receiver([503, 500, 200])
    rejecting = Receiver([400])
    try:
        dispatcher = WebhookDispatcher([flaky.url, rejecting.url], SECRET, backoff=0.01, attempts=5)
        envelope = dispatcher.emit("drift.detected", {"constants": []})
        assert dispatcher.flush()
        assert len(flaky.events) == 3 and len(rejecting.events) == 1  # 4xx is final
        assert {e["json"]["id"] for e in flaky.events} == {envelope["id"]}  # same delivery id on every attempt
        attempts = [int({k.lower(): v for k, v in e["headers"].items()}["x-paramfabric-attempt"]) for e in flaky.events]
        assert attempts == [1, 2, 3]
        assert [d.url for d in dispatcher.dead_letters] == [rejecting.url]
        dispatcher.close()
    finally:
        flaky.close()
        rejecting.close()


def test_unreachable_endpoint_ends_in_dead_letters():
    dispatcher = WebhookDispatcher(["http://127.0.0.1:9/nothing"], None, backoff=0.01, attempts=2, timeout=1)
    dispatcher.emit("parameter.merged", {})
    assert dispatcher.flush()
    (dead,) = dispatcher.dead_letters
    assert dead.attempts == 2 and dead.error and not dead.delivered
    dispatcher.close()


def test_environment_configuration(monkeypatch):
    monkeypatch.delenv("PARAMFABRIC_WEBHOOK_URL", raising=False)
    assert WebhookDispatcher.from_environment() is None
    monkeypatch.setenv("PARAMFABRIC_WEBHOOK_URL", "https://a.example/hook, https://b.example/hook")
    monkeypatch.setenv("PARAMFABRIC_WEBHOOK_SECRET", "s")
    instance = WebhookDispatcher.from_environment()
    assert instance.urls == ["https://a.example/hook", "https://b.example/hook"] and instance.secret == "s"


@tasks.handler("solve-webhook-test")
def _fake_solve(payload, context):
    if payload.get("fail"):
        raise RuntimeError("solver diverged")
    context.progress(1, {"p": 0.1})
    return {"engine": "openfoam", "converged": True, "iterations": 1, "summary": {"drag_coefficient": 1.1, "y_plus": {"mean": 4}}}


def test_simulation_events_from_task_execution(receiver, dispatcher, tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "SIMULATION_KINDS", {"solve-webhook-test"})
    queue = tasks.LocalTaskQueue(tmp_path, workers=1)
    good = queue.submit("solve-webhook-test", {"analysis": "cfd"}, "alice", organization="acme", project="wing")
    bad = queue.submit("solve-webhook-test", {"analysis": "cfd", "fail": True}, "alice", organization="acme", project="wing")
    queue.executor.shutdown(wait=True)
    assert dispatcher.flush()
    events = {e["json"]["data"]["task_id"]: e["json"] for e in receiver.events}
    assert events[good.id]["type"] == "simulation.completed" and events[good.id]["data"]["summary"] == {"drag_coefficient": 1.1}
    assert events[good.id]["organization"] == "acme" and "progress.json" in events[good.id]["data"]["artifacts"]
    assert events[bad.id]["type"] == "simulation.failed" and events[bad.id]["data"]["error"] == "solver diverged"


def test_parameter_merged_event_from_the_review_workflow(receiver, dispatcher, tmp_path):
    from fastapi.testclient import TestClient

    from paramfabric.core.schema import Fabric
    from paramfabric.security.auth import issue_access_token
    from paramfabric.server.app import create_app

    (tmp_path / "params.yaml").write_text("parameters: [{name: chord, value: 2, unit: m}, {name: span, value: '5 * chord', unit: m}]\n")
    secret = "test signing secret with at least thirty-two bytes"
    client = TestClient(create_app(tmp_path / "params.yaml", database_url=f"sqlite:///{(tmp_path / 's.db').as_posix()}", jwt_secret=secret,
                                   audit_key="audit key with at least thirty-two bytes!!", audit_path=tmp_path / "audit.jsonl"))
    auth = {name: {"Authorization": f"Bearer {issue_access_token(name, [name], secret)}"} for name in ("engineer", "approver", "admin")}
    proposal = Fabric.model_validate({"parameters": [{"name": "chord", "value": 3, "unit": "m"}, {"name": "span", "value": "5 * chord", "unit": "m"}]})
    request = client.post("/api/change-requests", json={"proposed_fabric": proposal.model_dump(mode="json"), "rationale": "stiffer"}, headers=auth["engineer"]).json()
    client.post(f"/api/change-requests/{request['id']}/review", json={"decision": "approve"}, headers=auth["approver"])
    client.post(f"/api/change-requests/{request['id']}/merge", headers=auth["admin"])
    assert dispatcher.flush()
    (event,) = [e["json"] for e in receiver.events if e["json"]["type"] == "parameter.merged"]
    data=event["data"]
    assert data["change_request"] == request["id"] and data["approvers"] == ["approver"] and data["merged_by"] == "admin"
    assert [c["name"] for c in data["changes"]] == ["chord", "span"] and data["impact"] == ["chord", "span"]


def test_drift_detected_once_per_new_drift(receiver, dispatcher, tmp_path, monkeypatch):
    from tests.test_native3d import _client

    client = _client(tmp_path, monkeypatch)
    fabric = client.get("/api/local/fabric").json()
    fabric["parameters"] = [{"name": "root_chord", "value": 2.0, "unit": "m"}]
    client.post("/api/local/fabric", json=fabric)
    (tmp_path / "config.py").write_text("ROOT_CHORD = 2.0\n")
    client.get("/api/local/sync/status")
    (tmp_path / "config.py").write_text("ROOT_CHORD = 2.5\n")  # source edited: drift
    for _ in range(3):  # the Studio polls; the event is sent once
        client.get("/api/local/sync/status")
    assert dispatcher.flush()
    drift = [e["json"] for e in receiver.events if e["json"]["type"] == "drift.detected"]
    assert len(drift) == 1
    (constant,) = drift[0]["data"]["constants"]
    assert constant["key"] == "config.py::ROOT_CHORD" and constant["source_value"] == 2.5 and constant["fabric_value"] == 2.0
