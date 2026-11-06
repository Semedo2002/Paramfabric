"""Generate small, valid STEP AP214 B-rep parts (extruded profiles with holes).

Used for the Studio demo part and as round-trip fixtures for the STEP reader.
"""

from __future__ import annotations

from collections.abc import Sequence


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, text: str) -> int:
        self.lines.append(text)
        return len(self.lines)

    def point(self, xyz: Sequence[float]) -> int:
        return self.add(f"CARTESIAN_POINT('',({xyz[0]:.6f},{xyz[1]:.6f},{xyz[2]:.6f}))")

    def direction(self, xyz: Sequence[float]) -> int:
        return self.add(f"DIRECTION('',({xyz[0]:.6f},{xyz[1]:.6f},{xyz[2]:.6f}))")

    def placement(self, origin: Sequence[float], axis: Sequence[float], ref: Sequence[float]) -> int:
        return self.add(
            f"AXIS2_PLACEMENT_3D('',#{self.point(origin)},#{self.direction(axis)},#{self.direction(ref)})"
        )


def prism_step(
    name: str,
    profile: Sequence[tuple[float, float]],
    depth: float,
    holes: Sequence[tuple[float, float, float]] = (),
    plane: str = "xy",
    unit: str = "mm",
) -> str:
    """Write a STEP solid for `profile` (CCW, in `plane`) extruded by `depth`.

    `holes` are `(u, v, radius)` through-holes along the extrusion axis.
    `plane` is `"xy"` (extrude along +Z) or `"xz"` (extrude along +Y).
    """
    if plane not in {"xy", "xz"}:
        raise ValueError("plane must be 'xy' or 'xz'")

    def to3d(u: float, v: float, w: float) -> tuple[float, float, float]:
        return (u, v, w) if plane == "xy" else (u, w, v)

    axis = to3d(0, 0, 1)
    writer = _Writer()
    count = len(profile)
    bottom = [writer.add(f"VERTEX_POINT('',#{writer.point(to3d(u, v, 0))})") for u, v in profile]
    top = [writer.add(f"VERTEX_POINT('',#{writer.point(to3d(u, v, depth))})") for u, v in profile]

    def line_edge(start: int, end: int, a: Sequence[float], b: Sequence[float]) -> int:
        delta=[b[i] - a[i] for i in range(3)]
        length = sum(component * component for component in delta) ** 0.5 or 1.0
        vector = writer.add(f"VECTOR('',#{writer.direction([c / length for c in delta])},{length:.6f})")
        line = writer.add(f"LINE('',#{writer.point(a)},#{vector})")
        return writer.add(f"EDGE_CURVE('',#{start},#{end},#{line},.T.)")

    def oriented(edge: int, forward: bool) -> int:
        return writer.add(f"ORIENTED_EDGE('',*,*,#{edge},{'.T.' if forward else '.F.'})")

    bottom_edges, top_edges, vertical_edges = [], [], []
    for index, (u, v) in enumerate(profile):
        nu, nv = profile[(index + 1) % count]
        bottom_edges.append(line_edge(bottom[index], bottom[(index + 1) % count], to3d(u, v, 0), to3d(nu, nv, 0)))
        top_edges.append(line_edge(top[index], top[(index + 1) % count], to3d(u, v, depth), to3d(nu, nv, depth)))
        vertical_edges.append(line_edge(bottom[index], top[index], to3d(u, v, 0), to3d(u, v, depth)))

    faces: list[int] = []

    def plane_face(label: str, loops: list[tuple[list[int], bool]], origin: Sequence[float], normal: Sequence[float], ref: Sequence[float]) -> None:
        bounds=[]
        for edges, outer in loops:
            loop = writer.add(f"EDGE_LOOP('',({','.join(f'#{e}' for e in edges)}))")
            kind = "FACE_OUTER_BOUND" if outer else "FACE_BOUND"
            bounds.append(writer.add(f"{kind}('',#{loop},.T.)"))
        surface = writer.add(f"PLANE('',#{writer.placement(origin, normal, ref)})")
        faces.append(writer.add(f"ADVANCED_FACE('{label}',({','.join(f'#{b}' for b in bounds)}),#{surface},.T.)"))

    hole_loops_bottom, hole_loops_top = [], []
    for number, (cu, cv, radius) in enumerate(holes, start=1):
        start_b = writer.add(f"VERTEX_POINT('',#{writer.point(to3d(cu + radius, cv, 0))})")
        start_t=writer.add(f"VERTEX_POINT('',#{writer.point(to3d(cu + radius, cv, depth))})")
        ref = to3d(1, 0, 0)
        circle_b = writer.add(f"CIRCLE('',#{writer.placement(to3d(cu, cv, 0), axis, ref)},{radius:.6f})")
        circle_t = writer.add(f"CIRCLE('',#{writer.placement(to3d(cu, cv, depth), axis, ref)},{radius:.6f})")
        edge_b = writer.add(f"EDGE_CURVE('',#{start_b},#{start_b},#{circle_b},.T.)")
        edge_t = writer.add(f"EDGE_CURVE('',#{start_t},#{start_t},#{circle_t},.T.)")
        seam = line_edge(start_b, start_t, to3d(cu + radius, cv, 0), to3d(cu + radius, cv, depth))
        hole_loops_bottom.append(([oriented(edge_b, False)], False))
        hole_loops_top.append(([oriented(edge_t, True)], False))
        cylinder = writer.add(
            f"CYLINDRICAL_SURFACE('',#{writer.placement(to3d(cu, cv, 0), axis, ref)},{radius:.6f})"
        )
        loop = writer.add(
            "EDGE_LOOP('',(" + ",".join(
                f"#{e}" for e in (oriented(edge_b, True), oriented(seam, True), oriented(edge_t, False), oriented(seam, False))
            ) + "))"
        )
        bound = writer.add(f"FACE_OUTER_BOUND('',#{loop},.T.)")
        faces.append(writer.add(f"ADVANCED_FACE('hole {number} wall',(#{bound}),#{cylinder},.F.)"))

    plane_face(
        "bottom",
        [([oriented(e, False) for e in reversed(bottom_edges)], True), *hole_loops_bottom],
        to3d(0, 0, 0), to3d(0, 0, -1), to3d(1, 0, 0),
    )
    plane_face(
        "top",
        [([oriented(e, True) for e in top_edges], True), *hole_loops_top],
        to3d(0, 0, depth), axis, to3d(1, 0, 0),
    )
    for index, (u, v) in enumerate(profile):
        nu, nv = profile[(index + 1) % count]
        edge_u, edge_v = nu - u, nv - v
        length = (edge_u ** 2 + edge_v ** 2) ** 0.5 or 1.0
        outward = to3d(edge_v / length, -edge_u / length, 0)
        plane_face(
            f"side {index + 1}",
            [([
                oriented(bottom_edges[index], True),
                oriented(vertical_edges[(index + 1) % count], True),
                oriented(top_edges[index], False),
                oriented(vertical_edges[index], False),
            ], True)],
            to3d(u, v, 0), outward, to3d(edge_u / length, edge_v / length, 0),
        )

    shell = writer.add(f"CLOSED_SHELL('',({','.join(f'#{f}' for f in faces)}))")
    solid = writer.add(f"MANIFOLD_SOLID_BREP('{name}',#{shell})")
    prefix = {"mm": ".MILLI.", "m": "$", "cm": ".CENTI."}[unit]
    length_unit = writer.add(f"(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT({prefix},.METRE.))")
    angle_unit = writer.add("(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))")
    context = writer.add(
        "(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNIT_ASSIGNED_CONTEXT("
        f"(#{length_unit},#{angle_unit}))REPRESENTATION_CONTEXT('',''))"
    )
    origin = writer.placement((0, 0, 0), (0, 0, 1), (1, 0, 0))
    representation = writer.add(f"ADVANCED_BREP_SHAPE_REPRESENTATION('',(#{origin},#{solid}),#{context})")
    application = writer.add("APPLICATION_CONTEXT('automotive design')")
    product_context=writer.add(f"PRODUCT_CONTEXT('',#{application},'mechanical')")
    product = writer.add(f"PRODUCT('{name}','{name}','',(#{product_context}))")
    formation=writer.add(f"PRODUCT_DEFINITION_FORMATION('','',#{product})")
    definition_context = writer.add(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{application},'design')")
    definition = writer.add(f"PRODUCT_DEFINITION('design','',#{formation},#{definition_context})")
    shape = writer.add(f"PRODUCT_DEFINITION_SHAPE('','',#{definition})")
    writer.add(f"SHAPE_DEFINITION_REPRESENTATION(#{shape},#{representation})")

    body = "\n".join(f"#{index}={line};" for index, line in enumerate(writer.lines, start=1))
    return (
        "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('Paramfabric generated part'),'2;1');\n"
        f"FILE_NAME('{name}.step','2026-01-01T00:00:00',('paramfabric'),(''),'paramfabric','paramfabric','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\n"
        f"DATA;\n{body}\nENDSEC;\nEND-ISO-10303-21;\n"
    )


def demo_bracket_step() -> str:
    return prism_step(
        "cantilever_bracket",
        [(0, 0), (200, 0), (200, 20), (0, 20)],
        depth=40,
        holes=[(150, 10, 5)],
        plane="xz",
    )
