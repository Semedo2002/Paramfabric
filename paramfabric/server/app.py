"""Authenticated FastAPI parameter governance and synchronization server"""

from __future__ import annotations

import os
import ipaddress
import json
import secrets
from contextlib import asynccontextmanager
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import yaml

from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric, load_fabric
from paramfabric.generators.cpp import generate_cpp
from paramfabric.generators.docs import generate_latex, generate_markdown
from paramfabric.generators.python import generate_python
from paramfabric.generators.rust import generate_rust
from paramfabric.governance.baselines import baseline_diff, create_baseline, list_baselines
from paramfabric.governance.impact import blast_radius
from paramfabric.governance.baselines import get_baseline
from paramfabric.governance.pr_engine import (
    ChangeRequestCreate,
    ReviewCreate,
    add_review,
    create_request,
    current_fabric,
    fabric_hash,
    get_request,
    list_requests,
    merge_request,
    request_summary,
)
from paramfabric.governance.tenancy import DEFAULT_TENANT, SLUG, Tenant, current_tenant, tenant_scope
from paramfabric.governance.store import (
    BaselineRecord,
    ChangeRequestRecord,
    FabricStateRecord,
    create_database,
)
from paramfabric.licensing.validator import LicenseClaims, LicenseError, verify_license
from paramfabric.integrations.cad_nx import generate_nx_journal
from paramfabric.integrations.openscad import generate_openscad
from paramfabric.integrations.cad_solidworks import generate_solidworks_macro
from paramfabric.integrations.code import export_code
from paramfabric.integrations.simulation import export_solver
from paramfabric.security.audit import AuditLedger
from paramfabric.security.auth import (
    AuthenticationError,
    Principal,
    decode_access_token,
    issue_access_token,
)
from paramfabric.security.rbac import Action, RBACPolicy
from paramfabric.server import webhooks
from paramfabric.governance.approvals import sign_approval
from paramfabric.server.auth import OIDCConfig, OIDCValidator, is_oidc_token
from paramfabric.server.models import (
    Organization,
    Project,
    ensure_tenant,
    list_projects,
    organization_public_keys,
    register_public_key,
)
from paramfabric.security.signing import SigningError, load_public_key, resolve_signer
from paramfabric.server.tasks import queue_from_environment
from paramfabric.server.workbench import WorkbenchContext, register_workbench_routes
import paramfabric.solvers.manager  # noqa: F401 - registers mesh/solve task handlers
from paramfabric.studio.views import studio_response
from paramfabric.studio.importers import parse_import
from paramfabric.utils.git_tools import parameter_diff

_BEARER = HTTPBearer(auto_error=False)


class ImportPayload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=2_000_000)


class ConvertPayload(BaseModel):
    name: str
    unit: str


