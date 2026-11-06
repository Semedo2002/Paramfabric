"""OpenFOAM production CFD path: polyMesh writer, case generation, result readers,
launcher, and (when OpenFOAM is installed) real checkMesh/simpleFoam runs."""

import sys
import textwrap
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("gmsh")

from paramfabric.gmsh_session import GmshSession  # noqa: E402
from paramfabric.meshing.gmsh_mesher import BoundaryLayer, MeshOptions, mesh_external, mesh_internal  # noqa: E402
from paramfabric.server import tasks  # noqa: E402
from paramfabric.solvers import manager  # noqa: E402
from paramfabric.solvers import openfoam as of  # noqa: E402
from paramfabric.solvers.runner import SolverRunner  # noqa: E402


@pytest.fixture(scope="module")
def duct(tmp_path_factory):
    folder = tmp_path_factory.mktemp("duct")
    with GmshSession() as session:
        session.model.occ.addBox(0, -0.01, -0.01, 0.1, 0.02, 0.02)
        session.model.occ.synchronize()
        session.write(str(folder / "duct.step"))
    return mesh_internal([folder / "duct.step"], MeshOptions(size=0.005, curvature=0),
                         inlets=[((0, 0, 0), (1, 0, 0))], outlets=[((0.1, 0, 0), (1, 0, 0))])


@pytest.fixture(scope="module")
def cylinder(tmp_path_factory):
    folder = tmp_path_factory.mktemp("cyl")
    with GmshSession() as session:
        session.model.occ.addCylinder(0, 0, -0.05, 0, 0, 0.1, 0.05)
        session.model.occ.synchronize()
        session.write(str(folder / "cyl.step"))
    layer=BoundaryLayer(first_height=0.001, layers=3, growth=1.2)
    return mesh_external([folder / "cyl.step"], [-0.3, -0.2, -0.15], [0.6, 0.2, 0.15],
                         MeshOptions(size=0.04, min_size=0.004, curvature=8, boundary_layer=layer))


def _read_list(path: Path) -> list[str]:
    text=path.read_text(encoding="ascii")
    body = text[text.index("}") + 1:]
    count_line = next(line for line in body.splitlines() if line.strip().isdigit())
    start = body.index("(", body.index(count_line)) + 1
    items = body[start:body.rindex(")")].split("\n")
    items = [item.strip() for item in items if item.strip()]
    assert len(items) == int(count_line)
    return items


def _faces(path: Path) -> list[list[int]]:
    return [[int(v) for v in item[item.index("(") + 1:-1].split()] for item in _read_list(path)]


def test_polymesh_obeys_openfoam_ordering_rules(tmp_path, cylinder):
    info = of.write_polymesh(tmp_path, cylinder)
    mesh_dir=tmp_path / "constant" / "polyMesh"
    points = np.array([[float(v) for v in item.strip("()").split()] for item in _read_list(mesh_dir / "points")])
    faces = _faces(mesh_dir / "faces")
    owner = np.array([int(v) for v in _read_list(mesh_dir / "owner")])
    neighbour=np.array([int(v) for v in _read_list(mesh_dir / "neighbour")])
    assert len(faces) == len(owner) == info.faces and len(neighbour) == info.internal_faces
    assert info.cells == cylinder.cell_count and len(points) == info.points
    # Only the corner nodes referenced by cells are written
    assert len(np.unique(np.concatenate([np.array(f) for f in faces]))) == len(points)
    # internal faces: owner < neighbour, sorted by owner then neighbour (upper-triangular)
    assert np.all(owner[: len(neighbour)] < neighbour)
    keys=owner[: len(neighbour)] * (info.cells + 1) + neighbour
    assert np.all(np.diff(keys) > 0)
    # Every face normal points out of its owner; the patches tile the boundary
    centres = info.topology.cell_centres
    for index, face in enumerate(faces):
        corners = points[face]
        normal = np.cross(corners[1] - corners[0], corners[2] - corners[0])
        assert normal @ (corners.mean(axis=0) - centres[owner[index]]) > 0
    starts = [patch["startFace"] for patch in info.patches]
    sizes = [patch["nFaces"] for patch in info.patches]
    assert starts[0] == len(neighbour) and starts[-1] + sizes[-1] == len(faces)
    assert {patch["name"]: patch["type"] for patch in info.patches} == {"inlet": "patch", "outlet": "patch", "sides": "patch", "walls": "wall"}
    boundary = (mesh_dir / "boundary").read_text(encoding="ascii")
    assert "inGroups    1(wall);" in boundary and f"startFace   {starts[-1]};" in boundary
    # prism layers survive the conversion: their side faces are quadrilaterals
    assert len(cylinder.cells["prism6"]) and any(len(face) == 4 for face in faces)


