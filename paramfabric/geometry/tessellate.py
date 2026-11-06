"""Pure-Python vector math, B-spline evaluation, and polygon triangulation"""

from __future__ import annotations

import math
from collections.abc import Sequence

Vec = tuple[float, float, float]
Vec2 = tuple[float, float]


def sub(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(a: Sequence[float], factor: float) -> Vec:
    return (a[0] * factor, a[1] * factor, a[2] * factor)


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a: Sequence[float]) -> float:
    return math.sqrt(dot(a, a))


def normalize(a: Sequence[float]) -> Vec:
    length = norm(a)
    if length < 1e-300:
        return (0.0, 0.0, 0.0)
    return (a[0] / length, a[1] / length, a[2] / length)


def distance(a: Sequence[float], b: Sequence[float]) -> float:
    return norm(sub(a, b))


def frame(axis: Sequence[float], reference: Sequence[float] | None) -> tuple[Vec, Vec, Vec]:
    """Return an orthonormal (x, y, z) frame from a z axis and optional x hint"""
    z = normalize(axis)
    if norm(z) == 0:
        z = (0.0, 0.0, 1.0)
    hint = reference if reference is not None and norm(cross(z, reference)) > 1e-9 else (
        (1.0, 0.0, 0.0) if abs(z[0]) < 0.9 else (0.0, 1.0, 0.0)
    )
    x = normalize(sub(hint, scale(z, dot(hint, z))))
    return x, cross(z, x), z


def newell_normal(points: Sequence[Sequence[float]]) -> Vec:
    """Robust polygon normal (Newell's method), not normalized"""
    nx = ny = nz = 0.0
    count = len(points)
    for index in range(count):
        current, following = points[index], points[(index + 1) % count]
        nx += (current[1] - following[1]) * (current[2] + following[2])
        ny += (current[2] - following[2]) * (current[0] + following[0])
        nz += (current[0] - following[0]) * (current[1] + following[1])
    return (nx, ny, nz)


def triangle_area(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
    return 0.5 * norm(cross(sub(b, a), sub(c, a)))


# B-splines


def expand_knots(multiplicities: Sequence[int], knots: Sequence[float]) -> list[float]:
    expanded: list[float] = []
    for count, knot in zip(multiplicities, knots):
        expanded.extend([float(knot)] * int(count))
    return expanded


def de_boor(
    degree: int,
    knots: Sequence[float],
    control: Sequence[Sequence[float]],
    parameter: float,
    weights: Sequence[float] | None = None,
) -> Vec:
    """Evaluate a (rational) B-spline curve point with de Boor's algorithm"""
    count = len(control)
    if count == 0:
        raise ValueError("B-spline has no control points")
    degree = max(1, min(degree, count - 1))
    lower, upper = knots[degree], knots[count]
    parameter = min(max(parameter, lower), upper)
    span = degree
    for index in range(degree, count):
        if knots[index] <= parameter < knots[index + 1]:
            span = index
            break
    else:
        span = count - 1
        while span > degree and knots[span] == knots[span + 1]:
            span -= 1
    weight_values = weights or [1.0] * count
    points = []
    for index in range(span - degree, span + 1):
        weight=float(weight_values[index])
        point = control[index]
        points.append([point[0] * weight, point[1] * weight, point[2] * weight, weight])
    for level in range(1, degree + 1):
        for index in range(degree, level - 1, -1):
            knot_index = index + span - degree
            denominator = knots[knot_index + degree - level + 1] - knots[knot_index]
            alpha = 0.0 if denominator == 0 else (parameter - knots[knot_index]) / denominator
            points[index] = [
                (1 - alpha) * points[index - 1][axis] + alpha * points[index][axis] for axis in range(4)
            ]
    x, y, z, w = points[degree]
    w = w or 1.0
    return (x / w, y / w, z / w)


def sample_bspline(
    degree: int,
    knots: Sequence[float],
    control: Sequence[Sequence[float]],
    weights: Sequence[float] | None = None,
    samples: int = 24,
) -> list[Vec]:
    count = len(control)
    if count < 2 or len(knots) < count + degree + 1:
        return [tuple(map(float, point)) for point in control]  # type: ignore[misc]
    effective_degree=max(1, min(degree, count - 1))
    lower, upper = knots[effective_degree], knots[count]
    return [
        de_boor(effective_degree, knots, control, lower + (upper - lower) * step / samples, weights)
        for step in range(samples + 1)
    ]


# polygon triangulation (ear clipping with hole bridging)


def signed_area(polygon: Sequence[Vec2]) -> float:
    area = 0.0
    for index in range(len(polygon)):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % len(polygon)]
        area += x1 * y2 - x2 * y1
    return area / 2


def _orientation(a: Vec2, b: Vec2, c: Vec2) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_in_triangle(point: Vec2, a: Vec2, b: Vec2, c: Vec2) -> bool:
    d1, d2, d3 = _orientation(point, a, b), _orientation(point, b, c), _orientation(point, c, a)
    has_negative = d1 < 0 or d2 < 0 or d3 < 0
    has_positive = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_negative and has_positive)


