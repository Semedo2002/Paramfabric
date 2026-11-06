"""Beam and shell elements of the native structural solver against closed-form results"""

import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from paramfabric.solvers.structural import (  # noqa: E402
    FrameModel,
    Section,
    ShellModel,
    StructuralError,
    dkt_stiffness,
    rectangle_mesh,
    solve_frame,
    solve_shell,
)

E, NU = 2.0e11, 0.3


def _line(length, count, direction=(1.0, 0.0, 0.0)):
    nodes = np.outer(np.linspace(0, length, count + 1), direction)
    return nodes, np.c_[np.arange(count), np.arange(1, count + 1)]


def test_frame_cantilever_bending_torsion_and_axial_are_exact():
    section = Section.rectangle(0.05, 0.1)
    nodes, elements = _line(2.0, 8)
    tip = len(nodes) - 1
    result = solve_frame(FrameModel(nodes, elements, section, E, NU), {0: "all"}, {tip: [3e4, 500, -1000, 100, 0, 0]})
    shear = E / (2 * (1 + NU))
    assert result.displacement[tip, 0] == pytest.approx(3e4 * 2.0 / (E * section.area), rel=1e-10)
    assert result.displacement[tip, 1] == pytest.approx(500 * 8 / (3 * E * section.iz), rel=1e-10)
    assert result.displacement[tip, 2] == pytest.approx(-1000 * 8 / (3 * E * section.iy), rel=1e-10)
    assert result.displacement[tip, 3] == pytest.approx(100 * 2.0 / (shear * section.j), rel=1e-10)
    assert result.reactions[0] == pytest.approx([-3e4, -500, 1000, -100, -2000, -1000], abs=1e-6)


def test_frame_portal_is_independent_of_member_orientation():
    # A tilted cantilever must deflect like the axis-aligned one along its own normal
    section = Section.tube(0.06, 0.004)
    direction = np.array([1.0, 1.0, 1.0]) / math.sqrt(3)
    nodes, elements = _line(1.5, 6, direction)
    tip = len(nodes) - 1
    normal=np.cross(direction, [0.0, 0.0, 1.0])
    normal /= np.linalg.norm(normal)
    result = solve_frame(FrameModel(nodes, elements, section, E, NU), {0: "all"}, {tip: [*(800 * normal), 0, 0, 0]})
    assert result.displacement[tip, :3] @ normal == pytest.approx(800 * 1.5 ** 3 / (3 * E * section.iy), rel=1e-9)
    assert result.displacement[tip, :3] @ direction == pytest.approx(0, abs=1e-15)


def test_mechanism_is_reported():
    nodes, elements = _line(1.0, 2)
    with pytest.raises(StructuralError):
        solve_frame(FrameModel(nodes, elements, Section.circle(0.02), E, NU), {0: "translations"}, {2: [0, 0, 1, 0, 0, 0]})


def test_dkt_has_exactly_three_rigid_modes():
    x, y = np.array([0.0, 1.0, 0.3]), np.array([0.0, 0.2, 0.9])
    stiffness = dkt_stiffness(x, y, np.diag([1.0, 1.0, 0.35]))
    eigenvalues = np.linalg.eigvalsh(stiffness)
    assert np.all(eigenvalues > -1e-9) and np.sum(eigenvalues < 1e-9 * eigenvalues.max()) == 3
    rotation_x = np.ravel([[yy, 1, 0] for yy in y])  # w = y, θx = ∂w/∂y
    rotation_y = np.ravel([[-xx, 0, 1] for xx in x])  # w = −x, θy = −∂w/∂x
    assert np.abs(stiffness @ rotation_x).max() < 1e-12 and np.abs(stiffness @ rotation_y).max() < 1e-12


