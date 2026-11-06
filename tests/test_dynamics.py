"""Modal analysis (solid, beam, plate) and node-to-surface penalty contact against closed-form results"""

import math

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from paramfabric.solvers.structural import (  # noqa: E402
    BeamGroup,
    MemberModel,
    Section,
    ShellGroup,
    StructuralError,
    modal_members,
    rectangle_mesh,
)

E, NU, RHO = 2.0e11, 0.3, 7850.0
# euler-Bernoulli cantilever eigenvalues (β L)² of the first two bending modes
BETA1, BETA2 = 1.875104 ** 2, 4.694091 ** 2


def _cantilever_frequency(beta, inertia, area, length=1.0):
    return beta / (2 * math.pi) * math.sqrt(E * inertia / (RHO * area * length ** 4))


# modal: beams and plates (exact references)

def test_beam_cantilever_modes_and_effective_mass_match_euler_bernoulli():
    section=Section.rectangle(0.05, 0.1)  # weak axis bends along y
    count = 40
    nodes = np.c_[np.linspace(0, 1.0, count + 1), np.zeros(count + 1), np.zeros(count + 1)]
    model = MemberModel(nodes, E, NU, beams=[BeamGroup(np.c_[np.arange(count), np.arange(1, count + 1)], section)], density=RHO)
    result = modal_members(model, {0: "all"}, modes=4)
    weak=_cantilever_frequency(BETA1, section.iz, section.area)
    strong = _cantilever_frequency(BETA1, section.iy, section.area)
    assert result.frequencies[:3] == pytest.approx([weak, strong, weak * BETA2 / BETA1], rel=2e-3)
    assert result.total_mass == pytest.approx(RHO * section.area * 1.0, rel=1e-12)
    # Effective modal mass of a uniform cantilever: 61.3 % (mode 1) and 18.8 % (mode 2) of the mass
    assert result.effective_mass_fraction[0, 1] == pytest.approx(0.6131, abs=2e-3)
    assert result.effective_mass_fraction[2, 1] == pytest.approx(0.1883, abs=2e-3)
    assert result.effective_mass_fraction[1, 2] == pytest.approx(0.6131, abs=2e-3)
    assert np.abs(result.mode_shapes[:, :, :3]).max(axis=(1, 2)) == pytest.approx(np.ones(4))


def test_simply_supported_plate_modes_match_kirchhoff_theory():
    side, thickness = 1.0, 0.01
    nodes, triangles = rectangle_mesh(side, side, 24, 24)
    edge = (nodes[:, 0] < 1e-9) | (nodes[:, 0] > side - 1e-9) | (nodes[:, 1] < 1e-9) | (nodes[:, 1] > side - 1e-9)
    fixed = {i: [0, 1, 5] + ([2] if edge[i] else []) for i in range(len(nodes))}
    model = MemberModel(nodes, E, NU, shells=[ShellGroup(triangles, thickness)], density=RHO)
    result=modal_members(model, fixed, modes=3)
    rigidity = E * thickness ** 3 / (12 * (1 - NU ** 2))
    f11 = math.pi / side ** 2 * math.sqrt(rigidity / (RHO * thickness))  # f_mn = π/2 (m² + n²)/a² √(D/ρh)
    assert result.frequencies[0] == pytest.approx(f11, rel=0.01)
    assert result.frequencies[1:3] == pytest.approx([2.5 * f11, 2.5 * f11], rel=0.02)  # degenerate (1,2)/(2,1) pair


def test_modal_rejects_massless_models():
    nodes = np.array([[0.0, 0, 0], [1.0, 0, 0]])
    model = MemberModel(nodes, E, NU, beams=[BeamGroup(np.array([[0, 1]]), Section.circle(0.02))], density=0.0)
    with pytest.raises(StructuralError, match="density"):
        modal_members(model, {0: "all"})


# modal and contact on solid meshes

gmsh = pytest.importorskip("gmsh")

