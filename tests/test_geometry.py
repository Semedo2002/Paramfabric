import math
import struct

import pytest

from paramfabric.core.schema import Fabric
from paramfabric.geometry import GeometryImportError, associate_dimensions, create_feature, import_geometry, resolve_scene
from paramfabric.geometry.samples import demo_bracket_step, prism_step
from paramfabric.geometry.scene import box_triangles, si_values, transformed_triangles
from paramfabric.geometry.step import parse_part21
from paramfabric.geometry.stl import write_stl
from paramfabric.geometry.tessellate import signed_area, triangulate
from paramfabric.core.graph import ParameterGraph


def test_triangulate_polygon_with_hole_covers_exact_area():
    outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
    hole=[(4, 4), (4, 6), (6, 6), (6, 4)]
    points, triangles, exact = triangulate(outer, [hole])
    area = sum(abs(signed_area([points[a], points[b], points[c]])) for a, b, c in triangles)
    assert exact
    assert area == pytest.approx(100 - 4)


def test_step_reader_tessellates_planes_cylinders_and_inner_loops():
    model = import_geometry("bracket.step", demo_bracket_step().encode())
    assert model.format == "step"
    assert model.length_unit == "mm"
    (body,) = model.bodies
    assert body.kind == "solid"
    assert body.bbox_min == pytest.approx([0, 0, 0])
    assert body.bbox_max == pytest.approx([0.2, 0.04, 0.02])
    surfaces = sorted(face.surface for face in body.faces)
    assert surfaces.count("plane") == 6 and surfaces.count("cylinder") == 1
    assert not any(face.approximate for face in body.faces)
    # 48-gon chord approximation of the Ø10 mm hole through a 200×20×40 mm block
    chord_circle = 0.5 * 48 * 25 * math.sin(2 * math.pi / 48)
    expected_mm2 = 25600 - 2 * chord_circle + 48 * 2 * 5 * math.sin(math.pi / 48) * 40
    assert body.surface_area * 1e6 == pytest.approx(expected_mm2, rel=1e-4)
    assert len(body.edges) == 15
    assert body.vertex_count == 10


def test_step_units_header_and_malformed_input():
    model=import_geometry("block.stp", prism_step("block", [(0, 0), (2, 0), (2, 1), (0, 1)], 0.5, unit="m").encode())
    assert model.length_unit == "m"
    assert model.bodies[0].bbox_max == pytest.approx([2, 1, 0.5])
    entities, name = parse_part21("ISO-10303-21;\nHEADER;FILE_NAME('it''s.step','',(''),(''),'','','');ENDSEC;\nDATA;\n#1=CARTESIAN_POINT('a;b',(1.,2.,3.));\nENDSEC;END-ISO-10303-21;")
    assert name == "it's.step"
    assert entities[1][0][1] == ["a;b", [1.0, 2.0, 3.0]]
    with pytest.raises(GeometryImportError, match="ISO-10303-21"):
        import_geometry("bad.step", b"solid nothing")
    with pytest.raises(GeometryImportError, match="Parasolid"):
        import_geometry("part.x_t", b"**PART1;")


def _iges(entities: list[tuple[int, list[float], int]]) -> str:
    def section(text: str, letter: str, width: int = 72) -> list[str]:
        chunks = [text[i : i + width] for i in range(0, len(text), width)] or [""]
        return [f"{chunk:<72}{letter}{index:7d}" for index, chunk in enumerate(chunks, start=1)]

    start = section("Paramfabric IGES test", "S")
    global_text = "1H,,1H;,4Hpart,8Hpart.igs,2Hpf,2Hpf,32,38,6,308,15,4Hpart,1.0,2,2HMM,1,0.01,15H20260101.000000,0.001,1000.0,2Hpf,2Hpf,11,0;"
    glob = section(global_text, "G")
    directory, parameters = [], []
    for number, (kind, values, transform) in enumerate(entities):
        de = 2 * number + 1
        text=f"{kind}," + ",".join(f"{value:g}" for value in values) + ";"
        first_line = len(parameters) + 1
        for i in range(0, len(text), 64):
            parameters.append(f"{text[i:i + 64]:<64} {de:7d}P{len(parameters) + 1:7d}")
        count = len(parameters) - first_line + 1
        directory.append(f"{kind:8d}{first_line:8d}{0:8d}{1:8d}{0:8d}{0:8d}{transform:8d}{0:8d}{0:8d}D{de:7d}")
        directory.append(f"{kind:8d}{0:8d}{0:8d}{count:8d}{0:8d}{'':8}{'':8}{'':8}{0:8d}D{de + 1:7d}")
    terminate = f"S{len(start):7d}G{len(glob):7d}D{len(directory):7d}P{len(parameters):7d}{'':40}T{1:7d}"
    return "\n".join([*start, *glob, *directory, *parameters, terminate]) + "\n"


