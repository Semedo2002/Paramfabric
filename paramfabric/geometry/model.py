"""In-memory and JSON representation of imported CAD geometry"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from paramfabric.geometry.tessellate import triangle_area

AXES = ("x", "y", "z")


@dataclass
class Face:
    """A selectable face (B-rep face or coplanar facet region)"""

    id: int
    name: str
    surface: str
    area: float = 0.0
    approximate: bool = False
    normal: list[float] | None = None
    centroid: list[float] | None = None


@dataclass
class Body:
    """One solid, shell, mesh, or wireframe body; coordinates are metres"""

    id: str
    name: str
    kind: str
    triangles: list[float] = field(default_factory=list)
    face_ids: list[int] = field(default_factory=list)
    faces: list[Face] = field(default_factory=list)
    edges: list[list[float]] = field(default_factory=list)
    vertex_count: int = 0
    bbox_min: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    bbox_max: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    dimension_parameters: dict[str, str] = field(default_factory=dict)
    color: list[float] | None = None
    path: str = ""

    def finalize(self, extra_points: list[list[float]] | None = None) -> None:
        """Compute the bounding box and per-face area, normal, and centroid"""
        xs: list[float] = self.triangles[0::3]
        ys: list[float] = self.triangles[1::3]
        zs: list[float] = self.triangles[2::3]
        for polyline in self.edges:
            xs.extend(polyline[0::3])
            ys.extend(polyline[1::3])
            zs.extend(polyline[2::3])
        for point in extra_points or []:
            xs.append(point[0])
            ys.append(point[1])
            zs.append(point[2])
        if xs:
            self.bbox_min = [min(xs), min(ys), min(zs)]
            self.bbox_max = [max(xs), max(ys), max(zs)]
        accumulators: dict[int, list[float]] = {}
        for triangle, face_id in enumerate(self.face_ids):
            base = triangle * 9
            a=self.triangles[base : base + 3]
            b = self.triangles[base + 3 : base + 6]
            c = self.triangles[base + 6 : base + 9]
            area = triangle_area(a, b, c)
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            entry = accumulators.setdefault(face_id, [0.0] * 7)
            entry[0] += area
            entry[1] += nx / 2
            entry[2] += ny / 2
            entry[3] += nz / 2
            entry[4] += area * (a[0] + b[0] + c[0]) / 3
            entry[5] += area * (a[1] + b[1] + c[1]) / 3
            entry[6] += area * (a[2] + b[2] + c[2]) / 3
        for face in self.faces:
            entry=accumulators.get(face.id)
            if not entry or entry[0] <= 0:
                continue
            face.area = entry[0]
            length = math.sqrt(entry[1] ** 2 + entry[2] ** 2 + entry[3] ** 2) or 1.0
            if face.surface == "plane":
                face.normal = [round(entry[i] / length, 9) for i in (1, 2, 3)]
            face.centroid = [entry[i] / entry[0] for i in (4, 5, 6)]

    @property
    def extents(self) -> list[float]:
        return [self.bbox_max[i] - self.bbox_min[i] for i in range(3)]

    @property
    def surface_area(self) -> float:
        return sum(face.area for face in self.faces)


@dataclass
class GeometryModel:
    """An imported CAD document: one or more bodies plus provenance"""

    id: str
    name: str
    filename: str
    format: str
    length_unit: str
    bodies: list[Body] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assembly: str = ""
    features: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    kernel: str = "faceted"  # "occ" when an exact B-Rep is stored beside the model

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeometryModel:
        bodies = []
        for raw in data.get("bodies", []):
            body_data = dict(raw)
            body_data["faces"] = [Face(**face) for face in body_data.get("faces", [])]
            bodies.append(Body(**body_data))
        return cls(**{**data, "bodies": bodies})

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "filename": self.filename,
            "format": self.format,
            "length_unit": self.length_unit,
            "assembly": self.assembly,
            "kernel": self.kernel,
            "warnings": self.warnings,
            "features": self.features,
            "bodies": [
                {
                    "id": body.id,
                    "name": body.name,
                    "kind": body.kind,
                    "bbox_min": body.bbox_min,
                    "bbox_max": body.bbox_max,
                    "dimension_parameters": body.dimension_parameters,
                    "stats": {
                        "faces": len(body.faces),
                        "edges": len(body.edges),
                        "vertices": body.vertex_count,
                        "triangles": len(body.face_ids),
                    },
                }
                for body in self.bodies
            ],
        }
