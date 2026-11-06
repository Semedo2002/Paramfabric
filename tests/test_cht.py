"""Conjugate heat transfer: conforming fluid/solid meshing, chtMultiRegionSimpleFoam case, energy balance"""

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("gmsh")

from paramfabric.gmsh_session import GmshSession  # noqa: E402
from paramfabric.meshing.gmsh_mesher import MeshOptions, mesh_cht  # noqa: E402
from paramfabric.solvers import openfoam as of  # noqa: E402
from tests.test_openfoam import requires_openfoam  # noqa: E402

DOMAIN = ([0.0, 0.0, 0.0], [0.2, 0.06, 0.04])


@pytest.fixture(scope="module")
def cht_meshes(tmp_path_factory):
    folder = tmp_path_factory.mktemp("cht")
    with GmshSession() as session:
        session.model.occ.addBox(0.05, 0.015, 0.0, 0.03, 0.03, 0.015)  # heat-sink block on the channel floor
        session.model.occ.synchronize()
        session.write(str(folder / "block.step"))
    return mesh_cht([folder / "block.step"], *DOMAIN, MeshOptions(size=0.006, curvature=0), heater=[((0.065, 0.03, 0.0), (0, 0, -1))])


def _groups(mesh):
    return {group.name: group.kind for group in mesh.boundaries}


def test_regions_share_a_conforming_interface(cht_meshes):
    fluid, solid = cht_meshes["fluid"], cht_meshes["solid"]
    assert _groups(fluid)["fluid_to_solid"] == "interface" and _groups(solid)["solid_to_fluid"] == "interface"
    assert _groups(solid)["heater"] == "heatflux"
    a = np.round(fluid.nodes[fluid.boundary("fluid_to_solid").nodes], 9)
    b = np.round(solid.nodes[solid.boundary("solid_to_fluid").nodes], 9)
    assert {tuple(p) for p in a} == {tuple(p) for p in b}  # node-for-node: mappedWall maps face to face
    heater = solid.nodes[solid.boundary("heater").nodes]
    assert np.allclose(heater[:, 2], 0.0)


def test_cht_case_dictionaries(tmp_path, cht_meshes):
    setup = of.ChtSetup(velocity=0.02, heater_power=20.0, fluid="water", solid="aluminium")
    infos = of.write_cht_case(tmp_path, cht_meshes, setup)
    assert infos["fluid"].heater_area == pytest.approx(0.03 * 0.03, rel=1e-6)
    regions = (tmp_path / "constant" / "regionProperties").read_text()
    assert "fluid (fluid)" in regions and "solid (solid)" in regions
    fluid_thermo=(tmp_path / "constant" / "fluid" / "thermophysicalProperties").read_text()
    solid_thermo = (tmp_path / "constant" / "solid" / "thermophysicalProperties").read_text()
    assert "heRhoThermo" in fluid_thermo and "rhoConst" in fluid_thermo
    assert "heSolidThermo" in solid_thermo and "constIso" in solid_thermo
    assert f"kappa {of.SOLIDS['aluminium']['kappa']:g};" in " ".join(solid_thermo.split())
    for region, method in (("fluid", "fluidThermo"), ("solid", "solidThermo")):
        temperature = (tmp_path / "0" / region / "T").read_text()
        assert "compressible::turbulentTemperatureCoupledBaffleMixed" in temperature and f"kappaMethod {method}" in temperature
    heater = (tmp_path / "0" / "solid" / "T").read_text()
    assert "externalWallHeatFluxTemperature" in heater and "mode power" in heater and "Q constant 20" in heater
    boundary = (tmp_path / "constant" / "fluid" / "polyMesh" / "boundary").read_text()
    assert "mappedWall" in boundary and "sampleRegion solid" in boundary and "samplePatch solid_to_fluid" in boundary
    control = (tmp_path / "system" / "controlDict").read_text()
    assert "chtMultiRegionSimpleFoam" in control
    for name in ("outletTemperature", "outletMassFlow", "solidTemperature"):
        assert name in control


@requires_openfoam
def test_real_cht_run_closes_the_energy_balance(tmp_path, cht_meshes):
    setup = of.ChtSetup(velocity=0.02, heater_power=20.0, end_time=2500)
    of.write_cht_case(tmp_path, cht_meshes, setup)
    launcher = of.detect_launcher()
    for region in ("fluid", "solid"):
        check = launcher.run(["checkMesh", "-region", region], tmp_path, timeout=300)
        assert check.returncode == 0 and "Mesh OK." in check.stdout, check.stdout[-2000:]
    run = launcher.run(["chtMultiRegionSimpleFoam"], tmp_path, timeout=900)
    assert run.returncode == 0, (run.stdout + run.stderr)[-3000:]
    summary = of.cht_summary(tmp_path, setup)
    inlet_area = (DOMAIN[1][1] - DOMAIN[0][1]) * (DOMAIN[1][2] - DOMAIN[0][2])
    assert summary["mass_flow"] == pytest.approx(of.FLUIDS["water"]["rho"] * 0.02 * inlet_area, rel=1e-3)
    assert abs(summary["energy_balance_error"]) < 0.02  # ṁ·cp·ΔT carries the heater power out
    assert summary["max_solid_temperature"] > summary["outlet_temperature"] > setup.inlet_temperature
    assert summary["thermal_resistance"] > 0