def test_sst_case_has_wall_functions_inlet_turbulence_and_function_objects(tmp_path, cylinder):
    info = of.write_polymesh(tmp_path, cylinder)
    setup = of.FoamSetup((10.0, 0.0, 0.0), 1.5e-5, reference_area=0.01, reference_length=0.1, length_scale=0.007, processors=4)
    written = of.write_simple_case(tmp_path, info, setup)
    assert {"0/U", "0/p", "0/k", "0/omega", "0/nut", "system/decomposeParDict"} <= set(written)
    k_expected = 1.5 * (0.05 * 10.0) ** 2
    omega_expected = k_expected ** 0.5 / (0.09 ** 0.25 * 0.007)
    assert of.turbulence_inlet(10.0, 0.05, 0.007)["k"] == pytest.approx(k_expected)
    k_file = (tmp_path / "0" / "k").read_text()
    assert "kqRWallFunction" in k_file and f"uniform {k_expected:.9g}" in k_file
    assert "omegaWallFunction" in (tmp_path / "0" / "omega").read_text()
    assert f"{omega_expected:.9g}"[:6] in (tmp_path / "0" / "omega").read_text()
    assert "nutkWallFunction" in (tmp_path / "0" / "nut").read_text()
    assert "model kOmegaSST;" in (tmp_path / "constant" / "momentumTransport").read_text()
    assert "noSlip" in (tmp_path / "0" / "U").read_text() and "slip" in (tmp_path / "0" / "U").read_text()
    control = (tmp_path / "system" / "controlDict").read_text()
    for needle in ("forceCoeffs;", "Aref        0.01", "magUInf     10", "type yPlus", "type wallShearStress",
                   "surfaceFormat   vtk", "legacy true", "type cuttingPlane", "stopAt          endTime;", "runTimeModifiable true;"):
        assert needle in control
    schemes = (tmp_path / "system" / "fvSchemes").read_text()
    assert "div(phi,omega)" in schemes and "wallDist" in schemes
    laminar = tmp_path / "laminar"
    of.write_polymesh(laminar, cylinder)
    written = of.write_simple_case(laminar, info, of.FoamSetup((1.0, 0, 0), 1e-3, turbulence="laminar"))
    assert "0/k" not in written and "simulationType laminar;" in (laminar / "constant" / "momentumTransport").read_text()


def test_internal_inlet_uses_surface_normal_velocity(tmp_path, duct):
    info=of.write_polymesh(tmp_path, duct)
    of.write_simple_case(tmp_path, info, of.FoamSetup((0.2, 0, 0), 1e-5, turbulence="laminar", inlet_normal=True))
    velocity=(tmp_path / "0" / "U").read_text()
    assert "surfaceNormalFixedValue; refValue uniform -0.2;" in velocity