def test_iges_reader_applies_units_transforms_and_samples_arcs():
    text = _iges([
        (124, [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 50], 0),   # DE 1: translate +50 mm in Z
        (110, [0, 0, 0, 100, 0, 0], 1),                     # DE 3: line, transformed
        (100, [0, 0, 0, 10, 0, -10, 0], 0),                 # DE 5: CCW upper half circle r=10
    ])
    model = import_geometry("part.igs", text.encode("latin-1"))
    assert model.length_unit == "mm"
    (body,) = model.bodies
    assert body.kind == "wireframe"
    assert len(body.edges) == 2
    assert body.bbox_min == pytest.approx([-0.01, 0, 0], abs=1e-9)
    assert body.bbox_max == pytest.approx([0.1, 0.01, 0.05], abs=1e-9)


def test_stl_ascii_and_binary_with_facet_grouping():
    triangles = box_triangles([0, 0, 0], [2, 1, 1])
    ascii_model = import_geometry("box.stl", write_stl("box", triangles, 1.0).encode(), units="m")
    (body,) = ascii_model.bodies
    assert len(body.face_ids) == 12
    assert len(body.faces) == 6
    assert body.surface_area == pytest.approx(2 * (2 + 2 + 1))
    binary = b"binary box".ljust(80, b"\0") + struct.pack("<I", 12)
    for t in range(12):
        binary += struct.pack("<12fH", 0, 0, 0, *triangles[t * 9 : t * 9 + 9], 0)
    binary_model = import_geometry("box.stl", binary, units="mm")
    assert binary_model.bodies[0].bbox_max == pytest.approx([0.002, 0.001, 0.001])
    with pytest.raises(GeometryImportError, match="unit"):
        import_geometry("box.stl", binary, units="furlong")


def test_associativity_creates_unit_parameters_and_morphs_scene():
    model = import_geometry("bracket.step", demo_bracket_step().encode())
    fabric = Fabric.model_validate({"parameters": [{"name": "cantilever_bracket_length", "value": 1, "unit": "m"}]})
    created=associate_dimensions(model, fabric)
    names = [parameter.name for parameter in created]
    assert names == ["cantilever_bracket_length_2", "cantilever_bracket_width", "cantilever_bracket_height"]
    assert [(p.value, p.unit) for p in created] == [(200.0, "mm"), (40.0, "mm"), (20.0, "mm")]
    assert model.bodies[0].dimension_parameters == {"x": names[0], "y": names[1], "z": names[2]}
    fabric = Fabric(parameters=[*fabric.parameters, *created])
    document=fabric.model_dump()
    document["parameters"][1]["value"] = "2 * cantilever_bracket_length * 0.2"  # 400 mm, via formula
    values = si_values(ParameterGraph(Fabric.model_validate(document)).evaluate())
    scene = resolve_scene([model], [], values)
    entry = scene["bodies"][0]
    assert entry["scale"] == pytest.approx([2.0, 1.0, 1.0])
    assert entry["bbox_max"] == pytest.approx([0.4, 0.04, 0.02])
    morphed = transformed_triangles([model], entry)
    assert max(morphed[0::3]) == pytest.approx(0.4)


def test_parametric_features_resolve_from_fabric_values():
    model = import_geometry("bracket.step", demo_bracket_step().encode())
    fabric=Fabric()
    features, parameters = [], []
    for kind, options in (("flow_volume", {}), ("extrude", {"face": "+x"}), ("split", {"axis": "x"}), ("scale", {})):
        feature, created = create_feature([model], features, Fabric(parameters=parameters), kind, options)
        features.append(feature)
        parameters.extend(created)
    with pytest.raises(ValueError, match="already has a flow volume"):
        create_feature([model], features, fabric, "flow_volume", {})
    values = si_values(ParameterGraph(Fabric(parameters=parameters)).evaluate())
    scene = resolve_scene([model], features, values)
    assert scene["errors"] == []
    (extrusion,) = scene["derived"]
    assert extrusion["bbox_min"][0] == pytest.approx(0.2)
    assert extrusion["bbox_max"][0] == pytest.approx(0.25)
    assert scene["bodies"][0]["split"]["position"] == pytest.approx(0.1)
    domain = scene["flow_domain"]
    assert domain["bbox_min"][0] == pytest.approx(-0.3)
    assert domain["bbox_max"][0] == pytest.approx(0.25 + 0.6)
    bad = dict(values, **{features[2]["params"]["fraction"]: 1.5})
    assert any("between 0 and 1" in error for error in resolve_scene([model], features, bad)["errors"])