from paramfabric.geometry.samples import prism_step  # noqa: E402
from paramfabric.gmsh_session import GmshSession  # noqa: E402
from paramfabric.meshing.gmsh_mesher import MeshOptions, mesh_solid  # noqa: E402
from paramfabric.solvers.fem.structural import ContactCase, ContactPair, solve_contact, solve_modal  # noqa: E402


@pytest.fixture(scope="module")
def beam_step(tmp_path_factory):
    path = tmp_path_factory.mktemp("modal") / "beam.step"
    path.write_text(prism_step("beam", [(0, 0), (1000, 0), (1000, 100), (0, 100)], 50, plane="xz"))
    return path


@pytest.mark.parametrize("element, order, size", [("tet", 2, 0.025), ("hex", 2, 0.025)])
def test_solid_cantilever_modes_match_beam_theory(beam_step, element, order, size):
    named = {"fixed": [((0.0, 0.025, 0.05), (-1, 0, 0))]}
    mesh = mesh_solid([beam_step], MeshOptions(size=size, order=order, element=element, curvature=0), named)
    result=solve_modal(mesh, E, NU, RHO, ["fixed"], modes=3)
    area = 0.05 * 0.1
    weak=_cantilever_frequency(BETA1, 0.1 * 0.05 ** 3 / 12, area)
    strong = _cantilever_frequency(BETA1, 0.05 * 0.1 ** 3 / 12, area)
    assert result["frequencies"][:2] == pytest.approx([weak, strong], rel=0.02)
    assert result["total_mass"] == pytest.approx(RHO * area * 1.0, rel=1e-9)  # consistent mass integrates exactly
    assert result["effective_mass_fraction"][0, 1] == pytest.approx(0.613, abs=0.02)
    assert result["mode_shapes"].shape == (3, len(mesh.nodes), 3)


def _box(folder, name, x, y, z, dx, dy, dz):
    with GmshSession() as session:
        session.model.occ.addBox(x, y, z, dx, dy, dz)
        session.model.occ.synchronize()
        session.write(str(folder / f"{name}.step"))
    return folder / f"{name}.step"


def test_stacked_blocks_transmit_the_full_load_through_contact(tmp_path):
    files = [_box(tmp_path, "lower", 0, 0, 0, 0.1, 0.1, 0.05), _box(tmp_path, "upper", 0, 0, 0.05, 0.1, 0.1, 0.05)]
    named = {"bottom": [((0.05, 0.05, 0.0), (0, 0, -1))], "top": [((0.05, 0.05, 0.1), (0, 0, 1))]}
    mesh = mesh_solid(files, MeshOptions(size=0.02, order=1, element="hex", curvature=0), named, separate_bodies=True)
    interface = [g for g in mesh.boundaries if g.name.startswith("face:") and np.allclose(mesh.nodes[g.nodes, 2], 0.05)]
    assert len(interface) == 2  # unfused bodies: two coincident faces, no shared nodes
    slave, master = sorted(interface, key=lambda g: g.name)
    pressure = 1e6
    case = ContactCase(mesh, E, NU, fixed=["bottom"], constraints=[{"group": "top", "dofs": [0, 1]}],
                       loads=[{"group": "top", "type": "pressure", "value": pressure}], pairs=[ContactPair(slave.name, master.name)])
    summary = solve_contact(case)["summary"]
    assert summary["total_contact_force"] == pytest.approx(pressure * 0.1 * 0.1, rel=1e-6)
    assert summary["active_nodes"] == summary["slave_nodes"] and summary["max_penetration"] < 1e-7


@pytest.fixture(scope="module")
def stop_meshes(tmp_path_factory):
    folder = tmp_path_factory.mktemp("contact")
    meshes = {}
    for gap in (0.5e-3, 5e-3, 20e-3):
        files=[_box(folder, f"beam{gap}", 0, 0, 0, 1.0, 0.05, 0.1), _box(folder, f"stop{gap}", 0.9, 0, -0.06 - gap, 0.1, 0.05, 0.06)]
        named={"root": [((0, 0.025, 0.05), (-1, 0, 0))], "tip": [((1.0, 0.025, 0.05), (1, 0, 0))],
                 "beam_bottom": [((0.5, 0.025, 0.0), (0, 0, -1))], "stop_top": [((0.95, 0.025, -gap), (0, 0, 1))],
                 "stop_bottom": [((0.95, 0.025, -0.06 - gap), (0, 0, -1))]}
        meshes[gap] = mesh_solid(files, MeshOptions(size=0.02, order=1, element="hex", curvature=0), named, separate_bodies=True)
    return meshes


