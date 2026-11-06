from datetime import datetime, timedelta, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from paramfabric.core.schema import Fabric
from paramfabric.security.rbac import RBACPolicy
from paramfabric.security.auth import issue_access_token
from paramfabric.server.app import create_app


def test_reviewed_server_workflow_and_access_control(tmp_path):
    fabric_file = tmp_path / "params.yaml"
    fabric_file.write_text(
        "parameters:\n"
        "  - name: chord\n"
        "    value: 2\n"
        "    unit: m\n"
        "    tags: [geometry]\n"
        "  - name: span\n"
        "    value: '5 * chord'\n"
        "    unit: m\n"
        "    tags: [geometry]\n",
        encoding="utf-8",
    )
    secret = "local test signing key with at least thirty two bytes"
    app = create_app(
        fabric_file,
        database_url=f"sqlite:///{(tmp_path / 'server.db').as_posix()}",
        jwt_secret=secret,
        audit_key="separate audit key with at least thirty two bytes",
        audit_path=tmp_path / "audit.jsonl",
    )
    client = TestClient(app)
    assert client.get("/api/parameters").status_code == 401
    studio = client.get("/studio")
    assert studio.status_code == 200
    assert "script-src 'self' 'nonce-" in studio.headers["content-security-policy"]
    assert "/studio-static/d3.min.js" in studio.text
    assert "cdn.jsdelivr.net" not in studio.text
    assert "/studio-static/style.css" in studio.text
    assert "/studio-static/app.js" in studio.text
    assert client.get("/studio-static/three.module.min.js").status_code == 200
    assert client.get("/studio-static/three.core.min.js").status_code == 200
    assert client.get("/studio-static/style.css").status_code == 200
    assert client.get("/studio-static/app.js").status_code == 200
    assert client.get("/api/local/session").status_code == 404

    engineer = issue_access_token("engineer", ["engineer"], secret)
    approver = issue_access_token("approver", ["approver"], secret)
    admin = issue_access_token("admin", ["admin"], secret)
    proposal = Fabric.model_validate(
        {
            "parameters": [
                {"name": "chord", "value": 3, "unit": "m", "tags": ["geometry"]},
                {"name": "span", "value": "5 * chord", "unit": "m", "tags": ["geometry"]},
            ]
        }
    )
    created = client.post(
        "/api/change-requests",
        headers={"Authorization": f"Bearer {engineer}"},
        json={"proposed_fabric": proposal.model_dump(mode="json"), "rationale": "Updated chord"},
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]
    self_review = client.post(
        f"/api/change-requests/{request_id}/review",
        headers={"Authorization": f"Bearer {engineer}"},
        json={"decision": "approve"},
    )
    assert self_review.status_code == 403
    review = client.post(
        f"/api/change-requests/{request_id}/review",
        headers={"Authorization": f"Bearer {approver}"},
        json={"decision": "approve"},
    )
    assert review.status_code == 200, review.text
    merged = client.post(
        f"/api/change-requests/{request_id}/merge",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert merged.status_code == 200, merged.text
    assert merged.json() == {"branch": "main", "status": "merged"}
    parameters = client.get(
        "/api/parameters",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert parameters.status_code == 200
    assert parameters.json()["parameters"][0]["value"] == 3
    requests = client.get("/api/change-requests", headers={"Authorization": f"Bearer {admin}"})
    assert requests.status_code == 200
    archived_request = requests.json()[0]
    assert {change["name"] for change in archived_request["changes"]} == {"chord", "span"}
    assert archived_request["changes"][1]["before_resolved"] == 10
    audit=client.get("/api/audit", headers={"Authorization": f"Bearer {admin}"})
    assert audit.status_code == 200
    assert audit.json()["valid"] is True


def test_tag_scoped_reads_and_edits(tmp_path):
    fabric_file = tmp_path / "params.yaml"
    fabric_file.write_text(
        "parameters:\n"
        "  - name: thermal_limit\n"
        "    value: 350\n"
        "    unit: K\n"
        "    tags: [thermal.sizing]\n"
        "  - name: structural_limit\n"
        "    value: 500\n"
        "    unit: MPa\n"
        "    tags: [structures]\n",
        encoding="utf-8",
    )
    secret = "thermal scope test key with at least thirty two bytes"
    policy = RBACPolicy(
        {
            "thermal": {
                "read": ("thermal.*",),
                "edit": ("thermal.*",),
                "approve": ("thermal.*",),
                "export": ("thermal.*",),
            }
        }
    )
    app = create_app(
        fabric_file,
        database_url=f"sqlite:///{(tmp_path / 'scope.db').as_posix()}",
        jwt_secret=secret,
        audit_key="independent thermal audit key at least thirty-two bytes",
        audit_path=tmp_path / "scope-audit.jsonl",
        policy=policy,
    )
    client = TestClient(app)
    token = issue_access_token("thermal-engineer", ["thermal"], secret)
    headers = {"Authorization": f"Bearer {token}"}
    parameters = client.get("/api/parameters", headers=headers)
    assert parameters.status_code == 200
    assert [item["name"] for item in parameters.json()["parameters"]] == ["thermal_limit"]
    assert client.get("/api/export/markdown", headers=headers).status_code == 403

    thermal_proposal = Fabric.model_validate(
        {
            "parameters": [
                {"name": "thermal_limit", "value": 360, "unit": "K", "tags": ["thermal.sizing"]},
                {"name": "structural_limit", "value": 500, "unit": "MPa", "tags": ["structures"]},
            ]
        }
    )
    request = client.post(
        "/api/change-requests",
        headers=headers,
        json={"proposed_fabric": thermal_proposal.model_dump(mode="json"), "rationale": "thermal update"},
    )
    assert request.status_code == 201, request.text
    assert [item["name"] for item in request.json()["proposed_fabric"]["parameters"]] == ["thermal_limit"]
    assert "structural_limit" not in request.text
    listed = client.get("/api/change-requests", headers=headers)
    assert listed.status_code == 200
    assert "structural_limit" not in listed.text

    reviewer = issue_access_token("thermal-reviewer", ["thermal"], secret)
    review = client.post(
        f"/api/change-requests/{request.json()['id']}/review",
        headers={"Authorization": f"Bearer {reviewer}"},
        json={"decision": "approve"},
    )
    assert review.status_code == 200, review.text
    assert "proposed_fabric" not in review.json()
    assert "structural_limit" not in review.text

    proposal = Fabric.model_validate(
        {
            "parameters": [
                {"name": "thermal_limit", "value": 360, "unit": "K", "tags": ["thermal.sizing"]},
                {"name": "structural_limit", "value": 600, "unit": "MPa", "tags": ["structures"]},
            ]
        }
    )
    response = client.post(
        "/api/change-requests",
        headers=headers,
        json={"proposed_fabric": proposal.model_dump(mode="json"), "rationale": "test scope"},
    )
    assert response.status_code == 403


def test_server_enforces_signed_license_features(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    now = datetime.now(timezone.utc)
    license_token=jwt.encode(
        {
            "license_id": "governance-only",
            "seats": 5,
            "features": ["governance"],
            "iss": "paramfabric-licensing",
            "iat": now,
            "exp": now + timedelta(days=1),
        },
        private_pem,
        algorithm="RS256",
    )
    secret = "licensed server jwt key at least thirty two bytes"
    app = create_app(
        tmp_path / "missing.yaml",
        database_url=f"sqlite:///{(tmp_path / 'licensed.db').as_posix()}",
        jwt_secret=secret,
        audit_key="licensed audit key with at least thirty two bytes",
        audit_path=tmp_path / "licensed-audit.jsonl",
        license_token=license_token,
        license_public_key=public_pem.decode("utf-8"),
        require_license=True,
    )
    client = TestClient(app)
    token = issue_access_token("admin", ["admin"], secret)
    response = client.get("/studio", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_tampered_audit_ledger_blocks_mutation(tmp_path):
    fabric_file = tmp_path / "params.yaml"
    fabric_file.write_text(
        "parameters:\n  - name: length\n    value: 2\n    unit: m\n    tags: [geometry]\n",
        encoding="utf-8",
    )
    secret = "tamper test JWT signing secret with 32 bytes min"
    ledger_path = tmp_path / "audit.jsonl"
    app = create_app(
        fabric_file,
        database_url=f"sqlite:///{(tmp_path / 'tamper.db').as_posix()}",
        jwt_secret=secret,
        audit_key="tamper test audit secret with minimum 32 bytes",
        audit_path=ledger_path,
    )
    app.state.ledger.append({"actor": "system", "action": "seed"})
    record = ledger_path.read_text(encoding="utf-8").replace('"actor":"system"', '"actor":"forged"')
    ledger_path.write_text(record, encoding="utf-8")
    client = TestClient(app)
    token = issue_access_token("engineer", ["engineer"], secret)
    response = client.post(
        "/api/change-requests",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "proposed_fabric": {
                "parameters": [
                    {"name": "length", "value": 3, "unit": "m", "tags": ["geometry"]}
                ]
            },
            "rationale": "ledger safety test",
        },
    )
    assert response.status_code == 503
    assert client.get(
        "/api/change-requests",
        headers={"Authorization": f"Bearer {token}"},
    ).json() == []


def test_local_studio_import_edit_check_and_build(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fabric_file = tmp_path / "params.yaml"
    fabric_file.write_text(
        "parameters:\n"
        "  - name: width\n"
        "    value: 2\n"
        "    unit: m\n"
        "    tags: [geometry]\n"
        "    bindings: {cpp_header: generated/params.hpp, openscad: generated/params.scad}\n"
        "  - name: span\n"
        "    value: '5 * width'\n"
        "    unit: m\n"
        "    tags: [geometry]\n",
        encoding="utf-8",
    )
    app = create_app(
        fabric_file,
        database_url=f"sqlite:///{(tmp_path / 'local.db').as_posix()}",
        auth_required=False,
        audit_path=tmp_path / ".paramfabric" / "audit.jsonl",
    )
    client=TestClient(app, client=("127.0.0.1", 54321))

    session = client.get("/api/local/session")
    assert session.status_code == 200
    assert session.json()["role"] == "Local Admin"
    assert session.json()["token"] == app.state.local_session_token
    assert client.get("/api/parameters").status_code == 200
    assert client.get("/studio-static/style.css").status_code == 200
    assert client.get("/studio-static/app.js").status_code == 200

    python_import = client.post(
        "/api/import/preview",
        json={"filename": "constants.py", "content": "WING_SPAN = 12.5\n"},
    )
    assert python_import.status_code == 200, python_import.text
    assert python_import.json()["parameters"][0]["name"] == "wing_span"
    cpp_import = client.post(
        "/api/import/preview",
        json={
            "filename": "values.hpp",
            "content": "constexpr double AIR_DENSITY = 1.225; // sea-level density\n",
        },
    )
    assert cpp_import.status_code == 200, cpp_import.text
    assert cpp_import.json()["parameters"][0]["name"] == "air_density"

    fabric = client.get("/api/local/fabric").json()
    fabric["parameters"][0]["value"] = 3
    saved = client.post("/api/local/fabric", json=fabric)
    assert saved.status_code == 200, saved.text
    assert client.post(
        "/api/local/convert",
        json={"name": "width", "unit": "cm"},
    ).json()["value"] == 300
    assert client.post("/api/local/check").json()["status"] == "passed"

    built = client.post("/api/local/build", json=["cpp_header", "openscad"])
    assert built.status_code == 200, built.text
    assert built.json()["count"] == 2
    assert (tmp_path / "generated" / "params.hpp").is_file()
    assert (tmp_path / "generated" / "params.scad").is_file()

    remote_client = TestClient(app, client=("192.0.2.10", 54321))
    assert remote_client.get("/api/local/session").status_code == 403


def test_local_import_rejects_invalid_fabric_and_unknown_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(
        tmp_path / "missing.yaml",
        database_url=f"sqlite:///{(tmp_path / 'import.db').as_posix()}",
        auth_required=False,
        audit_path=tmp_path / ".paramfabric" / "audit.jsonl",
    )
    client=TestClient(app, client=("127.0.0.1", 54321))
    invalid = client.post(
        "/api/import/preview",
        json={"filename": "bad.json", "content": '{"parameters": ['},
    )
    assert invalid.status_code == 422
    unsupported = client.post(
        "/api/import/preview",
        json={"filename": "model.step", "content": "geometry"},
    )
    assert unsupported.status_code == 422


def test_local_build_rejects_binding_path_escape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fabric_file = tmp_path / "params.yaml"
    fabric_file.write_text(
        "parameters:\n"
        "  - name: length\n"
        "    value: 1\n"
        "    unit: m\n"
        "    bindings: {cpp_header: ../outside.hpp}\n",
        encoding="utf-8",
    )
    app = create_app(
        fabric_file,
        database_url=f"sqlite:///{(tmp_path / 'escape.db').as_posix()}",
        auth_required=False,
        audit_path=tmp_path / ".paramfabric" / "audit.jsonl",
    )
    client=TestClient(app, client=("127.0.0.1", 54321))
    response = client.post("/api/local/build", json=["cpp_header"])
    assert response.status_code == 422
    assert not (tmp_path.parent / "outside.hpp").exists()
