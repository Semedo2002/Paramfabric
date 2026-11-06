"""Exact B-Rep kernel, unstructured meshing, and embedded 3D solvers.

These tests need the CAE extras (gmsh, numpy, scipy); they are skipped otherwise.
"""

import math
import time

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
gmsh = pytest.importorskip("gmsh")

from paramfabric.geometry import exact  # noqa: E402
from paramfabric.geometry.samples import demo_bracket_step, prism_step  # noqa: E402
from paramfabric.geometry.scene import GeometryStore  # noqa: E402
from paramfabric.gmsh_session import GmshSession  # noqa: E402
from paramfabric.meshing.gmsh_mesher import BoundaryLayer, MeshOptions, first_layer_height, mesh_external, mesh_internal, mesh_solid  # noqa: E402
from paramfabric.meshing.mesh import BoundaryGroup, VolumeMesh, read_vtk_unstructured, write_vtk_unstructured  # noqa: E402
from paramfabric.solvers.fem.elements import GMSH_CODES, REFERENCE_NODES, element  # noqa: E402
from paramfabric.solvers.fem.structural import StructuralCase, solve_structural  # noqa: E402
from paramfabric.solvers.fvm.navier_stokes import FlowCase, solve_flow  # noqa: E402


# elements

@pytest.mark.parametrize("name", ["tet4", "tet10", "hex8", "hex20", "tri3", "tri6", "quad4", "quad8"])
def test_elements_match_gmsh_reference_nodes_and_partition_unity(name):
    with GmshSession() as session:
        properties=session.model.mesh.getElementProperties(GMSH_CODES[name])
    dim, count = properties[1], properties[3]
    assert np.allclose(np.array(properties[4]).reshape(count, dim), REFERENCE_NODES[name])
    ref = element(name)
    assert np.allclose(ref.shape(ref.quad_points).sum(axis=1), 1)
    assert np.allclose(ref.shape(REFERENCE_NODES[name]), np.eye(ref.nodes))


# exact B-Rep kernel

@pytest.fixture()
def bracket(tmp_path):
    store=GeometryStore(tmp_path)
    data = demo_bracket_step().encode()
    model = exact.import_cad("bracket.step", data)
    exact.store_source(store, model, data)
    store.save(model)
    return store, model


def test_exact_import_keeps_true_surfaces(bracket):
    _, model = bracket
    (body,) = model.bodies
    assert model.kernel == "occ" and model.length_unit == "mm"
    cylinder = [face for face in body.faces if face.surface == "cylinder"]
    assert len(cylinder) == 1 and cylinder[0].area == pytest.approx(2 * math.pi * 0.005 * 0.04, rel=1e-6)
    assert body.bbox_max == pytest.approx([0.2, 0.04, 0.02], abs=1e-9)


def test_push_pull_regeneration_is_exact_and_preserves_holes(bracket):
    store, model = bracket
    model.bodies[0].dimension_parameters = {"x": "length"}
    result = exact.regenerate(store, model, [], {"length": 0.3})
    data = result["bodies"]["b1"]
    assert result["errors"] == [] and "push-pull" in result["log"][0]
    assert data["bbox_max"][0] == pytest.approx(0.3, abs=1e-9)
    assert data["volume"] == pytest.approx(0.3 * 0.04 * 0.02 - math.pi * 0.005 ** 2 * 0.04, rel=1e-6)
    hole = [face for face in data["faces"] if face["surface"] == "cylinder"]
    assert hole and hole[0]["area"] == pytest.approx(2 * math.pi * 0.005 * 0.04, rel=5e-3)


