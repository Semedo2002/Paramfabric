"""Multi-tenant isolation (organizations, projects, database-enforced boundaries) and OIDC bearer tokens"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from paramfabric.core.schema import Fabric
from paramfabric.governance.baselines import create_baseline, list_baselines
from paramfabric.governance.pr_engine import ChangeRequestCreate, create_request, list_requests
from paramfabric.governance.store import BaselineRecord, create_database
from paramfabric.governance.tenancy import Tenant
from paramfabric.security.auth import AuthenticationError, issue_access_token
from paramfabric.server.app import create_app
from paramfabric.server.auth import OIDCConfig, OIDCValidator
from paramfabric.server.models import Organization, Project, SimulationTaskRecord, ensure_tenant

SECRET = "tenant test signing secret, thirty-two bytes+"
AUDIT = "tenant test audit key with thirty-two bytes++"
FABRIC = Fabric.model_validate({"parameters": [{"name": "chord", "value": 2, "unit": "m"}]})
CHANGED = Fabric.model_validate({"parameters": [{"name": "chord", "value": 3, "unit": "m"}]})


def test_store_queries_are_scoped_and_the_database_enforces_boundaries(tmp_path):
    _, factory = create_database(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    acme, globex = Tenant("acme", "wing"), Tenant("globex", "wing")
    with factory() as session:
        ensure_tenant(session, acme)
        ensure_tenant(session, globex)
        create_request(session, ChangeRequestCreate(proposed_fabric=CHANGED, rationale="acme"), "alice", FABRIC, tenant=acme)
        create_baseline(session, "v1", FABRIC, "alice", tenant=acme)
        create_baseline(session, "v1", CHANGED, "bob", tenant=globex)  # same tag, other tenant: independent
        assert [r.rationale for r in list_requests(session, acme)] == ["acme"] and list_requests(session, globex) == []
        assert {b.created_by for b in list_baselines(session, acme)} == {"alice"}
        assert {b.created_by for b in list_baselines(session, globex)} == {"bob"}
        # A row claiming another organization's project is rejected by the database (composite foreign key)
        session.add(BaselineRecord(organization_id="globex", project_id="does-not-exist", tag="x", fabric_hash="h", fabric_json="{}", created_by="m"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        session.add(SimulationTaskRecord(id="t1", organization_id="acme", project_id="secret-globex-project", kind="solve", owner="m", status="queued"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert session.get(Project, ("acme", "wing")) is not None and session.get(Organization, "default") is not None


def _server(tmp_path, monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PARAMFABRIC_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "params.yaml").write_text("parameters: [{name: chord, value: 2, unit: m}]\n", encoding="utf-8")
    app = create_app(tmp_path / "params.yaml", database_url=f"sqlite:///{(tmp_path / 's.db').as_posix()}",
                     jwt_secret=SECRET, audit_key=AUDIT, audit_path=tmp_path / "audit.jsonl")
    return app, TestClient(app)


def token(subject, role, organization="default"):
    return {"Authorization": f"Bearer {issue_access_token(subject, [role], SECRET, organization=organization)}"}


def test_api_isolates_organizations_and_projects(tmp_path, monkeypatch):
    app, client = _server(tmp_path, monkeypatch, PARAMFABRIC_AUTO_PROVISION_ORGS="true")
    acme_admin, globex_admin = token("ann", "admin", "acme"), token("gus", "admin", "globex")
    acme_engineer = token("eve", "engineer", "acme")
    assert client.get("/api/organization", headers=acme_admin).json()["projects"] == [{"id": "default", "name": "default", "description": ""}]
    created = client.post("/api/projects", json={"id": "wing", "name": "Wing"}, headers=acme_admin)
    assert created.status_code == 201
    assert client.post("/api/projects", json={"id": "wing", "name": "again"}, headers=acme_admin).status_code == 409
    assert client.post("/api/projects", json={"id": "x", "name": "x"}, headers=acme_engineer).status_code == 403
    wing = {"X-Paramfabric-Project": "wing"}
    proposal = {"proposed_fabric": CHANGED.model_dump(mode="json"), "rationale": "acme wing"}
    assert client.post("/api/change-requests", json=proposal, headers=acme_engineer | wing).status_code == 201
    # Same request listing from another project, another organization, or a project that is not theirs
    assert len(client.get("/api/change-requests", headers=acme_admin | wing).json()) == 1
    assert client.get("/api/change-requests", headers=acme_admin).json() == []
    assert client.get("/api/change-requests", headers=globex_admin).json() == []
    assert client.get("/api/change-requests", headers=globex_admin | wing).status_code == 404  # globex has no "wing"
    request_id=client.get("/api/change-requests", headers=acme_admin | wing).json()[0]["id"]
    assert client.post(f"/api/change-requests/{request_id}/merge", headers=globex_admin).status_code == 404
    # Tenant projects start empty; only the platform's default organization is seeded from the configured fabric
    assert client.get("/api/parameters", headers=acme_admin | wing).json()["parameters"] == []
    assert client.get("/api/parameters", headers=acme_admin).json()["parameters"] == []
    assert [p["name"] for p in client.get("/api/parameters", headers=token("root", "admin")).json()["parameters"]] == ["chord"]
    # workbench data lives in a per-tenant workspace
    client.post("/api/workbench/geometry/demo", headers=acme_admin | wing)
    assert (tmp_path / "data" / "tenants" / "acme" / "wing" / ".paramfabric").is_dir()
    assert client.get("/api/workbench", headers=globex_admin).json()["models"] == []
    assert client.get("/api/workbench/tasks", headers=globex_admin).json() == []
    assert client.get("/api/organization", headers={**acme_admin, "X-Paramfabric-Project": "Bad Name!"}).status_code == 400


def test_unprovisioned_organizations_are_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("PARAMFABRIC_AUTO_PROVISION_ORGS", raising=False)
    _, client = _server(tmp_path, monkeypatch)
    assert client.get("/api/parameters", headers=token("x", "admin", "initech")).status_code == 403
    assert client.get("/api/parameters", headers=token("x", "admin")).status_code == 200  # default organization exists


# OIDC

class IdentityProvider:
    """A local OIDC issuer: discovery document, JWKS (with rotation), and token minting"""

    def __init__(self):
        self.keys = {"k1": rsa.generate_private_key(public_exponent=65537, key_size=2048)}
        self.fetches = 0
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                provider.fetches += 1
                if self.path == "/.well-known/openid-configuration":
                    body = {"issuer": provider.issuer, "jwks_uri": provider.issuer + "/keys"}
                else:
                    body = {"keys": [{**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": kid, "use": "sig", "alg": "RS256"}
                                     for kid, key in provider.keys.items()]}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def token(self, kid="k1", audience="paramfabric-api", issuer=None, lifetime=300, **claims):
        now = int(time.time())
        payload = {"iss": issuer or self.issuer, "aud": audience, "sub": "00u1", "iat": now, "exp": now + lifetime, **claims}
        return jwt.encode(payload, self.keys[kid], algorithm="RS256", headers={"kid": kid})


@pytest.fixture()
def idp():
    provider = IdentityProvider()
    yield provider
    provider.server.shutdown()


def test_oidc_validation_claim_mapping_and_key_rotation(idp):
    config = OIDCConfig(issuer=idp.issuer, audience="paramfabric-api", roles_claim="groups",
                        role_map={"eng-design": "engineer", "quality": "approver"}, org_claim="tid", org_map={"9188040d": "acme"})
    validator = OIDCValidator(config)
    principal = validator.validate(idp.token(groups=["eng-design", "quality", "sales"], tid="9188040d", email="ann@acme.example", name="Ann"))
    assert (principal.roles, principal.organization, principal.email, principal.subject) == (["approver", "engineer"], "acme", "ann@acme.example", "00u1")
    assert validator.validate(idp.token(tid="Contoso Ltd")).organization == "contoso-ltd"  # unmapped: slugified
    assert validator.validate(idp.token(tid="x")).roles == ["viewer"]  # no mapped group: default role
    for bad in (idp.token(audience="other-app", tid="x"), idp.token(issuer="https://evil.example", tid="x"),
                idp.token(lifetime=-600, tid="x"), idp.token(tid=None)):
        with pytest.raises(AuthenticationError):
            validator.validate(bad)
    hs256 = jwt.encode({"iss": idp.issuer, "aud": "paramfabric-api", "sub": "x", "iat": 0, "exp": 2**31}, "secret" * 8, algorithm="HS256")
    with pytest.raises(AuthenticationError, match="not allowed"):
        validator.validate(hs256)
    idp.keys["k2"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # provider rotates keys
    assert validator.validate(idp.token(kid="k2", tid="x")).subject == "00u1"
    with pytest.raises(AuthenticationError, match="no signing key"):
        idp.keys["k3"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token_k3 = idp.token(kid="k3", tid="x")
        del idp.keys["k3"]
        validator.validate(token_k3)


def test_oidc_tokens_drive_the_api(tmp_path, monkeypatch, idp):
    _, client = _server(
        tmp_path, monkeypatch, PARAMFABRIC_OIDC_ISSUER=idp.issuer, PARAMFABRIC_OIDC_AUDIENCE="paramfabric-api",
        PARAMFABRIC_OIDC_ROLE_MAP=json.dumps({"paramfabric-admins": "admin"}), PARAMFABRIC_OIDC_ORG_CLAIM="hd",
        PARAMFABRIC_AUTO_PROVISION_ORGS="true",
    )
    headers = {"Authorization": f"Bearer {idp.token(groups=['paramfabric-admins'], hd='acme.example', email='ann@acme.example')}"}
    organization = client.get("/api/organization", headers=headers).json()
    assert organization["id"] == "acme-example" and organization["roles"] == ["admin"] and organization["subject"] == "00u1"
    assert client.post("/api/projects", json={"id": "rotor", "name": "Rotor"}, headers=headers).status_code == 201
    viewer={"Authorization": f"Bearer {idp.token(hd='acme.example')}"}
    assert client.post("/api/projects", json={"id": "x", "name": "x"}, headers=viewer).status_code == 403
    assert client.get("/api/parameters", headers={"Authorization": "Bearer " + idp.token(audience="wrong", hd="acme.example")}).status_code == 401
    # paramfabric's own HS256 tokens keep working next to OIDC
    assert client.get("/api/parameters", headers=token("svc", "viewer")).status_code == 200
