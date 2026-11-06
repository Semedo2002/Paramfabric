"""Universal simulation manager: one pipeline for desktop threads and remote workers.

A task payload carries everything a worker needs - the workspace root (shared
storage), the fabric snapshot, the analysis, and the requested engine - so the
same code runs in-process or on a `paramfabric worker` node.

Engine resolution for `engine="auto"`:

* CFD: OpenFOAM on the body-fitted gmsh mesh (`openfoam3d`) whenever an
  OpenFOAM installation is found (PATH, WSL, Docker); otherwise the native
  experimental FVM solver, flagged as such in the log and result.
* FEA: CalculiX when on PATH, otherwise the native 3D finite-element solver.
* Without numpy/scipy/gmsh: the pure-Python 2D reference solvers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paramfabric.core.diagnostics import diagnose
from paramfabric.core.schema import Fabric
from paramfabric.geometry import GeometryStore, resolve_scene, selections
from paramfabric.geometry.scene import si_values, transformed_triangles
from paramfabric.geometry.stl import write_stl
from paramfabric.server.tasks import TaskContext, handler
from paramfabric.server.workspace_sync import task_root
from paramfabric.solvers.mesher import MeshingError, build_mesh
from paramfabric.solvers.physics import PhysicsError, resolve_physics
from paramfabric.solvers.runner import SolverRunner, SolveSetup

VIEWPORT_ARTIFACT = "viewport.json"
ENGINES = ("auto", "native3d", "builtin", "openfoam", "calculix")
NATIVE_CFD_WARNING = (
    "OpenFOAM was not found, so CFD runs on the experimental native solver; "
    "install OpenFOAM (Linux, WSL, or Docker) for production results"
)


class SimulationError(ValueError):
    """Raised for setups that cannot be meshed or solved"""


def native3d_available() -> tuple[bool, str]:
    try:
        from paramfabric.solvers import native3d
    except ImportError as exc:  # pragma: no cover - depends on optional extras
        return False, f"native 3D pipeline unavailable: {exc}"
    return native3d.available()


def openfoam_available() -> tuple[bool, str]:
    """Body-fitted OpenFOAM pipeline: needs the native mesher and an OpenFOAM install"""
    native, reason = native3d_available()
    if not native:
        return False, reason
    from paramfabric.solvers import openfoam

    return openfoam.available()


def workspace_state(root: Path, fabric: Fabric) -> dict[str, Any]:
    store = GeometryStore(root)
    report = diagnose(fabric)
    if not report["ok"]:
        raise SimulationError(report["issues"][0]["message"])
    models=store.models()
    if not models:
        raise SimulationError("import CAD geometry before meshing")
    settings = selections.apply_to_conditions(store, models, store.settings(), report["quantities"])
    scene = resolve_scene(models, settings["features"], si_values(report["quantities"]))
    if scene["errors"]:
        raise SimulationError(scene["errors"][0])
    return {"store": store, "report": report, "models": models, "settings": settings, "scene": scene}


def reference_setup(state: dict[str, Any], analysis: str) -> tuple[SolveSetup, dict[str, Any]]:
    """Cartesian voxel mesh + inputs for the 2D reference and external engines"""
    try:
        physics = resolve_physics(state["report"]["quantities"], analysis)
    except PhysicsError as exc:
        raise SimulationError(str(exc)) from exc
    scene = dict(state["scene"])
    if analysis == "fea":
        scene["flow_domain"] = None
    try:
        mesh = build_mesh(state["models"], scene, physics["mesh_fineness_ratio"])
    except MeshingError as exc:
        raise SimulationError(str(exc)) from exc
    solids = [entry for entry in [*scene["bodies"], *scene["derived"]] if entry["role"] == "solid"]
    body_stl = "".join(
        write_stl(entry["name"].replace(" ", "_"), transformed_triangles(state["models"], entry), 1.0) for entry in solids
    )
    setup = SolveSetup(
        analysis=analysis, mesh=mesh, physics=physics, body_stl=body_stl,
        y_range=[mesh.body_bbox[0][1], mesh.body_bbox[1][1]],
    )
    summary = {
        "analysis": analysis, "mesher": "cartesian", "quality": mesh.quality(), "warnings": mesh.warnings,
        "display": mesh.display(), "fineness": physics["mesh_fineness_ratio"],
    }
    return setup, summary


def resolve_pipeline(analysis: str, engine: str, runner: SolverRunner) -> str:
    """Map a requested engine to `openfoam3d`, `external:<name>`, `native3d`, or `builtin`"""
    if engine not in ENGINES:
        raise SimulationError(f"unknown engine {engine!r}; choose one of {', '.join(ENGINES)}")
    native, reason = native3d_available()
    if analysis == "cfd" and engine in {"auto", "openfoam"}:
        foam, foam_reason = openfoam_available()
        if foam:
            return "openfoam3d"
        if engine == "openfoam":
            if native:
                raise SimulationError(foam_reason)
            runner.resolve_engine(analysis, engine)
            return "external:openfoam"
    if engine == "auto":
        if analysis == "fea" and runner.engines()["calculix"]["available"]:
            return "external:calculix"
        return "native3d" if native else "builtin"
    if engine == "native3d":
        if not native:
            raise SimulationError(reason)
        return "native3d"
    if engine in {"openfoam", "calculix"}:
        runner.resolve_engine(analysis, engine)
        return f"external:{engine}"
    return "builtin"


def engine_catalog(runner: SolverRunner) -> dict[str, dict[str, Any]]:
    catalog = runner.engines()
    native, reason = native3d_available()
    foam, foam_reason = openfoam_available()
    if foam or native:
        catalog["openfoam"] = {
            "available": foam,
            "analyses": ["cfd"],
            "label": "OpenFOAM simpleFoam · k-ω SST",
            "detail": f"Body-fitted gmsh mesh with prism layers · {foam_reason}",
        }
    catalog["native3d"] = {
        "available": native,
        "analyses": ["cfd", "fea"],
        "label": "Native 3D FEA · experimental CFD",
        "detail": reason,
    }
    catalog["builtin"]["label"] = "2D reference solvers"
    return catalog


def _write_viewport(context: TaskContext, payload: dict[str, Any]) -> None:
    context.artifact(VIEWPORT_ARTIFACT).write_text(json.dumps(payload), encoding="utf-8")


@handler("mesh")
def mesh_task(payload: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    root, analysis = task_root(payload, context), payload["analysis"]
    state = workspace_state(root, Fabric.model_validate(payload["fabric"]))
    pipeline = payload.get("mesher", "auto")
    native, _ = native3d_available()
    if pipeline in {"auto", "unstructured"} and native:
        from paramfabric.solvers import native3d

        summary = native3d.mesh_only(state, analysis, payload.get("options", {}), context)
    else:
        if pipeline == "unstructured":
            raise SimulationError(native3d_available()[1])
        _, summary = reference_setup(state, analysis)
        context.log(f"Cartesian mesh: {summary['quality']['cells']:,} cells ({summary['quality']['grade']})")
    _write_viewport(context, {"mesh": summary})
    return {key: value for key, value in summary.items() if key not in {"display", "volume"}} | {"viewport": VIEWPORT_ARTIFACT}


@handler("solve")
def solve_task(payload: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    root, analysis = task_root(payload, context), payload["analysis"]
    runner = SolverRunner(root)
    pipeline = resolve_pipeline(analysis, payload.get("engine", "auto"), runner)
    state = workspace_state(root, Fabric.model_validate(payload["fabric"]))
    context.log(f"Pipeline: {pipeline}")
    if pipeline == "openfoam3d":
        from paramfabric.solvers import openfoam

        try:
            result = openfoam.solve(state, analysis, payload.get("options", {}), context)
        except openfoam.OpenFOAMError as exc:
            raise SimulationError(str(exc)) from exc
    elif pipeline == "native3d":
        from paramfabric.solvers import native3d

        if analysis == "cfd":
            context.log(f"WARNING: {NATIVE_CFD_WARNING}")
        result = native3d.solve(state, analysis, payload.get("options", {}), context)
        if analysis == "cfd":
            result["summary"].setdefault("warnings", []).insert(0, NATIVE_CFD_WARNING)
            result["summary"]["reliable"] = False
    else:
        setup, mesh_summary = reference_setup(state, analysis)
        context.log(f"Cartesian mesh: {mesh_summary['quality']['cells']:,} cells")
        engine = pipeline.split(":", 1)[1] if pipeline.startswith("external:") else "builtin"
        result = runner.execute(setup, engine, context)
        result["mesh"] = mesh_summary
    _write_viewport(context, result)
    return {
        key: result.get(key)
        for key in ("analysis", "engine", "method", "converged", "iterations", "summary", "note", "case_dir")
    } | {"viewport": VIEWPORT_ARTIFACT, "pipeline": pipeline}
