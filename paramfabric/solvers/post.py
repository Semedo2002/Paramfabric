"""Post-processing for unstructured results: surfaces, slices, streamlines"""

from __future__ import annotations

from typing import Any

import numpy as np
from scipy.spatial import cKDTree

from paramfabric.meshing.mesh import CORNERS, VolumeMesh

# Corner-node tetrahedral decompositions (gmsh ordering)
_TETS = {
    "tet4": [(0, 1, 2, 3)],
    "pyramid5": [(0, 1, 2, 4), (0, 2, 3, 4)],
    "prism6": [(0, 1, 2, 3), (1, 2, 3, 4), (2, 3, 4, 5)],
    "hex8": [(0, 1, 3, 4), (1, 2, 3, 6), (1, 4, 5, 6), (3, 4, 6, 7), (1, 3, 4, 6)],
}
_LINEAR = {"tet10": "tet4", "hex20": "hex8"}


def boundary_surface(mesh: VolumeMesh, names: list[str] | None = None) -> dict[str, Any]:
    """Triangulated boundary groups with compact node numbering"""
    triangles = []
    for group in mesh.boundaries:
        if names is not None and group.name not in names:
            continue
        for kind, conn in group.elements.items():
            corners = conn[:, : CORNERS[kind]]
            if corners.shape[1] == 3:
                triangles.append(corners)
            else:
                triangles.append(corners[:, [0, 1, 2]])
                triangles.append(corners[:, [0, 2, 3]])
    if not triangles:
        return {"nodes": np.zeros(0, int), "positions": [], "indices": []}
    tris = np.concatenate(triangles)
    nodes, inverse = np.unique(tris.ravel(), return_inverse=True)
    return {
        "nodes": nodes,
        "positions": np.round(mesh.nodes[nodes], 7).ravel().tolist(),
        "indices": inverse.astype(int).tolist(),
    }


def patch_surfaces(mesh: VolumeMesh, limit: int = 400_000) -> list[dict[str, Any]]:
    """Each boundary patch as its own surface, for the mesh viewer. Empty when the mesh is too large to send."""
    total = sum(len(conn) * (1 if conn.shape[1] == 3 else 2) for group in mesh.boundaries for conn in group.elements.values())
    if total > limit:
        return []
    patches = []
    for group in mesh.boundaries:
        surface = boundary_surface(mesh, [group.name])
        if surface["indices"]:
            patches.append({"name": group.name, "kind": group.kind, "positions": surface["positions"], "indices": surface["indices"]})
    return patches


def exterior_surface(mesh: VolumeMesh) -> dict[str, Any]:
    """Faces of solid elements that belong to exactly one element"""
    local = {"tet4": [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)],
             "hex8": [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]}
    faces=[]
    for kind, conn in mesh.cells.items():
        base = _LINEAR.get(kind, kind)
        if base not in local:
            continue
        corners = conn[:, : CORNERS[kind]]
        for face in local[base]:
            padded=np.full((len(corners), 4), -1, dtype=np.int64)
            padded[:, : len(face)] = corners[:, list(face)]
            faces.append(padded)
    stacked = np.concatenate(faces)
    keys = np.sort(stacked, axis=1)
    _, first, counts = np.unique(keys, axis=0, return_index=True, return_counts=True)
    outer = stacked[first[counts == 1]]
    tris = np.concatenate([outer[:, [0, 1, 2]], outer[outer[:, 3] >= 0][:, [0, 2, 3]]])
    nodes, inverse = np.unique(tris.ravel(), return_inverse=True)
    return {"nodes": nodes, "positions": np.round(mesh.nodes[nodes], 7).ravel().tolist(), "indices": inverse.astype(int).tolist()}