@pytest.mark.parametrize("divisions, tolerance", [(8, 0.015), (24, 0.002)])
def test_simply_supported_plate_matches_timoshenko(divisions, tolerance):
    side, thickness, pressure = 1.0, 0.01, 1000.0
    nodes, triangles = rectangle_mesh(side, side, divisions, divisions)
    edge = (nodes[:, 0] < 1e-9) | (nodes[:, 0] > side - 1e-9) | (nodes[:, 1] < 1e-9) | (nodes[:, 1] > side - 1e-9)
    fixed = {i: [0, 1, 5] + ([2] if edge[i] else []) for i in range(len(nodes))}
    result = solve_shell(ShellModel(nodes, triangles, thickness, E, NU), fixed, pressure=-pressure)
    rigidity = E * thickness ** 3 / (12 * (1 - NU ** 2))
    centre=np.argmin(np.linalg.norm(nodes[:, :2] - side / 2, axis=1))
    assert result.displacement[centre, 2] == pytest.approx(-0.00406 * pressure * side ** 4 / rigidity, rel=tolerance)
    assert result.reactions[:, 2].sum() == pytest.approx(pressure * side ** 2, rel=1e-9)


def test_shell_cantilever_strip_in_a_vertical_plane():
    # A plate strip in the x-z plane bending out of its plane (along y): beam
    # theory with ν = 0 is exact for the Kirchhoff strip
    length, width, thickness, load = 1.0, 0.1, 0.005, 10.0
    flat, triangles = rectangle_mesh(length, width, 20, 2)
    nodes = np.c_[flat[:, 0], np.zeros(len(flat)), flat[:, 1]]
    root = np.flatnonzero(nodes[:, 0] < 1e-9)
    tip = np.flatnonzero(nodes[:, 0] > length - 1e-9)
    result = solve_shell(ShellModel(nodes, triangles, thickness, E, 0.0), {int(i): "all" for i in root},
                         {int(i): [0, load / len(tip), 0, 0, 0, 0] for i in tip})
    inertia = width * thickness ** 3 / 12
    assert result.displacement[tip, 1].mean() == pytest.approx(load * length ** 3 / (3 * E * inertia), rel=0.01)
    assert result.summary["max_von_mises"] == pytest.approx(load * length * thickness / 2 / inertia, rel=0.15)


# I-beam sections and combined beam + shell models

from paramfabric.solvers.structural import BeamGroup, MemberModel, ShellGroup, solve_members  # noqa: E402


def test_i_beam_section_properties_and_spec():
    section = Section.i_beam(0.2, 0.1, 0.006, 0.01)
    assert section.area == pytest.approx(2 * 0.1 * 0.01 + 0.18 * 0.006)
    assert section.iy == pytest.approx((0.1 * 0.2 ** 3 - 0.094 * 0.18 ** 3) / 12)
    assert section.cz == 0.1 and section.cy == 0.05
    assert Section.from_spec({"type": "i_beam", "height": 0.2, "flange_width": 0.1, "web": 0.006, "flange": 0.01}) == section
    assert Section.from_spec({"type": "circular", "diameter": 0.02}).area == pytest.approx(math.pi * 1e-4)
    with pytest.raises(StructuralError, match="needs 'web'"):
        Section.from_spec({"type": "i_beam", "height": 0.2, "flange_width": 0.1, "flange": 0.01})
    with pytest.raises(StructuralError):
        Section.i_beam(0.02, 0.1, 0.006, 0.01)


def test_member_beam_matches_frame_and_reports_bending_stress():
    section = Section.i_beam(0.2, 0.1, 0.006, 0.01)
    nodes, elements = _line(2.0, 8)
    result = solve_members(MemberModel(nodes, E, NU, beams=[BeamGroup(elements, section)]), {0: "all"}, {8: [0, 0, -1000, 0, 0, 0]})
    assert result.displacement[8, 2] == pytest.approx(-1000 * 8 / (3 * E * section.iy), rel=1e-10)
    assert result.beam_stress[0][0] == pytest.approx(1000 * 2.0 * section.cz / section.iy, rel=1e-9)  # root: M·c/I