@pytest.mark.parametrize("gap", [0.5e-3, 5e-3, 20e-3])
def test_cantilever_closing_onto_a_stop(stop_meshes, gap):
    load, inertia, a, length = 20e3, 0.05 * 0.1 ** 3 / 12, 0.95, 1.0
    case = ContactCase(stop_meshes[gap], E, NU, fixed=["root", "stop_bottom"], loads=[{"group": "tip", "type": "force", "vector": [0, 0, -load]}],
                       pairs=[ContactPair("beam_bottom", "stop_top")])
    result = solve_contact(case)
    summary = result["summary"]
    free = load * a * a * (3 * length - a) / (6 * E * inertia)  # 7.4 mm free deflection over the stop
    # Rigid-stop beam estimate: the reaction that cancels the deflection beyond the gap
    estimate = max(0.0, (free - gap) * 3 * E * inertia / a ** 3)
    if estimate == 0:
        assert summary["active_nodes"] == 0 and summary["total_contact_force"] == 0
    else:
        assert summary["total_contact_force"] == pytest.approx(estimate, rel=0.15)
        assert summary["max_penetration"] < 0.02 * gap
        assert summary["iterations"] < case.max_iterations


def test_studio_modal_analysis_for_solid_and_members(tmp_path, monkeypatch):
    from tests.test_native3d import _client, _wait

    client = _client(tmp_path, monkeypatch)
    client.post("/api/workbench/geometry/demo")
    client.post("/api/workbench/physics", json={"analysis": "fea"})
    options = {**client.get("/api/workbench").json()["settings"].get("solver_options", {}), "structural_analysis": "modal", "modes": 4}
    assert client.post("/api/workbench/settings", json={"values": {"solver_options": options}}).status_code == 200
    run = _wait(client, client.post("/api/workbench/solver/run", json={"engine": "native3d"}).json()["job"]["id"])
    assert run["status"] == "completed", run["error"]
    result = run["result"]
    viewport = client.get(f"/api/workbench/tasks/{run['id']}/artifacts/viewport.json").json()
    frequencies = result["summary"]["frequencies"]
    assert result["summary"]["modes"][0]["mode"] == 1 and len(frequencies) == 4
    assert all(0 < a <= b for a, b in zip(frequencies, frequencies[1:]))
    assert viewport["physics"] == "modal" and viewport["field_names"] == ["mode_1", "mode_2", "mode_3", "mode_4"]
    surface = viewport["surfaces"][0]
    assert len(surface["mode_displacements"]["mode_1"]) == len(surface["positions"])
    assert max(surface["values"]["mode_1"]) == pytest.approx(1.0, abs=1e-3)  # unit-max shape (peak may sit just off the surface)
    assert "Hz" in viewport["field_labels"]["mode_1"]
    assert client.get(f"/api/workbench/tasks/{run['id']}/artifacts/modes.vtk").status_code == 200

    key = client.get("/api/workbench").json()["scene"]["bodies"][0]["key"]
    client.post("/api/workbench/members", json={"type": "beam", "body": key, "edges": [{"point": [0.1, 0.0, 0.02]}],
                                                "section": {"type": "rectangular", "width": 0.02, "height": 0.04}})
    members = _wait(client, client.post("/api/workbench/solver/run", json={"engine": "native3d"}).json()["job"]["id"])
    assert members["status"] == "completed", members["error"]
    assert members["result"]["method"].startswith("Modal analysis · beam/shell")
    assert all(f > 0 for f in members["result"]["summary"]["frequencies"])
