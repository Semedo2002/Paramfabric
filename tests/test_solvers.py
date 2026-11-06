import math
import sys
from pathlib import Path

import pytest

from paramfabric.core.schema import Fabric
from paramfabric.core.graph import ParameterGraph
from paramfabric.geometry import import_geometry, resolve_scene
from paramfabric.geometry.samples import prism_step
from paramfabric.solvers import calculix, openfoam
from paramfabric.solvers.builtin import solve_plane_stress, solve_potential_flow
from paramfabric.solvers.mesher import MeshingError, build_mesh
from paramfabric.solvers.physics import PhysicsError, ensure_parameters, resolve_physics
from paramfabric.server import tasks
from paramfabric.solvers.runner import SolverRunner, SolveSetup
from paramfabric.solvers.vtk import StructuredPoints, read_structured_points, write_structured_points


def _beam():
    model = import_geometry("beam.step", prism_step("beam", [(0, 0), (1000, 0), (1000, 100), (0, 100)], 50, plane="xz").encode())
    return model, resolve_scene([model], [], {})


def _cylinder():
    polygon = [(50 * math.cos(2 * math.pi * k / 32), 50 * math.sin(2 * math.pi * k / 32)) for k in range(32)]
    model = import_geometry("cyl.step", prism_step("cyl", polygon, 100).encode())
    scene = resolve_scene([model], [], {})
    scene["flow_domain"] = {"bbox_min": [-0.5, -0.4, -0.05], "bbox_max": [0.8, 0.4, 0.15]}
    return model, scene


def test_mesher_is_body_aligned_and_voxelizes_exact_box_volume():
    model, scene = _beam()
    mesh = build_mesh([model], scene, fineness=40)
    assert mesh.spacing == pytest.approx([0.025, 0.0125, 0.025])
    assert mesh.solid_count == 40 * 4 * 4
    quality = mesh.quality()
    assert quality["grade"] == "good"
    assert quality["min_cells_across_body"] == pytest.approx(4)
    coarse = build_mesh([model], scene, fineness=40, max_cells=600)
    assert coarse.cell_count <= 600 and coarse.warnings
    with pytest.raises(MeshingError, match="minimum mesh"):
        build_mesh([model], scene, fineness=10, max_cells=200)


def test_plane_stress_fea_matches_euler_bernoulli_cantilever():
    model, scene = _beam()
    residuals=[]
    result = solve_plane_stress(build_mesh([model], scene, fineness=40), 200e9, 0.3, 1000.0,
                                progress=lambda i, f: residuals.append(f["displacement"]))
    summary=result["summary"]
    assert result["converged"]
    assert summary["tip_deflection"] == pytest.approx(summary["beam_theory_tip_deflection"], rel=0.02)
    assert residuals[-1] < 1e-8 < residuals[0]


def test_potential_flow_recovers_cylinder_speedup_and_streams_residuals():
    model, scene = _cylinder()
    residuals=[]
    result = solve_potential_flow(build_mesh([model], scene, fineness=16), 10.0, 1.225,
                                  progress=lambda i, f: residuals.append(f["psi"]))
    assert result["converged"]
    # analytic potential flow peaks at 2U; the stair-stepped voxel cylinder lands close
    assert 1.6 < result["summary"]["max_speed"] / 10.0 < 2.2
    assert result["summary"]["min_pressure_coefficient"] < -1.5
    assert residuals[0] > residuals[-1]
    assert len(result["streamlines"]) > 10


def test_vtk_structured_points_round_trip(tmp_path: Path):
    data = StructuredPoints((2, 2, 1), (0, 0, 0.5), (0.1, 0.2, 1),
                            scalars={"p": [1, 2, 3, 4.5]}, vectors={"U": [(1, 0, 0)] * 4})
    write_structured_points(tmp_path / "a.vtk", data)
    loaded = read_structured_points(tmp_path / "a.vtk")
    assert loaded.dimensions == (2, 2, 1)
    assert loaded.spacing == (0.1, 0.2, 1.0)
    assert loaded.scalars["p"] == [1, 2, 3, 4.5]
    assert loaded.vectors["U"][3] == (1.0, 0.0, 0.0)


def test_physics_parameters_are_unit_checked():
    fabric = Fabric()
    created = ensure_parameters(fabric, "cfd")
    assert {p.name for p in created} == {"inlet_velocity", "fluid_density", "kinematic_viscosity", "mesh_fineness_ratio"}
    values = ParameterGraph(Fabric(parameters=created)).evaluate()
    physics = resolve_physics(values, "cfd")
    assert physics["inlet_velocity"] == 10.0
    document = Fabric(parameters=created).model_dump()
    document["parameters"][0]["unit"] = "m"
    with pytest.raises(PhysicsError, match="inlet_velocity must have dimensions"):
        resolve_physics(ParameterGraph(Fabric.model_validate(document)).evaluate(), "cfd")
    fea = resolve_physics(ParameterGraph(Fabric(parameters=ensure_parameters(Fabric(), "fea"))).evaluate(), "fea")
    assert fea["youngs_modulus"] == pytest.approx(200e9)