def slice_plane(mesh: VolumeMesh, values: dict[str, np.ndarray], origin: np.ndarray, normal: np.ndarray) -> dict[str, Any]:
    """Marching-tetrahedra cut of the mesh with nodal fields interpolated"""
    normal = normal / np.linalg.norm(normal)
    tets = []
    for kind, conn in mesh.cells.items():
        base = _LINEAR.get(kind, kind)
        if base in _TETS:
            corners = conn[:, : CORNERS[kind]]
            tets.extend(corners[:, list(t)] for t in _TETS[base])
    if not tets:
        return {"positions": [], "indices": [], "values": {}}
    tet = np.concatenate(tets)
    distance = (mesh.nodes - origin) @ normal
    signs = distance[tet] > 0
    count = signs.sum(axis=1)
    crossing = (count > 0) & (count < 4)
    tet, signs, count = tet[crossing], signs[crossing], count[crossing]
    edges = [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
    positions, field_rows, triangles = [], {name: [] for name in values}, []
    vertex_offset = 0
    for index in range(len(tet)):
        nodes = tet[index]
        points, interpolated = [], {name: [] for name in values}
        for a, b in edges:
            if signs[index, a] != signs[index, b]:
                da, db = distance[nodes[a]], distance[nodes[b]]
                t = da / (da - db)
                points.append(mesh.nodes[nodes[a]] + t * (mesh.nodes[nodes[b]] - mesh.nodes[nodes[a]]))
                for name, field in values.items():
                    interpolated[name].append(field[nodes[a]] + t * (field[nodes[b]] - field[nodes[a]]))
        if len(points) < 3:
            continue
        points_array = np.array(points)
        if len(points) == 4:  # order the quad around its centroid
            centre = points_array.mean(axis=0)
            u = points_array[0] - centre
            v = np.cross(normal, u)
            order = np.argsort(np.arctan2((points_array - centre) @ v, (points_array - centre) @ u))
            points_array = points_array[order]
            for name in values:
                interpolated[name] = [interpolated[name][i] for i in order]
            triangles.extend([(vertex_offset, vertex_offset + 1, vertex_offset + 2), (vertex_offset, vertex_offset + 2, vertex_offset + 3)])
        else:
            triangles.append((vertex_offset, vertex_offset + 1, vertex_offset + 2))
        positions.append(points_array)
        for name in values:
            field_rows[name].extend(interpolated[name])
        vertex_offset += len(points_array)
    if not positions:
        return {"positions": [], "indices": [], "values": {}}
    return {
        "positions": np.round(np.concatenate(positions), 7).ravel().tolist(),
        "indices": [i for tri in triangles for i in tri],
        "values": {name: np.asarray(rows, float).tolist() for name, rows in field_rows.items()},
    }


def streamlines(centres: np.ndarray, velocity: np.ndarray, seeds: np.ndarray, *, step: float, max_steps: int,
                max_gap: float) -> list[list[float]]:
    """RK2 integration through cell-centre velocities (inverse-distance, 4 neighbours).

    Lines stop when they leave the mesh - including entering a solid body,
    where no cells exist - detected as no cell centre within `max_gap`.
    """
    tree = cKDTree(centres)

    def sample(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        distances, neighbours = tree.query(points, k=4)
        weights = 1.0 / np.maximum(distances, 1e-12)
        weights /= weights.sum(axis=1, keepdims=True)
        return np.einsum("pk,pkc->pc", weights, velocity[neighbours]), distances[:, 0] <= max_gap

    position=seeds.astype(float).copy()
    alive = np.ones(len(seeds), bool)
    lines: list[list[float]] = [[] for _ in seeds]
    for _ in range(max_steps):
        if not alive.any():
            break
        active = np.flatnonzero(alive)
        u, inside = sample(position[active])
        speed = np.linalg.norm(u, axis=1)
        ok = inside & (speed > 1e-9)
        for index, point, magnitude, good in zip(active, position[active], speed, ok):
            if good:
                lines[index].extend((*np.round(point, 7).tolist(), round(float(magnitude), 6)))
        alive[active[~ok]] = False
        active=active[ok]
        if not len(active):
            break
        u1 = u[ok] / speed[ok][:, None]
        middle = position[active] + 0.5 * step * u1
        u2, inside2 = sample(middle)
        speed2 = np.linalg.norm(u2, axis=1)
        good = inside2 & (speed2 > 1e-9)
        position[active[good]] += step * u2[good] / speed2[good][:, None]
        alive[active[~good]] = False
    return [line for line in lines if len(line) >= 8]


VOLUME_LIMIT = 600_000  # tetrahedra sent to the browser for interactive cutting


def _compact_round(values: np.ndarray, digits: int = 6) -> list:
    values = np.asarray(values, float)
    scale = float(np.max(np.abs(values))) if values.size else 0.0
    decimals=digits - int(np.ceil(np.log10(scale))) if scale > 0 else 0
    return np.round(values, max(decimals, 0) if decimals < 15 else 15).ravel().tolist()


def volume_payload(mesh: VolumeMesh, scalars: dict[str, np.ndarray] | None = None,
                   vectors: dict[str, np.ndarray] | None = None) -> dict[str, Any] | None:
    """Corner-node tetrahedral view of a volume mesh with nodal fields, for the
    viewport's interactive cutting plane (sliced in the browser by marching tets).

    Returns `None` above :data:`VOLUME_LIMIT` tetrahedra."""
    blocks = []
    cells=[]  # which runs of tets came from which cell type (the mesh viewer scores real tets only)
    for kind, conn in mesh.cells.items():
        linear=_LINEAR.get(kind, kind)
        if linear not in _TETS or not len(conn):
            continue
        corners = conn[:, : CORNERS[kind]]
        blocks.append(np.concatenate([corners[:, list(tet)] for tet in _TETS[linear]]))
        cells.append({"kind": linear, "cells": int(len(conn)), "tets_per_cell": len(_TETS[linear])})
    if not blocks:
        return None
    tets = np.concatenate(blocks)
    if len(tets) > VOLUME_LIMIT:
        return None
    used, inverse = np.unique(tets.ravel(), return_inverse=True)
    return {
        "positions": np.round(mesh.nodes[used], 7).ravel().tolist(),
        "tets": inverse.astype(int).tolist(),
        "cells": cells,
        "scalars": {name: _compact_round(np.asarray(values)[used]) for name, values in (scalars or {}).items()},
        "vectors": {name: _compact_round(np.asarray(values)[used]) for name, values in (vectors or {}).items()},
    }


def port_glyphs(mesh: VolumeMesh, velocity: np.ndarray, kinds: tuple[str, ...] = ("inlet", "outlet"), limit: int = 150) -> list[dict[str, Any]]:
    ports = []
    for group in mesh.boundaries:
        if group.kind not in kinds or not group.elements:
            continue
        nodes = np.unique(np.concatenate([conn[:, : CORNERS[kind]].ravel() for kind, conn in group.elements.items()]))
        nodes = nodes[:: max(1, len(nodes) // limit)]
        ports.append({
            "name": group.name, "kind": group.kind,
            "positions": np.round(mesh.nodes[nodes], 7).ravel().tolist(),
            "vectors": _compact_round(velocity[nodes]),
        })
    return ports