def test_fillet_split_and_scale_features_on_exact_geometry(bracket):
    store, model = bracket
    key = f"{model.id}/b1"
    fillet = {"id": "f1", "type": "fillet", "body": key, "params": {"radius": "r"}, "edges": [{"point": [0.1, 0.0, 0.02]}]}
    base = exact.regenerate(store, model, [], {})["bodies"]["b1"]["volume"]
    filleted=exact.regenerate(store, model, [fillet], {"r": 0.004})["bodies"]["b1"]["volume"]
    assert base - filleted == pytest.approx(0.004 ** 2 * (1 - math.pi / 4) * 0.2, rel=1e-6)
    split = {"id": "s1", "type": "split", "body": key, "axis": "x", "params": {"fraction": "f"}}
    result = exact.regenerate(store, model, [split], {"f": 0.25})
    assert result["bodies"]["b1"]["volumes"] == 2
    offset = {"id": "o1", "type": "offset_face", "body": key, "params": {"distance": "d"},
              "selector": {"point": [0.2, 0.02, 0.01], "normal": [1, 0, 0]}}
    pushed = exact.regenerate(store, model, [offset], {"d": -0.05})["bodies"]["b1"]
    assert pushed["bbox_max"][0] == pytest.approx(0.15, abs=1e-9)
    scale = {"id": "k1", "type": "scale", "body": key, "params": {"factor": "k"}}
    from paramfabric.core.units import Q_

    text = exact.export_workspace(store, [model], {"features": [scale]}, {"k": Q_(2.0, "dimensionless")}, "step")
    reimported=exact.import_cad("scaled.step", text.encode())
    assert reimported.bodies[0].bbox_max == pytest.approx([0.4, 0.08, 0.04], abs=1e-7)


def test_persistent_face_names_survive_dimension_changes(tmp_path):
    # stepped block: lower step to x = 0.2, upper step to x = 0.1. Both end
    # faces point +x. Push the lower one, then lengthen the part to 0.5 m: the
    # picked point is now nearer the upper step face than the face it named.
    with GmshSession() as session:
        occ=session.model.occ
        lower, upper = occ.addBox(0, 0, 0, 0.2, 0.1, 0.05), occ.addBox(0, 0, 0.05, 0.1, 0.1, 0.05)
        occ.fuse([(3, lower)], [(3, upper)])
        occ.synchronize()
        session.write(str(tmp_path / "step.step"))
    data = (tmp_path / "step.step").read_bytes()
    store=GeometryStore(tmp_path)
    model = exact.import_cad("step.step", data)
    exact.store_source(store, model, data)
    model.bodies[0].dimension_parameters = {"x": "length"}
    store.save(model)
    from paramfabric.core.units import Q_

    feature = {"id": "o1", "type": "offset_face", "body": f"{model.id}/{model.bodies[0].id}", "params": {"distance": "d"},
               "selector": {"point": [0.2, 0.05, 0.04], "normal": [1, 0, 0]}}
    geometric = json_copy(feature)
    assert exact.annotate_feature(store, [model], [], feature, {"length": Q_(0.2, "m"), "d": Q_(0.02, "m")})
    assert feature["selector"]["topology"]["count"] == 1  # the lower end face has a unique fingerprint

    named=exact.regenerate(store, model, [feature], {"length": 0.5, "d": 0.02})["bodies"]["b1"]
    assert named["bbox_max"][0] == pytest.approx(0.52, abs=1e-9)
    assert named["volume"] == pytest.approx(0.52 * 0.1 * 0.05 + 0.1 * 0.1 * 0.05, rel=1e-9)
    # The point-and-normal selector alone re-matches the upper step face
    drifted = exact.regenerate(store, model, [geometric], {"length": 0.5, "d": 0.02})["bodies"]["b1"]
    assert drifted["bbox_max"][0] == pytest.approx(0.5, abs=1e-9)


def test_persistent_edge_names_rank_parallel_edges(bracket):
    store, model = bracket
    key = f"{model.id}/b1"
    model.bodies[0].dimension_parameters = {"z": "height"}
    fillet = {"id": "f1", "type": "fillet", "body": key, "params": {"radius": "r"}, "edges": [{"point": [0.1, 0.0, 0.02]}]}
    from paramfabric.core.units import Q_

    assert exact.annotate_feature(store, [model], [], fillet, {"height": Q_(0.02, "m"), "r": Q_(0.004, "m")})
    assert fillet["edges"][0]["topology"]["count"] == 1
    # At 50 mm height the picked point (z = 20 mm) is nearer the bottom edge
    # (z = 0) than the named top edge (z = 50 mm); the fillet must follow the name
    result = exact.regenerate(store, model, [fillet], {"height": 0.05, "r": 0.004})["bodies"]["b1"]
    rounds = [face for face in result["faces"] if face["surface"] == "cylinder" and face["centroid"][1] < 0.005]  # not the bore
    assert len(rounds) == 1 and rounds[0]["centroid"][2] > 0.045
    assert rounds[0]["area"] == pytest.approx(math.pi / 2 * 0.004 * 0.2, rel=5e-3)