def _segments_cross(p1: Vec2, p2: Vec2, q1: Vec2, q2: Vec2) -> bool:
    d1, d2 = _orientation(q1, q2, p1), _orientation(q1, q2, p2)
    d3, d4 = _orientation(p1, p2, q1), _orientation(p1, p2, q2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and 0 not in (d1, d2, d3, d4)


def _dedupe(indices: list[int], points: Sequence[Vec2]) -> list[int]:
    result: list[int] = []
    for index in indices:
        if result and points[result[-1]] == points[index]:
            continue
        result.append(index)
    while len(result) > 1 and points[result[0]] == points[result[-1]]:
        result.pop()
    return result


def triangulate(
    outer: Sequence[Vec2], holes: Sequence[Sequence[Vec2]] = (), best_ear: bool = False
) -> tuple[list[Vec2], list[tuple[int, int, int]], bool]:
    """Triangulate a simple polygon with holes.

    Returns the merged vertex list, triangles as index triples into that list,
    and whether the result is exact (`False` if a degenerate fallback ran).
    `best_ear` clips the ear with the shortest new diagonal first (O(n³)),
    which yields zipper strips on curved faces mapped to parameter space.
    """
    points: list[Vec2] = list(outer)
    ring=_dedupe(list(range(len(points))), points)
    if len(ring) < 3:
        return points, [], False
    if signed_area([points[i] for i in ring]) < 0:
        ring.reverse()
    hole_rings: list[list[int]] = []
    for hole in holes:
        start = len(points)
        points.extend(hole)
        indices=_dedupe(list(range(start, len(points))), points)
        if len(indices) < 3:
            continue
        if signed_area([points[i] for i in indices]) > 0:
            indices.reverse()
        hole_rings.append(indices)
    hole_rings.sort(key=lambda indices: -max(points[i][0] for i in indices))
    for hole_ring in hole_rings:
        ring = _bridge(ring, hole_ring, points)
    return points, *_ear_clip(ring, points, best_ear)


def _bridge(ring: list[int], hole: list[int], points: Sequence[Vec2]) -> list[int]:
    hole_position = max(range(len(hole)), key=lambda position: points[hole[position]][0])
    hole_vertex=points[hole[hole_position]]
    edges = [(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))]
    edges += [(hole[i], hole[(i + 1) % len(hole)]) for i in range(len(hole))]
    candidates = sorted(
        range(len(ring)),
        key=lambda position: (points[ring[position]][0] - hole_vertex[0]) ** 2
        + (points[ring[position]][1] - hole_vertex[1]) ** 2,
    )
    chosen = candidates[0]
    for position in candidates:
        target = points[ring[position]]
        if not any(
            _segments_cross(hole_vertex, target, points[a], points[b])
            for a, b in edges
            if ring[position] not in (a, b) and hole[hole_position] not in (a, b)
        ):
            chosen = position
            break
    rotated_hole = hole[hole_position:] + hole[:hole_position]
    return (
        ring[: chosen + 1]
        + rotated_hole
        + [rotated_hole[0], ring[chosen]]
        + ring[chosen + 1 :]
    )


def _ear_clip(
    ring: list[int], points: Sequence[Vec2], best_ear: bool = False
) -> tuple[list[tuple[int, int, int]], bool]:
    triangles: list[tuple[int, int, int]] = []
    remaining = list(ring)
    exact = True
    guard = 0
    while len(remaining) > 3 and guard < 4 * len(ring) * len(ring) + 16:
        guard += 1
        count = len(remaining)
        clipped = False
        best: tuple[float, int] | None = None
        for position in range(count):
            previous = remaining[position - 1]
            current = remaining[position]
            following = remaining[(position + 1) % count]
            a, b, c = points[previous], points[current], points[following]
            turn = _orientation(a, b, c)
            if turn <= 0:
                continue
            if any(
                _point_in_triangle(points[other], a, b, c)
                for other in remaining
                if other not in (previous, current, following) and points[other] not in (a, b, c)
            ):
                continue
            if best_ear:
                diagonal = (a[0] - c[0]) ** 2 + (a[1] - c[1]) ** 2
                if best is None or diagonal < best[0]:
                    best = (diagonal, position)
                continue
            triangles.append((previous, current, following))
            del remaining[position]
            clipped = True
            break
        if best is not None:
            position = best[1]
            triangles.append((remaining[position - 1], remaining[position], remaining[(position + 1) % count]))
            del remaining[position]
            clipped = True
        if not clipped:
            # degenerate or self-touching input: drop collinear vertices first,
            # then fall back to a fan so the face is still displayed
            for position in range(len(remaining)):
                a = points[remaining[position - 1]]
                b = points[remaining[position]]
                c = points[remaining[(position + 1) % len(remaining)]]
                if abs(_orientation(a, b, c)) < 1e-18:
                    del remaining[position]
                    break
            else:
                exact = False
                anchor = remaining[0]
                triangles.extend(
                    (anchor, remaining[i], remaining[i + 1]) for i in range(1, len(remaining) - 1)
                )
                return triangles, exact
    if len(remaining) == 3:
        triangles.append((remaining[0], remaining[1], remaining[2]))
    return triangles, exact
