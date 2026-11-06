"""Native 3D pipeline: exact B-Rep -> gmsh unstructured mesh -> embedded 3D solver.

Mesh controls live in workbench settings (`mesh_options`)::

    {"element": "tet" | "hex", "order": 1 | 2, "curvature": 16,
     "boundary_layer": {"enabled": true, "y_plus": 30, "layers": 5, "growth": 1.2},
     "refinements": [{"selectors": [{"point": [...], "normal": [...]}], "size_ratio": 0.25}]}

Boundary conditions (`boundary_conditions`) reference CAD faces by selector::

    {"type": "fixed" | "force" | "pressure" | "inlet" | "outlet", "selectors": [...],
     "params": {"magnitude": "<parameter>"}, "direction": [0, 0, -1]}

Structural defaults (when none are assigned): the minimum-X planar face is
fixed and `applied_load` acts on the maximum-X face along −Z. Flow defaults:
external flow through the extracted flow volume (+X), or - with
`solver_options.domain = "internal"` - the body is the fluid and assigned
inlet/outlet faces bound it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from paramfabric.geometry import exact
from paramfabric.meshing.gmsh_mesher import (
    BoundaryLayer,
    MeshingFailed,
    MeshOptions,
    MeshTooLarge,
    first_layer_height,
    mesh_external,
    mesh_internal,
    mesh_solid,
)
from paramfabric.meshing.mesh import VolumeMesh, write_vtk_unstructured
from paramfabric.server.tasks import TaskContext, TaskCancelled
from paramfabric.solvers import post
from paramfabric.solvers.physics import PhysicsError, resolve_physics


from paramfabric.solvers.structural import StructuralError as StructuralErrorTypes  # noqa: E402


class Native3DError(ValueError):
    """Raised for setups the native pipeline cannot handle"""


def available() -> tuple[bool, str]:
    try:
        import gmsh  # noqa: F401
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except ImportError as exc:
        return False, f"missing dependency: {exc.name}"
    return True, "OpenCASCADE B-Rep (gmsh) · unstructured mesh · native 3D FVM/FEM"


def _physics(state: dict[str, Any], analysis: str) -> dict[str, float]:
    try:
        return resolve_physics(state["report"]["quantities"], analysis)
    except PhysicsError as exc:
        raise Native3DError(str(exc)) from exc


def _bounds(state: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    solids = [entry for entry in state["scene"]["bodies"] if entry["role"] == "solid"]
    if not solids:
        raise Native3DError("there is no solid geometry to mesh")
    low = np.min([entry["bbox_min"] for entry in solids], axis=0)
    high=np.max([entry["bbox_max"] for entry in solids], axis=0)
    return low, high


def _selectors(entries: list[dict[str, Any]]) -> list[tuple[list[float], list[float] | None]]:
    return [(entry["point"], entry.get("normal")) for entry in entries]


def mesh_options(state: dict[str, Any], analysis: str, physics: dict[str, float]) -> MeshOptions:
    settings = state["settings"].get("mesh_options", {})
    low, high = _bounds(state)
    characteristic = float(np.max(high - low))
    size = characteristic / max(physics["mesh_fineness_ratio"], 1.0)
    refinements = [
        {"selectors": _selectors(item.get("selectors", [])), "size": size * float(item.get("size_ratio", 0.25)),
         "distance": float(item.get("distance", 0.0))}
        for item in settings.get("refinements", [])
    ]
    layer = None
    if analysis == "cfd" and settings.get("boundary_layer", {}).get("enabled"):
        options = settings["boundary_layer"]
        estimate = first_layer_height(
            float(options.get("y_plus", 30.0)), physics["inlet_velocity"], float(high[0] - low[0]) or characteristic,
            physics["kinematic_viscosity"],
        )
        layers = int(options.get("layers", 5))
        growth = float(options.get("growth", 1.2))
        first=min(estimate["first_height"], 0.2 * size / sum(growth ** k for k in range(layers)))
        layer = BoundaryLayer(first_height=first, layers=layers, growth=growth)
    return MeshOptions(
        size=size,
        min_size=size / 20,
        max_nodes=int(settings.get("max_nodes", 30_000 if analysis == "cfd" else 250_000)),
        curvature=int(settings.get("curvature", 16)),
        order=int(settings.get("order", 2 if analysis == "fea" else 1)),
        element=str(settings.get("element", "tet")) if analysis == "fea" else "tet",
        refinements=refinements,
        boundary_layer=layer,
    )


def _cad_files(state: dict[str, Any]) -> list[Path]:
    try:
        return exact.meshing_inputs(state["store"], state["models"], state["settings"], state["report"]["quantities"])
    except exact.ExactKernelUnavailable as exc:
        raise Native3DError(str(exc)) from exc
    except exact.RegenerationError as exc:
        raise Native3DError(f"geometry regeneration failed: {exc}") from exc


def _structural_faces(state: dict[str, Any]) -> tuple[dict[str, list], list[dict[str, Any]], list[str]]:
    conditions = [bc for bc in state["settings"].get("boundary_conditions", []) if bc.get("type") in {"fixed", "force", "pressure"}]
    named: dict[str, list] = {}
    loads: list[dict[str, Any]] = []
    fixed: list[str] = []
    values = {name: float(q.to_base_units().magnitude) for name, q in state["report"]["quantities"].items()}
    if not any(bc["type"] == "fixed" for bc in conditions):
        low, high = _bounds(state)
        centre = (low + high) / 2
        conditions = [
            {"id": "default-fixed", "type": "fixed", "selectors": [{"point": [low[0], centre[1], centre[2]], "normal": [-1, 0, 0]}]},
            *(conditions or [{"id": "default-load", "type": "force", "selectors": [{"point": [high[0], centre[1], centre[2]], "normal": [1, 0, 0]}],
                              "params": {"magnitude": "applied_load"}, "direction": [0, 0, -1]}]),
        ]
    for index, bc in enumerate(conditions):
        name = f"bc{index}:{bc['type']}"
        named[name] = _selectors(bc["selectors"])
        if bc["type"] == "fixed":
            fixed.append(name)
            continue
        parameter = bc.get("params", {}).get("magnitude")
        if parameter not in values:
            raise Native3DError(f"boundary condition {bc.get('id')} references missing parameter {parameter!r}")
        if bc["type"] == "force":
            direction=np.asarray(bc.get("direction", [0, 0, -1]), float)
            direction /= np.linalg.norm(direction) or 1.0
            loads.append({"group": name, "type": "force", "vector": (direction * values[parameter]).tolist()})
        else:
            loads.append({"group": name, "type": "pressure", "value": values[parameter]})
    return named, loads, fixed


def mesh_only(state: dict[str, Any], analysis: str, options: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    mesh, _, mesh_settings = _mesh(state, analysis, context)
    write_vtk_unstructured(context.artifact("mesh.vtk"), mesh, title="Paramfabric gmsh mesh")
    return _mesh_summary(mesh, analysis, mesh_settings) | {"vtk": "mesh.vtk"}


def _mesh(state: dict[str, Any], analysis: str, context: TaskContext) -> tuple[VolumeMesh, dict[str, float], MeshOptions]:
    physics=_physics(state, analysis)
    files = _cad_files(state)
    options = mesh_options(state, analysis, physics)
    for attempt in range(6):
        try:
            mesh = _generate_mesh(state, analysis, files, options, context)
            break
        except MeshTooLarge as exc:
            if attempt == 5:
                raise Native3DError(str(exc)) from exc
            options.size *= 1.35
            options.min_size = options.size / 20
            context.log(f"{exc}; coarsening to {options.size:.4g} m")
    context.check()
    quality = mesh.quality()
    context.log(f"Mesh: {quality['nodes']:,} nodes, {quality['cells']:,} cells {quality['cell_types']}")
    return mesh, physics, options


def _generate_mesh(state: dict[str, Any], analysis: str, files: list[Path], options: MeshOptions, context: TaskContext) -> VolumeMesh:
    context.log(f"Meshing with gmsh: target size {options.size:.4g} m, order {options.order}, element {options.element}")
    try:
        if analysis == "fea":
            named, _, _ = _structural_faces(state)
            mesh = mesh_solid(files, options, named)
        elif state["settings"].get("solver_options", {}).get("domain") == "internal":
            conditions = state["settings"].get("boundary_conditions", [])
            inlets = [s for bc in conditions if bc.get("type") == "inlet" for s in _selectors(bc["selectors"])]
            outlets = [s for bc in conditions if bc.get("type") == "outlet" for s in _selectors(bc["selectors"])]
            mesh = mesh_internal(files, options, inlets, outlets)
        else:
            domain = state["scene"].get("flow_domain")
            if not domain:
                raise Native3DError("external CFD needs a flow volume; add one with the Flow Volume tool")
            mesh = mesh_external(files, domain["bbox_min"], domain["bbox_max"], options, context.log)
    except MeshTooLarge:
        raise
    except MeshingFailed as exc:
        raise Native3DError(str(exc)) from exc
    return mesh


def _mesh_summary(mesh: VolumeMesh, analysis: str, options: MeshOptions) -> dict[str, Any]:
    quality = mesh.quality()
    surface = post.boundary_surface(mesh, [g.name for g in mesh.boundaries if g.kind in {"wall", "face", "named", "inlet", "outlet"}])
    if not surface["indices"]:
        surface=post.exterior_surface(mesh)
    return {
        "analysis": analysis,
        "mesher": "gmsh",
        "quality": quality,
        "warnings": [] if quality["grade"] != "poor" else ["Some tetrahedra are badly shaped; refine or check the geometry."],
        "fineness": None,
        "element_size": options.size,
        "display": {"kind": "surface", "positions": surface["positions"], "indices": surface["indices"], "patches": post.patch_surfaces(mesh.linear())},
        "volume": post.volume_payload(mesh.linear()),
    }


def solve(state: dict[str, Any], analysis: str, options: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    from paramfabric.solvers import members

    solver_options = state["settings"].get("solver_options", {})
    modal=analysis == "fea" and solver_options.get("structural_analysis", "static") == "modal"
    modes = int(solver_options.get("modes", 6))
    if analysis == "fea" and members.members_of(state["settings"]) and solver_options.get("structural_model", "auto") != "solid":
        context.log("Structural idealization: beams and shells (set the model to 'solid' to mesh the full volume)")
        try:
            return members.solve_modal(state, context, modes) if modal else members.solve(state, context)
        except (members.MembersError, StructuralErrorTypes) as exc:
            raise Native3DError(str(exc)) from exc
    mesh, physics, opts = _mesh(state, analysis, context)
    summary = _mesh_summary(mesh, analysis, opts)
    if modal:
        return _solve_modal(state, mesh, physics, context, modes) | {"mesh": summary}
    if analysis == "fea":
        return _solve_structural(state, mesh, physics, context) | {"mesh": summary}
    return _solve_flow(state, mesh, physics, context) | {"mesh": summary}


def _solve_structural(state: dict[str, Any], mesh: VolumeMesh, physics: dict[str, float], context: TaskContext) -> dict[str, Any]:
    from paramfabric.solvers.fem.structural import Cancelled, StructuralCase, StructuralError, solve_structural

    _, loads, fixed = _structural_faces(state)
    case = StructuralCase(
        mesh=mesh, young=physics["youngs_modulus"], poisson=physics["poissons_ratio"], fixed=fixed, loads=loads,
        expansion=physics.get("thermal_expansion", 0.0), delta_temperature=physics.get("temperature_change", 0.0),
    )
    try:
        result = solve_structural(case, progress=context.progress, cancel=_Cancel(context), log=context.log)
    except Cancelled as exc:
        raise TaskCancelled(str(exc)) from exc
    except StructuralError as exc:
        raise Native3DError(str(exc)) from exc
    displacement = result["displacement"]
    von_mises = result["von_mises"]
    write_vtk_unstructured(
        context.artifact("structure.vtk"), mesh,
        point_data={"displacement": displacement, "von_mises": von_mises},
        title="Paramfabric native 3D structural result",
    )
    surface=post.exterior_surface(mesh)
    nodes = surface["nodes"]
    magnitude = np.linalg.norm(displacement, axis=1)
    summary = result["summary"] | {"applied_loads": loads, "fixed_groups": fixed}
    context.log(f"Max von Mises {summary['max_von_mises'] / 1e6:.4g} MPa, max displacement {summary['max_displacement'] * 1e3:.4g} mm")
    return {
        "analysis": "fea", "engine": "native3d",
        "method": f"3D linear elasticity · {'/'.join(summary['elements'])} · sparse direct / AMG",
        "converged": result["residual"] < 1e-6, "iterations": 1,
        "surfaces": [{
            "name": "solid", "positions": surface["positions"], "indices": surface["indices"],
            "displacement": np.round(displacement[nodes], 12).ravel().tolist(),
            "values": {"von_mises": von_mises[nodes].tolist(), "displacement": magnitude[nodes].tolist()},
        }],
        "units": {"von_mises": "Pa", "displacement": "m"},
        "summary": summary,
        "volume": post.volume_payload(mesh.linear(), {"von_mises": von_mises, "displacement": magnitude}),
        "vtk": "structure.vtk",
    }


def modal_payload(frequencies: np.ndarray, fractions: np.ndarray, total_mass: float, method: str) -> dict[str, Any]:
    names = [f"mode_{k + 1}" for k in range(len(frequencies))]
    table = [{"mode": k + 1, "frequency": float(f), "effective_mass": [float(v) for v in fractions[k]]}
             for k, f in enumerate(frequencies)]
    return {
        "analysis": "fea", "engine": "native3d", "physics": "modal", "method": method,
        "converged": True, "iterations": 1, "field_names": names,
        "field_labels": {name: f"Mode {k + 1} · {frequencies[k]:.4g} Hz" for k, name in enumerate(names)},
        "units": dict.fromkeys(names, ""),
        "summary": {
            "modes": table, "frequencies": [float(f) for f in frequencies], "total_mass": total_mass,
            "effective_mass_total": [float(v) for v in fractions.sum(axis=0)],
            "first_frequency": float(frequencies[0]) if len(frequencies) else None,
        },
    }


def _solve_modal(state: dict[str, Any], mesh: VolumeMesh, physics: dict[str, float], context: TaskContext, modes: int) -> dict[str, Any]:
    from paramfabric.solvers.fem.structural import StructuralError, solve_modal

    _, _, fixed = _structural_faces(state)
    context.log(f"Modal analysis: {modes} modes, ρ = {physics['material_density']:.6g} kg/m³, shift-invert Lanczos")
    try:
        result = solve_modal(mesh, physics["youngs_modulus"], physics["poissons_ratio"], physics["material_density"], fixed, modes=modes)
    except StructuralError as exc:
        raise Native3DError(str(exc)) from exc
    shapes = result["mode_shapes"]
    magnitudes = np.linalg.norm(shapes, axis=2)
    names = [f"mode_{k + 1}" for k in range(len(shapes))]
    surface=post.exterior_surface(mesh)
    nodes = surface["nodes"]
    write_vtk_unstructured(context.artifact("modes.vtk"), mesh, point_data={name: shapes[k] for k, name in enumerate(names)},
                           title="Paramfabric modal analysis")
    for k, frequency in enumerate(result["frequencies"]):
        fraction = result["effective_mass_fraction"][k]
        context.log(f"Mode {k + 1}: {frequency:.5g} Hz · effective mass x {fraction[0]:.3f} y {fraction[1]:.3f} z {fraction[2]:.3f}")
    payload = modal_payload(result["frequencies"], result["effective_mass_fraction"], result["total_mass"],
                            f"Modal analysis · {'/'.join(sorted(mesh.cells))} · consistent mass · shift-invert Lanczos")
    payload["summary"] |= {"fixed_groups": fixed, "density": physics["material_density"]}
    return payload | {
        "surfaces": [{
            "name": "solid", "positions": surface["positions"], "indices": surface["indices"],
            "mode_displacements": {name: np.round(shapes[k][nodes], 9).ravel().tolist() for k, name in enumerate(names)},
            "values": {name: magnitudes[k][nodes].tolist() for k, name in enumerate(names)},
        }],
        "volume": post.volume_payload(mesh.linear(), {name: magnitudes[k] for k, name in enumerate(names)}),
        "vtk": "modes.vtk",
    }


def _solve_flow(state: dict[str, Any], mesh: VolumeMesh, physics: dict[str, float], context: TaskContext) -> dict[str, Any]:
    from paramfabric.solvers.fvm.navier_stokes import Cancelled, FlowCase, FlowError, solve_flow

    solver_options = state["settings"].get("solver_options", {})
    low, high = _bounds(state)
    internal = solver_options.get("domain") == "internal"
    reference_area = None if internal else float((high[1] - low[1]) * (high[2] - low[2]))
    case = FlowCase(
        mesh=mesh, density=physics["fluid_density"], viscosity=physics["fluid_density"] * physics["kinematic_viscosity"],
        inlet_velocity=physics["inlet_velocity"] if internal else (physics["inlet_velocity"], 0.0, 0.0),
        turbulence=_turbulence_model(solver_options, physics, float(np.max(high - low)), context),
        turbulence_intensity=float(solver_options.get("turbulence_intensity", 0.05)),
        max_iterations=int(solver_options.get("max_iterations", 300)),
        tolerance=float(solver_options.get("tolerance", 1e-4)),
        reference_area=reference_area,
    )
    if case.turbulence == "k-epsilon":  # steady RANS of separated flow: robust defaults
        # First-order convection measured stable across mesh realizations where the
        # linear-upwind correction was not; second order remains opt-in
        case.second_order = bool(solver_options.get("second_order", False))
        case.relax_velocity = float(solver_options.get("relax_velocity", 0.5))
        case.relax_pressure = float(solver_options.get("relax_pressure", 0.2))
        case.relax_turbulence = float(solver_options.get("relax_turbulence", 0.5))
    try:
        result=solve_flow(case, progress=context.progress, cancel=_Cancel(context), log=context.log)
    except Cancelled as exc:
        raise TaskCancelled(str(exc)) from exc
    except FlowError as exc:
        raise Native3DError(str(exc)) from exc
    topology = result["topology"]
    node_u = result["node"]["U"]
    node_p = result["node"]["p"]
    speed = np.linalg.norm(node_u, axis=1)
    point_data = {"U": node_u, "p": node_p, "speed": speed}
    if "k" in result["node"]:
        point_data.update(k=result["node"]["k"], nut=result["node"]["nut"])
    write_vtk_unstructured(context.artifact("flow.vtk"), mesh.linear(), point_data=point_data, title="Paramfabric native 3D flow result")
    walls = post.boundary_surface(mesh.linear(), [g.name for g in mesh.boundaries if g.kind == "wall"])
    wall_nodes=walls["nodes"]
    shear_node=np.zeros(len(mesh.nodes))
    counts=np.zeros(len(mesh.nodes))
    face_nodes = topology.face_nodes[result["wall_faces"]]
    for column in range(4):
        valid = face_nodes[:, column] >= 0
        np.add.at(shear_node, face_nodes[valid, column], result["wall_shear"][valid])
        np.add.at(counts, face_nodes[valid, column], 1)
    shear_node = shear_node / np.maximum(counts, 1)
    domain_low, domain_high = mesh.nodes.min(axis=0), mesh.nodes.max(axis=0)
    centre = (low + high) / 2
    slice_origin = np.array([centre[0], centre[1], centre[2]])
    section = post.slice_plane(mesh.linear(), {"pressure": node_p, "speed": speed}, slice_origin, np.array([0.0, 0.0, 1.0]))
    span = domain_high - domain_low
    if internal:
        inlet = [g for g in mesh.boundaries if g.kind == "inlet"]
        seed_nodes = np.unique(np.concatenate([conn.ravel() for g in inlet for conn in g.elements.values()]))
        seeds = mesh.nodes[seed_nodes[:: max(1, len(seed_nodes) // 40)]]
        seeds = seeds + 0.25 * (seeds.mean(axis=0) - seeds)
    else:
        ys = np.linspace(low[1] - 0.2 * span[1], high[1] + 0.2 * span[1], 8)
        zs=np.linspace(low[2] - 0.2 * span[2], high[2] + 0.2 * span[2], 5)
        seeds = np.array([[domain_low[0] + 0.02 * span[0], y, z] for y in ys for z in zs])
    cell_size=float(np.cbrt(np.median(topology.volumes)))
    lines=post.streamlines(
        topology.cell_centres, result["cell"]["U"], seeds, step=0.6 * cell_size,
        max_steps=int(4 * span.max() / (0.6 * cell_size)), max_gap=2.5 * cell_size,
    )
    summary = result["summary"]
    warnings = []
    if summary.get("velocity_limited_cells"):
        warnings.append(f"velocity limiting still active in {summary['velocity_limited_cells']} cell(s) at the final iteration")
    if summary.get("mass_imbalance") is not None and summary["mass_imbalance"] > 0.05:
        warnings.append(f"continuity imbalance {summary['mass_imbalance']:.2g} of the inlet mass flow")
    summary["reliable"] = not warnings
    summary["warnings"] = warnings
    for warning in warnings:
        context.log(f"WARNING: {warning}, results are not reliable; refine the mesh or lower relaxation")
    context.log(
        f"Drag {summary['drag']:.4g} N" + (f", Cd {summary['drag_coefficient']:.4g}" if "drag_coefficient" in summary else "")
    )
    return {
        "analysis": "cfd", "engine": "native3d",
        "method": f"3D incompressible FVM · SIMPLE · {case.turbulence}",
        "converged": summary["converged"], "iterations": summary["iterations"],
        "surfaces": [{
            "name": "walls", "positions": walls["positions"], "indices": walls["indices"],
            "values": {"pressure": node_p[wall_nodes].tolist(), "wall_shear": shear_node[wall_nodes].tolist()},
        }],
        "slice": section,
        "streamlines": lines,
        "units": {"pressure": "Pa", "speed": "m/s", "wall_shear": "Pa"},
        "summary": summary,
        "volume": post.volume_payload(mesh.linear(), {"pressure": node_p, "speed": speed}, {"U": node_u}),
        "ports": post.port_glyphs(mesh.linear(), node_u),
        "vtk": "flow.vtk",
    }


def _turbulence_model(options: dict[str, Any], physics: dict[str, float], length: float, context: TaskContext) -> str:
    """Explicit choice, or laminar below Re = 2000 and k-ε above (steady laminar
    SIMPLE has no physical solution for separated high-Re flows)."""
    choice = str(options.get("turbulence", "auto"))
    if choice in {"laminar"}:
        return choice
    if choice != "auto":
        if choice not in {"k-epsilon", "k_epsilon"}:
            context.log(f"The native solver has no {choice}; using standard k–ε")
        return "k-epsilon"
    reynolds = physics["inlet_velocity"] * length / physics["kinematic_viscosity"]
    model = "laminar" if reynolds < 2000 else "k-epsilon"
    context.log(f"Reynolds number {reynolds:.3g} → {model}")
    return model


class _Cancel:
    def __init__(self, context: TaskContext) -> None:
        self.context = context

    def is_set(self) -> bool:
        return bool(self.context.cancelled)


__all__ = ["available", "mesh_only", "solve", "mesh_options", "Native3DError"]
