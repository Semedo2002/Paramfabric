"""ASCII and binary STL reader and writer with coplanar facet grouping"""

from __future__ import annotations

import math
import re
import struct

from paramfabric.geometry.model import Body, Face, GeometryModel

_MAX_TRIANGLES = 500_000
_MAX_FACE_GROUPS = 256


class StlError(ValueError):
    """Raised for malformed STL content"""


def _is_binary(data: bytes) -> bool:
    if len(data) < 84:
        return False
    (count,) = struct.unpack_from("<I", data, 80)
    return 84 + count * 50 == len(data)


def _group_facets(triangles: list[float]) -> tuple[list[int], list[Face]]:
    keys: dict[tuple[int, int, int, int], int] = {}
    faces: list[Face] = []
    face_ids: list[int] = []
    overflow: int | None = None
    for index in range(len(triangles) // 9):
        a = triangles[index * 9 : index * 9 + 3]
        b = triangles[index * 9 + 3 : index * 9 + 6]
        c=triangles[index * 9 + 6 : index * 9 + 9]
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        nx, ny, nz = nx / length, ny / length, nz / length
        offset = nx * a[0] + ny * a[1] + nz * a[2]
        key = (round(nx * 1000), round(ny * 1000), round(nz * 1000), round(offset * 1e5))
        face_id=keys.get(key)
        if face_id is None:
            if len(faces) < _MAX_FACE_GROUPS - 1:
                face_id = len(faces)
                keys[key] = face_id
                faces.append(Face(id=face_id, name=f"Facet region {face_id + 1}", surface="plane"))
            else:
                if overflow is None:
                    overflow = len(faces)
                    faces.append(Face(id=overflow, name="Curved / remaining facets", surface="facets", approximate=True))
                face_id=overflow
        face_ids.append(face_id)
    return face_ids, faces


def read_stl(data: bytes, filename: str = "model.stl", unit: str = "mm", unit_scale: float = 1e-3) -> GeometryModel:
    solids: list[tuple[str, list[float]]] = []
    if _is_binary(data):
        (count,) = struct.unpack_from("<I", data, 80)
        if count > _MAX_TRIANGLES:
            raise StlError(f"STL has {count} triangles; the limit is {_MAX_TRIANGLES}")
        coordinates: list[float] = []
        for index in range(count):
            values = struct.unpack_from("<12f", data, 84 + index * 50)
            coordinates.extend(value * unit_scale for value in values[3:12])
        header = data[:80].split(b"\0", 1)[0].decode("ascii", "replace").strip()
        solids.append((header.removeprefix("solid").strip(), coordinates))
    else:
        text = data.decode("utf-8", "replace")
        if not re.match(r"\s*solid\b", text):
            raise StlError("not an STL file (expected 'solid' header or binary triangle count)")
        number = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
        vertex = re.compile(rf"vertex\s+({number})\s+({number})\s+({number})")
        for match in re.finditer(r"solid[ \t]*([^\r\n]*)(.*?)endsolid", text, re.DOTALL):
            coordinates = [
                float(value) * unit_scale for found in vertex.finditer(match.group(2)) for value in found.groups()
            ]
            if len(coordinates) // 9 > _MAX_TRIANGLES:
                raise StlError(f"STL exceeds {_MAX_TRIANGLES} triangles")
            solids.append((match.group(1).strip(), coordinates[: len(coordinates) - len(coordinates) % 9]))
    bodies = []
    base = filename.rsplit(".", 1)[0]
    for index, (name, coordinates) in enumerate(solids, start=1):
        if not coordinates:
            continue
        face_ids, faces = _group_facets(coordinates)
        body = Body(
            id=f"b{index}",
            name=name or (base if len(solids) == 1 else f"{base} body {index}"),
            kind="mesh",
            triangles=coordinates,
            face_ids=face_ids,
            faces=faces,
        )
        body.vertex_count = len({tuple(coordinates[i : i + 3]) for i in range(0, len(coordinates), 3)})
        body.finalize()
        bodies.append(body)
    if not bodies:
        raise StlError("STL file contains no triangles")
    return GeometryModel(
        id="", name=bodies[0].name if len(bodies) == 1 else base, filename=filename, format="stl",
        length_unit=unit, bodies=bodies,
        warnings=[f"STL carries no units; coordinates were interpreted as {unit}."],
    )


def write_stl(name: str, triangles: list[float], unit_scale: float = 1e3) -> str:
    """Write ASCII STL; `unit_scale` converts metres to the output unit"""
    lines = [f"solid {name}"]
    for index in range(len(triangles) // 9):
        a = triangles[index * 9 : index * 9 + 3]
        b = triangles[index * 9 + 3 : index * 9 + 6]
        c=triangles[index * 9 + 6 : index * 9 + 9]
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        lines.append(f"  facet normal {nx / length:.6e} {ny / length:.6e} {nz / length:.6e}")
        lines.append("    outer loop")
        for vertex in (a, b, c):
            lines.append("      vertex " + " ".join(f"{v * unit_scale:.6e}" for v in vertex))
        lines.extend(("    endloop", "  endfacet"))
    lines.append(f"endsolid {name}")
    return "\n".join(lines) + "\n"