def test_openfoam_case_writer_and_residual_parser(tmp_path: Path):
    model, scene = _cylinder()
    mesh = build_mesh([model], scene, fineness=8)
    files = openfoam.write_case(tmp_path, mesh, "solid body\nendsolid body\n", velocity=5, kinematic_viscosity=1e-5)
    assert {"system/blockMeshDict", "system/snappyHexMeshDict", "0/U", "0/p", "constant/triSurface/body.stl"} <= set(files)
    block = (tmp_path / "system/blockMeshDict").read_text()
    assert f"({mesh.shape[0]} {mesh.shape[1]} {mesh.shape[2]})" in block
    assert "uniform (5 0 0)" in (tmp_path / "0/U").read_text()
    parser = openfoam.ResidualParser()
    events = [parser.feed(line) for line in [
        "Time = 1", "smoothSolver:  Solving for Ux, Initial residual = 1, Final residual = 0.05, No Iterations 2",
        "GAMG:  Solving for p, Initial residual = 0.5, Final residual = 0.01, No Iterations 4",
        "GAMG:  Solving for p, Initial residual = 0.4, Final residual = 0.01, No Iterations 4", "Time = 2",
    ]]
    assert [event for event in events if event] == [(1, {"Ux": 1.0, "p": 0.5})]


def test_calculix_input_and_dat_reader(tmp_path: Path):
    model, scene = _beam()
    mesh = build_mesh([model], scene, fineness=20)
    calculix.write_input(tmp_path, mesh, young=2e11, poisson=0.3, load=100)
    deck = (tmp_path / "paramfabric.inp").read_text()
    assert "*ELEMENT, TYPE=C3D8" in deck and "*BOUNDARY" in deck and "FIXED, 1, 3" in deck
    (tmp_path / "paramfabric.dat").write_text(
        " displacements (vx,vy,vz) for set LOAD and time  0.1000000E+01\n\n"
        "        12  1.0E-06  0.0E+00 -4.0E-04\n        13  1.0E-06  0.0E+00 -4.2E-04\n"
    )
    rows = calculix.read_load_displacements(tmp_path / "paramfabric.dat")
    assert [row[3] for row in rows] == [-4.0e-4, -4.2e-4]


def _context(tmp_path: Path):
    store = tasks.MemoryStore()
    record = tasks.TaskRecord(id="job1", kind="solve", owner="tester", payload={})
    store.save(record)
    return store, record, tasks.TaskContext(store, record, tmp_path / "job1")


def test_runner_builtin_execution_streams_residuals_and_loads_vtk(tmp_path: Path):
    model, scene = _beam()
    mesh = build_mesh([model], scene, fineness=20)
    runner = SolverRunner(tmp_path, which=lambda name: None)
    assert runner.engines()["openfoam"]["available"] is False
    assert runner.resolve_engine("fea", "auto") == "builtin"
    with pytest.raises(ValueError, match="not available"):
        runner.resolve_engine("fea", "calculix")
    physics = {"youngs_modulus": 2e11, "poissons_ratio": 0.3, "applied_load": 500.0, "mesh_fineness_ratio": 20}
    store, record, context = _context(tmp_path)
    result = runner.execute(SolveSetup("fea", mesh, physics, y_range=[0, 0.05]), "builtin", context)
    progress, count = store.progress(record.id, 0)
    assert count > 5 and progress[-1]["fields"]["displacement"] < 1e-8
    assert Path(result["vtk"]).is_file() and "fea_plane.vtk" in record.artifacts
    assert len(result["von_mises"]) == len(result["nodes"])


def test_runner_streams_external_solver_output(tmp_path: Path):
    fake=tmp_path / "fake_foam.py"
    fake.write_text(
        "import sys\nname = sys.argv[1]\nprint(name, 'starting', flush=True)\n"
        "if name == 'simpleFoam':\n"
        "    for t in range(1, 4):\n"
        "        print(f'Time = {t}')\n"
        "        print(f'smoothSolver:  Solving for Ux, Initial residual = {10.0 ** -t}, Final residual = 0, No Iterations 1')\n"
        "        print(f'GAMG:  Solving for p, Initial residual = {10.0 ** (-t - 1)}, Final residual = 0, No Iterations 1')\n"
        "    print('End')\n"
    )
    commands={name: [sys.executable, str(fake), name] for name in ("blockMesh", "snappyHexMesh", "simpleFoam")}
    runner = SolverRunner(tmp_path, commands=commands, which=lambda name: None)
    assert runner.engines()["openfoam"]["available"]
    model, scene = _cylinder()
    mesh = build_mesh([model], scene, fineness=6)
    physics={"inlet_velocity": 3.0, "fluid_density": 1.2, "kinematic_viscosity": 1e-5, "mesh_fineness_ratio": 6}
    store, record, context = _context(tmp_path)
    result=runner.execute(SolveSetup("cfd", mesh, physics, body_stl="solid b\nendsolid b\n"), "auto", context)
    assert result["engine"] == "openfoam"
    progress, _ = store.progress(record.id, 0)
    assert [entry["iteration"] for entry in progress] == [1, 2, 3]
    assert progress[-1]["fields"] == {"Ux": 1e-3, "p": 1e-4}
    log, _ = store.log(record.id, 0)
    assert any("foamToVTK not found" in line for line in log)
    assert (Path(result["case_dir"]) / "log.simpleFoam").is_file()