def test_field_force_and_vtk_readers(tmp_path):
    (tmp_path / "U").write_text(textwrap.dedent("""\
        /* banner */
        FoamFile { version 2.0; format ascii; class volVectorField; object U; }
        dimensions [0 1 -1 0 0 0 0];
        internalField   nonuniform List<vector>
        3
        (
        (1 0 0)
        (2 0.5 -1e-3)
        (3 0 0)
        )
        ;
        boundaryField
        {
            walls { type noSlip; }
        }
        """))
    (tmp_path / "p").write_text("FoamFile { class volScalarField; }\ninternalField uniform 4.5;\nboundaryField\n{\n}\n")
    (tmp_path / "yPlus").write_text(textwrap.dedent("""\
        FoamFile { class volScalarField; }
        internalField uniform 0;
        boundaryField
        {
            inlet
            {
                type calculated;
                value uniform 0;
            }
            walls
            {
                type calculated;
                value nonuniform List<scalar> 3(1.5 2.5 3.5);
            }
        }
        """))
    velocity=of.read_internal_field(tmp_path / "U")
    assert velocity.shape == (3, 3) and velocity[1].tolist() == [2, 0.5, -1e-3]
    assert of.read_internal_field(tmp_path / "p").tolist() == [4.5]
    assert of.read_patch_field(tmp_path / "yPlus", "walls").tolist() == [1.5, 2.5, 3.5]
    assert of.read_patch_field(tmp_path / "yPlus", "inlet").tolist() == [0.0]

    coefficients=tmp_path / "postProcessing" / "forceCoeffs" / "0" / "coefficient.dat"
    coefficients.parent.mkdir(parents=True)
    coefficients.write_text("# Force coefficients\n# dragDir : (1 0 0)\n# Time\tCd\tCl\n1\t0.9\t0.1\n2\t0.8\t0.05\n")
    assert of.read_force_coefficients(tmp_path) == {"Time": [1.0, 2.0], "Cd": [0.9, 0.8], "Cl": [0.1, 0.05]}

    surface = tmp_path / "postProcessing" / "surfaces" / "200" / "walls.vtk"
    surface.parent.mkdir(parents=True)
    surface.write_text(textwrap.dedent("""\
        # vtk DataFile Version 2.0
        time='200'
        ASCII
        DATASET POLYDATA
        FIELD FieldData 1
        TimeValue 1 1 float
        200
        POINTS 4 float
        0 0 0 1 0 0 1 1 0 0 1 0
        POLYGONS 1 5
        4 0 1 2 3
        POINT_DATA 4
        FIELD attributes 2
        p 1 4 float
        1 2 3 4
        U 3 4 float
        1 0 0 1 0 0 1 0 0 1 0 0
        """))
    assert of.find_surface(tmp_path, "walls") == surface
    data=of.read_legacy_vtk_polydata(surface)
    assert data["points"].shape == (4, 3) and data["triangles"].tolist() == [[0, 1, 2], [0, 2, 3]]
    assert data["point_data"]["p"].tolist() == [1, 2, 3, 4] and data["point_data"]["U"].shape == (4, 3)


