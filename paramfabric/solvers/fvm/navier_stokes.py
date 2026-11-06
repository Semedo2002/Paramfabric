"""Native 3D steady incompressible Navier-Stokes solver (unstructured FVM).

Discretization: cell-centred collocated finite volumes on tet/prism/pyramid/hex
cells; SIMPLE pressure-velocity coupling with Rhie-Chow face fluxes;
linear-upwind (deferred correction) convection for momentum and first-order
upwind for turbulence; central diffusion with explicit non-orthogonal
correction; least-squares gradients.

Turbulence: laminar, or the standard k-ε model (Launder-Spalding) with
log-law wall functions. Boundary kinds: `inlet` (fixed velocity), `outlet`
(fixed static pressure p = 0), `wall` (no-slip), `slip` (symmetry).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from paramfabric.meshing.mesh import VolumeMesh
from paramfabric.solvers.fvm.topology import LeastSquaresGradient, Topology, build_topology, cell_to_node

Progress = Callable[[int, dict[str, float]], None]
INLET, OUTLET, WALL, SLIP = 0, 1, 2, 3
_KINDS = {"inlet": INLET, "outlet": OUTLET, "wall": WALL, "slip": SLIP, "symmetry": SLIP}
C_MU, C1, C2, SIGMA_K, SIGMA_E, KAPPA, E_WALL = 0.09, 1.44, 1.92, 1.0, 1.3, 0.41, 9.8
Y_STAR_LAMINAR = 11.225

try:  # Algebraic multigrid makes the pressure equation scale; optional
    import pyamg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised when pyamg is absent
    pyamg = None


class FlowError(ValueError):
    """Raised for an ill-posed flow case"""


class Cancelled(RuntimeError):
    """Raised when a solve is cancelled"""


@dataclass
class FlowCase:
    mesh: VolumeMesh
    density: float
    viscosity: float  # dynamic viscosity μ [Pa·s]
    inlet_velocity: float | tuple[float, float, float]
    turbulence: str = "laminar"
    turbulence_intensity: float = 0.05
    turbulence_length: float | None = None
    max_iterations: int = 400
    tolerance: float = 1e-4
    relax_velocity: float = 0.7
    relax_pressure: float = 0.3
    relax_turbulence: float = 0.6
    second_order: bool = True  # linear-upwind deferred correction
    first_order_iterations: int = 20  # upwind start-up before the deferred correction switches on
    velocity_limit: float = 5.0  # cells faster than this multiple of the inlet speed are capped
    reference_area: float | None = None


def _linear_solve(matrix: sp.csr_matrix, rhs: np.ndarray, guess: np.ndarray, symmetric: bool, rtol: float) -> np.ndarray:
    """Solve `A x = b` to `rtol` relative to the *initial* residual (OpenFOAM relTol).

    Small systems are factorized directly. Larger ones solve the correction
    equation `A δ = b − A x₀` so the tolerance tracks the current residual;
    a Krylov method that fails to converge escalates to an ILU-preconditioned
    method and finally to a direct factorization - failure is never ignored.
    """
    size=matrix.shape[0]
    if size <= 20000:
        return spla.splu(matrix.tocsc(), permc_spec="COLAMD").solve(rhs)
    residual = rhs - matrix @ guess
    if symmetric and pyamg is not None:
        hierarchy = pyamg.smoothed_aggregation_solver(matrix)
        return guess + np.column_stack([hierarchy.solve(residual[:, i], tol=rtol, accel="cg") for i in range(rhs.shape[1])])
    inverse_diagonal = 1.0 / matrix.diagonal()
    jacobi = spla.LinearOperator(matrix.shape, matvec=lambda x: x * inverse_diagonal)
    ilu: list[spla.LinearOperator] = []

    def ilu_operator() -> spla.LinearOperator:
        if not ilu:
            factor = spla.spilu(matrix.tocsc(), drop_tol=1e-4, fill_factor=10)
            ilu.append(spla.LinearOperator(matrix.shape, matvec=factor.solve))
        return ilu[0]

    columns = []
    for i in range(rhs.shape[1]):
        r = residual[:, i]
        norm = np.linalg.norm(r)
        if norm == 0:
            columns.append(guess[:, i])
            continue
        attempts = (
            [(spla.cg, "jacobi", 4000), (spla.cg, "ilu", 2000)] if symmetric
            else [(spla.bicgstab, "jacobi", 500), (spla.gmres, "ilu", 200)]
        )
        delta = None
        for method, kind, iterations in attempts:
            preconditioner = jacobi if kind == "jacobi" else ilu_operator()
            candidate, info = method(matrix, r, rtol=rtol, maxiter=iterations, M=preconditioner)
            if info == 0 and np.all(np.isfinite(candidate)) and np.linalg.norm(r - matrix @ candidate) <= max(rtol, 1e-6) * norm * 1.01:
                delta = candidate
                break
        if delta is None:
            delta=spla.splu(matrix.tocsc(), permc_spec="COLAMD").solve(r)
        columns.append(guess[:, i] + delta)
    return np.column_stack(columns)


def _normalised_residual(matrix: sp.csr_matrix, rhs: np.ndarray, field: np.ndarray) -> float:
    residual = np.abs(rhs - matrix @ field).sum()
    mean=field.mean(axis=0)
    row_sum = np.asarray(matrix.sum(axis=1)).ravel()
    reference = (np.abs(matrix @ field - np.outer(row_sum, mean)) + np.abs(rhs - np.outer(row_sum, mean))).sum()
    return float(residual / (reference + 1e-20))


class NavierStokes:
    def __init__(self, case: FlowCase, log: Callable[[str], None] | None = None) -> None:
        self.case = case
        self.say = log or (lambda message: None)
        self.t: Topology = build_topology(case.mesh.linear())
        t = self.t
        self.S = t.area
        self.Smag = np.linalg.norm(self.S, axis=1)
        self.n = self.S / self.Smag[:, None]
        I, B = t.internal, t.boundary
        self.P, self.N, self.Pb = t.owner[I], t.neighbour[I], t.owner[B]
        C, Cf = t.cell_centres, t.face_centres
        d = C[self.N] - C[self.P]
        self.d = d
        self.coef = self.Smag[I] ** 2 / np.einsum("ij,ij->i", d, self.S[I])
        self.k_vec = self.S[I] - d * self.coef[:, None]
        to_p = np.abs(np.einsum("ij,ij->i", Cf[I] - C[self.P], self.n[I]))
        to_n = np.abs(np.einsum("ij,ij->i", Cf[I] - C[self.N], self.n[I]))
        self.w = to_n / np.maximum(to_p + to_n, 1e-300)
        self.db = Cf[B] - C[self.Pb]
        self.y = np.maximum(np.abs(np.einsum("ij,ij->i", self.db, self.n[B])), 1e-12)
        self.coef_b = self.Smag[B] / self.y
        self.kind = np.full(len(B), WALL)
        position = np.full(len(t.owner), -1)
        position[B] = np.arange(len(B))
        self.patch_faces: dict[str, np.ndarray] = {}
        for patch in t.patches:
            code = _KINDS.get(patch.kind)
            if code is None:
                raise FlowError(f"boundary {patch.name!r} has unsupported kind {patch.kind!r}")
            self.kind[position[patch.faces]] = code
            self.patch_faces[patch.name] = position[patch.faces]
        if not np.any(self.kind == INLET):
            raise FlowError("the flow domain has no inlet boundary")
        nb = self.n[B]
        if np.isscalar(case.inlet_velocity):
            self.U_in = -float(case.inlet_velocity) * nb
        else:
            self.U_in = np.tile(np.asarray(case.inlet_velocity, float), (len(B), 1))
        self.U_in[self.kind != INLET] = 0.0
        # per-field gradient operators: only fixed-value boundary faces carry information
        self.grad = LeastSquaresGradient(t, self.kind != OUTLET)  # velocity: fixed at inlet/wall/slip
        self.grad_p = LeastSquaresGradient(t, self.kind == OUTLET)  # pressure: fixed at outlet only
        self.grad_turbulence = LeastSquaresGradient(t, self.kind == INLET)
        self.rows = np.concatenate([self.P, self.N, np.arange(t.cells)])
        self.cols = np.concatenate([self.N, self.P, np.arange(t.cells)])

    def _assemble(self, F: np.ndarray, gamma_cell: np.ndarray, fixed: np.ndarray, values: np.ndarray,
                  gamma_b: np.ndarray, old: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        t = self.t
        I, B = t.internal, t.boundary
        gamma_f = self.w * gamma_cell[self.P] + (1 - self.w) * gamma_cell[self.N]
        diffusion = gamma_f * self.coef
        Fi = F[I]
        a_pn = -diffusion - np.maximum(-Fi, 0)
        a_np = -diffusion - np.maximum(Fi, 0)
        diag = np.bincount(self.P, diffusion + np.maximum(Fi, 0), t.cells) + np.bincount(self.N, diffusion + np.maximum(-Fi, 0), t.cells)
        rhs = np.zeros_like(old)
        Fb = F[B]
        a_b = gamma_b * self.coef_b
        fixed_diag = np.where(fixed, a_b + np.maximum(Fb, 0), np.maximum(Fb, 0))
        diag += np.bincount(self.Pb, fixed_diag, t.cells)
        # Boundedness (Patankar): restore a_P >= Σ a_nb in cells whose flux field does not
        # yet conserve mass (net inflow), e.g. upstream of a wall in early iterations.
        # The term vanishes as continuity converges.
        net_outflow = np.bincount(self.P, Fi, t.cells) - np.bincount(self.N, Fi, t.cells) + np.bincount(self.Pb, Fb, t.cells)
        diag += np.maximum(-net_outflow, 0.0)
        inflow = -np.minimum(Fb, 0)
        source = np.where(fixed[:, None], (a_b + inflow)[:, None] * values, inflow[:, None] * old[self.Pb])
        np.add.at(rhs, self.Pb, source)
        return a_pn, a_np, diag, rhs

    def _matrix(self, a_pn, a_np, diag) -> sp.csr_matrix:
        n = self.t.cells
        return sp.csr_matrix((np.concatenate([a_pn, a_np, diag]), (self.rows, self.cols)), shape=(n, n))

    def _velocity_boundary(self, U: np.ndarray) -> np.ndarray:
        Ub = U[self.Pb].copy()
        Ub[self.kind == INLET] = self.U_in[self.kind == INLET]
        Ub[self.kind == WALL] = 0.0
        slip=self.kind == SLIP
        nb=self.n[self.t.boundary][slip]
        Ub[slip] -= np.einsum("ij,ij->i", Ub[slip], nb)[:, None] * nb
        return Ub

    def _pressure_boundary(self, p: np.ndarray, reference: float = 0.0) -> np.ndarray:
        pb = p[self.Pb].copy()
        pb[self.kind == OUTLET] = reference
        return pb

    def solve(self, progress: Progress | None = None, cancel: threading.Event | None = None) -> dict[str, Any]:
        case, t = self.case, self.t
        rho, mu = case.density, case.viscosity
        if rho <= 0 or mu <= 0:
            raise FlowError("density and viscosity must be positive")
        I, B = t.internal, t.boundary
        V = t.volumes
        nc = t.cells
        started = time.perf_counter()
        inlet_mean = self.U_in[self.kind == INLET].mean(axis=0)
        speed = float(np.linalg.norm(inlet_mean))
        U=np.tile(inlet_mean, (nc, 1)) if np.any(self.kind == OUTLET) else np.zeros((nc, 3))
        p = np.zeros(nc)
        F = np.zeros(len(t.owner))
        F[I] = rho * np.einsum("ij,ij->i", self.w[:, None] * U[self.P] + (1 - self.w)[:, None] * U[self.N], self.S[I])
        F[B] = rho * np.einsum("ij,ij->i", self._velocity_boundary(U), self.S[B])
        F[B[self.kind == WALL]] = 0.0
        F[B[self.kind == SLIP]] = 0.0
        has_outlet = bool(np.any(self.kind == OUTLET))
        turbulent = case.turbulence == "k-epsilon"
        extent = t.face_centres.max(axis=0) - t.face_centres.min(axis=0)
        length = case.turbulence_length or 0.07 * float(np.max(extent))
        k_in = 1.5 * (case.turbulence_intensity * max(speed, 1e-12)) ** 2
        eps_in = C_MU ** 0.75 * k_in ** 1.5 / length
        k = np.full(nc, k_in)
        eps = np.full(nc, eps_in)
        mu_t = np.zeros(nc)
        wall_faces = np.flatnonzero(self.kind == WALL)
        wall_cells = self.Pb[wall_faces]
        inlet_flux = abs(F[B[self.kind == INLET]].sum()) or 1.0
        history: list[dict[str, float]] = []
        converged = False
        iteration = 0
        mu_wall = np.full(len(wall_faces), mu)
        last_limited = 0
        for iteration in range(1, case.max_iterations + 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled("solve cancelled")
            mu_eff = mu + mu_t
            Ub = self._velocity_boundary(U)
            gradU = self.grad(U, Ub)  # (nc, 3, 3): d u_i / d x_j
            gamma_b = np.full(len(B), mu)
            gamma_b[wall_faces] = mu_wall
            fixed=self.kind != OUTLET
            a_pn, a_np, diag, rhs = self._assemble(F, mu_eff, fixed, Ub, gamma_b, U)
            # Explicit non-orthogonal diffusion correction
            gamma_f = self.w * mu_eff[self.P] + (1 - self.w) * mu_eff[self.N]
            grad_f = self.w[:, None, None] * gradU[self.P] + (1 - self.w)[:, None, None] * gradU[self.N]
            correction = gamma_f[:, None] * np.einsum("fij,fj->fi", grad_f, self.k_vec)
            # "limited 0.5" (OpenFOAM): the explicit part may not exceed the implicit
            # orthogonal flux, which keeps highly skewed tetrahedra stable
            orthogonal = np.abs(gamma_f * self.coef)[:, None] * np.abs(U[self.N] - U[self.P])
            correction = np.clip(correction, -orthogonal, orthogonal)
            np.add.at(rhs, self.P, correction)
            np.subtract.at(rhs, self.N, correction)
            if case.second_order and iteration > case.first_order_iterations:
                # deferred correction: implicit upwind + explicit linear-upwind face value
                Fi = F[I]
                upwind = np.where(Fi >= 0, self.P, self.N)
                Cf = t.face_centres[I]
                delta = np.einsum("fij,fj->fi", gradU[upwind], Cf - t.cell_centres[upwind])
                limited = Fi[:, None] * delta
                np.subtract.at(rhs, self.P, limited)
                np.add.at(rhs, self.N, limited)
            grad_p = self.grad_p(p, self._pressure_boundary(p))
            rhs -= V[:, None] * grad_p
            diag_relaxed=diag / case.relax_velocity
            rhs += ((1 - case.relax_velocity) * diag_relaxed)[:, None] * U
            momentum = self._matrix(a_pn, a_np, diag_relaxed)
            residual_u = [_normalised_residual(momentum, rhs[:, [i]], U[:, [i]]) for i in range(3)]
            U_star = _linear_solve(momentum, rhs, U, False, 1e-3)

            # Rhie-Chow face fluxes and pressure correction
            D = V / diag_relaxed
            D_f=self.w * D[self.P] + (1 - self.w) * D[self.N]
            grad_p_f = self.w[:, None] * grad_p[self.P] + (1 - self.w)[:, None] * grad_p[self.N]
            U_f = self.w[:, None] * U_star[self.P] + (1 - self.w)[:, None] * U_star[self.N]
            # Rhie-Chow: compare the interpolated gradient and the compact difference
            # along the same (orthogonal) direction Δ = d·|S|²/(d·S); the non-orthogonal
            # part k cancels, so a uniform pressure gradient produces no spurious flux
            F_int = rho * (np.einsum("ij,ij->i", U_f, self.S[I]) + D_f * self.coef * (np.einsum("ij,ij->i", grad_p_f, self.d) - (p[self.N] - p[self.P])))
            F_b = np.zeros(len(B))
            inlet = self.kind == INLET
            outlet = self.kind == OUTLET
            F_b[inlet] = rho * np.einsum("ij,ij->i", self.U_in[inlet], self.S[B][inlet])
            pb = self._pressure_boundary(p)
            owners = self.Pb[outlet]
            F_b[outlet] = rho * (np.einsum("ij,ij->i", U_star[owners], self.S[B][outlet]) + D[owners] * self.coef_b[outlet] * (np.einsum("ij,ij->i", grad_p[owners], self.db[outlet]) - (pb[outlet] - p[owners])))
            divergence = np.bincount(self.P, F_int, nc) - np.bincount(self.N, F_int, nc) + np.bincount(self.Pb, F_b, nc)
            c_int = rho * D_f * self.coef
            c_out = rho * D[owners] * self.coef_b[outlet]
            p_diag = np.bincount(self.P, c_int, nc) + np.bincount(self.N, c_int, nc) + np.bincount(owners, c_out, nc)
            if not has_outlet:
                p_diag[0] += p_diag.max() * 1e3
            pressure = self._matrix(-c_int, -c_int, p_diag)
            p_corr = _linear_solve(pressure, -divergence[:, None], np.zeros((nc, 1)), True, 1e-2)[:, 0]
            residual_p = float(np.abs(divergence).sum() / inlet_flux)
            F[I] = F_int - c_int * (p_corr[self.N] - p_corr[self.P])
            F_b[outlet] += c_out * p_corr[owners]
            F[B] = F_b
            p_corr_b=p_corr[self.Pb].copy()
            p_corr_b[outlet] = 0.0
            U = U_star - D[:, None] * self.grad_p(p_corr, p_corr_b)
            # Velocity limiting (as in commercial codes): cap runaway cells, report them
            magnitude = np.linalg.norm(U, axis=1)
            cap = case.velocity_limit * max(speed, 1e-12)
            limited = magnitude > cap
            if limited.any():
                U[limited] *= (cap / magnitude[limited])[:, None]
                limited_cells = int(limited.sum())
                if limited_cells != last_limited:
                    self.say(f"iteration {iteration}: velocity limited in {limited_cells} cell(s)")
                last_limited=limited_cells
            else:
                last_limited = 0
            p = p + case.relax_pressure * p_corr
            if not has_outlet:
                p -= p.mean()
            fields = {"Ux": residual_u[0], "Uy": residual_u[1], "Uz": residual_u[2], "p": residual_p}

            if turbulent:
                k, eps, mu_t, mu_wall, turb_res = self._turbulence(U, F, k, eps, mu_t, k_in, eps_in, wall_faces, wall_cells)
                fields.update(turb_res)
            history.append({"iteration": iteration, **fields})
            if progress:
                progress(iteration, fields)
            if not np.all(np.isfinite(U)) or not np.all(np.isfinite(p)) or np.abs(U).max() > 1e4 * max(speed, 1e-9):
                raise FlowError(f"solution diverged at iteration {iteration}; refine the mesh or lower the relaxation factors")
            if max(fields.values()) < case.tolerance and iteration > 5:
                converged = True
                break
        self.say(f"SIMPLE finished after {iteration} iterations in {time.perf_counter() - started:.1f} s")
        self.limited_cells = last_limited
        return self._results(U, p, F, k, eps, mu_t, mu_wall, converged, iteration, history, turbulent, speed)

    def _turbulence(self, U, F, k, eps, mu_t, k_in, eps_in, wall_faces, wall_cells):
        case, t = self.case, self.t
        rho, mu, V, nc = case.density, case.viscosity, t.volumes, t.cells
        Ub = self._velocity_boundary(U)
        gradU = self.grad(U, Ub)
        strain = 0.5 * (gradU + np.transpose(gradU, (0, 2, 1)))
        production=mu_t * 2 * np.einsum("cij,cij->c", strain, strain)
        # Production limiter (Menter): P_k <= 10 ρ ε stops spurious k growth in
        # strain-dominated regions (stagnation points, far-field corners)
        production = np.minimum(production, 10 * rho * np.maximum(eps, 0.0))
        # Wall functions on wall-adjacent cells
        y=self.y[wall_faces]
        u_star = C_MU ** 0.25 * np.sqrt(np.maximum(k[wall_cells], 1e-16))
        y_star = rho * u_star * y / mu
        nb = self.n[t.boundary][wall_faces]
        U_P = U[wall_cells]
        U_t = np.linalg.norm(U_P - np.einsum("ij,ij->i", U_P, nb)[:, None] * nb, axis=1)
        log_region=y_star > Y_STAR_LAMINAR
        mu_wall = np.where(log_region, rho * KAPPA * u_star * y / np.log(E_WALL * np.maximum(y_star, 1.01)), mu)
        tau_w = mu_wall * U_t / y
        wall_production=np.zeros(nc)
        wall_area = np.zeros(nc)
        weights = self.Smag[t.boundary][wall_faces]
        np.add.at(wall_production, wall_cells, weights * tau_w * u_star / (KAPPA * y))
        np.add.at(wall_area, wall_cells, weights)
        is_wall = wall_area > 0
        production[is_wall] = (wall_production[is_wall] / wall_area[is_wall])
        y_cell = np.full(nc, np.inf)
        np.minimum.at(y_cell, wall_cells, y)
        eps_wall = C_MU ** 0.75 * np.maximum(k, 1e-16) ** 1.5 / (KAPPA * np.where(is_wall, y_cell, 1.0))
        residuals = {}
        B = t.boundary
        inlet = self.kind == INLET
        values_k = np.where(inlet, k_in, 0.0)[:, None]
        values_e = np.where(inlet, eps_in, 0.0)[:, None]
        gamma_b = np.full(len(B), mu)
        for name, field, sigma, values in (("k", k, SIGMA_K, values_k), ("epsilon", eps, SIGMA_E, values_e)):
            gamma = mu + mu_t / sigma
            a_pn, a_np, diag, rhs = self._assemble(F, gamma, inlet, values, gamma_b * np.where(inlet, 1.0, 0.0), field[:, None])
            ratio = eps / np.maximum(k, 1e-16)
            if name == "k":
                rhs[:, 0] += production * V
                diag += rho * ratio * V
            else:
                rhs[:, 0] += C1 * production * ratio * V
                diag += C2 * rho * ratio * V
                big = diag.max() * 1e8
                diag[is_wall] = big
                rhs[is_wall, 0] = big * eps_wall[is_wall]
            diag_relaxed = diag / case.relax_turbulence
            rhs[:, 0] += (1 - case.relax_turbulence) * diag_relaxed * field
            matrix = self._matrix(a_pn, a_np, diag_relaxed)
            residuals[name] = _normalised_residual(matrix, rhs, field[:, None])
            solved = np.maximum(_linear_solve(matrix, rhs, field[:, None], False, 1e-3)[:, 0], 1e-14)
            if name == "k":
                k = solved
            else:
                eps = solved
        # Length-scale limiter: ε >= Cμ^¾ k^{3/2} / ℓ_max keeps the turbulent length
        # scale below a tenth of the domain size where ε would otherwise collapse
        extent = self.t.face_centres.max(axis=0) - self.t.face_centres.min(axis=0)
        length_max=0.1 * float(np.max(extent))
        eps = np.maximum(eps, C_MU ** 0.75 * k ** 1.5 / length_max)
        mu_t = np.minimum(rho * C_MU * k ** 2 / np.maximum(eps, 1e-16), 1e5 * mu)
        return k, eps, mu_t, mu_wall, residuals

    def _results(self, U, p, F, k, eps, mu_t, mu_wall, converged, iteration, history, turbulent, speed):
        case, t = self.case, self.t
        rho, mu = case.density, case.viscosity
        B = t.boundary
        grad_p = self.grad_p(p, self._pressure_boundary(p))
        walls = np.flatnonzero(self.kind == WALL)
        owners = self.Pb[walls]
        p_wall = p[owners] + np.einsum("ij,ij->i", grad_p[owners], self.db[walls])
        S_w = self.S[B][walls]
        nb = self.n[B][walls]
        U_t = U[owners] - np.einsum("ij,ij->i", U[owners], nb)[:, None] * nb
        mu_face = np.full(len(walls), mu)
        if turbulent:
            mu_face = mu_wall
        shear = mu_face[:, None] * U_t / self.y[walls][:, None]
        pressure_force=(p_wall[:, None] * S_w).sum(axis=0)
        viscous_force = (shear * self.Smag[B][walls][:, None]).sum(axis=0)
        total = pressure_force + viscous_force
        direction = self.U_in[self.kind == INLET].mean(axis=0)
        direction = direction / (np.linalg.norm(direction) or 1.0)
        tau = np.linalg.norm(shear, axis=1)
        y_plus = rho * np.sqrt(tau / rho) * self.y[walls] / mu
        summary: dict[str, Any] = {
            "converged": converged,
            "iterations": iteration,
            "max_velocity": float(np.linalg.norm(U, axis=1).max()),
            "pressure_range": [float(p.min()), float(p.max())],
            "force_pressure": pressure_force.tolist(),
            "force_viscous": viscous_force.tolist(),
            "force_total": total.tolist(),
            "drag": float(np.dot(total, direction)),
            "mean_wall_y_plus": float(y_plus.mean()) if len(y_plus) else None,
            "max_wall_y_plus": float(y_plus.max()) if len(y_plus) else None,
            "mass_imbalance": history[-1]["p"] if history else None,
            "cells": int(t.cells),
            "turbulence": case.turbulence,
            "velocity_limited_cells": int(getattr(self, "limited_cells", 0)),
        }
        if case.reference_area and speed > 0:
            summary["drag_coefficient"] = summary["drag"] / (0.5 * rho * speed ** 2 * case.reference_area)
        summary["mass_flow"] = {name: float(F[B][faces].sum()) for name, faces in self.patch_faces.items()}
        cell_fields = {"U": U, "p": p}
        if turbulent:
            cell_fields.update(k=k, epsilon=eps, nut=mu_t / rho)
        return {
            "topology": t,
            "cell": cell_fields,
            "node": {name: cell_to_node(t, value) for name, value in cell_fields.items()},
            "wall_shear": tau,
            "wall_faces": B[walls],
            "history": history,
            "summary": summary,
        }


def solve_flow(case: FlowCase, *, progress: Progress | None = None, cancel: threading.Event | None = None,
               log: Callable[[str], None] | None = None) -> dict[str, Any]:
    return NavierStokes(case, log).solve(progress, cancel)