def json_copy(value):
    import json

    return json.loads(json.dumps(value))


def test_step_and_iges_export_round_trip(bracket):
    store, model = bracket
    model.bodies[0].dimension_parameters = {"x": "length"}
    from paramfabric.core.units import Q_

    for kind, suffix in (("step", "step"), ("iges", "igs")):
        text=exact.export_workspace(store, [model], {"features": []}, {"length": Q_(250, "mm")}, kind)
        back = exact.import_cad(f"roundtrip.{suffix}", text.encode("latin-1"))
        assert back.kernel == "occ"
        assert back.bodies[0].bbox_max == pytest.approx([0.25, 0.04, 0.02], abs=1e-7)


# meshing

def test_first_layer_height_from_y_plus():
    estimate = first_layer_height(30, 10.0, 1.0, 1.5e-5)
    assert estimate["reynolds"] == pytest.approx(10 / 1.5e-5)
    assert 1e-4 < estimate["first_height"] < 2e-3


def test_external_mesh_grows_prism_layers_outward(tmp_path):
    with GmshSession() as session:
        session.model.occ.addCylinder(0, 0, -0.05, 0, 0, 0.1, 0.05)
        session.model.occ.synchronize()
        session.write(str(tmp_path / "cyl.step"))
    layer = BoundaryLayer(first_height=0.0008, layers=4, growth=1.25)
    mesh = mesh_external([tmp_path / "cyl.step"], [-0.3, -0.2, -0.15], [0.6, 0.2, 0.15], MeshOptions(size=0.04, min_size=0.004, curvature=12, boundary_layer=layer))
    assert len(mesh.cells.get("prism6", [])) > 0 and len(mesh.cells.get("tet4", [])) > 0
    assert mesh.statistics["boundary_layers"] == 4
    prism_nodes = np.unique(mesh.cells["prism6"])
    radius = np.hypot(mesh.nodes[prism_nodes, 0], mesh.nodes[prism_nodes, 1])
    inside_height = np.abs(mesh.nodes[prism_nodes, 2]) < 0.049
    assert radius[inside_height].min() >= 0.05 * (1 - 1e-3)  # layers grow into the fluid, not the body
    assert {group.kind for group in mesh.boundaries} == {"inlet", "outlet", "slip", "wall"}


# structural

@pytest.fixture(scope="module")
def beam_step(tmp_path_factory):
    path = tmp_path_factory.mktemp("beam") / "beam.step"
    path.write_text(prism_step("beam", [(0, 0), (1000, 0), (1000, 100), (0, 100)], 50, plane="xz"))
    return path


NAMED = {"fixed": [((0.0, 0.025, 0.05), (-1, 0, 0))], "tip": [((1.0, 0.025, 0.05), (1, 0, 0))]}


def test_tet10_cantilever_matches_timoshenko_and_balances(beam_step):
    mesh=mesh_solid([beam_step], MeshOptions(size=0.025, order=2, curvature=0), NAMED)
    assert "tet10" in mesh.cells and mesh.order == 2
    result = solve_structural(StructuralCase(mesh, 200e9, 0.3, fixed=["fixed"], loads=[{"group": "tip", "type": "force", "vector": [0, 0, -1000]}]))
    tip = result["displacement"][mesh.boundary("tip").nodes, 2].mean()
    inertia = 0.05 * 0.1 ** 3 / 12
    timoshenko = 1000 / (3 * 200e9 * inertia) + 1000 / ((5 / 6) * (200e9 / 2.6) * 0.005)
    assert -tip == pytest.approx(timoshenko, rel=0.02)
    assert result["reaction_force"] == pytest.approx([0, 0, 1000], abs=1e-6)
    assert result["summary"]["max_von_mises"] == pytest.approx(1000 * 1.0 * 0.05 / inertia, rel=0.1)


def test_hex20_pressure_and_thermal_loads(beam_step):
    mesh = mesh_solid([beam_step], MeshOptions(size=0.05, order=2, element="hex", curvature=0), NAMED)
    assert "hex20" in mesh.cells
    pressure = solve_structural(StructuralCase(mesh, 200e9, 0.3, fixed=["fixed"], loads=[{"group": "tip", "type": "pressure", "value": 1e5}]))
    # pressure pushes on the tip face along −X: the support reacts +p·A
    assert pressure["reaction_force"][0] == pytest.approx(1e5 * 0.05 * 0.1, rel=1e-6)
    thermal = solve_structural(StructuralCase(mesh, 200e9, 0.3, fixed=["fixed"], expansion=1.2e-5, delta_temperature=50.0))
    tip = thermal["displacement"][mesh.boundary("tip").nodes, 0].mean()
    assert tip == pytest.approx(1.2e-5 * 50 * 1.0, rel=0.02)