def test_launcher_commands_for_wsl_docker_and_sourced_installs(tmp_path, monkeypatch):
    case = Path("C:/Users/Some One/case dir") if sys.platform == "win32" else tmp_path
    wsl = of.FoamLauncher("wsl", "", "Ubuntu", "/usr/lib/openfoam/openfoam2506/etc/bashrc")
    argv = wsl.argv(["simpleFoam", "-parallel"], case)
    assert Path(argv[0]).name.lower() == "wsl.exe" and argv[1:5] == ["-d", "Ubuntu", "--exec", "bash"]
    assert "source /usr/lib/openfoam/openfoam2506/etc/bashrc" in argv[-1] and argv[-1].endswith("simpleFoam -parallel")
    if sys.platform == "win32":
        assert of.windows_to_wsl(case) == "/mnt/c/Users/Some One/case dir"
        # OpenFOAM aborts on a space in the case path, so such a folder is entered through a clean link
        assert "ln -sfn '/mnt/c/Users/Some One/case dir' \"$L\" && cd \"$L\"" in argv[-1]
        assert "cd '/mnt/c/Users/Some One/case dir'" not in argv[-1]
        clean = wsl.argv(["checkMesh"], Path("C:/work/case"))[-1]
        assert "cd /mnt/c/work/case && checkMesh" in clean and "ln -sfn" not in clean
    sourced = of.FoamLauncher("native", "", bashrc="/opt/openfoam/etc/bashrc").argv(["checkMesh"], tmp_path)
    assert sourced[:2] == ["bash", "-c"] and sourced[2].endswith("checkMesh")
    assert of.FoamLauncher("docker", "").argv(["blockMesh"], tmp_path)[:2] == ["docker", "run"]
    monkeypatch.setenv("PARAMFABRIC_OPENFOAM", "none")
    assert of.detect_launcher(refresh=True) is None and of.available()[0] is False
    monkeypatch.setenv("PARAMFABRIC_OPENFOAM", "wsl:Ubuntu-22.04:/opt/openfoam11/etc/bashrc")
    launcher = of.detect_launcher(refresh=True)
    assert (launcher.mode, launcher.distro, launcher.bashrc) == ("wsl", "Ubuntu-22.04", "/opt/openfoam11/etc/bashrc")


class _ScriptLauncher(of.FoamLauncher):
    def __init__(self, script: Path) -> None:
        super().__init__("native", "test double")
        self.script = script

    def argv(self, command, case_dir):
        return [sys.executable, str(self.script), *command]


def _context(tmp_path):
    store = tasks.MemoryStore()
    record = tasks.TaskRecord(id="job1", kind="solve", owner="tester", payload={})
    store.save(record)
    return store, record, tasks.TaskContext(store, record, tmp_path / "job1")


def test_stream_records_residuals_and_stops_gracefully_on_cancel(tmp_path):
    script = tmp_path / "fake_foam.py"
    script.write_text(textwrap.dedent("""\
        import sys, time, pathlib
        control = pathlib.Path("system/controlDict")
        for step in range(1, 400):
            print(f"Time = {step}", flush=True)
            print(f"smoothSolver:  Solving for Ux, Initial residual = {1.0 / step}, Final residual = 0, No Iterations 1", flush=True)
            print(f"GAMG:  Solving for p, Initial residual = {0.5 / step}, Final residual = 0, No Iterations 2", flush=True)
            if "writeNow" in control.read_text():
                print("stopping at writeNow", flush=True)
                break
            time.sleep(0.01)
        """))
    case = tmp_path / "case"
    (case / "system").mkdir(parents=True)
    (case / "system" / "controlDict").write_text("stopAt          endTime;\n")
    store, record, context = _context(tmp_path)
    of._stream(_ScriptLauncher(script), ["simpleFoam"], case, context, of.ResidualParser())  # runs to the end
    progress, _ = store.progress(record.id, 0)
    assert len(progress) == 399 and progress[0]["fields"] == {"Ux": 1.0, "p": 0.5}
    assert (case / "log.simpleFoam").read_text().count("Time =") == 399

    store, record, context = _context(tmp_path / "second")
    store.request_cancel(record.id)
    started = time.monotonic()
    with pytest.raises(tasks.TaskCancelled):
        of._stream(_ScriptLauncher(script), ["simpleFoam"], case, context, of.ResidualParser())
    assert "writeNow" in (case / "system" / "controlDict").read_text()
    assert "stopping at writeNow" in (case / "log.simpleFoam").read_text() and time.monotonic() - started < 30


def test_failed_utility_reports_its_output(tmp_path):
    script = tmp_path / "fail.py"
    script.write_text("import sys\nprint('--> FOAM FATAL ERROR: cannot find patchField entry for walls')\nsys.exit(1)\n")
    _, _, context = _context(tmp_path)
    with pytest.raises(of.OpenFOAMError, match="FOAM FATAL ERROR"):
        of._stream(_ScriptLauncher(script), ["simpleFoam"], tmp_path, context)


