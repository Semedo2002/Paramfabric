"""IGES 5.x reader for wireframe display and bounding geometry.

Curves (lines 110, circular arcs 100, points 116, rational B-spline curves
126, copious data 106) are sampled into polylines with their transformation
matrix (124) applied. Surface entities contribute control points to the
bounding box only; IGES faces are not tessellated.
"""

from __future__ import annotations

import math

from paramfabric.geometry.model import Body, GeometryModel
from paramfabric.geometry.tessellate import Vec, sample_bspline

_UNITS = {
    1: ("inch", 0.0254), 2: ("mm", 1e-3), 4: ("ft", 0.3048), 5: ("mi", 1609.344),
    6: ("m", 1.0), 7: ("km", 1e3), 8: ("mil", 2.54e-5), 9: ("um", 1e-6), 10: ("cm", 1e-2),
    11: ("uin", 2.54e-8),
}
_SURFACES = {114, 118, 120, 122, 128, 140, 143, 144, 190, 192, 194, 196, 198}


class IgesError(ValueError):
    """Raised for malformed IGES content"""


def _split_parameters(text: str, delimiter: str, terminator: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    index=0
    while index < len(text):
        char = text[index]
        if char == terminator:
            break
        if char == delimiter:
            fields.append("".join(current).strip())
            current = []
            index += 1
            continue
        if char.upper() == "H" and current and "".join(current).strip().isdigit():
            length = int("".join(current).strip())
            fields.append(text[index + 1 : index + 1 + length])
            current = []
            index += 1 + length
            while index < len(text) and text[index] not in (delimiter, terminator):
                index += 1
            if index < len(text) and text[index] == delimiter:
                index += 1
            continue
        current.append(char)
        index += 1
    fields.append("".join(current).strip())
    return fields


def _number(value: str, default: float = 0.0) -> float:
    value = value.strip().upper().replace("D", "E")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def read_iges(text: str, filename: str = "model.igs") -> GeometryModel:
    sections: dict[str, list[str]] = {"S": [], "G": [], "D": [], "P": [], "T": []}
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if len(line) < 73:
            continue
        section = line[72]
        if section in sections:
            sections[section].append(line)
    if not sections["D"] or not sections["P"]:
        raise IgesError("not an IGES file (missing directory or parameter section)")

    global_text = "".join(line[:72] for line in sections["G"])
    delimiter, terminator = ",", ";"
    if global_text.startswith("1H"):
        delimiter = global_text[2]
        if global_text[3:5] == "1H":
            terminator = global_text[5]
    global_fields=_split_parameters(global_text, delimiter, terminator)
    unit_flag = int(_number(global_fields[13], 2)) if len(global_fields) > 13 else 2
    unit_symbol, unit_scale = _UNITS.get(unit_flag, ("mm", 1e-3))
    product = global_fields[2].strip() if len(global_fields) > 2 else ""

    directory: dict[int, dict[str, int]] = {}
    for index in range(0, len(sections["D"]) - 1, 2):
        first, second = sections["D"][index], sections["D"][index + 1]
        pointer = index + 1
        try:
            directory[pointer] = {
                "type": int(first[0:8]),
                "parameter": int(first[8:16]),
                "transform": int(first[48:56] or 0),
                "form": int(second[32:40] or 0),
            }
        except ValueError:
            continue

    parameter_text: dict[int, list[str]] = {}
    for line in sections["P"]:
        try:
            owner = int(line[64:72])
        except ValueError:
            continue
        parameter_text.setdefault(owner, []).append(line[:64])
    parameters = {
        owner: _split_parameters("".join(chunks), delimiter, terminator)
        for owner, chunks in parameter_text.items()
    }

    def numbers(owner: int) -> list[float]:
        return [_number(value) for value in parameters.get(owner, [])[1:]]

    def transform(pointer: int, depth: int = 0) -> list[float] | None:
        entry = directory.get(pointer)
        if not entry or entry["type"] != 124 or depth > 8:
            return None
        values = numbers(pointer)
        if len(values) < 12:
            return None
        matrix = values[:12]
        parent = transform(entry["transform"], depth + 1) if entry["transform"] else None
        if parent:
            matrix = _compose(parent, matrix)
        return matrix

    polylines: list[list[Vec]] = []
    extra_points: list[Vec] = []
    counts: dict[int, int] = {}
    skipped = 0
    for pointer, entry in directory.items():
        kind = entry["type"]
        counts[kind] = counts.get(kind, 0) + 1
        values = numbers(pointer)
        try:
            if kind == 110 and len(values) >= 6:
                curve: list[Vec] = [tuple(values[0:3]), tuple(values[3:6])]  # type: ignore[list-item]
            elif kind == 100 and len(values) >= 7:
                zt, cx, cy, sx, sy, ex, ey = values[:7]
                radius = math.hypot(sx - cx, sy - cy)
                start, end = math.atan2(sy - cy, sx - cx), math.atan2(ey - cy, ex - cx)
                sweep = (end - start) % (2 * math.pi) or 2 * math.pi
                segments = max(4, math.ceil(sweep / (2 * math.pi) * 48))
                curve = [
                    (cx + radius * math.cos(start + sweep * k / segments),
                     cy + radius * math.sin(start + sweep * k / segments), zt)
                    for k in range(segments + 1)
                ]
            elif kind == 116 and len(values) >= 3:
                extra_points.append(_apply(transform(entry["transform"]), tuple(values[0:3])))  # type: ignore[arg-type]
                continue
            elif kind == 126 and len(values) >= 6:
                upper, degree = int(values[0]), int(values[1])
                count = upper + 1
                knots_start = 6
                knots = values[knots_start : knots_start + upper + degree + 2]
                weights = values[knots_start + len(knots) : knots_start + len(knots) + count]
                control_start = knots_start + len(knots) + count
                control = [tuple(values[control_start + 3 * i : control_start + 3 * i + 3]) for i in range(count)]
                curve=sample_bspline(degree, knots, control, weights, max(16, 4 * count))  # type: ignore[arg-type]
            elif kind == 106 and len(values) >= 3:
                form = entry["form"]
                count=int(values[1])
                if form == 1:
                    zt = values[2]
                    curve = [(values[3 + 2 * i], values[4 + 2 * i], zt) for i in range(count)]
                else:
                    curve = [tuple(values[2 + 3 * i : 5 + 3 * i]) for i in range(count)]  # type: ignore[misc]
            elif kind == 128 and len(values) >= 8:
                k1, k2, m1, m2 = (int(v) for v in values[:4])
                knot_count = (k1 + m1 + 2) + (k2 + m2 + 2)
                weight_count = (k1 + 1) * (k2 + 1)
                control_start = 9 + knot_count + weight_count
                for i in range(weight_count):
                    point=values[control_start + 3 * i : control_start + 3 * i + 3]
                    if len(point) == 3:
                        extra_points.append(_apply(transform(entry["transform"]), tuple(point)))  # type: ignore[arg-type]
                continue
            else:
                if kind not in _SURFACES and kind not in {124, 102, 142, 314, 402, 406, 212, 214, 216, 222, 230}:
                    skipped += 1
                continue
        except (ValueError, IndexError):
            skipped += 1
            continue
        matrix = transform(entry["transform"]) if entry["transform"] else None
        polylines.append([_apply(matrix, point) for point in curve])

    if not polylines and not extra_points:
        raise IgesError("IGES file contains no supported curve or point entities")
    body = Body(id="b1", name=product or filename.rsplit(".", 1)[0], kind="wireframe")
    body.edges = [[c * unit_scale for point in line for c in point] for line in polylines if len(line) >= 2]
    body.vertex_count = sum(len(line) for line in polylines)
    body.finalize([[c * unit_scale for c in point] for point in extra_points])
    warnings = ["IGES surfaces are not tessellated; the body is shown as wireframe with its bounding volume."]
    if skipped:
        warnings.append(f"{skipped} unsupported IGES entities were skipped.")
    return GeometryModel(
        id="", name=body.name, filename=filename, format="iges", length_unit=unit_symbol,
        bodies=[body], warnings=warnings,
    )


def _compose(outer: list[float], inner: list[float]) -> list[float]:
    def matrix(values: list[float]) -> list[list[float]]:
        return [values[0:4], values[4:8], values[8:12], [0, 0, 0, 1]]

    a, b = matrix(outer), matrix(inner)
    product = [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(3)]
    return [value for row in product for value in row]


def _apply(matrix: list[float] | None, point: Vec) -> Vec:
    if not matrix:
        return (float(point[0]), float(point[1]), float(point[2]))
    x, y, z = point
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3],
        matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7],
        matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11],
    )
