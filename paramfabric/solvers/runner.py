"""Solver engine discovery and execution with live residual streaming.

Engines:

`builtin`   pure-Python reference solvers (always available)
`openfoam`  blockMesh -> snappyHexMesh -> simpleFoam (-> foamToVTK), when on PATH
`calculix`  `ccx` linear static, when on PATH

`auto` picks the external engine for the analysis when it is installed and
falls back to the built-in solver otherwise. Built-in results are written as
legacy VTK and read back to build the viewport overlay payload.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paramfabric.solvers import calculix, openfoam
from paramfabric.solvers.builtin import SolverCancelled, solve_plane_stress, solve_potential_flow
from paramfabric.solvers.mesher import CartesianMesh
from paramfabric.solvers.vtk import StructuredPoints, read_structured_points, write_structured_points

_EXECUTABLES = {
    "openfoam": ("blockMesh", "snappyHexMesh", "simpleFoam"),
    "calculix": ("ccx",),
}


@dataclass
class SolveSetup:
    analysis: str
    mesh: CartesianMesh
    physics: dict[str, float]
    body_stl: str = ""
    y_range: list[float] = field(default_factory=lambda: [0.0, 0.0])


class Reporter:
    """Adapter giving engine code a uniform progress/log/cancel interface over a task context"""

    def __init__(self, context: Any) -> None:
        self.context = context
        self.residuals: list[dict[str, Any]] = []
        self.cancel_event = _ContextCancel(context)

    def record(self, iteration: int, fields: dict[str, float]) -> None:
        self.residuals.append({"iteration": iteration, "fields": fields})
        self.context.progress(iteration, fields)

    def write(self, line: str) -> None:
        self.context.log(line)


class _ContextCancel:
    def __init__(self, context: Any) -> None:
        self.context = context

    def is_set(self) -> bool:
        return bool(self.context.cancelled)


class SolverRunner:
    """Run solver jobs on background threads, one at a time per workspace"""

    def __init__(
        self,
        root: Path,
        *,
        commands: dict[str, list[str]] | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.root = Path(root)
        self._which = which
        self._commands = dict(commands or {})

    # engine discovery
    def command(self, executable: str) -> list[str] | None:
        if executable in self._commands:
            return list(self._commands[executable])
        override_dir = os.getenv("PARAMFABRIC_OPENFOAM_BIN") if executable != "ccx" else None
        if executable == "ccx" and os.getenv("PARAMFABRIC_CCX"):
            return [os.environ["PARAMFABRIC_CCX"]]
        if override_dir:
            candidate = Path(override_dir) / executable
            if candidate.exists():
                return [str(candidate)]
        found = self._which(executable)
        return [found] if found else None

    def engines(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {
            "builtin": {
                "available": True,
                "analyses": ["cfd", "fea"],
                "label": "Built-in reference solvers",
                "detail": "Potential flow (CFD) and plane-stress QM6 FEA; pure Python.",
            }
        }
        for engine, executables in _EXECUTABLES.items():
            missing = [name for name in executables if self.command(name) is None]
            result[engine] = {
                "available": not missing,
                "missing": missing,
                "analyses": ["cfd"] if engine == "openfoam" else ["fea"],
                "label": "OpenFOAM simpleFoam" if engine == "openfoam" else "CalculiX ccx",
                "detail": "Detected on PATH." if not missing else f"Not found on PATH: {', '.join(missing)}",
            }
        result["elmer"] = {
            "available": False,
            "analyses": [],
            "label": "Elmer",
            "detail": (
                "ElmerSolver detected, but no Paramfabric case writer exists for it yet."
                if self._which("ElmerSolver") else "Not supported in this release."
            ),
        }
        return result

    def resolve_engine(self, analysis: str, engine: str) -> str:
        engines = self.engines()
        if engine == "auto":
            external = "openfoam" if analysis == "cfd" else "calculix"
            return external if engines[external]["available"] else "builtin"
        if engine not in engines or analysis not in engines[engine]["analyses"]:
            raise ValueError(f"engine {engine!r} does not support {analysis!r}")
        if not engines[engine]["available"]:
            raise ValueError(f"{engines[engine]['label']} is not available: {engines[engine]['detail']}")
        return engine

    # execution
    def execute(self, setup: SolveSetup, engine: str, context: Any) -> dict[str, Any]:
        """Run one solve synchronously, streaming through `context` (a task context)"""
        chosen = self.resolve_engine(setup.analysis, engine)
        reporter = Reporter(context)
        case_dir = Path(context.artifact_dir)
        try:
            if chosen == "builtin":
                result = self._run_builtin(reporter, setup, case_dir)
            elif chosen == "openfoam":
                result = self._run_openfoam(reporter, setup, case_dir)
            else:
                result = self._run_calculix(reporter, setup, case_dir)
        except SolverCancelled as exc:
            from paramfabric.server.tasks import TaskCancelled

            raise TaskCancelled(str(exc)) from exc
        for path in sorted(case_dir.iterdir()):
            if path.is_file():
                context.artifact(path.name)
        result["engine"] = chosen
        return result

    def _run_builtin(self, job: Reporter, setup: SolveSetup, case_dir: Path) -> dict[str, Any]:
        physics = setup.physics
        mesh = setup.mesh
        job.write(f"Built-in {setup.analysis.upper()} solver on {mesh.shape[0]}×{mesh.shape[1]}×{mesh.shape[2]} mesh")
        if setup.analysis == "cfd":
            solution = solve_potential_flow(
                mesh, physics["inlet_velocity"], physics["fluid_density"], progress=job.record, cancel=job.cancel_event
            )
            nx, ny = solution["grid"]["shape"]
            vtk_path = case_dir / "cfd_slice.vtk"
            fields=solution["fields"]
            write_structured_points(vtk_path, StructuredPoints(
                (nx, ny, 1), tuple(solution["grid"]["origin"]), (*solution["grid"]["spacing"], 1.0),  # type: ignore[arg-type]
                scalars={"p": fields["pressure"], "speed": fields["speed"], "solid": [float(v) for v in fields["solid"]]},
                vectors={"U": [(u, v, 0.0) for u, v in zip(fields["velocity_x"], fields["velocity_y"])]},
                title="Paramfabric potential-flow slice",
            ))
            loaded = read_structured_points(vtk_path)
            job.write(f"Wrote and reloaded {vtk_path.name} ({loaded.point_count} points)")
            return {
                "analysis": "cfd",
                "engine": "builtin",
                "method": solution["method"],
                "converged": solution["converged"],
                "iterations": solution["iterations"],
                "grid": {
                    "origin": list(loaded.origin),
                    "spacing": list(loaded.spacing[:2]),
                    "shape": list(loaded.dimensions[:2]),
                },
                "fields": {
                    "pressure": loaded.scalars["p"],
                    "speed": loaded.scalars["speed"],
                    "solid": loaded.scalars["solid"],
                },
                "streamlines": solution["streamlines"],
                "summary": solution["summary"],
                "units": {"pressure": "Pa", "speed": "m/s"},
                "vtk": str(vtk_path),
            }
        solution = solve_plane_stress(
            mesh, physics["youngs_modulus"], physics["poissons_ratio"], physics["applied_load"],
            progress=job.record, cancel=job.cancel_event,
        )
        gx, gz = solution["grid"]["shape"]
        grid_index = {tuple(node): index for index, node in enumerate(solution["nodes"])}
        von_mises, displacement = [], []
        for k in range(gz):
            for i in range(gx):
                node = grid_index.get((i, k))
                von_mises.append(solution["von_mises"][node] if node is not None else 0.0)
                u, w = solution["displacement"][node] if node is not None else (0.0, 0.0)
                displacement.append((u, 0.0, w))
        vtk_path = case_dir / "fea_plane.vtk"
        origin = solution["grid"]["origin"]
        spacing=solution["grid"]["spacing"]
        write_structured_points(vtk_path, StructuredPoints(
            (gx, 1, gz), (origin[0], origin[1], origin[2]), (spacing[0], 1.0, spacing[1]),
            scalars={"von_mises": von_mises}, vectors={"displacement": displacement},
            title="Paramfabric plane-stress result",
        ))
        loaded = read_structured_points(vtk_path)
        job.write(f"Wrote and reloaded {vtk_path.name} ({loaded.point_count} points)")
        loaded_stress = loaded.scalars["von_mises"]
        loaded_displacement = loaded.vectors["displacement"]
        nodes = solution["nodes"]
        return {
            "analysis": "fea",
            "engine": "builtin",
            "method": solution["method"],
            "converged": solution["converged"],
            "iterations": solution["iterations"],
            "nodes": [
                [origin[0] + i * spacing[0], origin[2] + k * spacing[1]] for i, k in nodes
            ],
            "elements": solution["elements"],
            "von_mises": [loaded_stress[k * gx + i] for i, k in nodes],
            "displacement": [
                [loaded_displacement[k * gx + i][0], loaded_displacement[k * gx + i][2]] for i, k in nodes
            ],
            "y_range": setup.y_range,
            "summary": solution["summary"],
            "units": {"von_mises": "Pa", "displacement": "m"},
            "vtk": str(vtk_path),
        }

    def _stream(
        self, job: Reporter, command: list[str], cwd: Path, parser: openfoam.ResidualParser | None, log_name: str
    ) -> None:
        job.write("$ " + " ".join(command))
        try:
            process = subprocess.Popen(
                command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, errors="replace",
            )
        except OSError as exc:
            raise RuntimeError(f"could not start {command[0]}: {exc}") from exc
        assert process.stdout is not None
        with open(cwd / f"log.{log_name}", "w", encoding="utf-8") as log_file:
            for line in process.stdout:
                log_file.write(line)
                job.write(line)
                if parser:
                    completed = parser.feed(line)
                    if completed:
                        job.record(*completed)
                if job.cancel_event.is_set():
                    process.terminate()
                    process.wait(timeout=10)
                    raise SolverCancelled("solve cancelled")
        if parser:
            completed = parser.flush()
            if completed:
                job.record(*completed)
        if process.wait() != 0:
            raise RuntimeError(f"{Path(command[0]).name} exited with status {process.returncode}")

    def _run_openfoam(self, job: Reporter, setup: SolveSetup, case_dir: Path) -> dict[str, Any]:
        files = openfoam.write_case(
            case_dir, setup.mesh, setup.body_stl,
            velocity=setup.physics["inlet_velocity"],
            kinematic_viscosity=setup.physics["kinematic_viscosity"],
        )
        job.write(f"Wrote OpenFOAM case with {len(files)} files to {case_dir}")
        for command in openfoam.COMMANDS:
            resolved = self.command(command[0])
            if resolved is None:
                if command[0] == "foamToVTK":
                    job.write("foamToVTK not found; skipping VTK conversion.")
                    continue
                raise RuntimeError(f"{command[0]} is not available")
            parser = openfoam.ResidualParser() if command[0] == "simpleFoam" else None
            self._stream(job, resolved + command[1:], case_dir, parser, command[0])
        final = job.residuals[-1]["fields"] if job.residuals else {}
        return {
            "analysis": "cfd",
            "engine": "openfoam",
            "method": "OpenFOAM simpleFoam (laminar, steady)",
            "converged": bool(final) and all(value < 1e-4 for value in final.values()),
            "iterations": job.residuals[-1]["iteration"] if job.residuals else 0,
            "summary": {"final_residuals": final},
            "case_dir": str(case_dir),
            "note": "Field results are in the OpenFOAM case (VTK/ after foamToVTK); open them in ParaView.",
        }

    def _run_calculix(self, job: Reporter, setup: SolveSetup, case_dir: Path) -> dict[str, Any]:
        calculix.write_input(
            case_dir, setup.mesh,
            young=setup.physics["youngs_modulus"], poisson=setup.physics["poissons_ratio"],
            load=setup.physics["applied_load"],
        )
        resolved=self.command("ccx")
        if resolved is None:
            raise RuntimeError("ccx is not available")
        self._stream(job, resolved + ["-i", calculix.JOB_NAME], case_dir, None, "ccx")
        displacements = calculix.read_load_displacements(case_dir / f"{calculix.JOB_NAME}.dat")
        tip = sum(row[3] for row in displacements) / len(displacements) if displacements else None
        return {
            "analysis": "fea",
            "engine": "calculix",
            "method": "CalculiX linear static (C3D8)",
            "converged": True,
            "iterations": 1,
            "summary": {"tip_deflection": tip, "applied_load": setup.physics["applied_load"]},
            "case_dir": str(case_dir),
            "note": "Full stress fields are in paramfabric.frd; open them in CalculiX GraphiX or ParaView.",
        }