def test_engine_resolution_prefers_openfoam_and_flags_native_cfd(tmp_path, monkeypatch):
    runner = SolverRunner(tmp_path, which=lambda name: None)
    monkeypatch.setattr(of, "detect_launcher", lambda refresh=False: of.FoamLauncher("wsl", "WSL test", "Ubuntu", "/x/etc/bashrc"))
    assert manager.resolve_pipeline("cfd", "auto", runner) == "openfoam3d"
    assert manager.resolve_pipeline("cfd", "openfoam", runner) == "openfoam3d"
    assert manager.resolve_pipeline("fea", "auto", runner) == "native3d"
    catalog=manager.engine_catalog(runner)
    assert catalog["openfoam"]["available"] and "k-ω SST" in catalog["openfoam"]["label"]
    monkeypatch.setattr(of, "detect_launcher", lambda refresh=False: None)
    assert manager.resolve_pipeline("cfd", "auto", runner) == "native3d"
    with pytest.raises(manager.SimulationError, match="OpenFOAM not found"):
        manager.resolve_pipeline("cfd", "openfoam", runner)


# real OpenFOAM (skipped when not installed)

def _real_launcher():
    import os

    if os.getenv("PARAMFABRIC_OPENFOAM", "").lower() in {"none", "off", "0"}:
        os.environ.pop("PARAMFABRIC_OPENFOAM")
    return of.detect_launcher()


requires_openfoam = pytest.mark.skipif(_real_launcher() is None, reason="OpenFOAM is not installed")


@requires_openfoam
def test_real_checkmesh_accepts_tet_prism_polymesh(tmp_path, cylinder):
    info = of.write_polymesh(tmp_path, cylinder)
    of.write_simple_case(tmp_path, info, of.FoamSetup((10.0, 0, 0), 1.5e-5))
    result=_real_launcher().run(["checkMesh"], tmp_path, timeout=300)
    assert result.returncode == 0, result.stdout[-2000:]
    assert "Mesh OK." in result.stdout and "Upper triangular ordering OK." in result.stdout
    assert f"cells:            {info.cells}" in result.stdout


@requires_openfoam
def test_real_simplefoam_sst_produces_forces_yplus_and_surfaces(tmp_path, cylinder):
    info = of.write_polymesh(tmp_path, cylinder)
    of.write_simple_case(tmp_path, info, of.FoamSetup((10.0, 0, 0), 1.5e-5, end_time=60, write_interval=60,
                                                      reference_area=0.01, reference_length=0.1, length_scale=0.007))
    store, record, context = _context(tmp_path / "task")
    of._stream(_real_launcher(), ["simpleFoam"], tmp_path, context, of.ResidualParser())
    progress, _ = store.progress(record.id, 0)
    assert len(progress) == 60 and set(progress[-1]["fields"]) == {"Ux", "Uy", "Uz", "p", "k", "omega"}
    last = of.latest_time(tmp_path)
    assert last.name == "60"
    assert of.read_internal_field(last / "U").shape == (info.cells, 3)
    assert len(of.read_patch_field(last / "yPlus", "walls")) == info.patches[-1]["nFaces"]
    drag = of.read_force_coefficients(tmp_path)["Cd"]
    assert len(drag) == 60 and 0.2 < drag[-1] < 3.0
    walls=of.read_legacy_vtk_polydata(of.find_surface(tmp_path, "walls"))
    assert len(walls["triangles"]) == info.patches[-1]["nFaces"] and "p" in walls["point_data"]