def test_vtk_unstructured_round_trip(tmp_path, beam_step):
    mesh = mesh_solid([beam_step], MeshOptions(size=0.1, order=2, curvature=0), NAMED)
    values = np.arange(len(mesh.nodes), dtype=float)
    write_vtk_unstructured(tmp_path / "m.vtk", mesh, point_data={"v": values, "u": np.tile([1.0, 2.0, 3.0], (len(mesh.nodes), 1))})
    data = read_vtk_unstructured(tmp_path / "m.vtk")
    assert np.allclose(data["point_data"]["v"], values)
    assert np.allclose(data["point_data"]["u"][5], [1, 2, 3])


# flow

def _channel(nx, ny, length=0.2, height=0.02, width=0.004):
    x, y, z = np.linspace(0, length, nx + 1), np.linspace(0, height, ny + 1), np.linspace(0, width, 3)
    grid = np.stack(np.meshgrid(x, y, z, indexing="ij"), -1).reshape(-1, 3)
    nid = lambda i, j, k: (i * (ny + 1) + j) * 3 + k  # noqa: E731
    hexes = [[nid(i, j, k), nid(i + 1, j, k), nid(i + 1, j + 1, k), nid(i, j + 1, k),
              nid(i, j, k + 1), nid(i + 1, j, k + 1), nid(i + 1, j + 1, k + 1), nid(i, j + 1, k + 1)]
             for i in range(nx) for j in range(ny) for k in range(2)]
    face = lambda rows: np.array(rows, dtype=np.int64)  # noqa: E731
    groups = [
        BoundaryGroup("inlet", "inlet", {"quad4": face([[nid(0, j, k), nid(0, j + 1, k), nid(0, j + 1, k + 1), nid(0, j, k + 1)] for j in range(ny) for k in range(2)])}),
        BoundaryGroup("outlet", "outlet", {"quad4": face([[nid(nx, j, k), nid(nx, j + 1, k), nid(nx, j + 1, k + 1), nid(nx, j, k + 1)] for j in range(ny) for k in range(2)])}),
        BoundaryGroup("walls", "wall", {"quad4": face([[nid(i, j, k), nid(i + 1, j, k), nid(i + 1, j, k + 1), nid(i, j, k + 1)] for i in range(nx) for k in range(2) for j in (0, ny)])}),
        BoundaryGroup("sides", "slip", {"quad4": face([[nid(i, j, k), nid(i + 1, j, k), nid(i + 1, j + 1, k), nid(i, j + 1, k)] for i in range(nx) for j in range(ny) for k in (0, 2)])}),
    ]
    return VolumeMesh(grid, {"hex8": np.array(hexes)}, groups)


def _developed_gradient(result, length):
    centres, pressure = result["topology"].cell_centres, result["cell"]["p"]
    spacing=length / 40
    near = lambda x0: np.abs(centres[:, 0] - x0) < 0.6 * spacing  # noqa: E731
    return (pressure[near(0.1)].mean() - pressure[near(0.17)].mean()) / 0.07


def test_plane_poiseuille_on_hexes_matches_analytic():
    mesh = _channel(40, 20)
    mu, velocity, height = 4e-5, 0.1, 0.02
    result = solve_flow(FlowCase(mesh, 1.0, mu, (velocity, 0, 0), max_iterations=600, tolerance=1e-7))
    assert _developed_gradient(result, 0.2) == pytest.approx(12 * mu * velocity / height ** 2, rel=0.02)
    centres, ux = result["topology"].cell_centres, result["cell"]["U"][:, 0]
    assert ux[np.abs(centres[:, 0] - 0.17) < 0.003].max() == pytest.approx(1.5 * velocity, rel=0.02)
    flows = result["summary"]["mass_flow"]
    assert flows["inlet"] + flows["outlet"] == pytest.approx(0, abs=1e-12)


