"""Workbench API: CAD, parametric scene, meshing, and simulation tasks.

`/api/workbench` is served in both deployment modes:

* **local desktop** - the Local Admin principal; fabric changes are saved directly;
  the workspace is the current folder; tasks run on an in-process thread pool.
* **enterprise** - bearer tokens and RBAC (`read` to view, `edit` to change
  geometry/features, `simulate` to mesh/solve); fabric changes become change
  requests; the workspace is `PARAMFABRIC_DATA_DIR`; tasks run on the configured
  queue (`PARAMFABRIC_REDIS_URL` for remote `paramfabric worker` nodes).

`/api/local/sync` (two-way code sync against the developer's source tree) is
local-only: an enterprise server does not own engineers' working copies.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from datetime import datetime, timezone
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import paramfabric
from paramfabric.auditors.sync import SyncEngine
from paramfabric.core.diagnostics import diagnose
from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric, Parameter
from paramfabric.core.units import Q_
from paramfabric.geometry import (
    GeometryImportError,
    GeometryStore,
    associate_dimensions,
    create_feature,
    resolve_scene,
)
from paramfabric.geometry import selections
from paramfabric.geometry.model import AXES
from paramfabric.geometry.samples import demo_bracket_step
from paramfabric.geometry.scene import si_values, transformed_triangles
from paramfabric.geometry.stl import write_stl
from paramfabric.security.auth import Principal
from paramfabric.server.tasks import TERMINAL, TaskQueue
from paramfabric.server.workspace_sync import WorkspaceSyncError, upload_workspace
from paramfabric.solvers import calculix, manager, openfoam
from paramfabric.solvers.physics import ANALYSES, BOUNDARY_CONDITIONS, PHYSICS, PhysicsError, ensure_parameters, resolve_physics
from paramfabric.solvers.runner import SolverRunner
from paramfabric.governance.tenancy import current_tenant
from paramfabric.server import webhooks
from paramfabric.server.models import SimulationTaskRecord


class _ExportContext:
    """Minimal task context for synchronous case export (log only, never cancelled)"""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def log(self, line: str) -> None:
        self.lines.append(str(line))

    def check(self) -> None:
        pass


class GeometryUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(max_length=70_000_000)
    units: str | None = None
    associate: bool = True


class BindPayload(BaseModel):
    body: str
    axis: str
    parameter: str | None = None


class FeaturePayload(BaseModel):
    type: str
    options: dict[str, Any] = Field(default_factory=dict)


class PhysicsPayload(BaseModel):
    analysis: str


class RunPayload(BaseModel):
    engine: str = "auto"


class MeshPayload(BaseModel):
    mesher: str = "auto"
    wait: bool = False


class CasePayload(BaseModel):
    engine: str


class SettingsPayload(BaseModel):
    values: dict[str, Any]


class SyncKeys(BaseModel):
    keys: list[str] | None = None


class ResolvePayload(BaseModel):
    parameter: str
    strategy: str


@dataclass
class WorkbenchContext:
    root: Path
    get_session: Callable[[], Generator[Session, None, None]]
    get_principal: Callable[..., Principal]
    current: Callable[[Session], Fabric]
    persist: Callable[..., None]
    propose: Callable[[Session, Principal, Fabric, str], dict[str, Any]]
    require: Callable[..., None]
    require_local_mode: Callable[[], None]
    auth_enabled: bool
    queue: TaskQueue
    root_for: Callable[[], Path] | None = None  # per-tenant workspace (multi-tenant servers)
    session_factory: Any = None  # for the durable task registry


class _TenantScoped:
    """Proxy to one object per tenant workspace (GeometryStore, SolverRunner)"""

    def __init__(self, factory: Callable[[Path], Any], root: Callable[[], Path]) -> None:
        self._factory = factory
        self._root = root
        self._instances: dict[Path, Any] = {}

    def current(self) -> Any:
        root=self._root()
        instance = self._instances.get(root)
        if instance is None:
            root.mkdir(parents=True, exist_ok=True)
            instance = self._instances[root] = self._factory(root)
        return instance

    def __getattr__(self, name: str) -> Any:
        return getattr(self.current(), name)


DRIFT_STATES = {"drift", "structural_drift", "unit_mismatch", "conflict"}


EDITABLE_SETTINGS = {"mesh_options", "boundary_conditions", "solver_options", "structural_members"}


class NamedSelectionPayload(BaseModel):
    name: str
    kind: str
    items: list[dict[str, Any]]


class NamedSelectionPatch(BaseModel):
    name: str | None = None
    add_items: list[dict[str, Any]] | None = None


class RenamePayload(BaseModel):
    kind: str  # body | model | feature
    id: str
    name: str


class MemberPayload(BaseModel):
    type: str
    body: str
    edges: list[dict[str, Any]] | None = None
    selector: dict[str, Any] | None = None
    section: dict[str, Any] | None = None
    orientation: list[float] | None = None
    thickness: float | None = None
    offset: float = 0.0
    name: str | None = None


def register_workbench_routes(app: FastAPI, context: WorkbenchContext) -> None:
    root = context.root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    def tenant_root() -> Path:
        return context.root_for() if context.root_for is not None else root

    store = _TenantScoped(GeometryStore, tenant_root)
    runner = _TenantScoped(SolverRunner, tenant_root)
    queue = context.queue
    app.state.geometry_store = store
    app.state.task_queue = queue
    get_session = context.get_session
    principal_dependency = Depends(context.get_principal)
    api = APIRouter(prefix="/api/workbench", tags=["workbench"])

    def evaluate(fabric: Fabric) -> dict[str, Any]:
        report = diagnose(fabric)
        settings = store.settings()
        models = store.models()
        scene = resolve_scene(models, settings["features"], si_values(report["quantities"])) if models else None
        if scene is not None and models:
            from paramfabric.geometry import exact

            exact.attach_tessellations(store, models, settings, report["quantities"], scene)
        return {"report": report, "scene": scene, "settings": settings, "models": models}

    def require_valid(fabric: Fabric) -> dict[str, Any]:
        state = evaluate(fabric)
        if not state["report"]["ok"]:
            raise HTTPException(status_code=422, detail=state["report"]["issues"][0]["message"])
        return state

    def public_report(report: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in report.items() if key != "quantities"}

    def merge_parameters(fabric: Fabric, parameters: list[Parameter]) -> Fabric:
        return Fabric(version=fabric.version, parameters=[*fabric.parameters, *parameters])

    def commit(session: Session, principal: Principal, fabric: Fabric, action: str, details: dict[str, Any], rationale: str) -> dict[str, Any]:
        """Save locally, or open a change request in enterprise mode"""
        if context.auth_enabled:
            return context.propose(session, principal, fabric, rationale)
        context.persist(session, fabric, action, details)
        return {"saved": True}

    def latest_solve(principal: Principal) -> dict[str, Any] | None:
        owner = None if not context.auth_enabled else principal.subject
        tenant = current_tenant()
        records = [record for record in queue.list(owner, organization=tenant.organization, project=tenant.project) if record.kind == "solve"]
        return job_snapshot(records[0].id, include_result=False) if records else None

    def workbench_state(fabric: Fabric, principal: Principal) -> dict[str, Any]:
        state = evaluate(fabric)
        settings = state["settings"]
        analysis = settings["analysis"]
        return {
            "mode": "enterprise" if context.auth_enabled else "local",
            "settings": settings,
            "models": [model.summary() for model in state["models"]],
            "scene": state["scene"],
            "diagnostics": public_report(state["report"]),
            "engines": manager.engine_catalog(runner),
            "physics": {
                "analysis": analysis,
                "analyses": list(ANALYSES),
                "parameters": [spec.name for spec in PHYSICS[analysis]] + ["mesh_fineness_ratio"],
                "boundary_conditions": BOUNDARY_CONDITIONS[analysis],
            },
            "mesh": getattr(app.state, "last_mesh", None),
            "latest_job": latest_solve(principal),
        }

    @api.get("")
    def get_workbench(principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "read")
        return workbench_state(context.current(session), principal)

    @api.post("/evaluate")
    def post_evaluate(fabric: Fabric, principal: Principal = principal_dependency) -> dict[str, Any]:
        context.require(principal, "read")
        state = evaluate(fabric)
        return {"diagnostics": public_report(state["report"]), "scene": state["scene"]}

    # geometry
    @api.get("/geometry/{model_id}")
    def get_geometry(model_id: str, principal: Principal = principal_dependency) -> dict[str, Any]:
        context.require(principal, "read")
        try:
            return store.get(model_id).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="geometry model not found") from exc

    def add_model(session: Session, principal: Principal, filename: str, data: bytes, units: str | None, associate: bool) -> dict[str, Any]:
        context.require(principal, "edit")
        from paramfabric.geometry import exact

        try:
            model = exact.import_cad(filename, data, units)
        except GeometryImportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        fabric = context.current(session)
        created = associate_dimensions(model, fabric) if associate else []
        outcome: dict[str, Any] = {}
        if created:
            outcome = commit(
                session, principal, merge_parameters(fabric, created), "geometry.imported",
                {"file": filename, "parameters": [parameter.name for parameter in created]},
                f"Associativity parameters for imported CAD {filename}",
            )
        exact.store_source(store, model, data)
        store.save(model)
        return {
            "model": model.summary(),
            "created_parameters": [parameter.model_dump(mode="json") for parameter in created],
            "governance": outcome,
            "workbench": workbench_state(context.current(session), principal),
        }

    @api.post("/geometry/import")
    def post_geometry_import(payload: GeometryUpload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        try:
            data = base64.b64decode(payload.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail="file content must be base64 encoded") from exc
        return add_model(session, principal, Path(payload.filename).name, data, payload.units, payload.associate)

    @api.post("/geometry/demo")
    def post_geometry_demo(principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        return add_model(session, principal, "cantilever_bracket.step", demo_bracket_step().encode(), None, True)

    @api.delete("/geometry/{model_id}")
    def delete_geometry(model_id: str, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        try:
            store.delete(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="geometry model not found") from exc
        return workbench_state(context.current(session), principal)

    @api.post("/geometry/bind")
    def post_bind(payload: BindPayload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        if payload.axis not in AXES:
            raise HTTPException(status_code=422, detail="axis must be x, y, or z")
        model_id, _, body_id = payload.body.partition("/")
        try:
            model = store.get(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="geometry model not found") from exc
        body=next((item for item in model.bodies if item.id == body_id), None)
        if body is None:
            raise HTTPException(status_code=404, detail="body not found")
        if payload.parameter:
            quantity = diagnose(context.current(session))["quantities"].get(payload.parameter)
            if quantity is None:
                raise HTTPException(status_code=422, detail=f"parameter {payload.parameter!r} is missing or invalid")
            if quantity.dimensionality != Q_(1, "m").dimensionality:
                raise HTTPException(
                    status_code=422,
                    detail=f"{payload.parameter} has units of {quantity.units}; only lengths can drive body dimensions",
                )
            body.dimension_parameters[payload.axis] = payload.parameter
        else:
            body.dimension_parameters.pop(payload.axis, None)
        store.save(model)
        return workbench_state(context.current(session), principal)

    @api.get("/geometry-export.{kind}", response_class=PlainTextResponse)
    def get_export(kind: str, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> Any:
        context.require(principal, "export")
        state = require_valid(context.current(session))
        if not state["models"]:
            raise HTTPException(status_code=404, detail="no geometry to export")
        if kind == "stl":
            parts = []
            for entry in [*state["scene"]["bodies"], *state["scene"]["derived"]]:
                if entry["role"] == "solid":
                    triangles = entry.get("tessellation", {}).get("triangles") or transformed_triangles(state["models"], entry)
                    parts.append(write_stl(entry["name"].replace(" ", "_"), triangles, 1e3))
            return PlainTextResponse(
                "".join(parts), headers={"Content-Disposition": 'attachment; filename="paramfabric_morphed_mm.stl"'},
            )
        if kind in {"step", "iges"}:
            from paramfabric.geometry import exact

            try:
                text = exact.export_workspace(store, state["models"], state["settings"], state["report"]["quantities"], kind)
            except exact.ExactKernelUnavailable as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            suffix="step" if kind == "step" else "igs"
            return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="paramfabric_model.{suffix}"'})
        raise HTTPException(status_code=404, detail="export kind must be stl, step, or iges")

    # features, settings & physics
    @api.post("/features")
    def post_feature(payload: FeaturePayload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        settings = store.settings()
        fabric = context.current(session)
        try:
            feature, parameters = create_feature(store.models(), settings["features"], fabric, payload.type, payload.options)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc).strip("'\"")) from exc
        outcome: dict[str, Any] = {}
        if parameters:
            outcome = commit(
                session, principal, merge_parameters(fabric, parameters), "feature.created",
                {"feature": feature["type"], "parameters": [parameter.name for parameter in parameters]},
                f"Parameters for new {feature['type']} feature",
            )
        from paramfabric.geometry import exact

        try:
            exact.annotate_feature(store, store.models(), settings["features"], feature, diagnose(fabric)["quantities"])
        except (exact.RegenerationError, exact.ExactKernelUnavailable):
            pass  # the geometric selector still works; persistent naming is an upgrade
        settings["features"].append(feature)
        store.save_settings(settings)
        return {"feature": feature, "governance": outcome, "workbench": workbench_state(context.current(session), principal)}

    @api.delete("/features/{feature_id}")
    def delete_feature(feature_id: str, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        settings = store.settings()
        remaining=[feature for feature in settings["features"] if feature.get("id") != feature_id]
        if len(remaining) == len(settings["features"]):
            raise HTTPException(status_code=404, detail="feature not found")
        settings["features"] = remaining
        settings["boundary_conditions"] = [bc for bc in settings.get("boundary_conditions", []) if bc.get("feature") != feature_id]
        store.save_settings(settings)
        return workbench_state(context.current(session), principal)

    # named selections and renaming
    @api.post("/named-selections")
    def post_named_selection(payload: NamedSelectionPayload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        settings = store.settings()
        fabric = context.current(session)
        try:
            selection = selections.create(settings, payload.name, payload.kind, payload.items)
        except selections.SelectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        selections.annotate(store, store.models(), settings, selection["kind"], selection["items"], diagnose(fabric)["quantities"])
        store.save_settings(settings)
        return {"selection": selection, "workbench": workbench_state(fabric, principal)}

    @api.patch("/named-selections/{selection_id}")
    def patch_named_selection(selection_id: str, payload: NamedSelectionPatch, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        settings = store.settings()
        fabric = context.current(session)
        try:
            if payload.name is not None:
                selections.rename(settings, selection_id, payload.name)
            if payload.add_items:
                fresh = selections.add_items(settings, selection_id, payload.add_items)
                kind = selections.find(settings, selection_id)["kind"]
                selections.annotate(store, store.models(), settings, kind, fresh, diagnose(fabric)["quantities"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="named selection not found") from exc
        except selections.SelectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.save_settings(settings)
        return {"selection": selections.find(settings, selection_id), "workbench": workbench_state(fabric, principal)}

    @api.delete("/named-selections/{selection_id}")
    def delete_named_selection(selection_id: str, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        settings = store.settings()
        try:
            selections.remove(settings, selection_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="named selection not found") from exc
        store.save_settings(settings)
        return workbench_state(context.current(session), principal)

    @api.get("/named-selections/{selection_id}/resolve")
    def get_named_selection_items(selection_id: str, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        """Where the selection's entities are on the current geometry"""
        context.require(principal, "read")
        settings = store.settings()
        try:
            selection = selections.find(settings, selection_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="named selection not found") from exc
        quantities=diagnose(context.current(session))["quantities"]
        return {"selection": selection, "items": selections.resolve(store, store.models(), settings, selection, quantities)}

    @api.post("/rename")
    def post_rename(payload: RenamePayload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        """Change the display name of a body, CAD document, or feature (parameters keep their names)"""
        context.require(principal, "edit")
        try:
            name=selections.clean_name(payload.name)
        except selections.SelectionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if payload.kind == "feature":
            settings = store.settings()
            feature = next((item for item in settings["features"] if item.get("id") == payload.id), None)
            if feature is None:
                raise HTTPException(status_code=404, detail="feature not found")
            feature["name"] = name
            store.save_settings(settings)
        elif payload.kind in {"body", "model"}:
            model_id, _, body_id = payload.id.partition("/")
            try:
                model = store.get(model_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="model not found") from exc
            if payload.kind == "model":
                model.name = name
                model.assembly = name if model.assembly else ""
            else:
                body=next((item for item in model.bodies if item.id == body_id), None)
                if body is None:
                    raise HTTPException(status_code=404, detail="body not found")
                body.name = name
            store.save(model)
        else:
            raise HTTPException(status_code=422, detail="kind must be body, model, or feature")
        return workbench_state(context.current(session), principal)

    @api.post("/settings")
    def post_settings(payload: SettingsPayload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        unknown = set(payload.values) - EDITABLE_SETTINGS
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown setting(s): {', '.join(sorted(unknown))}")
        values = json.loads(json.dumps(payload.values))
        if "structural_members" in values:
            from paramfabric.solvers.members import MembersError, validate_member

            try:
                values["structural_members"] = [validate_member(member) for member in values["structural_members"]]
            except MembersError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        settings = store.settings()
        settings.update(values)
        store.save_settings(settings)
        return workbench_state(context.current(session), principal)

    @api.post("/members")
    def post_member(payload: MemberPayload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        """Assign a 1D beam (selected edges) or 2D shell (selected face) for structural analysis"""
        from paramfabric.solvers.members import MembersError, validate_member

        context.require(principal, "edit")
        member={key: value for key, value in payload.model_dump().items() if value is not None}
        member["id"] = uuid.uuid4().hex[:8]
        settings = store.settings()
        existing = settings.get("structural_members", [])
        member.setdefault("name", f"{member['type']}{sum(1 for m in existing if m.get('type') == member['type']) + 1}")
        try:
            validate_member(member)
        except MembersError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        settings["structural_members"] = [*existing, member]
        store.save_settings(settings)
        return {"member": member, "workbench": workbench_state(context.current(session), principal)}

    @api.delete("/members/{member_id}")
    def delete_member(member_id: str, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        settings = store.settings()
        remaining = [m for m in settings.get("structural_members", []) if m.get("id") != member_id]
        if len(remaining) == len(settings.get("structural_members", [])):
            raise HTTPException(status_code=404, detail="member not found")
        settings["structural_members"] = remaining
        store.save_settings(settings)
        return workbench_state(context.current(session), principal)

    @api.post("/physics")
    def post_physics(payload: PhysicsPayload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "edit")
        if payload.analysis not in ANALYSES:
            raise HTTPException(status_code=422, detail=f"analysis must be one of {', '.join(ANALYSES)}")
        settings = store.settings()
        settings["analysis"] = payload.analysis
        store.save_settings(settings)
        fabric = context.current(session)
        created = ensure_parameters(fabric, payload.analysis)
        outcome: dict[str, Any] = {}
        if created:
            outcome = commit(
                session, principal, merge_parameters(fabric, created), "physics.configured",
                {"analysis": payload.analysis, "parameters": [parameter.name for parameter in created]},
                f"Default {payload.analysis.upper()} physics parameters",
            )
        return {
            "created_parameters": [parameter.name for parameter in created],
            "governance": outcome,
            "workbench": workbench_state(context.current(session), principal),
        }

    # tasks: meshing & solving
    def visible(record, principal: Principal) -> bool:
        tenant = current_tenant()
        if (record.organization, record.project) != (tenant.organization, tenant.project):
            return False  # other tenants' tasks do not exist for this caller
        if not context.auth_enabled or record.owner == principal.subject:
            return True
        try:
            context.require(principal, "admin")
            return True
        except HTTPException:
            return False

    def load_viewport(task_id: str) -> dict[str, Any] | None:
        try:
            path = queue.artifact_path(task_id, manager.VIEWPORT_ARTIFACT)
        except KeyError:
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def sync_registry(record: Any) -> None:
        """Mirror a terminal task into the tenant-scoped SQL registry (idempotent)"""
        if context.session_factory is None or record.status not in TERMINAL:
            return
        with context.session_factory() as session:
            row = session.get(SimulationTaskRecord, record.id)
            if row is None or row.status in TERMINAL:
                return
            row.status = record.status
            row.engine = (record.result or {}).get("engine")
            row.finished_at = datetime.now(timezone.utc)
            session.commit()

    def job_snapshot(task_id: str, residuals: int = 0, log: int = 0, include_result: bool = True) -> dict[str, Any]:
        snapshot=queue.snapshot(task_id, residuals, log, include_result=False)
        record = queue.get(task_id)
        sync_registry(record)
        summary = record.result or {}
        return {
            **snapshot,
            "analysis": record.payload.get("analysis"),
            "engine": summary.get("engine") or record.payload.get("engine"),
            "pipeline": summary.get("pipeline"),
            "residuals": snapshot["progress"],
            "residual_count": snapshot["progress_count"],
            "case_dir": summary.get("case_dir"),
            "result": load_viewport(task_id) if include_result and record.status == "completed" else None,
        }

    def submit(kind: str, principal: Principal, session: Session, extra: dict[str, Any]) -> Any:
        context.require(principal, "simulate")
        fabric = context.current(session)
        report = diagnose(fabric)
        if not report["ok"]:
            raise HTTPException(status_code=422, detail=report["issues"][0]["message"])
        if not store.models():
            raise HTTPException(status_code=422, detail="import CAD geometry before meshing")
        try:  # fail fast on dimensionally wrong physics instead of inside the task
            resolve_physics(report["quantities"], store.settings()["analysis"])
        except PhysicsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if kind == "solve" and any(r.organization == current_tenant().organization and r.project == current_tenant().project
                                   for r in queue.active({"solve"})):
            raise HTTPException(status_code=409, detail="a solver task is already running; cancel it or wait for it to finish")
        payload = {"root": str(tenant_root()), "fabric": fabric.model_dump(mode="json"), "analysis": store.settings()["analysis"], **extra}
        tenant = current_tenant()
        if queue.remote_storage:  # workers may run elsewhere: ship the CAD, settings, and manifest with the task
            try:
                payload["workspace"] = upload_workspace(queue.storage, tenant_root(), tenant.organization, tenant.project, payload["fabric"])
            except WorkspaceSyncError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            record = queue.submit(kind, payload, principal.subject, organization=tenant.organization, project=tenant.project)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        session.add(SimulationTaskRecord(
            id=record.id, organization_id=tenant.organization, project_id=tenant.project, kind=kind,
            owner=principal.subject, analysis=payload["analysis"], status="queued",
        ))
        session.commit()
        return record

    def wait_for(task_id: str, timeout: float = 900) -> None:
        deadline = time.monotonic() + timeout
        while queue.get(task_id).status not in TERMINAL and time.monotonic() < deadline:
            time.sleep(0.05)

    @api.post("/mesh")
    def post_mesh(payload: MeshPayload = MeshPayload(), principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        record = submit("mesh", principal, session, {"mesher": payload.mesher})
        if not payload.wait:
            return {"task": job_snapshot(record.id)}
        wait_for(record.id)
        snapshot = job_snapshot(record.id)
        if snapshot["status"] != "completed":
            raise HTTPException(status_code=422, detail=snapshot["error"] or f"meshing {snapshot['status']}")
        mesh = snapshot["result"]["mesh"]
        app.state.last_mesh = {key: value for key, value in mesh.items() if key != "display"}
        return mesh

    @api.get("/solver/engines")
    def get_engines(principal: Principal = principal_dependency) -> dict[str, Any]:
        context.require(principal, "read")
        return manager.engine_catalog(runner)

    @api.post("/solver/run")
    def post_run(payload: RunPayload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        record = submit("solve", principal, session, {"engine": payload.engine})
        return {"job": job_snapshot(record.id)}

    @api.get("/solver/jobs/{job_id}")
    def get_job(job_id: str, residuals: int = 0, log: int = 0, principal: Principal = principal_dependency) -> dict[str, Any]:
        try:
            record = queue.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not visible(record, principal):
            raise HTTPException(status_code=404, detail="unknown task")
        snapshot = job_snapshot(job_id, residuals, log)
        if snapshot["status"] == "completed" and record.kind == "mesh" and snapshot["result"]:
            app.state.last_mesh = {key: value for key, value in snapshot["result"]["mesh"].items() if key != "display"}
        return snapshot

    @api.post("/solver/jobs/{job_id}/cancel")
    def post_cancel(job_id: str, principal: Principal = principal_dependency) -> dict[str, Any]:
        try:
            record = queue.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not visible(record, principal):
            raise HTTPException(status_code=404, detail="unknown task")
        context.require(principal, "simulate")
        queue.cancel(job_id)
        return job_snapshot(job_id, include_result=False)

    @api.get("/tasks")
    def get_tasks(principal: Principal = principal_dependency) -> list[dict[str, Any]]:
        context.require(principal, "read")
        tenant = current_tenant()
        return [record.public() for record in queue.list(organization=tenant.organization, project=tenant.project) if visible(record, principal)]

    @api.get("/tasks/{task_id}/artifacts/{name}")
    def get_artifact(task_id: str, name: str, principal: Principal = principal_dependency) -> FileResponse:
        try:
            record = queue.get(task_id)
            if not visible(record, principal):
                raise KeyError(task_id)
            path = queue.artifact_path(task_id, name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        return FileResponse(path, filename=path.name)

    @api.post("/solver/case")
    def post_case(payload: CasePayload, principal: Principal = principal_dependency, session: Session = Depends(get_session)) -> dict[str, Any]:
        context.require(principal, "export")
        analysis = {"openfoam": "cfd", "calculix": "fea"}.get(payload.engine)
        if analysis is None:
            raise HTTPException(status_code=422, detail="engine must be openfoam or calculix")
        case_dir=tenant_root() / ".paramfabric" / "cases" / f"{payload.engine}-export"
        if payload.engine == "openfoam" and manager.native3d_available()[0]:
            # body-fitted case: exact B-Rep -> gmsh (prism layers) -> polyMesh + simpleFoam dictionaries
            from paramfabric.solvers import native3d

            lines: list[str] = []
            try:
                state = manager.workspace_state(tenant_root(), context.current(session))
                prepared = openfoam.prepare_case(state, {}, case_dir, _ExportContext(lines))
            except (manager.SimulationError, native3d.Native3DError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            files = sorted(str(path.relative_to(case_dir)).replace("\\", "/") for path in case_dir.rglob("*") if path.is_file())
            return {"engine": "openfoam", "case_dir": str(case_dir), "files": files, "cells": prepared.info.cells,
                    "turbulence": prepared.setup.turbulence, "log": lines}
        try:
            setup, _ = manager.reference_setup(manager.workspace_state(tenant_root(), context.current(session)), analysis)
        except manager.SimulationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            if payload.engine == "openfoam":
                files = openfoam.write_case(
                    case_dir, setup.mesh, setup.body_stl,
                    velocity=setup.physics["inlet_velocity"], kinematic_viscosity=setup.physics["kinematic_viscosity"],
                )
            else:
                files = calculix.write_input(
                    case_dir, setup.mesh, young=setup.physics["youngs_modulus"],
                    poisson=setup.physics["poissons_ratio"], load=setup.physics["applied_load"],
                )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"engine": payload.engine, "case_dir": str(case_dir), "files": files}

    app.include_router(api)

    # bi-directional code sync (local only)
    package_dir = Path(paramfabric.__file__).resolve().parent
    sync = SyncEngine(root, exclude=[package_dir])
    app.state.sync_engine = sync
    local_api = APIRouter(prefix="/api/local/sync", tags=["code sync"])

    def local() -> None:
        context.require_local_mode()

    def sync_inputs(session: Session) -> tuple[Fabric, dict[str, Any]]:
        fabric = context.current(session)
        return fabric, diagnose(fabric)["quantities"]

    def sync_status(session: Session) -> list[dict[str, Any]]:
        fabric, values = sync_inputs(session)
        return sync.status(fabric, values)

    def save_pulled(session: Session, updated: Fabric, pulled: list[dict[str, Any]]) -> None:
        if pulled:
            context.persist(
                session, updated, "fabric.synced_from_source",
                {"parameters": sorted({item["parameter"] for item in pulled})},
            )

    reported_drift: set[str] = set()

    def report_drift(items: list[dict[str, Any]]) -> None:
        """Emit `drift.detected` once per newly drifted constant (not on every poll)"""
        drifted = {item["key"]: item for item in items if item.get("state") in DRIFT_STATES}
        fresh=sorted(set(drifted) - reported_drift)
        reported_drift.intersection_update(drifted)
        if not fresh:
            return
        reported_drift.update(fresh)
        webhooks.emit("drift.detected", {"constants": [
            {"key": key, "parameter": drifted[key]["parameter"], "file": drifted[key].get("path"), "line": drifted[key].get("line"),
             "state": drifted[key]["state"], "source_value": drifted[key].get("value"), "fabric_value": drifted[key].get("fabric_value")}
            for key in fresh
        ]})

    @local_api.get("/status")
    def get_sync_status(session: Session = Depends(get_session)) -> dict[str, Any]:
        local()
        items = sync_status(session)
        report_drift(items)
        return {"items": items, "root": str(root)}

    @local_api.get("/diff")
    def get_sync_diff(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        local()
        fabric, values = sync_inputs(session)
        return sync.diff(fabric, values)

    @local_api.post("/reconcile")
    def post_reconcile(session: Session = Depends(get_session)) -> dict[str, Any]:
        local()
        fabric, values = sync_inputs(session)
        result = sync.reconcile(fabric, values)
        if result["fabric"] is not None:
            save_pulled(session, result["fabric"], result["pulled"])
        return {
            "pulled": result["pulled"],
            "pushed": result["pushed"],
            "conflicts": [item["key"] for item in result["conflicts"]],
            "items": sync_status(session),
        }

    @local_api.post("/push")
    def post_push(payload: SyncKeys, session: Session = Depends(get_session)) -> dict[str, Any]:
        local()
        fabric, values = sync_inputs(session)
        try:
            written = sync.push(fabric, values, set(payload.keys) if payload.keys is not None else None)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"pushed": written, "items": sync_status(session)}

    @local_api.post("/pull")
    def post_pull(payload: SyncKeys, session: Session = Depends(get_session)) -> dict[str, Any]:
        local()
        if not payload.keys:
            raise HTTPException(status_code=422, detail="select at least one source constant to pull")
        fabric, values = sync_inputs(session)
        try:
            updated, applied = sync.pull(fabric, values, set(payload.keys))
            ParameterGraph(updated).evaluate()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        save_pulled(session, updated, applied)
        return {"pulled": applied, "items": sync_status(session)}

    @local_api.post("/resolve")
    def post_resolve(payload: ResolvePayload, session: Session = Depends(get_session)) -> dict[str, Any]:
        local()
        fabric, values = sync_inputs(session)
        try:
            result = sync.resolve(fabric, values, payload.parameter, payload.strategy)
            if result["fabric"] is not None:
                ParameterGraph(result["fabric"]).evaluate()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if result["fabric"] is not None:
            save_pulled(session, result["fabric"], result["pulled"])
        return {"pulled": result["pulled"], "pushed": result["pushed"], "items": sync_status(session)}

    app.include_router(local_api)
