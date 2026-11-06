"""ISO 10303-21 (STEP AP203/AP214/AP242) boundary-representation reader.

The reader resolves the B-rep topology (solid -> shell -> face -> loop -> edge ->
vertex) and tessellates each face in its surface parameter space:

* planes, cylinders, and cones are tessellated exactly along their boundary;
* lines, circles, ellipses, polylines, and (rational) B-spline edges are sampled;
* other surface types (spheres, tori, B-spline surfaces) are triangulated from
  their boundary polygon and flagged `approximate`.

Assembly placement transforms are not applied; each body is read in the
coordinates of its own shape representation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from paramfabric.geometry.model import Body, Face, GeometryModel
from paramfabric.geometry.tessellate import (
    Vec,
    add,
    distance,
    dot,
    expand_knots,
    frame,
    newell_normal,
    norm,
    sample_bspline,
    scale,
    signed_area,
    sub,
    triangulate,
)


class StepError(ValueError):
    """Raised for malformed or unsupported STEP content"""


@dataclass(frozen=True)
class Ref:
    id: int


@dataclass(frozen=True)
class Typed:
    name: str
    args: list[Any]


Entity = list[tuple[str, list[Any]]]

_PREFIXES = {
    None: 1.0, "EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9, "MEGA": 1e6,
    "KILO": 1e3, "HECTO": 1e2, "DECA": 1e1, "DECI": 1e-1, "CENTI": 1e-2,
    "MILLI": 1e-3, "MICRO": 1e-6, "NANO": 1e-9,
}
_PREFIX_SYMBOLS = {"MILLI": "mm", "CENTI": "cm", "KILO": "km", "MICRO": "um", "DECI": "dm", None: "m"}
_CONVERSION_UNITS = {"INCH": ("inch", 0.0254), "FOOT": ("ft", 0.3048), "MILLIMETRE": ("mm", 1e-3)}
_CIRCLE_SEGMENTS = 48


# Part 21 lexical layer


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)


def _records(data: str) -> list[str]:
    records, current, in_string = [], [], False
    index = 0
    while index < len(data):
        char=data[index]
        if char == "'":
            if in_string and index + 1 < len(data) and data[index + 1] == "'":
                current.append("''")
                index += 2
                continue
            in_string = not in_string
        if char == ";" and not in_string:
            records.append("".join(current).strip())
            current=[]
        else:
            current.append(char)
        index += 1
    return [record for record in records if record]


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def skip(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def peek(self) -> str:
        self.skip()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def expect(self, char: str) -> None:
        if self.peek() != char:
            raise StepError(f"expected {char!r} at offset {self.pos} in {self.text[:80]!r}")
        self.pos += 1

    def identifier(self) -> str:
        self.skip()
        match = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(self.text, self.pos)
        if not match:
            raise StepError(f"expected identifier in {self.text[:80]!r}")
        self.pos = match.end()
        return match.group(0).upper()

    def arguments(self) -> list[Any]:
        self.expect("(")
        values: list[Any] = []
        if self.peek() == ")":
            self.pos += 1
            return values
        while True:
            values.append(self.value())
            separator = self.peek()
            self.pos += 1
            if separator == ")":
                return values
            if separator != ",":
                raise StepError(f"expected ',' or ')' in {self.text[:80]!r}")

    def value(self) -> Any:
        char = self.peek()
        if char == "(":
            return self.arguments()
        if char == "'":
            self.pos += 1
            out=[]
            while self.pos < len(self.text):
                current = self.text[self.pos]
                if current == "'":
                    if self.text[self.pos + 1 : self.pos + 2] == "'":
                        out.append("'")
                        self.pos += 2
                        continue
                    self.pos += 1
                    return "".join(out)
                out.append(current)
                self.pos += 1
            raise StepError("unterminated string")
        if char == "#":
            match = re.compile(r"#(\d+)").match(self.text, self.pos)
            if not match:
                raise StepError("malformed entity reference")
            self.pos = match.end()
            return Ref(int(match.group(1)))
        if char in "$*":
            self.pos += 1
            return None
        if char == "." and re.compile(r"\.[A-Za-z_]").match(self.text, self.pos):
            end = self.text.index(".", self.pos + 1)
            token = self.text[self.pos + 1 : end].upper()
            self.pos = end + 1
            return {"T": True, "F": False}.get(token, token)
        if char == '"':
            end = self.text.index('"', self.pos + 1)
            self.pos = end + 1
            return self.text[self.pos - 1 : end]
        match = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?").match(self.text, self.pos)
        if match:
            self.pos = match.end()
            token = match.group(0)
            return float(token) if any(c in token for c in ".eE") else int(token)
        name=self.identifier()
        return Typed(name, self.arguments())


def parse_part21(text: str) -> tuple[dict[int, Entity], str]:
    """Return `{id: [(type, args), ...]}` and the FILE_NAME header name."""
    clean = _strip_comments(text)
    if "ISO-10303-21" not in clean[:200].upper():
        raise StepError("not an ISO 10303-21 STEP file (missing ISO-10303-21 header)")
    header_name = ""
    header = re.search(r"FILE_NAME\s*\(\s*'((?:[^']|'')*)'", clean, re.IGNORECASE)
    if header:
        header_name = header.group(1).replace("''", "'")
    data_match = re.search(r"\bDATA\s*(?:\([^)]*\))?\s*;(.*?)\bENDSEC\s*;", clean, re.DOTALL | re.IGNORECASE)
    if not data_match:
        raise StepError("STEP file has no DATA section")
    entities: dict[int, Entity] = {}
    for record in _records(data_match.group(1)):
        match = re.match(r"#(\d+)\s*=\s*", record)
        if not match:
            continue
        parser = _Parser(record[match.end() :])
        try:
            if parser.peek() == "(":
                parser.pos += 1
                parts: Entity = []
                while parser.peek() != ")":
                    name = parser.identifier()
                    parts.append((name, parser.arguments()))
                entities[int(match.group(1))] = parts
            else:
                name = parser.identifier()
                entities[int(match.group(1))] = [(name, parser.arguments())]
        except (StepError, ValueError, IndexError):
            continue
    if not entities:
        raise StepError("STEP DATA section contains no readable entities")
    return entities, header_name


# Semantic layer


class _StepReader:
    def __init__(self, entities: dict[int, Entity]) -> None:
        self.entities = entities
        self.edge_cache: dict[int, tuple[list[Vec], bool]] = {}
        self.approximate_edges = 0

    # entity helpers
    def kind(self, ref: Any) -> str:
        if not isinstance(ref, Ref) or ref.id not in self.entities:
            return ""
        parts = self.entities[ref.id]
        return parts[0][0] if len(parts) == 1 else "COMPLEX"

    def args(self, ref: Ref) -> list[Any]:
        return self.entities[ref.id][0][1]

    def part(self, ref: Ref, name: str) -> list[Any] | None:
        for part_name, part_args in self.entities.get(ref.id, []):
            if part_name == name:
                return part_args
        return None

    def point(self, ref: Ref) -> Vec:
        if self.kind(ref) == "VERTEX_POINT":
            ref = self.args(ref)[1]
        coordinates = [float(value) for value in self.args(ref)[1]]
        while len(coordinates) < 3:
            coordinates.append(0.0)
        return (coordinates[0], coordinates[1], coordinates[2])

    def direction(self, ref: Any) -> Vec | None:
        if not isinstance(ref, Ref):
            return None
        if self.kind(ref) == "VECTOR":
            return self.direction(self.args(ref)[1])
        values = [float(value) for value in self.args(ref)[1]]
        while len(values) < 3:
            values.append(0.0)
        return (values[0], values[1], values[2])

    def placement(self, ref: Ref) -> tuple[Vec, Vec, Vec, Vec]:
        arguments = self.args(ref)
        origin = self.point(arguments[1])
        axis = self.direction(arguments[2]) if len(arguments) > 2 else None
        reference = self.direction(arguments[3]) if len(arguments) > 3 else None
        x, y, z = frame(axis or (0.0, 0.0, 1.0), reference)
        return origin, x, y, z

    # units
    def length_unit(self) -> tuple[str, float]:
        for parts in self.entities.values():
            names = {name for name, _ in parts}
            if "LENGTH_UNIT" not in names:
                continue
            for name, arguments in parts:
                if name == "SI_UNIT" and arguments and arguments[-1] in {"METRE", "METER"}:
                    prefix = arguments[0] if isinstance(arguments[0], str) else None
                    if prefix in _PREFIXES:
                        symbol = _PREFIX_SYMBOLS.get(prefix, f"{_PREFIXES[prefix]:g} m")
                        return symbol, _PREFIXES[prefix]
                if name == "CONVERSION_BASED_UNIT" and arguments:
                    label = str(arguments[0]).strip().upper()
                    if label in _CONVERSION_UNITS:
                        return _CONVERSION_UNITS[label]
        return "mm", 1e-3

    # curves
    def basis_curve(self, ref: Ref) -> Ref:
        seen = 0
        while seen < 8:
            kind = self.kind(ref)
            if kind in {"SURFACE_CURVE", "SEAM_CURVE", "INTERSECTION_CURVE"}:
                ref = self.args(ref)[1]
            elif kind == "TRIMMED_CURVE":
                ref = self.args(ref)[1]
            else:
                return ref
            seen += 1
        return ref

    def edge_points(self, edge_ref: Ref) -> list[Vec]:
        """Sample an EDGE_CURVE from its start vertex to its end vertex"""
        if edge_ref.id in self.edge_cache:
            return self.edge_cache[edge_ref.id][0]
        arguments = self.args(edge_ref)
        start, end = self.point(arguments[1]), self.point(arguments[2])
        same_sense = arguments[4] is not False if len(arguments) > 4 else True
        curve = self.basis_curve(arguments[3])
        kind=self.kind(curve)
        closed = distance(start, end) < 1e-12
        exact = True
        if kind == "LINE":
            points = [start, end]
        elif kind in {"CIRCLE", "ELLIPSE"}:
            origin, x, y, _ = self.placement(self.args(curve)[1])
            radius_a = float(self.args(curve)[2])
            radius_b=float(self.args(curve)[3]) if kind == "ELLIPSE" else radius_a

            def angle(point: Vec) -> float:
                offset = sub(point, origin)
                return math.atan2(dot(offset, y) / radius_b, dot(offset, x) / radius_a)

            first, last = angle(start), angle(end)
            if same_sense:
                sweep = (last - first) % (2 * math.pi)
            else:
                sweep = -((first - last) % (2 * math.pi))
            if closed or abs(sweep) < 1e-12:
                sweep = 2 * math.pi if same_sense else -2 * math.pi
            segments = max(2, math.ceil(abs(sweep) / (2 * math.pi) * _CIRCLE_SEGMENTS))
            points = []
            for step in range(segments + 1):
                theta = first + sweep * step / segments
                points.append(
                    add(origin, add(scale(x, radius_a * math.cos(theta)), scale(y, radius_b * math.sin(theta))))
                )
            points[0], points[-1] = start, end
        elif kind in {"B_SPLINE_CURVE_WITH_KNOTS", "COMPLEX", "POLYLINE"}:
            samples = self.curve_samples(curve)
            if samples is None:
                points, exact = [start, end], False
            else:
                points = _trim(samples, start, end, same_sense, closed)
        else:
            points, exact = [start, end], False
        if not exact:
            self.approximate_edges += 1
        self.edge_cache[edge_ref.id] = (points, exact)
        return points

    def curve_samples(self, curve: Ref) -> list[Vec] | None:
        kind=self.kind(curve)
        if kind == "POLYLINE":
            return [self.point(ref) for ref in self.args(curve)[1]]
        if kind == "B_SPLINE_CURVE_WITH_KNOTS":
            arguments = self.args(curve)
            degree, control = int(arguments[1]), [self.point(ref) for ref in arguments[2]]
            knots = expand_knots(arguments[6], arguments[7])
            return sample_bspline(degree, knots, control, None, max(16, 4 * len(control)))
        base=self.part(curve, "B_SPLINE_CURVE")
        knot_part=self.part(curve, "B_SPLINE_CURVE_WITH_KNOTS")
        if base is None or knot_part is None:
            return None
        weights_part = self.part(curve, "RATIONAL_B_SPLINE_CURVE")
        control = [self.point(ref) for ref in base[1]]
        knots = expand_knots(knot_part[0], knot_part[1])
        weights = [float(value) for value in weights_part[0]] if weights_part else None
        return sample_bspline(int(base[0]), knots, control, weights, max(16, 4 * len(control)))

    # faces
    def loop_points(self, loop_ref: Ref) -> list[Vec]:
        kind=self.kind(loop_ref)
        if kind == "POLY_LOOP":
            return [self.point(ref) for ref in self.args(loop_ref)[1]]
        if kind != "EDGE_LOOP":
            return []
        points: list[Vec] = []
        for oriented in self.args(loop_ref)[1]:
            oriented_args = self.args(oriented)
            edge_ref, orientation = oriented_args[3], oriented_args[4]
            edge = list(self.edge_points(edge_ref))
            if orientation is False:
                edge.reverse()
            if points and distance(points[-1], edge[0]) < 1e-12:
                edge = edge[1:]
            points.extend(edge)
        if len(points) > 1 and distance(points[0], points[-1]) < 1e-12:
            points.pop()
        return points

    def face_edges(self, face_ref: Ref) -> list[int]:
        edges: list[int] = []
        for bound in self.args(face_ref)[1]:
            loop = self.args(bound)[1]
            if self.kind(loop) == "EDGE_LOOP":
                edges.extend(self.args(oriented)[3].id for oriented in self.args(loop)[1])
        return edges

    def tessellate_face(self, face_ref: Ref) -> tuple[list[Vec], list[tuple[int, int, int]], str, bool]:
        arguments=self.args(face_ref)
        loops: list[tuple[list[Vec], bool]] = []
        for bound in arguments[1]:
            bound_kind = self.kind(bound)
            bound_args=self.args(bound)
            points = self.loop_points(bound_args[1])
            if bound_args[2] is False:
                points.reverse()
            if len(points) >= 3:
                loops.append((points, bound_kind == "FACE_OUTER_BOUND"))
        surface = arguments[2]
        surface_kind = self.kind(surface)
        label={
            "PLANE": "plane", "CYLINDRICAL_SURFACE": "cylinder", "CONICAL_SURFACE": "cone",
            "SPHERICAL_SURFACE": "sphere", "TOROIDAL_SURFACE": "torus",
        }.get(surface_kind, "freeform")
        if not loops:
            return [], [], label, False
        if surface_kind == "PLANE":
            origin, x, y, _ = self.placement(self.args(surface)[1])
            mapped = [[(dot(sub(p, origin), x), dot(sub(p, origin), y)) for p in loop] for loop, _ in loops]
            return (*_triangulate_loops(loops, mapped), label, True)
        if surface_kind in {"CYLINDRICAL_SURFACE", "CONICAL_SURFACE"}:
            surface_args=self.args(surface)
            origin, x, y, z = self.placement(surface_args[1])
            radius=float(surface_args[2])
            half_angle = float(surface_args[3]) if surface_kind == "CONICAL_SURFACE" else 0.0
            if surface_kind == "CONICAL_SURFACE" and abs(half_angle) > 2 * math.pi:
                half_angle = math.radians(half_angle)
            windings, mapped = [], []
            for loop, _ in loops:
                thetas=_unwrapped_angles(loop, origin, x, y)
                windings.append(abs(thetas[-1] - thetas[0] + _closing_step(thetas)) > math.pi)
                reference = max(radius, 1e-12)
                mapped.append([(theta * reference, dot(sub(p, origin), z)) for theta, p in zip(thetas, loop)])
            if any(windings):
                if len(loops) == 2 and all(windings):
                    band = _revolved_band(loops, origin, x, y, z, radius, math.tan(half_angle))
                    return (*band, label, True)
                return (*_projected(loops), label, False)
            return (*_triangulate_loops(loops, mapped, best_ear=True), label, True)
        return (*_projected(loops), label, False)


def _closing_step(thetas: list[float]) -> float:
    step = thetas[0] - thetas[-1]
    return (step + math.pi) % (2 * math.pi) - math.pi


def _unwrapped_angles(loop: list[Vec], origin: Vec, x: Vec, y: Vec) -> list[float]:
    thetas: list[float] = []
    for point in loop:
        offset = sub(point, origin)
        theta = math.atan2(dot(offset, y), dot(offset, x))
        if thetas:
            while theta - thetas[-1] > math.pi:
                theta -= 2 * math.pi
            while theta - thetas[-1] < -math.pi:
                theta += 2 * math.pi
        thetas.append(theta)
    return thetas


def _trim(samples: list[Vec], start: Vec, end: Vec, forward: bool, closed: bool) -> list[Vec]:
    nearest_start = min(range(len(samples)), key=lambda i: distance(samples[i], start))
    if closed:
        ordered = samples[nearest_start:] + samples[1:nearest_start + 1]
        if not forward:
            ordered.reverse()
        return [start, *ordered[1:-1], end]
    nearest_end = min(range(len(samples)), key=lambda i: distance(samples[i], end))
    if nearest_start <= nearest_end:
        segment = samples[nearest_start : nearest_end + 1]
    else:
        segment = list(reversed(samples[nearest_end : nearest_start + 1]))
    return [start, *segment[1:-1], end]


def _triangulate_loops(
    loops: list[tuple[list[Vec], bool]], mapped: list[list[tuple[float, float]]], best_ear: bool = False
) -> tuple[list[Vec], list[tuple[int, int, int]]]:
    outer_index = next((i for i, (_, outer) in enumerate(loops) if outer), None)
    if outer_index is None:
        outer_index = max(range(len(mapped)), key=lambda i: abs(signed_area(mapped[i])))
    order = [outer_index] + [i for i in range(len(loops)) if i != outer_index]
    points3d: list[Vec] = []
    for index in order:
        points3d.extend(loops[index][0])
    _, triangles, _ = triangulate(mapped[outer_index], [mapped[i] for i in order[1:]], best_ear)
    return points3d, triangles


def _projected(loops: list[tuple[list[Vec], bool]]) -> tuple[list[Vec], list[tuple[int, int, int]]]:
    outer = next((loop for loop, is_outer in loops if is_outer), loops[0][0])
    normal = newell_normal(outer)
    if norm(normal) < 1e-18:
        normal = (0.0, 0.0, 1.0)
    x, y, _ = frame(normal, None)
    centre = outer[0]
    mapped = [[(dot(sub(p, centre), x), dot(sub(p, centre), y)) for p in loop] for loop, _ in loops]
    return _triangulate_loops(loops, mapped)


def _revolved_band(
    loops: list[tuple[list[Vec], bool]], origin: Vec, x: Vec, y: Vec, z: Vec, radius: float, slope: float
) -> tuple[list[Vec], list[tuple[int, int, int]]]:
    rings = []
    for loop, _ in loops:
        height = sum(dot(sub(p, origin), z) for p in loop) / len(loop)
        local_radius = radius + height * slope
        rings.append(
            [
                add(origin, add(scale(z, height), add(
                    scale(x, local_radius * math.cos(2 * math.pi * k / _CIRCLE_SEGMENTS)),
                    scale(y, local_radius * math.sin(2 * math.pi * k / _CIRCLE_SEGMENTS)),
                )))
                for k in range(_CIRCLE_SEGMENTS)
            ]
        )
    points = rings[0] + rings[1]
    count = _CIRCLE_SEGMENTS
    triangles = []
    for k in range(count):
        a, b = k, (k + 1) % count
        triangles.append((a, b, count + b))
        triangles.append((a, count + b, count + a))
    return points, triangles


def _shell_faces(reader: _StepReader, shell: Ref) -> list[Ref]:
    kind = reader.kind(shell)
    if kind in {"CLOSED_SHELL", "OPEN_SHELL"}:
        return [face for face in reader.args(shell)[1] if isinstance(face, Ref)]
    if kind == "ORIENTED_CLOSED_SHELL":
        return _shell_faces(reader, reader.args(shell)[2])
    return []


def read_step(text: str, filename: str = "model.step") -> GeometryModel:
    """Parse STEP text into a :class:`GeometryModel` with metre coordinates"""
    entities, header_name = parse_part21(text)
    reader = _StepReader(entities)
    unit_symbol, unit_scale = reader.length_unit()
    warnings: list[str] = []
    products = [
        str(parts[0][1][0] or parts[0][1][1])
        for parts in entities.values()
        if len(parts) == 1 and parts[0][0] == "PRODUCT" and parts[0][1]
    ]
    if any(len(parts) == 1 and parts[0][0] == "NEXT_ASSEMBLY_USAGE_OCCURRENCE" for parts in entities.values()):
        warnings.append("Assembly placement transforms are not applied; bodies are shown in their part coordinates.")

    groups: list[tuple[str, str, list[Ref]]] = []
    for entity_id, parts in sorted(entities.items()):
        name, arguments = parts[0]
        if len(parts) != 1:
            continue
        if name in {"MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS", "FACETED_BREP"}:
            faces = _shell_faces(reader, arguments[1])
            for void in (arguments[2] if name == "BREP_WITH_VOIDS" else []):
                faces += _shell_faces(reader, void)
            groups.append((str(arguments[0] or ""), "solid", faces))
        elif name == "SHELL_BASED_SURFACE_MODEL":
            faces = [face for shell in arguments[1] for face in _shell_faces(reader, shell)]
            groups.append((str(arguments[0] or ""), "shell", faces))
    if not groups:
        loose = [Ref(i) for i, parts in entities.items() if parts[0][0] in {"ADVANCED_FACE", "FACE_SURFACE"}]
        if loose:
            groups.append(("", "shell", loose))
            warnings.append("No solid or shell container found; loose faces were grouped into one body.")
    if not groups:
        raise StepError("STEP file contains no B-rep solids, shells, or faces")

    base_name = products[0] if products else (header_name or filename).rsplit(".", 1)[0]
    bodies: list[Body] = []
    approximate_faces = 0
    for index, (name, kind, face_refs) in enumerate(groups, start=1):
        body_name = name or (base_name if len(groups) == 1 else f"{base_name} body {index}")
        body = Body(id=f"b{index}", name=body_name, kind=kind)
        edge_ids: set[int] = set()
        vertex_ids: set[int] = set()
        for face_number, face_ref in enumerate(face_refs, start=1):
            if reader.kind(face_ref) not in {"ADVANCED_FACE", "FACE_SURFACE"}:
                continue
            try:
                points, triangles, surface, exact = reader.tessellate_face(face_ref)
            except (StepError, KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
                approximate_faces += 1
                continue
            face_edge_ids=reader.face_edges(face_ref)
            edge_ids.update(face_edge_ids)
            for edge_id in face_edge_ids:
                edge_args = reader.args(Ref(edge_id))
                vertex_ids.update(ref.id for ref in edge_args[1:3] if isinstance(ref, Ref))
            label = str(reader.args(face_ref)[0] or "") or f"Face {face_number}"
            exact = exact and all(reader.edge_cache.get(e, ([], True))[1] for e in face_edge_ids)
            if not exact:
                approximate_faces += 1
            face = Face(id=len(body.faces), name=label, surface=surface, approximate=not exact)
            body.faces.append(face)
            for a, b, c in triangles:
                for vertex in (points[a], points[b], points[c]):
                    body.triangles.extend(coordinate * unit_scale for coordinate in vertex)
                body.face_ids.append(face.id)
        for edge_id in sorted(edge_ids):
            polyline = reader.edge_cache.get(edge_id, ([], True))[0]
            if len(polyline) >= 2:
                body.edges.append([coordinate * unit_scale for point in polyline for coordinate in point])
        body.vertex_count = len(vertex_ids)
        body.finalize()
        if body.faces:
            bodies.append(body)
    if not bodies:
        raise StepError("no STEP faces could be tessellated")
    if approximate_faces:
        warnings.append(
            f"{approximate_faces} face(s) use freeform surfaces or curves and are approximated "
            "from their boundary polygon."
        )
    model_name = base_name or filename
    return GeometryModel(
        id="",
        name=model_name,
        filename=filename,
        format="step",
        length_unit=unit_symbol,
        bodies=bodies,
        warnings=warnings,
        assembly=products[0] if len(products) > 1 else "",
    )


__all__ = ["StepError", "parse_part21", "read_step", "Ref", "Typed"]