def test_tetrahedral_square_duct_converges_conservatively(tmp_path):
    with GmshSession() as session:
        session.model.occ.addBox(0, -0.01, -0.01, 0.2, 0.02, 0.02)
        session.model.occ.synchronize()
        session.write(str(tmp_path / "duct.step"))
    mesh = mesh_internal([tmp_path / "duct.step"], MeshOptions(size=0.003, curvature=0),
                         inlets=[((0, 0, 0), (1, 0, 0))], outlets=[((0.2, 0, 0), (1, 0, 0))])
    assert set(mesh.cells) == {"tet4"}
    mu, velocity, diameter = 4e-5, 0.1, 0.02
    history = []
    result = solve_flow(FlowCase(mesh, 1.0, mu, velocity, max_iterations=400, tolerance=1e-5), progress=lambda i, f: history.append(f))
    assert result["summary"]["converged"]
    centres, pressure = result["topology"].cell_centres, result["cell"]["p"]
    developed = (centres[:, 0] > 0.08) & (centres[:, 0] < 0.17)
    gradient = -np.polyfit(centres[developed, 0], pressure[developed], 1)[0]
    # square duct fRe = 56.91. Tetrahedral meshes at ~7 cells per hydraulic
    # diameter over-predict this by ~10% (hexahedra are within 2%); see docs/REFERENCE.md.
    assert gradient == pytest.approx(28.45 * mu * velocity / diameter ** 2, rel=0.15)
    flows = result["summary"]["mass_flow"]
    assert flows["inlet"] + flows["outlet"] == pytest.approx(0, abs=1e-12)


# end-to-end through the API

def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from paramfabric.server.app import create_app

    monkeypatch.chdir(tmp_path)
    (tmp_path / "params.yaml").write_text("parameters: []\n", encoding="utf-8")
    app = create_app(tmp_path / "params.yaml", database_url=f"sqlite:///{(tmp_path / 'n.db').as_posix()}",
                     auth_required=False, audit_path=tmp_path / ".paramfabric" / "a.jsonl")
    return TestClient(app, client=("127.0.0.1", 50000))


def _wait(client, job_id, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/workbench/solver/jobs/{job_id}").json()
        if snapshot["status"] in {"completed", "failed", "cancelled"}:
            return snapshot
        time.sleep(0.2)
    raise AssertionError("task did not finish")


def test_native3d_structural_and_flow_through_the_task_queue(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    model = client.post("/api/workbench/geometry/demo").json()["model"]
    assert model["kernel"] == "occ"
    client.post("/api/workbench/physics", json={"analysis": "fea"})
    fabric = client.get("/api/local/fabric").json()
    for parameter in fabric["parameters"]:
        if parameter["name"] == "mesh_fineness_ratio":
            parameter["value"] = 12
    client.post("/api/local/fabric", json=fabric)
    run = client.post("/api/workbench/solver/run", json={"engine": "native3d"}).json()["job"]
    structural = _wait(client, run["id"])
    assert structural["status"] == "completed", structural["error"]
    result = structural["result"]
    assert result["engine"] == "native3d" and result["surfaces"][0]["values"]["von_mises"]
    assert result["summary"]["reaction_force"][2] == pytest.approx(1000.0, rel=1e-6)
    assert result["mesh"]["quality"]["cell_types"].get("tet10")
    artifact = client.get(f"/api/workbench/tasks/{run['id']}/artifacts/structure.vtk")
    assert artifact.status_code == 200 and artifact.text.startswith("# vtk DataFile")

    client.post("/api/workbench/physics", json={"analysis": "cfd"})
    client.post("/api/workbench/features", json={"type": "flow_volume"})
    client.post("/api/workbench/settings", json={"values": {"solver_options": {"max_iterations": 25}}})
    flow = _wait(client, client.post("/api/workbench/solver/run", json={"engine": "native3d"}).json()["job"]["id"])
    assert flow["status"] == "completed", flow["error"]
    payload = flow["result"]
    assert payload["analysis"] == "cfd" and payload["surfaces"][0]["name"] == "walls"
    assert payload["slice"]["positions"] and payload["streamlines"]
    # re ~ 1e5 for air at 10 m/s over the bracket: the automatic model is k-ε
    assert len(flow["residuals"]) == 25 and set(flow["residuals"][0]["fields"]) == {"Ux", "Uy", "Uz", "p", "k", "epsilon"}
    assert payload["summary"]["turbulence"] == "k-epsilon"
