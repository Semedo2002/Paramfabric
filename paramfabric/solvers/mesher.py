"""Cartesian hex-dominant volume mesher with ray-parity voxelization.

The mesh is a body-aligned Cartesian grid over the flow domain (or the solids
plus one cell of padding for structural analysis). The target cell size is the
characteristic body length divided by `mesh_fineness_ratio`; per axis it is
adjusted so body extents are whole multiples of the cell size with at least
four cells across, and domain margins are rounded to whole cells. Cells whose centres lie inside
a solid body (ray-parity test against the morphed tessellation) are marked
solid; boolean `subtract` tools clear cells, `union` tools add them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from paramfabric.geometry.model import GeometryModel
from paramfabric.geometry.scene import transformed_triangles

DEFAULT_FINENESS = 24.0
MAX_CELLS = 600_000
MIN_CELLS_ACROSS = 4


class MeshingError(ValueError):
    """Raised when the scene cannot be meshed"""


@dataclass
class CartesianMesh:
    origin: list[float]
    spacing: list[float]
    shape: list[int]
    solid: bytearray
    body_bbox: list[list[float]]
    warnings: list[str] = field(default_factory=list)
    surface: dict[str, Any] = field(default_factory=dict)

    def index(self, i: int, j: int, k: int) -> int:
        nx, ny, _ = self.shape
        return (k * ny + j) * nx + i

    def is_solid(self, i: int, j: int, k: int) -> bool:
        return bool(self.solid[self.index(i, j, k)])

    def centre(self, i: int, j: int, k: int) -> tuple[float, float, float]:
        return (
            self.origin[0] + (i + 0.5) * self.spacing[0],
            self.origin[1] + (j + 0.5) * self.spacing[1],
            self.origin[2] + (k + 0.5) * self.spacing[2],
        )

    @property
    def cell_count(self) -> int:
        return self.shape[0] * self.shape[1] * self.shape[2]

    @property
    def solid_count(self) -> int:
        return sum(self.solid)

    def quality(self) -> dict[str, Any]:
        dx, dy, dz = self.spacing
        aspect=max(dx, dy, dz) / max(min(dx, dy, dz), 1e-300)
        body_extent = [self.body_bbox[1][i] - self.body_bbox[0][i] for i in range(3)]
        cells_across=min(
            (body_extent[i] / self.spacing[i] for i in range(3) if body_extent[i] > 0), default=0.0
        )
        if aspect <= 3 and cells_across >= 4:
            grade="good"
        elif aspect <= 6 and cells_across >= 2:
            grade = "fair"
        else:
            grade = "poor"
        return {
            "grade": grade,
            "cells": self.cell_count,
            "fluid_cells": self.cell_count - self.solid_count,
            "solid_cells": self.solid_count,
            "max_aspect_ratio": round(aspect, 3),
            "max_non_orthogonality_deg": 0.0,
            "min_cells_across_body": round(cells_across, 2),
            "surface": self.surface,
        }

    def display(self, limit: int = 30_000) -> dict[str, Any]:
        """Solid voxel centres (strided to `limit`) for the viewport"""
        nx, ny, nz = self.shape
        solid_cells = [index for index, flag in enumerate(self.solid) if flag]
        stride=max(1, math.ceil(len(solid_cells) / limit))
        centres=[]
        for index in solid_cells[::stride]:
            i = index % nx
            j = (index // nx) % ny
            k = index // (nx * ny)
            centres.extend(round(value, 7) for value in self.centre(i, j, k))
        return {
            "origin": self.origin,
            "spacing": self.spacing,
            "shape": self.shape,
            "solid_centres": centres,
            "stride": stride,
        }


def _surface_quality(triangles: list[float]) -> dict[str, Any]:
    if not triangles:
        return {"triangles": 0}
    worst_angle = 180.0
    worst_aspect = 1.0
    for index in range(len(triangles) // 9):
        points = [triangles[index * 9 + 3 * v : index * 9 + 3 * v + 3] for v in range(3)]
        lengths = [math.dist(points[v], points[(v + 1) % 3]) for v in range(3)]
        if min(lengths) <= 0:
            continue
        a, b, c = lengths
        for opposite, side1, side2 in ((a, b, c), (b, c, a), (c, a, b)):
            cosine = max(-1.0, min(1.0, (side1 ** 2 + side2 ** 2 - opposite ** 2) / (2 * side1 * side2)))
            worst_angle = min(worst_angle, math.degrees(math.acos(cosine)))
        worst_aspect = max(worst_aspect, max(lengths) / min(lengths))
    return {
        "triangles": len(triangles) // 9,
        "min_angle_deg": round(worst_angle, 2),
        "max_edge_ratio": round(worst_aspect, 2),
    }


def _voxelize(triangles: list[float], origin: list[float], spacing: list[float], shape: list[int]) -> bytearray:
    nx, ny, nz = shape
    inside = bytearray(nx * ny * nz)
    columns: dict[tuple[int, int], list[int]] = {}
    count = len(triangles) // 9
    for t in range(count):
        ys = triangles[t * 9 + 1 : t * 9 + 9 : 3]
        zs = triangles[t * 9 + 2 : t * 9 + 9 : 3]
        j0 = max(0, math.floor((min(ys) - origin[1]) / spacing[1] - 0.5))
        j1 = min(ny - 1, math.ceil((max(ys) - origin[1]) / spacing[1] - 0.5))
        k0 = max(0, math.floor((min(zs) - origin[2]) / spacing[2] - 0.5))
        k1 = min(nz - 1, math.ceil((max(zs) - origin[2]) / spacing[2] - 0.5))
        for j in range(j0, j1 + 1):
            for k in range(k0, k1 + 1):
                columns.setdefault((j, k), []).append(t)
    for (j, k), candidates in columns.items():
        # Offset the ray slightly to avoid passing exactly through shared edges
        y = origin[1] + (j + 0.5) * spacing[1] + 1.37e-9 * spacing[1]
        z = origin[2] + (k + 0.5) * spacing[2] + 2.71e-9 * spacing[2]
        hits = []
        for t in candidates:
            base = t * 9
            ax, ay, az = triangles[base : base + 3]
            bx, by, bz = triangles[base + 3 : base + 6]
            cx, cy, cz = triangles[base + 6 : base + 9]
            # barycentric coordinates of (y, z) in the triangle projected to the YZ plane
            d = (by - cy) * (az - cz) - (bz - cz) * (ay - cy)
            if abs(d) < 1e-30:
                continue
            l1 = ((by - cy) * (z - cz) - (bz - cz) * (y - cy)) / d
            l2 = ((cy - ay) * (z - cz) - (cz - az) * (y - cy)) / d
            l3 = 1 - l1 - l2
            if l1 < 0 or l2 < 0 or l3 < 0:
                continue
            hits.append(l1 * ax + l2 * bx + l3 * cx)
        if len(hits) < 2:
            continue
        hits.sort()
        for start, end in zip(hits[0::2], hits[1::2]):
            i0 = max(0, math.ceil((start - origin[0]) / spacing[0] - 0.5))
            i1 = min(nx - 1, math.floor((end - origin[0]) / spacing[0] - 0.5))
            row=(k * ny + j) * nx
            for i in range(i0, i1 + 1):
                inside[row + i] = 1
    return inside


def build_mesh(
    models: list[GeometryModel],
    scene: dict[str, Any],
    fineness: float = DEFAULT_FINENESS,
    *,
    max_cells: int = MAX_CELLS,
) -> CartesianMesh:
    """Mesh the resolved scene; uses the flow domain if one is defined"""
    if fineness <= 0:
        raise MeshingError("mesh_fineness_ratio must be positive")
    solids = [entry for entry in scene["bodies"] if entry["role"] == "solid"] + scene.get("derived", [])
    tools = [entry for entry in scene["bodies"] if entry["role"] == "tool"]
    if not solids:
        raise MeshingError("there is no solid geometry to mesh")
    low = [min(entry["bbox_min"][i] for entry in solids) for i in range(3)]
    high = [max(entry["bbox_max"][i] for entry in solids) for i in range(3)]
    extents=[high[i] - low[i] for i in range(3)]
    characteristic = max(extents)
    if characteristic <= 0:
        raise MeshingError("geometry has zero size")
    warnings: list[str] = []
    flow_domain = scene.get("flow_domain")
    size = characteristic / fineness
    previous_shape: list[int] = []
    while True:
        origin, spacing, shape = [], [], []
        for axis in range(3):
            if extents[axis] > 0:
                body_cells = max(MIN_CELLS_ACROSS, round(extents[axis] / size))
                step = extents[axis] / body_cells
            else:
                body_cells, step = 1, size
            if flow_domain:
                below = max(0, round((low[axis] - flow_domain["bbox_min"][axis]) / step))
                above = max(0, round((flow_domain["bbox_max"][axis] - high[axis]) / step))
            else:
                below=above = 1
            origin.append(low[axis] - below * step)
            spacing.append(step)
            shape.append(below + body_cells + above)
        if shape[0] * shape[1] * shape[2] <= max_cells:
            break
        if shape == previous_shape and size > 4 * max(extents):
            raise MeshingError(
                f"the minimum mesh ({MIN_CELLS_ACROSS} cells across each body axis) needs "
                f"{shape[0] * shape[1] * shape[2]:,} cells, above the {max_cells:,}-cell limit"
            )
        previous_shape = shape
        size *= 1.15
        if not warnings:
            warnings.append(f"Mesh was coarsened to stay below {max_cells:,} cells.")
    domain_low = origin
    solid=bytearray(shape[0] * shape[1] * shape[2])
    all_triangles: list[float] = []
    for entry in solids:
        triangles = transformed_triangles(models, entry)
        all_triangles.extend(triangles)
        mask = _voxelize(triangles, domain_low, spacing, shape)
        for index, flag in enumerate(mask):
            if flag:
                solid[index] = 1
    for entry in tools:
        mask = _voxelize(transformed_triangles(models, entry), domain_low, spacing, shape)
        for index, flag in enumerate(mask):
            if flag:
                solid[index] = 0
    if not any(solid):
        warnings.append("No cell centres fell inside the geometry; refine the mesh or check that bodies are closed solids.")
    return CartesianMesh(
        origin=list(domain_low), spacing=spacing, shape=shape, solid=solid,
        body_bbox=[low, high], warnings=warnings, surface=_surface_quality(all_triangles),
    )