class ProjectCreate(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    name: str = Field(min_length=1, max_length=255)
    description: str = ""


PROJECT_HEADER = "X-Paramfabric-Project"


def create_app(
    fabric_path: str | Path = "params.yaml",
    *,
    database_url: str | None = None,
    jwt_secret: str | None = None,
    audit_key: str | None = None,
    policy: RBACPolicy | None = None,
    audit_path: str | Path | None = None,
    license_token: str | None = None,
    license_public_key: str | Path | None = None,
    require_license: bool | None = None,
    auth_required: bool | None = None,
) -> FastAPI:
    """Build the API with explicit signing keys and configurable storage"""
    auth_enabled=(
        os.getenv("PARAMFABRIC_AUTH_REQUIRED", "false").lower() == "true" or jwt_secret is not None
        if auth_required is None
        else auth_required
    )
    source_path = Path(os.getenv("PARAMFABRIC_CONFIG_PATH", str(fabric_path)))
    local_secrets_path = Path(
        os.getenv("PARAMFABRIC_LOCAL_SECRETS", str(Path.cwd() / ".paramfabric" / "local-secrets.json"))
    )
    if not auth_enabled:
        local_secrets_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            local_secrets = json.loads(local_secrets_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            local_secrets = {
                "jwt": secrets.token_urlsafe(48),
                "audit": secrets.token_urlsafe(48),
            }
            local_secrets_path.write_text(json.dumps(local_secrets), encoding="utf-8")
        if not isinstance(local_secrets, dict) or not isinstance(local_secrets.get("jwt"), str) or not isinstance(local_secrets.get("audit"), str):
            raise ValueError(f"invalid local signing-key file: {local_secrets_path}")
    else:
        local_secrets = {}
    signing_secret = jwt_secret or os.getenv("PARAMFABRIC_JWT_SECRET") or local_secrets.get("jwt")
    ledger_secret = audit_key or os.getenv("PARAMFABRIC_AUDIT_KEY") or local_secrets.get("audit")
    if signing_secret is None or len(signing_secret.encode("utf-8")) < 32:
        raise ValueError("set PARAMFABRIC_JWT_SECRET to a random value of at least 32 bytes")
    if ledger_secret is None or len(ledger_secret.encode("utf-8")) < 32:
        raise ValueError("set PARAMFABRIC_AUDIT_KEY to an independent random value of at least 32 bytes")

    db_url = database_url or os.getenv(
        "PARAMFABRIC_DATABASE_URL", "sqlite:///paramfabric-enterprise.db"
    )
    create_schema = os.getenv("PARAMFABRIC_CREATE_SCHEMA", "true").lower() == "true"
    engine, session_factory = create_database(db_url, create_schema=create_schema)
    default_audit_path = ".paramfabric/audit-ledger.jsonl" if not auth_enabled else "var/audit-ledger.jsonl"
    audit_file = Path(audit_path or os.getenv("PARAMFABRIC_AUDIT_PATH", default_audit_path))
    ledger = AuditLedger(audit_file, ledger_secret.encode("utf-8"))
    access_policy = policy or RBACPolicy.default()
    license_text = license_token or os.getenv("PARAMFABRIC_LICENSE")
    public_key_value = license_public_key or os.getenv("PARAMFABRIC_LICENSE_PUBLIC_KEY")
    required_license = (
        os.getenv("PARAMFABRIC_LICENSE_REQUIRED", "false").lower() == "true"
        if require_license is None
        else require_license
    )
    claims: LicenseClaims | None = None
    if required_license and (not license_text or not public_key_value):
        raise ValueError(
            "license-required deployment needs PARAMFABRIC_LICENSE and PARAMFABRIC_LICENSE_PUBLIC_KEY"
        )
    if license_text:
        if not public_key_value:
            raise ValueError("license token provided without a license public key")
        if "-----BEGIN " in public_key_value:
            key_bytes = public_key_value.encode("utf-8")
        else:
            key_bytes = Path(public_key_value).read_bytes()
        try:
            claims = verify_license(
                license_text,
                key_bytes,
                required_seats=int(os.getenv("PARAMFABRIC_REQUIRED_SEATS", "1")),
            )
        except LicenseError as exc:
            raise ValueError(str(exc)) from exc
    if required_license and claims is None:
        raise ValueError("a valid enterprise license is required")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        try:
            yield
        finally:
            engine.dispose()

    oidc_config = OIDCConfig.from_environment()
    oidc = OIDCValidator(oidc_config) if oidc_config else None
    auto_provision = os.getenv("PARAMFABRIC_AUTO_PROVISION_ORGS", "false").lower() == "true"

    def authenticate(token: str) -> Principal:
        """Identity-provider tokens (asymmetric, OIDC) or Paramfabric's own HS256 tokens"""
        if oidc is not None and is_oidc_token(token):
            return oidc.validate(token)
        return decode_access_token(token, signing_secret)

    app = FastAPI(title="Paramfabric Studio", version="0.6.0", lifespan=lifespan)
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.ledger = ledger
    app.state.policy = access_policy
    app.state.initial_fabric_path = source_path
    app.state.jwt_secret = signing_secret
    app.state.auth_required = auth_enabled
    app.state.local_session_token = (
        issue_access_token(
            "local-admin",
            ["admin"],
            signing_secret,
            lifetime=timedelta(hours=12),
        )
        if not auth_enabled
        else None
    )
    app.state.oidc = oidc
    app.state.license_claims = claims
    app.state.license_required = required_license
    app.mount(
        "/studio-static",
        StaticFiles(directory=Path(__file__).parents[1] / "studio" / "static"),
        name="studio-static",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        if not auth_enabled:
            client_host=request.client.host if request.client else ""
            try:
                if not ipaddress.ip_address(client_host).is_loopback:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "local mode accepts loopback connections only"},
                    )
            except ValueError:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "local mode accepts loopback connections only"},
                )
        # tenant scope for this request: the token's organization and the project header
        tenant = DEFAULT_TENANT
        if auth_enabled:
            organization="default"
            header=request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                try:
                    organization = authenticate(header[7:].strip()).organization
                except AuthenticationError:
                    pass  # the endpoint's dependency rejects the token with 401
            project = request.headers.get(PROJECT_HEADER, "default").strip().lower() or "default"
            if not SLUG.match(project):
                return JSONResponse(status_code=400, content={"detail": f"invalid {PROJECT_HEADER} header"})
            try:
                tenant = Tenant(organization, project)
            except ValueError:
                return JSONResponse(status_code=403, content={"detail": "token organization is not a valid organization id"})
        with tenant_scope(tenant):
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    def get_session() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def get_principal(
        credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    ) -> Principal:
        if not auth_enabled:
            return Principal(subject="local-admin", roles=["admin"], issuer="paramfabric-local")
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Bearer token required")
        try:
            principal = authenticate(credentials.credentials)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        verify_tenant(principal)
        return principal

    def verify_tenant(principal: Principal) -> None:
        """The request's tenant must be the caller's organization and an existing project in it"""
        tenant = current_tenant()
        if tenant.organization != principal.organization:
            raise HTTPException(status_code=403, detail="token organization does not match the request scope")
        with session_factory() as session:
            if session.get(Organization, tenant.organization) is None:
                if not auto_provision:
                    raise HTTPException(status_code=403, detail=f"organization {tenant.organization!r} is not provisioned")
                ensure_tenant(session, Tenant(tenant.organization, "default"), issuer=principal.issuer)
            if session.get(Project, (tenant.organization, tenant.project)) is None:
                if tenant.project != "default":
                    raise HTTPException(status_code=404, detail=f"project {tenant.project!r} not found in {tenant.organization!r}")
                ensure_tenant(session, Tenant(tenant.organization, "default"))

    def require(principal: Principal, action: Action, tags: list[str] | None = None) -> None:
        try:
            access_policy.require(principal, action, tags)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def require_feature(feature_name: str) -> None:
        if claims is None:
            return
        if feature_name not in claims.features:
            raise HTTPException(status_code=403, detail=f"license does not include {feature_name!r}")

    def load_initial() -> Fabric:
        try:
            return load_fabric(source_path)
        except FileNotFoundError:
            return Fabric()

    def current(session: Session, branch: str = "main") -> Fabric:
        tenant = current_tenant()
        # only the default project is seeded from the configured fabric file
        initial = load_initial() if tenant.is_default else Fabric()
        return current_fabric(session, initial, branch, tenant=tenant)

    with session_factory() as startup_session:
        current_fabric(startup_session, load_fabric(source_path) if source_path.is_file() else Fabric())

    def request_base(session: Session, record: ChangeRequestRecord) -> Fabric:
        if record.base_fabric is not None:
            return Fabric.model_validate_json(record.base_fabric)
        return current(session, record.target_branch)

    def changed_names(before: Fabric, after: Fabric) -> set[str]:
        previous, proposed = before.by_name(), after.by_name()
        return {
            name for name in previous.keys() | proposed.keys()
            if name not in previous or name not in proposed or previous[name] != proposed[name]
        }

    def authorize_changes(principal: Principal, before: Fabric, after: Fabric, action: Action) -> None:
        old, new = before.by_name(), after.by_name()
        for name in changed_names(before, after):
            parameter = new.get(name) or old[name]
            require(principal, action, parameter.tags)

    def visible_request_summary(
        record: ChangeRequestRecord,
        principal: Principal,
        session: Session,
    ) -> dict[str, object] | None:
        proposed=Fabric.model_validate_json(record.proposed_fabric)
        before = request_base(session, record)
        previous, upcoming = before.by_name(), proposed.by_name()
        names = changed_names(before, proposed)
        readable_names = {
            name
            for name in names
            if access_policy.allows(
                principal,
                "read",
                (upcoming.get(name) or previous[name]).tags,
            )
        }
        if names and readable_names != names:
            return None

        item = request_summary(record)
        proposed_document = proposed.model_dump(mode="json")
        proposed_document["parameters"] = [
            parameter.model_dump(mode="json")
            for parameter in proposed.parameters
            if access_policy.allows(principal, "read", parameter.tags)
        ]
        item["proposed_fabric"] = proposed_document

        graph = ParameterGraph(proposed)
        impacts = blast_radius(graph, names)
        visible_impacts = [
            impact
            for impact in impacts
            if access_policy.allows(
                principal,
                "read",
                (upcoming.get(str(impact["parameter"])) or previous[str(impact["parameter"])]).tags,
            )
        ]
        item["impact"] = visible_impacts
        readable_impacts = readable_names | {
            str(impact["parameter"]) for impact in visible_impacts
        }
        item["changes"] = [
            change
            for change in parameter_diff(before, proposed)
            if str(change["name"]) in readable_impacts
        ]
        item["can_review"] = all(
            access_policy.allows(
                principal,
                "approve",
                (upcoming.get(name) or previous[name]).tags,
            )
            for name in names
        )
        return item

    def append_event(actor: str, action: str, details: dict[str, Any]) -> None:
        try:
            ledger.append({"actor": actor, "action": action, **details})
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="Audit ledger is invalid; refusing mutation") from exc

    def ensure_audit_ready() -> None:
        if not ledger.verify()[0]:
            raise HTTPException(status_code=503, detail="Audit ledger verification failed; refusing mutation")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def require_local_mode() -> None:
        if auth_enabled:
            raise HTTPException(status_code=404, detail="local desktop endpoints are disabled")

    @app.get("/api/local/session")
    def local_session() -> dict[str, str]:
        require_local_mode()
        return {
            "mode": "local",
            "subject": "local-admin",
            "role": "Local Admin",
            "token": app.state.local_session_token,
        }

    @app.get("/api/local/fabric")
    def get_local_fabric(session: Session = Depends(get_session)) -> dict[str, object]:
        require_local_mode()
        return current(session).model_dump(mode="json")

    def persist_local_fabric(
        session: Session,
        fabric: Fabric,
        action: str = "fabric.saved",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Validate and store the local main branch, recording a ledger event"""
        try:
            ParameterGraph(fabric).evaluate()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        ensure_audit_ready()
        tenant = current_tenant()
        state = session.get(FabricStateRecord, (tenant.organization, tenant.project, "main"))
        if state is None:
            state=FabricStateRecord(
                organization_id=tenant.organization,
                project_id=tenant.project,
                branch="main",
                fabric_json=fabric.model_dump_json(),
                updated_by="local-admin",
            )
            session.add(state)
        else:
            state.fabric_json = fabric.model_dump_json()
            state.updated_by = "local-admin"
        session.commit()
        append_event("local-admin", action, {"parameter_count": len(fabric.parameters), **(details or {})})

    @app.post("/api/local/fabric")
    def save_local_fabric(
        fabric: Fabric,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        require_local_mode()
        persist_local_fabric(session, fabric)
        return {"status": "saved", "parameter_count": len(fabric.parameters)}

    def propose_change(session: Session, principal: Principal, fabric: Fabric, rationale: str) -> dict[str, Any]:
        """Enterprise path for workbench edits: open a reviewed change request"""
        require_feature("governance")
        before = current(session)
        authorize_changes(principal, before, fabric, "edit")
        ensure_audit_ready()
        try:
            record, _ = create_request(
                session, ChangeRequestCreate(proposed_fabric=fabric, rationale=rationale), principal.subject, before,
                tenant=current_tenant(),
            )
        except (ValueError, LookupError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        append_event(principal.subject, "change_request.created", {"request_id": record.id, "rationale": rationale})
        return {"change_request": record.id, "status": record.status}

    workspace = (
        Path(os.getenv("PARAMFABRIC_DATA_DIR", "var/workbench")) if auth_enabled else Path.cwd()
    ).resolve()

    def tenant_workspace() -> Path:
        """Each organization/project gets its own workspace (geometry, settings, cases)"""
        tenant = current_tenant()
        if tenant.is_default:
            return workspace
        return workspace / "tenants" / tenant.organization / tenant.project

    app.state.tenant_workspace = tenant_workspace

    def organization_signer(session: Session, organization: str) -> Any:
        """The organization's Ed25519 signer; its public key is registered for verifiers"""
        try:
            signer=resolve_signer(workspace, organization)
        except SigningError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if session.get(Organization, organization) is None:
            ensure_tenant(session, Tenant(organization, "default"))
        register_public_key(session, organization, signer)
        return signer

    app.state.organization_signer = organization_signer

    def public_key_list(session: Session, organization: str) -> list[dict[str, Any]]:
        return [{"key_id": k.key_id, "algorithm": k.algorithm, "public_key": k.public_key_pem, "purpose": k.purpose,
                 "created_at": k.created_at.isoformat() if k.created_at else None,
                 "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None}
                for k in organization_public_keys(session, organization)]

    @app.get("/api/public/organizations/{organization}/signing-keys")
    def get_public_signing_keys(organization: str, format: str = "json", session: Session = Depends(get_session)) -> Any:
        """Public keys of an organization: no account needed (third-party auditors, CI)"""
        keys = public_key_list(session, organization)
        if format == "pem":
            return Response("".join(k["public_key"] for k in keys), media_type="application/x-pem-file")
        return {"organization": organization, "keys": keys}

    @app.post("/api/public/organizations/{organization}/verify-report")
    async def post_verify_report(organization: str, request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
        """Verify an uploaded HTML/PDF report (raw request body) against the organization's public keys"""
        import tempfile

        from paramfabric.reporting.pdf_exporter import ReportError, verify_report

        body = await request.body()
        if not body or len(body) > 50_000_000:
            raise HTTPException(status_code=422, detail="send the report file as the request body (max 50 MB)")
        keys = [load_public_key(k["public_key"]) for k in public_key_list(session, organization)]
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / ("report.pdf" if body.startswith(b"%PDF") else "report.html")
            path.write_bytes(body)
            try:
                result = verify_report(path, keys)
            except ReportError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        result.pop("seal", None)
        return {"organization": organization, **result}

    @app.get("/api/signing-keys")
    def get_signing_keys(principal: Principal = Depends(get_principal), session: Session = Depends(get_session)) -> dict[str, Any]:
        require(principal, "read")
        organization = current_tenant().organization
        organization_signer(session, organization)  # make sure the active key is published
        return {"organization": organization, "keys": public_key_list(session, organization)}
    register_workbench_routes(
        app,
        WorkbenchContext(
            root=workspace,
            get_session=get_session,
            get_principal=get_principal,
            current=current,
            persist=persist_local_fabric,
            propose=propose_change,
            require=require,
            require_local_mode=require_local_mode,
            auth_enabled=auth_enabled,
            queue=queue_from_environment(workspace / ".paramfabric" / "tasks"),
            root_for=tenant_workspace,
            session_factory=session_factory,
        ),
    )

    @app.get("/api/workbench/report")
    def get_compliance_report(
        format: str = "html",
        download: bool = False,
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> Response:
        """One-click sealed compliance report (HTML or PDF) for the caller's organization and project"""
        import tempfile

        from paramfabric.reporting.pdf_exporter import collect_report_data, export_report, governance_summary
        from paramfabric.server.models import SimulationTaskRecord
        from sqlalchemy import select

        if format not in {"html", "pdf"}:
            raise HTTPException(status_code=422, detail="format must be html or pdf")
        require(principal, "export")
        fabric=current(session)
        for parameter in fabric.parameters:
            require(principal, "read", parameter.tags)
        tenant = current_tenant()
        task_ids = None
        if auth_enabled:  # shared task directory: only this tenant's runs
            task_ids = list(session.scalars(select(SimulationTaskRecord.id).where(
                SimulationTaskRecord.organization_id == tenant.organization, SimulationTaskRecord.project_id == tenant.project)))
        signer = principal.email or principal.subject
        data = collect_report_data(
            tenant_workspace(), fabric, organization=tenant.organization, project=tenant.project,
            governance=governance_summary(session, tenant), ledger=ledger, task_ids=task_ids, prepared_by=signer,
        )
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / f"compliance-report.{format}"
            seal_record = export_report(target, data, organization_signer(session, tenant.organization), signer=signer)
            content = target.read_bytes()
        append_event(principal.subject, "report.exported", {"format": format, "fabric_hash": data["fabric_hash"], "signature": seal_record["signature"], "key_id": seal_record["key_id"]})
        stamp = data["generated_at"][:10]
        filename = f"paramfabric-compliance-{tenant.project}-{stamp}.{format}"
        if download:
            # studio path: a JSON envelope, so no browser PDF viewer or download manager intercepts the bytes
            import base64

            return JSONResponse({"filename": filename, "format": format, "seal": seal_record,
                                 "content_base64": base64.b64encode(content).decode("ascii")})
        return Response(
            content, media_type="application/pdf" if format == "pdf" else "text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"', "X-Paramfabric-Seal": seal_record["signature"],
                     "X-Paramfabric-Key-Id": seal_record["key_id"]},
        )

    @app.post("/api/import/preview")
    def import_file_preview(
        payload: ImportPayload,
        _: Principal = Depends(get_principal),
    ) -> dict[str, object]:
        try:
            fabric = parse_import(payload.filename, payload.content)
        except (ValueError, SyntaxError, yaml.YAMLError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return fabric.model_dump(mode="json")

    @app.post("/api/local/check")
    def check_local_fabric(session: Session = Depends(get_session)) -> dict[str, object]:
        require_local_mode()
        graph = ParameterGraph(current(session))
        try:
            resolved = graph.evaluate()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "status": "passed",
            "parameters": len(graph.fabric.parameters),
            "resolved": {
                name: {"value": float(value.magnitude), "unit": str(value.units)}
                for name, value in resolved.items()
                if graph.parameters[name].type not in {"str", "bool"}
            },
        }

    @app.post("/api/local/convert")
    def convert_local_parameter(
        payload: ConvertPayload,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        require_local_mode()
        graph = ParameterGraph(current(session))
        try:
            graph.evaluate()
            converted=graph.value(payload.name).to(payload.unit)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"name": payload.name, "value": float(converted.magnitude), "unit": payload.unit}

    @app.post("/api/local/build")
    def build_local_fabric(
        targets: list[str] | None = None,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        require_local_mode()
        graph = ParameterGraph(current(session))
        try:
            graph.evaluate()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        outputs = {
            "cpp_header": generate_cpp(graph),
            "py_module": generate_python(graph),
            "rust": generate_rust(graph),
            "latex": generate_latex(graph),
            "markdown": generate_markdown(graph),
            "solidworks": generate_solidworks_macro(graph),
            "nx": generate_nx_journal(graph),
            "ansys": export_solver(graph, "ansys"),
            "openfoam": export_solver(graph, "openfoam"),
            "comsol": export_solver(graph, "comsol"),
            "openscad": generate_openscad(graph),
        }
        configured = {
            (kind, relative_path)
            for parameter in graph.fabric.parameters
            for kind, relative_path in parameter.bindings.items()
        }
        selected = set(targets or [])
        unknown = selected - outputs.keys()
        if unknown:
            raise HTTPException(status_code=422, detail=f"unsupported build target(s): {', '.join(sorted(unknown))}")
        if selected:
            configured = {(kind, path) for kind, path in configured if kind in selected}
        root = Path.cwd().resolve()
        built: list[dict[str, str]] = []
        for kind, relative_path in sorted(configured):
            destination = (root / relative_path).resolve()
            if not destination.is_relative_to(root):
                raise HTTPException(status_code=422, detail=f"binding path escapes the project folder: {relative_path}")
            if kind not in outputs:
                raise HTTPException(status_code=422, detail=f"unsupported binding type: {kind}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(outputs[kind], encoding="utf-8")
            built.append({"kind": kind, "path": str(destination.relative_to(root))})
        return {"status": "built", "files": built, "count": len(built)}

    @app.get("/studio", include_in_schema=False)
    def studio() -> Any:
        require_feature("studio")
        return studio_response()

    @app.get("/api/parameters")
    def get_parameters(
        branch: str = "main",
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        fabric = current(session, branch)
        graph = ParameterGraph(fabric)
        try:
            values = graph.evaluate()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        allowed = [
            parameter
            for parameter in fabric.parameters
            if access_policy.allows(principal, "read", parameter.tags)
        ]
        return {
            "branch": branch,
            "parameters": [parameter.model_dump(mode="json") for parameter in allowed],
            "resolved": {
                name: float(value.magnitude)
                for name, value in values.items()
                if name in {parameter.name for parameter in allowed}
                and fabric.by_name()[name].type not in {"str", "bool"}
            },
        }

    @app.get("/api/graph")
    def get_graph(
        branch: str = "main",
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> dict[str, list[dict[str, object]]]:
        require_feature("studio")
        fabric = current(session, branch)
        visible=[
            parameter
            for parameter in fabric.parameters
            if access_policy.allows(principal, "read", parameter.tags)
        ]
        graph = ParameterGraph(fabric)
        try:
            values = graph.evaluate()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "nodes": [
                {
                    "id": parameter.name,
                    "value": parameter.value
                    if parameter.type in {"str", "bool"}
                    else float(values[parameter.name].magnitude),
                    "unit": parameter.unit,
                    "description": parameter.description,
                    "tags": parameter.tags,
                    "bindings": parameter.bindings,
                }
                for parameter in visible
            ],
            "links": [
                {"source": source, "target": target}
                for source, target in graph.graph.edges
                if source in {parameter.name for parameter in visible}
                and target in {parameter.name for parameter in visible}
            ],
        }

    @app.post("/api/change-requests", status_code=201)
    def post_change_request(
        payload: ChangeRequestCreate,
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        require_feature("governance")
        before = current(session, payload.target_branch)
        authorize_changes(principal, before, payload.proposed_fabric, "edit")
        ensure_audit_ready()
        try:
            record, _ = create_request(session, payload, principal.subject, before, tenant=current_tenant())
        except (ValueError, LookupError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        append_event(
            principal.subject,
            "change_request.created",
            {"request_id": record.id, "rationale": payload.rationale},
        )
        return visible_request_summary(record, principal, session) or {
            "id": record.id,
            "author": record.author,
            "rationale": record.rationale,
            "target_branch": record.target_branch,
            "base_revision": record.base_revision,
            "status": record.status,
            "created_at": record.created_at.isoformat(),
        }

    @app.get("/api/change-requests")
    def get_change_requests(
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> list[dict[str, object]]:
        require_feature("governance")
        result: list[dict[str, object]] = []
        for record in list_requests(session, current_tenant()):
            item = visible_request_summary(record, principal, session)
            if item is not None:
                result.append(item)
        return result

    @app.post("/api/change-requests/{request_id}/review")
    def post_review(
        request_id: str,
        payload: ReviewCreate,
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        require_feature("governance")
        record = get_request(session, request_id, current_tenant())
        if record is None:
            raise HTTPException(status_code=404, detail="change request not found")
        try:
            proposed=Fabric.model_validate_json(record.proposed_fabric)
            before = request_base(session, record)
            authorize_changes(principal, before, proposed, "read")
            authorize_changes(principal, before, proposed, "approve")
            ensure_audit_ready()
            result = add_review(session, request_id, principal.subject, payload, tenant=current_tenant())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        append_event(
            principal.subject,
            "change_request.reviewed",
            {"request_id": request_id, "decision": payload.decision, "comment": payload.comment},
        )
        return {
            "id": result.id,
            "status": result.status,
            "reviews": [
                {
                    "reviewer": review.reviewer,
                    "decision": review.decision,
                    "comment": review.comment,
                    "created_at": review.created_at.isoformat(),
                }
                for review in result.reviews
            ],
        }

    @app.post("/api/change-requests/{request_id}/merge")
    def post_merge(
        request_id: str,
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        require_feature("governance")
        record = get_request(session, request_id, current_tenant())
        if record is None:
            raise HTTPException(status_code=404, detail="change request not found")
        proposed=Fabric.model_validate_json(record.proposed_fabric)
        before = request_base(session, record)
        authorize_changes(principal, before, proposed, "read")
        authorize_changes(principal, before, proposed, "merge")
        ensure_audit_ready()
        try:
            merge_request(session, request_id, principal.subject, tenant=current_tenant())
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        append_event(principal.subject, "change_request.merged", {"request_id": request_id})
        names = sorted(changed_names(before, proposed))
        webhooks.emit("parameter.merged", {
            "change_request": request_id, "branch": record.target_branch, "merged_by": principal.subject,
            "author": record.author, "rationale": record.rationale, "fabric_hash": fabric_hash(proposed),
            "approvers": [review.reviewer for review in record.reviews if review.decision == "approve"],
            "changes": parameter_diff(before, proposed),
            "impact": [impact["parameter"] for impact in blast_radius(ParameterGraph(proposed), set(names))],
        })
        return {"branch": record.target_branch, "status": "merged"}

    @app.get("/api/change-requests/{request_id}/approval")
    def get_approval(
        request_id: str,
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        """Signed approval record of a merged request, for the CI gate's `paramfabric.lock`"""
        require_feature("governance")
        require(principal, "read")
        record = get_request(session, request_id, current_tenant())
        if record is None:
            raise HTTPException(status_code=404, detail="change request not found")
        if record.status != "merged":
            raise HTTPException(status_code=409, detail="only merged change requests have an approval record")
        proposed=Fabric.model_validate_json(record.proposed_fabric)
        return sign_approval({
            "change_request": record.id, "organization": record.organization_id, "project": record.project_id,
            "branch": record.target_branch, "fabric_hash": fabric_hash(proposed), "author": record.author,
            "approvers": [review.reviewer for review in record.reviews if review.decision == "approve"],
            "rationale": record.rationale,
        }, organization_signer(session, record.organization_id))

    @app.get("/api/organization")
    def get_organization(principal: Principal = Depends(get_principal), session: Session = Depends(get_session)) -> dict[str, object]:
        require(principal, "read")
        tenant = current_tenant()
        organization = session.get(Organization, tenant.organization)
        return {
            "id": tenant.organization, "name": organization.name if organization else tenant.organization,
            "project": tenant.project, "subject": principal.subject, "roles": principal.roles,
            "projects": [{"id": p.id, "name": p.name, "description": p.description} for p in list_projects(session, tenant.organization)],
        }

    @app.get("/api/projects")
    def get_projects(principal: Principal = Depends(get_principal), session: Session = Depends(get_session)) -> list[dict[str, object]]:
        require(principal, "read")
        return [{"id": p.id, "name": p.name, "description": p.description, "created_by": p.created_by}
                for p in list_projects(session, current_tenant().organization)]

    @app.post("/api/projects", status_code=201)
    def post_project(payload: ProjectCreate, principal: Principal = Depends(get_principal), session: Session = Depends(get_session)) -> dict[str, object]:
        require(principal, "admin")
        organization = current_tenant().organization
        if session.get(Project, (organization, payload.id)) is not None:
            raise HTTPException(status_code=409, detail=f"project {payload.id!r} already exists")
        session.add(Project(organization_id=organization, id=payload.id, name=payload.name,
                            description=payload.description, created_by=principal.subject))
        session.commit()
        append_event(principal.subject, "project.created", {"organization": organization, "project": payload.id})
        return {"id": payload.id, "name": payload.name, "organization": organization}

    @app.post("/api/baselines", status_code=201)
    def post_baseline(
        tag: str,
        branch: str = "main",
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        require_feature("governance")
        require(principal, "admin")
        ensure_audit_ready()
        try:
            record = create_baseline(session, tag, current(session, branch), principal.subject, tenant=current_tenant())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        append_event(principal.subject, "baseline.created", {"tag": tag, "hash": record.fabric_hash})
        return {
            "tag": record.tag,
            "hash": record.fabric_hash,
            "created_by": record.created_by,
            "created_at": record.created_at.isoformat(),
        }

    @app.get("/api/baselines")
    def get_baselines(
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> list[dict[str, object]]:
        require_feature("governance")
        require(principal, "read")
        return [
            {
                "tag": item.tag,
                "hash": item.fabric_hash,
                "created_by": item.created_by,
                "created_at": item.created_at.isoformat(),
            }
            for item in list_baselines(session, current_tenant())
        ]

    @app.get("/api/baselines/{first}/diff/{second}")
    def get_baseline_diff(
        first: str,
        second: str,
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> list[dict[str, object]]:
        require_feature("governance")
        try:
            changes = baseline_diff(session, first, second, current_tenant())
            left = get_baseline(session, first, current_tenant())
            right = get_baseline(session, second, current_tenant())
            if left is None or right is None:
                raise LookupError("one or both baseline tags were not found")
            old, new = Fabric.model_validate_json(left.fabric_json).by_name(), Fabric.model_validate_json(right.fabric_json).by_name()
            return [
                change
                for change in changes
                if access_policy.allows(
                    principal,
                    "read",
                    (new.get(str(change["name"])) or old[str(change["name"])]).tags,
                )
            ]
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/audit")
    def get_audit(
        principal: Principal = Depends(get_principal),
    ) -> dict[str, object]:
        require_feature("governance")
        require(principal, "admin")
        valid, head = ledger.verify()
        if not valid:
            raise HTTPException(status_code=503, detail="Audit ledger verification failed")
        return {"valid": True, "head": head, "records": ledger.records()}

    @app.get("/api/export/{kind}")
    def get_export(
        kind: str,
        branch: str = "main",
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        feature_name={
            "solidworks": "cad",
            "nx": "cad",
            "ansys": "simulation",
            "openfoam": "simulation",
            "comsol": "simulation",
        }.get(kind)
        if feature_name:
            require_feature(feature_name)
        fabric = current(session, branch)
        if not fabric.parameters:
            require(principal, "export")
        for parameter in fabric.parameters:
            require(principal, "read", parameter.tags)
            require(principal, "export", parameter.tags)
        try:
            graph = ParameterGraph(fabric)
            graph.evaluate()
            if kind in {"solidworks", "nx"}:
                content = generate_solidworks_macro(graph) if kind == "solidworks" else generate_nx_journal(graph)
            elif kind in {"ansys", "openfoam", "comsol"}:
                content = export_solver(graph, kind)
            elif kind == "openscad":
                content = generate_openscad(graph)
            else:
                content = export_code(graph, kind)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"kind": kind, "content": content}

    return app