@requires_openfoam
def test_studio_cht_pipeline_end_to_end(tmp_path, monkeypatch):
    from tests.test_native3d import _client, _wait

    client = _client(tmp_path, monkeypatch)
    client.post("/api/workbench/geometry/demo")
    client.post("/api/workbench/physics", json={"analysis": "cfd"})
    assert client.post("/api/workbench/features", json={"type": "flow_volume"}).status_code == 200
    # this test checks the pipeline joins up, not accuracy (the real runs above do that): a coarse mesh keeps it
    # independent of machine speed. The default mesh here is about 200k cells.
    fabric = client.get("/api/local/fabric").json()
    for parameter in fabric["parameters"]:
        if parameter["name"] == "mesh_fineness_ratio":
            parameter["value"] = 8
    assert client.post("/api/local/fabric", json=fabric).status_code == 200
    settings = client.get("/api/workbench").json()["settings"]
    options = {**settings.get("solver_options", {}), "max_iterations": 150,
               "heat_transfer": {"enabled": True, "heater_power": 10.0, "fluid": "air", "solid": "aluminium"}}
    assert client.post("/api/workbench/settings", json={"values": {"solver_options": options}}).status_code == 200
    job = client.post("/api/workbench/solver/run", json={"engine": "openfoam"}).json()["job"]["id"]
    try:
        run = _wait(client, job)
    except AssertionError:
        client.post(f"/api/workbench/solver/jobs/{job}/cancel")  # do not leave the solver running after the test
        raise
    assert run["status"] == "completed", run["error"]
    result = run["result"]
    assert result["physics"] == "cht" and result["field_names"] == ["temperature", "speed"]
    summary = result["summary"]
    assert summary["fluid_cells"] > 0 and summary["solid_cells"] > 0
    assert summary["max_solid_temperature"] > 293.15
    temperatures=result["surfaces"][0]["values"]["temperature"]
    assert min(temperatures) >= 293.15 - 2.0 and max(temperatures) == pytest.approx(summary["max_solid_temperature"], rel=0.02)
    assert "temperature" in result["volume"]["scalars"] and result["ports"]
    for name in ("cht.vtk", "openfoam_case.zip", "log.txt"):
        assert client.get(f"/api/workbench/tasks/{run['id']}/artifacts/{name}").status_code == 200, name


@pytest.fixture(scope="module")
def immersed_meshes(tmp_path_factory):
    folder = tmp_path_factory.mktemp("cht-immersed")
    with GmshSession() as session:  # block floating in the channel: every face is wetted by the coolant
        session.model.occ.addBox(0.05, 0.015, 0.01, 0.03, 0.03, 0.015)
        session.model.occ.synchronize()
        session.write(str(folder / "block.step"))
    return mesh_cht([folder / "block.step"], *DOMAIN, MeshOptions(size=0.006, curvature=0), heater=[((0.065, 0.03, 0.01), (0, 0, -1))])


def test_immersed_body_gets_volumetric_heat_generation(tmp_path, immersed_meshes):
    assert "heater" not in _groups(immersed_meshes["solid"])  # wetted faces stay coupled interfaces
    infos = of.write_cht_case(tmp_path, immersed_meshes, of.ChtSetup(velocity=0.02, heater_power=20.0))
    assert infos["fluid"].heater_mode == "volume"
    options = " ".join((tmp_path / "system" / "solid" / "fvOptions").read_text().split())
    assert "scalarSemiImplicitSource" in options and "h (20 0);" in options and "volumeMode absolute;" in options


@requires_openfoam
def test_real_immersed_cht_run_closes_the_energy_balance(tmp_path, immersed_meshes):
    setup = of.ChtSetup(velocity=0.02, heater_power=20.0, end_time=2500)
    of.write_cht_case(tmp_path, immersed_meshes, setup)
    run = of.detect_launcher().run(["chtMultiRegionSimpleFoam"], tmp_path, timeout=900)
    assert run.returncode == 0, (run.stdout + run.stderr)[-3000:]
    summary = of.cht_summary(tmp_path, setup)
    assert abs(summary["energy_balance_error"]) < 0.02
    assert summary["max_solid_temperature"] > summary["outlet_temperature"] > setup.inlet_temperature
