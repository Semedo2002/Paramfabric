"""Unstructured volume-mesh container shared by the meshers and 3D solvers"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

VOLUME_TYPES = ("tet4", "tet10", "hex8", "hex20", "prism6", "pyramid5")
SURFACE_TYPES = ("tri3", "tri6", "quad4", "quad8")
CORNERS = {"tet4": 4, "tet10": 4, "hex8": 8, "hex20": 8, "prism6": 6, "pyramid5": 5, "tri3": 3, "tri6": 3, "quad4": 4, "quad8": 4, "line2": 2}
VTK_CELL = {"tet4": 10, "tet10": 24, "hex8": 12, "hex20": 25, "prism6": 13, "pyramid5": 14, "tri3": 5, "tri6": 22, "quad4": 9, "quad8": 23, "line2": 3}
# gmsh and VTK share corner ordering; tet10 and hex20 mid-edge ordering differs
_HEX20_GMSH_TO_VTK = [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 13, 9, 16, 18, 19, 17, 10, 12, 14, 15]
_TET10_GMSH_TO_VTK = [0, 1, 2, 3, 4, 5, 6, 7, 9, 8]
_TO_VTK = {"hex20": _HEX20_GMSH_TO_VTK, "tet10": _TET10_GMSH_TO_VTK}


@dataclass
class BoundaryGroup:
    """A named set of boundary faces (one CAD face, a patch, or a BC region)"""

    name: str
    kind: str
    elements: dict[str, np.ndarray] = field(default_factory=dict)
    source_faces: list[int] = field(default_factory=list)

    def corner_faces(self) -> list[np.ndarray]:
        return [conn[:, : CORNERS[kind]] for kind, conn in self.elements.items()]

    @property
    def nodes(self) -> np.ndarray:
        arrays = [conn.ravel() for conn in self.elements.values()]
        return np.unique(np.concatenate(arrays)) if arrays else np.zeros(0, int)


@dataclass
class VolumeMesh:
    nodes: np.ndarray
    cells: dict[str, np.ndarray]
    boundaries: list[BoundaryGroup] = field(default_factory=list)
    order: int = 1
    statistics: dict[str, Any] = field(default_factory=dict)

    @property
    def cell_count(self) -> int:
        return int(sum(len(conn) for conn in self.cells.values()))

    def boundary(self, name: str) -> BoundaryGroup:
        for group in self.boundaries:
            if group.name == name:
                return group
        raise KeyError(f"no boundary group {name!r}")

    def boundaries_of_kind(self, kind: str) -> list[BoundaryGroup]:
        return [group for group in self.boundaries if group.kind == kind]

    def linear(self) -> VolumeMesh:
        """Corner-node-only view (for the finite-volume solver)"""
        mapping={"tet10": "tet4", "hex20": "hex8", "tri6": "tri3", "quad8": "quad4"}
        cells = {mapping.get(kind, kind): conn[:, : CORNERS[kind]] for kind, conn in self.cells.items()}
        groups = [
            BoundaryGroup(g.name, g.kind, {mapping.get(k, k): c[:, : CORNERS[k]] for k, c in g.elements.items()}, g.source_faces)
            for g in self.boundaries
        ]
        return VolumeMesh(self.nodes, cells, groups, 1, dict(self.statistics))

    def quality(self) -> dict[str, Any]:
        """Element counts plus tetrahedral shape quality (normalized radius ratio)"""
        counts = {kind: int(len(conn)) for kind, conn in self.cells.items() if len(conn)}
        result: dict[str, Any] = {"nodes": int(len(self.nodes)), "cells": self.cell_count, "cell_types": counts}
        tets = [conn[:, :4] for kind, conn in self.cells.items() if kind in {"tet4", "tet10"}]
        if tets:
            corners = self.nodes[np.concatenate(tets)]
            a, b, c, d = (corners[:, i] for i in range(4))
            volume = np.abs(np.einsum("ij,ij->i", b - a, np.cross(c - a, d - a))) / 6
            edges = [b - a, c - a, d - a, c - b, d - b, d - c]
            rms = np.sqrt(sum(np.einsum("ij,ij->i", e, e) for e in edges) / 6)
            # 6√2·V / l_rms³ equals 1 for a regular tetrahedron
            shape = 6 * np.sqrt(2) * volume / np.maximum(rms ** 3, 1e-300)
            result.update(
                min_tet_quality=float(shape.min()),
                mean_tet_quality=float(shape.mean()),
                poor_tets=int((shape < 0.1).sum()),
            )
            quality = float(shape.min())
            result["grade"] = "good" if quality >= 0.2 else "fair" if quality >= 0.05 else "poor"
        else:
            result["grade"] = "good"
        result.update(self.statistics)
        return result


def write_vtk_unstructured(
    path,
    mesh: VolumeMesh,
    point_data: dict[str, np.ndarray] | None = None,
    cell_data: dict[str, np.ndarray] | None = None,
    title: str = "paramfabric",
) -> None:
    """Write a legacy ASCII VTK UNSTRUCTURED_GRID (scalars and 3-vectors)"""
    lines = ["# vtk DataFile Version 3.0", title[:255], "ASCII", "DATASET UNSTRUCTURED_GRID", f"POINTS {len(mesh.nodes)} double"]
    lines += [f"{x:.9g} {y:.9g} {z:.9g}" for x, y, z in mesh.nodes]
    # volume cells, plus shell (surface) and beam (line2) cells for structural member models
    kinds = [kind for kind in (*VOLUME_TYPES, *SURFACE_TYPES, "line2") if kind in mesh.cells and len(mesh.cells[kind])]
    total = sum(len(mesh.cells[k]) for k in kinds)
    size = sum(len(mesh.cells[k]) * (mesh.cells[k].shape[1] + 1) for k in kinds)
    lines.append(f"CELLS {total} {size}")
    for kind in kinds:
        conn = mesh.cells[kind][:, _TO_VTK[kind]] if kind in _TO_VTK else mesh.cells[kind]
        lines += [f"{conn.shape[1]} " + " ".join(map(str, row)) for row in conn]
    lines.append(f"CELL_TYPES {total}")
    for kind in kinds:
        lines += [str(VTK_CELL[kind])] * len(mesh.cells[kind])

    def block(data: dict[str, np.ndarray]) -> None:
        for name, values in data.items():
            values = np.asarray(values, float)
            if values.ndim == 2 and values.shape[1] == 3:
                lines.append(f"VECTORS {name} double")
                lines.extend(f"{a:.9g} {b:.9g} {c:.9g}" for a, b, c in values)
            else:
                lines.extend((f"SCALARS {name} double 1", "LOOKUP_TABLE default"))
                lines.extend(f"{v:.9g}" for v in values.ravel())

    if point_data:
        lines.append(f"POINT_DATA {len(mesh.nodes)}")
        block(point_data)
    if cell_data:
        lines.append(f"CELL_DATA {total}")
        block(cell_data)
    with open(path, "w", encoding="ascii") as handle:
        handle.write("\n".join(lines) + "\n")


def read_vtk_unstructured(path) -> dict[str, Any]:
    """Read back point/cell data written by :func:`write_vtk_unstructured`"""
    tokens = open(path, encoding="ascii").read().split()
    result: dict[str, Any] = {"point_data": {}, "cell_data": {}}
    position = tokens.index("POINTS")
    count = int(tokens[position + 1])
    result["points"] = np.array(tokens[position + 3 : position + 3 + 3 * count], float).reshape(-1, 3)
    section = None
    index = position
    while index < len(tokens):
        token=tokens[index]
        if token in {"POINT_DATA", "CELL_DATA"}:
            section = ("point_data" if token == "POINT_DATA" else "cell_data", int(tokens[index + 1]))
            index += 2
        elif token == "SCALARS" and section:
            name, n = tokens[index + 1], section[1]
            start = index + 6
            result[section[0]][name] = np.array(tokens[start : start + n], float)
            index = start + n
        elif token == "VECTORS" and section:
            name, n = tokens[index + 1], section[1]
            start = index + 3
            result[section[0]][name] = np.array(tokens[start : start + 3 * n], float).reshape(-1, 3)
            index = start + 3 * n
        else:
            index += 1
    return result