def test_case_export_writes_body_fitted_sst_case(tmp_path, monkeypatch):
    from tests.test_native3d import _client

    client = _client(tmp_path, monkeypatch)
    client.post("/api/workbench/geometry/demo")
    client.post("/api/workbench/physics", json={"analysis": "cfd"})
    client.post("/api/workbench/features", json={"type": "flow_volume"})
    fabric = client.get("/api/local/fabric").json()
    for parameter in fabric["parameters"]:
        if parameter["name"] == "mesh_fineness_ratio":
            parameter["value"] = 8
    client.post("/api/local/fabric", json=fabric)
    exported = client.post("/api/workbench/solver/case", json={"engine": "openfoam"})
    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert body["turbulence"] == "kOmegaSST" and body["cells"] > 1000
    assert {"Allrun", "0/omega", "constant/polyMesh/faces", "constant/polyMesh/boundary", "system/controlDict"} <= set(body["files"])
    case = Path(body["case_dir"])
    assert "walls" in (case / "constant" / "polyMesh" / "boundary").read_text()
    assert "simpleFoam" in (case / "Allrun").read_text()


@pytest.mark.parametrize("model, fields, wall", [
    ("k_epsilon", {"k", "epsilon", "nut"}, "epsilonWallFunction"),
    ("spalart_allmaras", {"nuTilda", "nut"}, "nutUSpaldingWallFunction"),
])
def test_k_epsilon_and_spalart_allmaras_cases(tmp_path, cylinder, model, fields, wall):
    info = of.write_polymesh(tmp_path, cylinder)
    written = of.write_simple_case(tmp_path, info, of.FoamSetup((10.0, 0, 0), 1.5e-5, turbulence=model))
    zero = {Path(name).name for name in written if name.startswith("0/")}
    assert zero == {"U", "p"} | fields
    text = "".join((tmp_path / "0" / name).read_text() for name in fields)
    assert wall in text
    name = of.MODEL_ALIASES[model]
    assert f"model {name};" in (tmp_path / "constant" / "turbulenceProperties").read_text()
    schemes = (tmp_path / "system" / "fvSchemes").read_text()
    assert all(f"div(phi,{field})" in schemes for field in fields - {"nut"})
    if model == "spalart_allmaras":
        assert f"uniform {3 * 1.5e-5:.9g}" in (tmp_path / "0" / "nuTilda").read_text()  # ν̃ = 3ν at the inlet
    with pytest.raises(ValueError):
        of.write_simple_case(tmp_path, info, of.FoamSetup((1, 0, 0), 1e-5, turbulence="kOmega2006"))


def test_expert_relaxation_and_matrix_tolerances(tmp_path, duct):
    info=of.write_polymesh(tmp_path, duct)
    of.write_simple_case(tmp_path, info, of.FoamSetup((0.1, 0, 0), 1e-5, turbulence="laminar"))
    default = (tmp_path / "system" / "fvSolution").read_text()
    assert "fields" not in default and "equations { U 0.9;" in default
    of.write_simple_case(tmp_path, info, of.FoamSetup((0.1, 0, 0), 1e-5, turbulence="laminar", relax_velocity=0.6, relax_pressure=0.3,
                                                      tolerance_pressure=1e-9, tolerance_velocity=1e-10))
    tuned = (tmp_path / "system" / "fvSolution").read_text()
    assert "fields { p 0.3; }" in tuned and "equations { U 0.6;" in tuned
    assert "tolerance 1e-09; relTol 0.05" in tuned and "tolerance 1e-10; relTol 0.1" in tuned


def test_case_archive_excludes_processor_directories(tmp_path, duct):
    import zipfile

    case = tmp_path / "openfoam"
    info = of.write_polymesh(case, duct)
    of.write_simple_case(case, info, of.FoamSetup((0.1, 0, 0), 1e-5, turbulence="laminar"))
    (case / "processor0" / "0").mkdir(parents=True)
    (case / "processor0" / "0" / "U").write_text("x")
    names = zipfile.ZipFile(of.archive_case(case, tmp_path / "case.zip")).namelist()
    assert "openfoam/constant/polyMesh/faces" in names and "openfoam/system/controlDict" in names
    assert not any("processor" in name for name in names)


