"""Beam (1D) and shell (2D) idealization of the exact B-Rep for the Studio.

Members are assigned in the viewport and stored in the workbench settings as
`structural_members`:

* `{"type": "beam", "body": key, "edges": [{"point": [...]}, ...],
  "section": {"type": "rectangular"|"circular"|"tube"|"i_beam", ...metres},
  "orientation": [0, 0, 1]}`: the selected B-Rep edges become frame elements;
* `{"type": "shell", "body": key, "selector": {"point", "normal"},
  "thickness": t, "offset": e}`: the selected face becomes a facet shell
  whose mid-surface sits `e` along the face normal.

The regenerated B-Rep is meshed once in 2D with gmsh, so beams and shells
that share a B-Rep edge share nodes. Supports and loads come from the same
face boundary conditions as the solid analysis: member nodes on a *fixed* face
are clamped, a *force* is shared equally by the member nodes on its face, and
a *pressure* on a shell face is integrated over that shell.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from paramfabric.meshing.mesh import VolumeMesh, write_vtk_unstructured
from paramfabric.solvers.structural import BeamGroup, MemberModel, Section, ShellGroup, StructuralError, modal_members, solve_members


class MembersError(ValueError):
    """Raised for member assignments that cannot be meshed or solved"""


def members_of(settings: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in settings.get("structural_members", []) if m.get("type") in {"beam", "shell"}]


def validate_member(member: dict[str, Any]) -> dict[str, Any]:
    kind = member.get("type")
    if kind == "beam":
        if not member.get("edges"):
            raise MembersError("a beam needs at least one selected edge")
        try:
            Section.from_spec(member.get("section", {}))
        except StructuralError as exc:
            raise MembersError(str(exc)) from exc
    elif kind == "shell":
        if not member.get("selector", {}).get("point"):
            raise MembersError("a shell needs a selected face")
        if float(member.get("thickness", 0)) <= 0:
            raise MembersError("shell thickness must be positive")
    else:
        raise MembersError(f"unknown member type {kind!r}")
    return member


def _closest_curve(gmsh: Any, curves: list[int], point: list[float]) -> int:
    return min(curves, key=lambda curve: math.dist(gmsh.model.getClosestPoint(1, curve, list(point))[0], point))


def _surface_normal(gmsh: Any, surface: int, point: Any, hint: Any) -> np.ndarray:
    _, parametric = gmsh.model.getClosestPoint(2, surface, list(point))
    normal = np.asarray(gmsh.model.getNormal(surface, parametric), float)
    if hint is not None and float(np.dot(normal, hint)) < 0:
        normal = -normal
    return normal


def build(state: dict[str, Any], context: Any) -> dict[str, Any]:
    """Mesh the member geometry and resolve supports and loads"""
    from paramfabric.gmsh_session import GmshSession
    from paramfabric.meshing.gmsh_mesher import _closest_surface, _import
    from paramfabric.solvers import native3d

    members = [validate_member(m) for m in members_of(state["settings"])]
    if not members:
        raise MembersError("no beams or shells are assigned")
    physics = native3d._physics(state, "fea")
    size = native3d.mesh_options(state, "fea", physics).size
    files = native3d._cad_files(state)
    named, loads, fixed = native3d._structural_faces(state)
    with GmshSession() as gmsh:
        gmsh.model.add("members")
        _import(gmsh, files)
        surfaces = [tag for _, tag in gmsh.model.getEntities(2)]
        curves = [tag for _, tag in gmsh.model.getEntities(1)]
        beam_curves: list[list[int]] = []
        shell_surfaces: list[int] = []
        for member in members:
            if member["type"] == "beam":
                beam_curves.append(sorted({_closest_curve(gmsh, curves, edge["point"]) for edge in member["edges"]}))
            else:
                selector = member["selector"]
                shell_surfaces.append(_closest_surface(gmsh, surfaces, (selector["point"], selector.get("normal")))[0])
        gmsh.option.setNumber("Mesh.MeshSizeMax", size)
        gmsh.option.setNumber("Mesh.MeshSizeMin", size / 20)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.option.setNumber("Mesh.ElementOrder", 1)
        gmsh.option.setNumber("Mesh.SubdivisionAlgorithm", 0)
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.model.mesh.generate(2)
        tags, coordinates, _ = gmsh.model.mesh.getNodes()
        index_of = {int(tag): i for i, tag in enumerate(tags)}
        all_nodes = np.asarray(coordinates, float).reshape(-1, 3)

        def connectivity(dim: int, tag: int, width: int) -> np.ndarray:
            types, _, element_nodes = gmsh.model.mesh.getElements(dim, tag)
            rows = [np.asarray(nodes, np.int64).reshape(-1, width) for kind, nodes in zip(types, element_nodes)
                    if gmsh.model.mesh.getElementProperties(kind)[3] == width]
            if not rows:
                return np.zeros((0, width), np.int64)
            return np.vectorize(index_of.__getitem__)(np.concatenate(rows))

        beams = [np.concatenate([connectivity(1, curve, 2) for curve in group]) for group in beam_curves]
        shells = [connectivity(2, surface, 3) for surface in shell_surfaces]

        def nodes_on(group: str) -> np.ndarray:
            found: set[int] = set()
            for selector in named[group]:
                face = _closest_surface(gmsh, surfaces, selector)[0]
                face_tags, _, _ = gmsh.model.mesh.getNodes(2, face, True, False)
                found |= {index_of[int(t)] for t in face_tags}
            return np.array(sorted(found), np.int64)

        supports = {group: nodes_on(group) for group in fixed}
        load_nodes = {load["group"]: nodes_on(load["group"]) for load in loads}
        pressure_faces = {
            load["group"]: [(_closest_surface(gmsh, surfaces, selector)[0], selector) for selector in named[load["group"]]]
            for load in loads if load["type"] == "pressure"
        }
        normals = {
            group: [(face, _surface_normal(gmsh, face, selector[0], None if selector[1] is None else np.asarray(selector[1], float)))
                    for face, selector in faces]
            for group, faces in pressure_faces.items()
        }
        face_area = {face: gmsh.model.occ.getMass(2, face) for faces in pressure_faces.values() for face, _ in faces}

    used = np.unique(np.concatenate([*(b.ravel() for b in beams), *(s.ravel() for s in shells)]))
    if not len(used):
        raise MembersError("the selected edges and faces produced no elements")
    remap = np.full(len(all_nodes), -1, np.int64)
    remap[used] = np.arange(len(used))
    nodes = all_nodes[used]
    beam_groups, shell_groups = [], []
    for member, elements in zip([m for m in members if m["type"] == "beam"], beams):
        section = Section.from_spec(member["section"])
        beam_groups.append(BeamGroup(remap[elements], section, member.get("name", "beam"), member.get("orientation")))
    for member, triangles, surface in zip([m for m in members if m["type"] == "shell"], shells, shell_surfaces):
        group = ShellGroup(remap[triangles], float(member["thickness"]), member.get("name", "shell"), float(member.get("offset", 0.0)))
        group.surface = surface  # type: ignore[attr-defined]
        shell_groups.append(group)

    clamp = {}
    for group, members_nodes in supports.items():
        local = remap[members_nodes]
        local = local[local >= 0]
        if not len(local):
            raise MembersError("no beam or shell touches a fixed support face; fix a face the members end on")
        clamp.update({int(n): "all" for n in local})
    if not clamp:
        raise MembersError("assign a fixed support")
    nodal: dict[int, np.ndarray] = {}

    def add(node: int, force: np.ndarray) -> None:
        nodal.setdefault(node, np.zeros(6))[:3] += force

    for load in loads:
        local=remap[load_nodes[load["group"]]]
        local = local[local >= 0]
        if load["type"] == "force":
            if not len(local):
                raise MembersError("no beam or shell touches the loaded face; load a face the members connect to")
            for node in local:
                add(int(node), np.asarray(load["vector"], float) / len(local))
            continue
        for face, normal in normals[load["group"]]:
            shell=next((g for g in shell_groups if getattr(g, "surface", None) == face), None)
            if shell is not None:
                for triangle in shell.triangles:
                    corners = nodes[triangle]
                    area = 0.5 * np.linalg.norm(np.cross(corners[1] - corners[0], corners[2] - corners[0]))
                    for node in triangle:
                        add(int(node), -load["value"] * area / 3 * normal)
            elif len(local):
                context.log(f"Pressure face {face} is not a shell; applying its resultant at {len(local)} member node(s)")
                for node in local:
                    add(int(node), -load["value"] * face_area[face] / len(local) * normal)
            else:
                raise MembersError("a pressure face is neither a shell nor touched by a member")
    return {
        "model": MemberModel(nodes, physics["youngs_modulus"], physics["poissons_ratio"], beam_groups, shell_groups,
                             density=physics.get("material_density", 7850.0)),
        "fixed": clamp, "loads": {k: v.tolist() for k, v in nodal.items()}, "size": size,
    }


def solve(state: dict[str, Any], context: Any) -> dict[str, Any]:
    """Build, solve, and package a beam/shell model for the viewport"""
    built = build(state, context)
    model: MemberModel = built["model"]
    context.log(f"Members: {sum(len(g.elements) for g in model.beams)} beam and {sum(len(g.triangles) for g in model.shells)} "
                f"shell elements on {len(model.nodes)} nodes (target size {built['size']:.4g} m)")
    try:
        result=solve_members(model, built["fixed"], built["loads"])
    except StructuralError as exc:
        raise MembersError(str(exc)) from exc
    displacement = result.displacement[:, :3]
    magnitude = np.linalg.norm(displacement, axis=1)
    surfaces, lines = [], []
    nodal_stress = np.zeros(len(model.nodes))
    for group, stress in zip(model.shells, result.shell_von_mises):
        used, inverse = np.unique(group.triangles.ravel(), return_inverse=True)
        values = np.zeros(len(used))
        counts = np.zeros(len(used))
        np.add.at(values, inverse, np.repeat(stress, 3))
        np.add.at(counts, inverse, 1)
        values /= np.maximum(counts, 1)
        nodal_stress[used] = np.maximum(nodal_stress[used], values)
        surfaces.append({
            "name": group.name, "kind": "shell", "thickness": group.thickness, "offset": group.offset,
            "positions": np.round(model.nodes[used], 7).ravel().tolist(), "indices": inverse.astype(int).tolist(),
            "displacement": np.round(displacement[used], 12).ravel().tolist(),
            "values": {"von_mises": values.tolist(), "displacement": magnitude[used].tolist()},
        })
    for group, stress in zip(model.beams, result.beam_stress):
        segments = group.elements.ravel()
        lines.append({
            "name": group.name, "kind": "beam",
            "positions": np.round(model.nodes[segments], 7).ravel().tolist(),
            "displacement": np.round(displacement[segments], 12).ravel().tolist(),
            "values": {"von_mises": np.repeat(stress, 2).tolist(), "displacement": magnitude[segments].tolist()},
        })
        np.maximum.at(nodal_stress, group.elements[:, 0], stress)
        np.maximum.at(nodal_stress, group.elements[:, 1], stress)
    cells = {}
    if model.shells:
        cells["tri3"] = np.concatenate([g.triangles for g in model.shells])
    if model.beams:
        cells["line2"] = np.concatenate([g.elements for g in model.beams])
    write_vtk_unstructured(context.artifact("members.vtk"), VolumeMesh(model.nodes, cells),
                           point_data={"displacement": displacement, "stress": nodal_stress}, title="Paramfabric beam/shell result")
    reaction = result.reactions[:, :3].sum(axis=0)
    summary = result.summary | {
        "max_von_mises": result.summary["max_stress"], "reaction_force": reaction.tolist(),
        "elements": {"beam": result.summary["beam_elements"], "shell": result.summary["shell_elements"]},
        "dof": result.summary["dofs"], "relative_residual": 0.0,
        "stress_measure": "shells: von Mises at the outer fibres; beams: |N|/A + |My|/Wy + |Mz|/Wz",
    }
    context.log(f"Max stress {summary['max_von_mises'] / 1e6:.4g} MPa, max displacement {summary['max_displacement'] * 1e3:.4g} mm")
    return {
        "analysis": "fea", "engine": "native3d",
        "method": "Beam/shell idealization · Euler–Bernoulli frames · DKT/CST facet shells",
        "converged": True, "iterations": 1,
        "surfaces": surfaces, "lines": lines,
        "units": {"von_mises": "Pa", "displacement": "m"},
        "summary": summary, "vtk": "members.vtk",
    }


def solve_modal(state: dict[str, Any], context: Any, modes: int = 6) -> dict[str, Any]:
    """Natural frequencies and mode shapes of the beam/shell model"""
    from paramfabric.solvers.native3d import modal_payload

    built = build(state, context)
    model: MemberModel = built["model"]
    context.log(f"Modal analysis of {len(model.nodes)} member nodes: {modes} modes, ρ = {model.density:.6g} kg/m³")
    try:
        result = modal_members(model, built["fixed"], modes)
    except StructuralError as exc:
        raise MembersError(str(exc)) from exc
    shapes = result.mode_shapes[:, :, :3]
    magnitudes = np.linalg.norm(shapes, axis=2)
    names=[f"mode_{k + 1}" for k in range(len(shapes))]
    surfaces, lines = [], []
    for group in model.shells:
        used, inverse = np.unique(group.triangles.ravel(), return_inverse=True)
        surfaces.append({
            "name": group.name, "kind": "shell", "thickness": group.thickness, "offset": group.offset,
            "positions": np.round(model.nodes[used], 7).ravel().tolist(), "indices": inverse.astype(int).tolist(),
            "mode_displacements": {name: np.round(shapes[k][used], 9).ravel().tolist() for k, name in enumerate(names)},
            "values": {name: magnitudes[k][used].tolist() for k, name in enumerate(names)},
        })
    for group in model.beams:
        segments = group.elements.ravel()
        lines.append({
            "name": group.name, "kind": "beam", "positions": np.round(model.nodes[segments], 7).ravel().tolist(),
            "mode_displacements": {name: np.round(shapes[k][segments], 9).ravel().tolist() for k, name in enumerate(names)},
            "values": {name: magnitudes[k][segments].tolist() for k, name in enumerate(names)},
        })
    for k, frequency in enumerate(result.frequencies):
        context.log(f"Mode {k + 1}: {frequency:.5g} Hz")
    payload = modal_payload(result.frequencies, result.effective_mass_fraction, result.total_mass,
                            "Modal analysis · beam/shell idealization · consistent frame mass, lumped shell mass")
    return payload | {"surfaces": surfaces, "lines": lines}