def test_offset_shell_and_beam_act_as_a_tee_section():
    # A 100 × 5 mm flange (shell) on a 5 × 50 mm web (beam): the flange mid-surface is
    # offset 27.5 mm above the beam axis. Composite beam theory gives the tip deflection.
    flat, triangles = rectangle_mesh(1.0, 0.1, 40, 4)
    flat[:, 1] -= 0.05
    centre = sorted((i for i in range(len(flat)) if abs(flat[i, 1]) < 1e-9), key=lambda i: flat[i, 0])
    root = [i for i in range(len(flat)) if flat[i, 0] < 1e-9]
    model = MemberModel(flat, E, 0.0, beams=[BeamGroup(np.c_[centre[:-1], centre[1:]], Section.rectangle(0.005, 0.05))],
                        shells=[ShellGroup(triangles, 0.005, offset=0.0275)])
    result = solve_members(model, {i: "all" for i in root}, {centre[-1]: [0, 0, -100, 0, 0, 0]})
    a1, z1, a2 = 0.1 * 0.005, 0.0275, 0.005 * 0.05
    zc = a1 * z1 / (a1 + a2)
    inertia = 0.1 * 0.005 ** 3 / 12 + a1 * (z1 - zc) ** 2 + 0.005 * 0.05 ** 3 / 12 + a2 * zc ** 2
    assert result.displacement[centre[-1], 2] == pytest.approx(-100 / (3 * E * inertia), rel=0.005)
    assert result.reactions[:, 2].sum() == pytest.approx(100, rel=1e-9)
    no_offset=solve_members(MemberModel(flat, E, 0.0, beams=model.beams, shells=[ShellGroup(triangles, 0.005)]),
                              {i: "all" for i in root}, {centre[-1]: [0, 0, -100, 0, 0, 0]})
    assert abs(no_offset.displacement[centre[-1], 2]) > 3 * abs(result.displacement[centre[-1], 2])


def test_studio_members_api_and_solve(tmp_path, monkeypatch):
    pytest.importorskip("gmsh")
    from tests.test_native3d import _client, _wait

    client = _client(tmp_path, monkeypatch)
    client.post("/api/workbench/geometry/demo")
    client.post("/api/workbench/physics", json={"analysis": "fea"})
    key = client.get("/api/workbench").json()["scene"]["bodies"][0]["key"]
    bad = client.post("/api/workbench/members", json={"type": "shell", "body": key, "selector": {"point": [0.1, 0.02, 0.02]}, "thickness": 0})
    assert bad.status_code == 422
    beam=client.post("/api/workbench/members", json={
        "type": "beam", "body": key, "edges": [{"point": [0.1, 0.0, 0.02]}],
        "section": {"type": "i_beam", "height": 0.04, "flange_width": 0.02, "web": 0.003, "flange": 0.004},
    }).json()["member"]
    shell = client.post("/api/workbench/members", json={
        "type": "shell", "body": key, "selector": {"point": [0.1, 0.02, 0.02], "normal": [0, 0, 1]}, "thickness": 0.002, "offset": -0.001,
    }).json()["member"]
    assert beam["name"] == "beam1" and shell["name"] == "shell1"
    run = _wait(client, client.post("/api/workbench/solver/run", json={"engine": "native3d"}).json()["job"]["id"])
    assert run["status"] == "completed", run["error"]
    result = run["result"]
    assert result["vtk"] == "members.vtk" and len(result["lines"]) == 1 and result["surfaces"][0]["name"] == "shell1"
    assert result["summary"]["reaction_force"][2] == pytest.approx(1000.0, rel=1e-6)  # default end load balances
    artifact = client.get(f"/api/workbench/tasks/{run['id']}/artifacts/members.vtk")
    cell_types = artifact.text.split("CELL_TYPES")[1].split("POINT_DATA")[0].split()[1:]
    assert artifact.status_code == 200 and set(cell_types) == {"3", "5"}  # VTK line (beam) and triangle (shell) cells
    assert client.delete(f"/api/workbench/members/{beam['id']}").status_code == 200
    assert [m["id"] for m in client.get("/api/workbench").json()["settings"]["structural_members"]] == [shell["id"]]