def test_volume_payload_and_port_glyphs(cylinder):
    from paramfabric.solvers import post

    linear=of.compact(cylinder.linear())
    speed=np.linalg.norm(linear.nodes, axis=1)
    velocity = np.tile([10.0, 0.0, 0.0], (len(linear.nodes), 1))
    volume = post.volume_payload(linear, {"speed": speed}, {"U": velocity})
    positions=np.array(volume["positions"]).reshape(-1, 3)
    tets = np.array(volume["tets"]).reshape(-1, 4)
    expected_tets = len(linear.cells["tet4"]) + 3 * len(linear.cells.get("prism6", []))
    assert len(tets) == expected_tets and tets.max() < len(positions)
    a, b, c, d = (positions[tets[:, k]] for k in range(4))
    total=np.abs(np.einsum("ij,ij->i", b - a, np.cross(c - a, d - a))).sum() / 6
    assert total == pytest.approx(0.9 * 0.4 * 0.3 - np.pi * 0.05 ** 2 * 0.1, rel=0.01)  # box minus the cylinder
    assert np.allclose(volume["scalars"]["speed"], np.linalg.norm(positions, axis=1), atol=1e-5)
    ports = post.port_glyphs(linear, velocity)
    assert {port["kind"] for port in ports} == {"inlet", "outlet"}
    assert all(len(port["positions"]) == len(port["vectors"]) and len(port["positions"]) <= 3 * 160 for port in ports)
    assert post.volume_payload(linear)["scalars"] == {}  # geometry only (mesh preview)


@requires_openfoam
@pytest.mark.parametrize("model", ["kEpsilon", "SpalartAllmaras"])
def test_real_simplefoam_runs_k_epsilon_and_spalart_allmaras(tmp_path, cylinder, model):
    info = of.write_polymesh(tmp_path, cylinder)
    of.write_simple_case(tmp_path, info, of.FoamSetup((10.0, 0, 0), 1.5e-5, turbulence=model, end_time=20, write_interval=20,
                                                      reference_area=0.01, reference_length=0.1, length_scale=0.007))
    store, record, context = _context(tmp_path / "task")
    of._stream(_real_launcher(), ["simpleFoam"], tmp_path, context, of.ResidualParser())
    progress, _ = store.progress(record.id, 0)
    expected = {"kEpsilon": {"k", "epsilon"}, "SpalartAllmaras": {"nuTilda"}}[model]
    assert len(progress) == 20 and expected <= set(progress[-1]["fields"])
    assert np.isfinite(of.read_force_coefficients(tmp_path)["Cd"][-1])


@pytest.mark.parametrize("path, safe", [("/mnt/c/work/case-1.2/x_y", True), ("/mnt/c/Users/me/Desktop/ai cfd/tasks", False),
                                        ("/home/o'neil/case", False), ('/a"b', False), ("/opt/x(86)", False), ("/tmp/caf\u00e9", False)])
def test_paths_openfoam_would_reject_are_detected(path, safe):
    assert of.foam_safe_path(path) is safe


def test_failure_message_says_what_happened_and_drops_the_banner():
    lines=["/*---------------------------------------------------------------------------*\\", "| =========                 |                                                 |",
             "| \\\\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox           |", "Build  : _615aae61-20250627 OPENFOAM=2506 version=2506",
             "Arch   : \"LSB;label=32;scalar=64\"", "Exec   : simpleFoam", "Host   : DESKTOP", "PID    : 761", "Time = 12",
             "#0  Foam::error::printStack(Foam::Ostream&) at ??:?", "#1  Foam::sigFpe::sigHandler(int) at ??:?"]
    message = of.explain_failure("simpleFoam", 136, lines)
    assert message.startswith("The solution diverged") and "lower the relaxation" in message
    assert "simpleFoam failed (exit 136)" in message and "sigHandler" in message
    assert "Open Source CFD Toolbox" not in message and "Build" not in message and "DESKTOP" not in message
    assert "unknown thing" in of.explain_failure("checkMesh", 1, ["unknown thing"])  # no hint: still the raw reason


@pytest.fixture
def spaced_case(tmp_path, duct):
    case = tmp_path / "my project's folder" / "ai cfd" / "case"
    case.mkdir(parents=True)
    return case


@requires_openfoam
def test_real_openfoam_runs_in_a_folder_with_spaces_and_quotes(spaced_case, duct):
    # the Windows path C:\\...\\ai cfd\\... used to abort: fileName::stripInvalid() called for invalid fileName.
    info=of.write_polymesh(spaced_case, duct)
    of.write_simple_case(spaced_case, info, of.FoamSetup((1.0, 0, 0), 1.5e-5))
    launcher = _real_launcher()
    check = launcher.run(["checkMesh"], spaced_case, timeout=300)
    assert check.returncode == 0, check.stdout[-1500:]
    assert "Mesh OK." in check.stdout and "stripInvalid" not in check.stdout


@requires_openfoam
def test_exported_allrun_works_in_a_folder_with_spaces(spaced_case, duct):
    info=of.write_polymesh(spaced_case, duct)
    of.write_simple_case(spaced_case, info, of.FoamSetup((1.0, 0, 0), 1.5e-5))
    launcher = _real_launcher()
    (spaced_case / "Check").write_text("#!/bin/sh\ncd \"${0%/*}\" || exit 1\n" + of.ALLRUN_ENTER + "\ncheckMesh > log.checkMesh 2>&1\n", encoding="utf-8", newline="\n")
    # started from outside the folder, like a user running the script: it must find its own way in
    target = of.windows_to_wsl(spaced_case / "Check") if launcher.mode == "wsl" else str(spaced_case / "Check")
    run = launcher.run(["sh", target], spaced_case.parent.parent.parent, timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Mesh OK." in (spaced_case / "log.checkMesh").read_text(encoding="utf-8", errors="replace")


@requires_openfoam
def test_studio_openfoam_solve_in_a_workspace_with_a_space(tmp_path, monkeypatch):
    from tests.test_native3d import _client, _wait

    workspace = tmp_path / "ai cfd"
    workspace.mkdir()
    client = _client(workspace, monkeypatch)
    client.post("/api/workbench/geometry/demo")
    client.post("/api/workbench/physics", json={"analysis": "cfd"})
    assert client.post("/api/workbench/features", json={"type": "flow_volume"}).status_code == 200
    fabric = client.get("/api/local/fabric").json()
    for parameter in fabric["parameters"]:
        if parameter["name"] == "mesh_fineness_ratio":
            parameter["value"] = 8
    assert client.post("/api/local/fabric", json=fabric).status_code == 200
    options = {**client.get("/api/workbench").json()["settings"].get("solver_options", {}), "max_iterations": 15}
    assert client.post("/api/workbench/settings", json={"values": {"solver_options": options}}).status_code == 200
    run = _wait(client, client.post("/api/workbench/solver/run", json={"engine": "openfoam"}).json()["job"]["id"])
    assert run["status"] == "completed", run["error"]
    assert run["result"]["engine"] == "openfoam" and " " in run["result"]["case_dir"]


def test_viewer_gets_patches_and_cell_types(cylinder):
    from paramfabric.solvers import post

    linear = cylinder.linear()
    patches = post.patch_surfaces(linear)
    assert {patch["kind"] for patch in patches} >= {"inlet", "outlet", "wall"}
    assert all(max(patch["indices"]) < len(patch["positions"]) / 3 for patch in patches)
    assert post.patch_surfaces(linear, limit=1) == []  # too large to send: the viewer falls back to one surface
    volume = post.volume_payload(linear)
    total = sum(block["cells"] * block["tets_per_cell"] for block in volume["cells"])
    assert total == len(volume["tets"]) / 4  # runs of tets line up with the cell types
    assert {block["kind"] for block in volume["cells"]} >= {"tet4", "prism6"}
