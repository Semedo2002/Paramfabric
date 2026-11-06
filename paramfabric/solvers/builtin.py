"""Built-in reference solvers (pure Python, no external dependencies).

`solve_potential_flow`
    Steady, inviscid, incompressible 2D potential flow on the mid-height
    XY slice of the Cartesian mesh. The stream function satisfies Laplace's
    equation (SOR); bodies are streamlines with constant ψ. Velocity comes from
    ψ, gauge pressure from Bernoulli. Inviscid: no boundary layers, separation,
    or drag (d'Alembert) - use OpenFOAM for viscous results.

`solve_plane_stress`
    Linear-elastic plane-stress FEA on the mid-depth XZ slice using QM6
    incompatible-mode quadrilaterals (exact in pure bending on rectangles),
    Jacobi-preconditioned conjugate gradients, the minimum-X face clamped, and
    the load applied at the maximum-X face in −Z.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from typing import Any

from paramfabric.solvers.mesher import CartesianMesh

Progress = Callable[[int, dict[str, float]], None]


class SolverCancelled(RuntimeError):
    """Raised when a running solve is cancelled"""


def _components(mask: list[bool], nx: int, ny: int) -> list[int]:
    labels=[-1] * (nx * ny)
    current = 0
    for start in range(nx * ny):
        if not mask[start] or labels[start] >= 0:
            continue
        stack = [start]
        labels[start] = current
        while stack:
            cell = stack.pop()
            i, j = cell % nx, cell // nx
            for ni, nj in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if 0 <= ni < nx and 0 <= nj < ny:
                    neighbour = nj * nx + ni
                    if mask[neighbour] and labels[neighbour] < 0:
                        labels[neighbour] = current
                        stack.append(neighbour)
        current += 1
    return labels


def solve_potential_flow(
    mesh: CartesianMesh,
    velocity: float,
    density: float,
    *,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    tolerance: float = 1e-6,
    max_iterations: int = 20_000,
    streamline_count: int = 28,
) -> dict[str, Any]:
    if velocity <= 0:
        raise ValueError("inlet_velocity must be positive")
    nx, ny, nz = mesh.shape
    if nx < 3 or ny < 3:
        raise ValueError("mesh is too coarse for a 2D flow solution (need at least 3×3 cells in XY)")
    dx, dy = mesh.spacing[0], mesh.spacing[1]
    body_mid_z = 0.5 * (mesh.body_bbox[0][2] + mesh.body_bbox[1][2])
    k = min(nz - 1, max(0, int((body_mid_z - mesh.origin[2]) / mesh.spacing[2])))
    slice_z = mesh.origin[2] + (k + 0.5) * mesh.spacing[2]
    mask = [bool(mesh.solid[mesh.index(i, j, k)]) for j in range(ny) for i in range(nx)]
    y0 = mesh.origin[1]
    height = ny * dy

    def freestream(j: int) -> float:
        return velocity * (j + 0.5) * dy

    labels = _components(mask, nx, ny)
    body_values: dict[int, float] = {}
    for label in set(labels) - {-1}:
        rows = [cell // nx for cell, value in enumerate(labels) if value == label]
        body_values[label] = velocity * ((sum(rows) / len(rows)) + 0.5) * dy

    psi = [freestream(cell // nx) for cell in range(nx * ny)]
    fixed=[False] * (nx * ny)
    for cell in range(nx * ny):
        i, j = cell % nx, cell // nx
        if mask[cell]:
            psi[cell] = body_values[labels[cell]]
            fixed[cell] = True
        elif i == 0 or j == 0 or j == ny - 1:
            fixed[cell] = True
    ax, ay = 1 / (dx * dx), 1 / (dy * dy)
    diagonal = 2 * (ax + ay)
    omega = 2 / (1 + math.sin(math.pi / max(nx, ny)))
    update_cells = [cell for cell in range(nx * ny) if not fixed[cell] and cell % nx != nx - 1]
    outlet_cells = [j * nx + nx - 1 for j in range(1, ny - 1) if not mask[j * nx + nx - 1]]
    scale_reference = velocity * height
    residual = 1.0
    iteration = 0
    history_every = 5
    while iteration < max_iterations:
        if cancel is not None and cancel.is_set():
            raise SolverCancelled("solve cancelled")
        iteration += 1
        largest = 0.0
        for cell in update_cells:
            gauss = (ax * (psi[cell + 1] + psi[cell - 1]) + ay * (psi[cell + nx] + psi[cell - nx])) / diagonal
            change = omega * (gauss - psi[cell])
            psi[cell] += change
            if change < 0:
                change = -change
            if change > largest:
                largest = change
        for cell in outlet_cells:
            psi[cell] = psi[cell - 1]
        residual = largest / scale_reference
        if progress and (iteration % history_every == 0 or residual < tolerance):
            progress(iteration, {"psi": residual})
        if residual < tolerance:
            break

    u = [0.0] * (nx * ny)
    v = [0.0] * (nx * ny)
    for cell in range(nx * ny):
        if mask[cell]:
            continue
        i, j = cell % nx, cell // nx
        if 0 < j < ny - 1:
            u[cell] = (psi[cell + nx] - psi[cell - nx]) / (2 * dy)
        elif j == 0:
            u[cell] = (psi[cell + nx] - psi[cell]) / dy
        else:
            u[cell] = (psi[cell] - psi[cell - nx]) / dy
        if 0 < i < nx - 1:
            v[cell] = -(psi[cell + 1] - psi[cell - 1]) / (2 * dx)
        elif i == 0:
            v[cell] = -(psi[cell + 1] - psi[cell]) / dx
        else:
            v[cell] = -(psi[cell] - psi[cell - 1]) / dx
    speed=[math.hypot(u[c], v[c]) for c in range(nx * ny)]
    pressure = [0.0 if mask[c] else 0.5 * density * (velocity ** 2 - speed[c] ** 2) for c in range(nx * ny)]
    fluid_speeds = [speed[c] for c in range(nx * ny) if not mask[c]]
    fluid_pressures=[pressure[c] for c in range(nx * ny) if not mask[c]]

    def sample(field: list[float], x: float, y: float) -> float | None:
        fx = (x - mesh.origin[0]) / dx - 0.5
        fy = (y - y0) / dy - 0.5
        i, j = math.floor(fx), math.floor(fy)
        if i < 0 or j < 0 or i >= nx - 1 or j >= ny - 1:
            return None
        tx, ty = fx - i, fy - j
        cells = (j * nx + i, j * nx + i + 1, (j + 1) * nx + i, (j + 1) * nx + i + 1)
        if any(mask[c] for c in cells):
            return None
        return (
            field[cells[0]] * (1 - tx) * (1 - ty) + field[cells[1]] * tx * (1 - ty)
            + field[cells[2]] * (1 - tx) * ty + field[cells[3]] * tx * ty
        )

    streamlines: list[list[float]] = []
    step = 0.4 * min(dx, dy)
    max_steps = int(4 * (nx * dx + ny * dy) / step)
    for seed in range(streamline_count):
        x = mesh.origin[0] + 0.51 * dx
        y = y0 + dy + (height - 2 * dy) * (seed + 0.5) / streamline_count
        line: list[float] = []
        for _ in range(max_steps):
            ux, vy = sample(u, x, y), sample(v, x, y)
            if ux is None or vy is None:
                break
            magnitude=math.hypot(ux, vy)
            line.extend((round(x, 7), round(y, 7), round(slice_z, 7), round(magnitude, 5)))
            if magnitude < 1e-6 * velocity:
                break
            mx, my = x + 0.5 * step * ux / magnitude, y + 0.5 * step * vy / magnitude
            um, vm = sample(u, mx, my), sample(v, mx, my)
            if um is None or vm is None:
                break
            mid = math.hypot(um, vm) or 1.0
            x, y = x + step * um / mid, y + step * vm / mid
        if len(line) >= 8:
            streamlines.append(line)
    return {
        "analysis": "cfd",
        "method": "Potential flow (inviscid, incompressible) · stream-function SOR",
        "converged": residual < tolerance,
        "iterations": iteration,
        "final_residual": residual,
        "grid": {"origin": [mesh.origin[0] + 0.5 * dx, y0 + 0.5 * dy, slice_z], "spacing": [dx, dy], "shape": [nx, ny]},
        "fields": {"velocity_x": u, "velocity_y": v, "speed": speed, "pressure": pressure, "solid": [int(m) for m in mask]},
        "streamlines": streamlines,
        "summary": {
            "inlet_velocity": velocity,
            "max_speed": max(fluid_speeds, default=0.0),
            "min_pressure": min(fluid_pressures, default=0.0),
            "max_pressure": max(fluid_pressures, default=0.0),
            "min_pressure_coefficient": min(
                (1 - (s / velocity) ** 2 for s in fluid_speeds), default=0.0
            ),
        },
    }


# plane-stress FEA


def _solve_dense(matrix: list[list[float]], rhs: list[list[float]]) -> list[list[float]]:
    size = len(matrix)
    augmented = [list(matrix[r]) + list(rhs[r]) for r in range(size)]
    columns = len(augmented[0])
    for pivot in range(size):
        best = max(range(pivot, size), key=lambda r: abs(augmented[r][pivot]))
        augmented[pivot], augmented[best] = augmented[best], augmented[pivot]
        divisor = augmented[pivot][pivot]
        if abs(divisor) < 1e-300:
            raise ValueError("singular element matrix")
        augmented[pivot] = [value / divisor for value in augmented[pivot]]
        for row in range(size):
            if row != pivot and augmented[row][pivot] != 0:
                factor = augmented[row][pivot]
                augmented[row] = [augmented[row][c] - factor * augmented[pivot][c] for c in range(columns)]
    return [row[size:] for row in augmented]


def qm6_stiffness(a: float, b: float, young: float, poisson: float, thickness: float) -> tuple[list[list[float]], list[list[float]]]:
    """Condensed 8×8 QM6 stiffness and 3×8 centroid strain-displacement matrix"""
    factor = young / (1 - poisson ** 2)
    elasticity = [
        [factor, factor * poisson, 0.0],
        [factor * poisson, factor, 0.0],
        [0.0, 0.0, factor * (1 - poisson) / 2],
    ]
    nodes = ((-1, -1), (1, -1), (1, 1), (-1, 1))
    gauss = 1 / math.sqrt(3)

    def strain_matrix(xi: float, eta: float) -> list[list[float]]:
        rows = [[0.0] * 12 for _ in range(3)]
        for index, (xi_i, eta_i) in enumerate(nodes):
            dndx=xi_i * (1 + eta * eta_i) / 4 * 2 / a
            dndz = eta_i * (1 + xi * xi_i) / 4 * 2 / b
            rows[0][2 * index] = dndx
            rows[1][2 * index + 1] = dndz
            rows[2][2 * index] = dndz
            rows[2][2 * index + 1] = dndx
        rows[0][8] = -2 * xi * 2 / a
        rows[2][9] = -2 * xi * 2 / a
        rows[1][11] = -2 * eta * 2 / b
        rows[2][10] = -2 * eta * 2 / b
        return rows

    stiffness = [[0.0] * 12 for _ in range(12)]
    jacobian=a * b / 4
    for xi in (-gauss, gauss):
        for eta in (-gauss, gauss):
            strain = strain_matrix(xi, eta)
            stress = [[sum(elasticity[r][k] * strain[k][c] for k in range(3)) for c in range(12)] for r in range(3)]
            for r in range(12):
                for c in range(12):
                    stiffness[r][c] += sum(strain[k][r] * stress[k][c] for k in range(3)) * jacobian * thickness
    kcc = [row[:8] for row in stiffness[:8]]
    kci = [row[8:] for row in stiffness[:8]]
    kii = [row[8:] for row in stiffness[8:]]
    kic=[row[:8] for row in stiffness[8:]]
    kii_inverse_kic = _solve_dense(kii, kic)
    condensed = [
        [kcc[r][c] - sum(kci[r][k] * kii_inverse_kic[k][c] for k in range(4)) for c in range(8)]
        for r in range(8)
    ]
    centre = strain_matrix(0.0, 0.0)
    centre_stress = [[sum(elasticity[r][k] * centre[k][c] for k in range(3)) for c in range(8)] for r in range(3)]
    return condensed, centre_stress


def solve_plane_stress(
    mesh: CartesianMesh,
    young: float,
    poisson: float,
    load: float,
    *,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    tolerance: float = 1e-9,
    max_iterations: int = 20_000,
) -> dict[str, Any]:
    if young <= 0:
        raise ValueError("youngs_modulus must be positive")
    if not 0 <= poisson < 0.5:
        raise ValueError("poissons_ratio must be in [0, 0.5)")
    nx, ny, nz = mesh.shape
    dx, dz = mesh.spacing[0], mesh.spacing[2]
    body_mid_y = 0.5 * (mesh.body_bbox[0][1] + mesh.body_bbox[1][1])
    j=min(ny - 1, max(0, int((body_mid_y - mesh.origin[1]) / mesh.spacing[1])))
    thickness = mesh.body_bbox[1][1] - mesh.body_bbox[0][1]
    if thickness <= 0:
        raise ValueError("body has no thickness in Y")
    elements = [(i, k) for k in range(nz) for i in range(nx) if mesh.solid[mesh.index(i, j, k)]]
    if len(elements) < 2:
        raise ValueError("the structural slice contains fewer than two solid cells; refine the mesh")
    node_ids: dict[tuple[int, int], int] = {}
    connectivity: list[tuple[int, int, int, int]] = []
    for i, k in elements:
        corners = ((i, k), (i + 1, k), (i + 1, k + 1), (i, k + 1))
        connectivity.append(tuple(node_ids.setdefault(c, len(node_ids)) for c in corners))  # type: ignore[misc]
    node_count = len(node_ids)
    positions=[(0, 0)] * node_count
    for (i, k), node in node_ids.items():
        positions[node] = (i, k)
    min_i = min(i for i, _ in positions)
    max_i = max(i for i, _ in positions)
    fixed = [False] * (2 * node_count)
    for node, (i, _) in enumerate(positions):
        if i == min_i:
            fixed[2 * node] = fixed[2 * node + 1] = True
    loaded = [node for node, (i, _) in enumerate(positions) if i == max_i]
    force = [0.0] * (2 * node_count)
    for node in loaded:
        force[2 * node + 1] = -load / len(loaded)
    stiffness, centre_stress = qm6_stiffness(dx, dz, young, poisson, thickness)
    element_dofs = [[2 * n + d for n in element for d in (0, 1)] for element in connectivity]

    def multiply(vector: list[float]) -> list[float]:
        result = [0.0] * (2 * node_count)
        for dofs in element_dofs:
            local = [vector[d] for d in dofs]
            for r in range(8):
                row=stiffness[r]
                result[dofs[r]] += (
                    row[0] * local[0] + row[1] * local[1] + row[2] * local[2] + row[3] * local[3]
                    + row[4] * local[4] + row[5] * local[5] + row[6] * local[6] + row[7] * local[7]
                )
        for dof, is_fixed in enumerate(fixed):
            if is_fixed:
                result[dof] = vector[dof]
        return result

    diagonal = [0.0] * (2 * node_count)
    for dofs in element_dofs:
        for r in range(8):
            diagonal[dofs[r]] += stiffness[r][r]
    preconditioner = [1.0 if fixed[d] or diagonal[d] == 0 else 1 / diagonal[d] for d in range(2 * node_count)]
    displacement = [0.0] * (2 * node_count)
    residual = [0.0 if fixed[d] else force[d] for d in range(2 * node_count)]
    z = [residual[d] * preconditioner[d] for d in range(2 * node_count)]
    direction = list(z)
    rz = sum(r * q for r, q in zip(residual, z))
    reference = math.sqrt(sum(r * r for r in residual)) or 1.0
    relative=1.0
    iteration = 0
    while iteration < max_iterations:
        if cancel is not None and cancel.is_set():
            raise SolverCancelled("solve cancelled")
        iteration += 1
        product = multiply(direction)
        denominator = sum(p * q for p, q in zip(direction, product))
        if denominator <= 0:
            break
        alpha = rz / denominator
        for d in range(2 * node_count):
            displacement[d] += alpha * direction[d]
            residual[d] -= alpha * product[d]
        relative = math.sqrt(sum(r * r for r in residual)) / reference
        if progress and (iteration % 2 == 0 or relative < tolerance):
            progress(iteration, {"displacement": relative})
        if relative < tolerance:
            break
        z = [residual[d] * preconditioner[d] for d in range(2 * node_count)]
        rz_next=sum(r * q for r, q in zip(residual, z))
        beta = rz_next / rz
        rz=rz_next
        direction=[z[d] + beta * direction[d] for d in range(2 * node_count)]

    element_stress: list[float] = []
    nodal_sum = [0.0] * node_count
    nodal_count = [0] * node_count
    for element, dofs in zip(connectivity, element_dofs):
        local=[displacement[d] for d in dofs]
        sx, sz, txz = (sum(centre_stress[r][c] * local[c] for c in range(8)) for r in range(3))
        von_mises = math.sqrt(max(0.0, sx * sx - sx * sz + sz * sz + 3 * txz * txz))
        element_stress.append(von_mises)
        for node in element:
            nodal_sum[node] += von_mises
            nodal_count[node] += 1
    nodal_stress = [nodal_sum[n] / max(1, nodal_count[n]) for n in range(node_count)]
    tip_nodes = loaded
    tip_deflection = sum(displacement[2 * n + 1] for n in tip_nodes) / len(tip_nodes)
    span = (max_i - min_i) * dx
    depth = mesh.body_bbox[1][2] - mesh.body_bbox[0][2]
    inertia = thickness * depth ** 3 / 12
    beam_deflection=-load * span ** 3 / (3 * young * inertia) if inertia > 0 else 0.0
    beam_stress = load * span * (depth / 2) / inertia if inertia > 0 else 0.0
    slice_y = mesh.origin[1] + (j + 0.5) * mesh.spacing[1]
    return {
        "analysis": "fea",
        "method": "Plane stress · QM6 incompatible-mode quads · PCG",
        "converged": relative < tolerance,
        "iterations": iteration,
        "final_residual": relative,
        "grid": {"origin": [mesh.origin[0], slice_y, mesh.origin[2]], "spacing": [dx, dz], "shape": [nx + 1, nz + 1]},
        "nodes": [list(p) for p in positions],
        "elements": [list(e) for e in connectivity],
        "displacement": [[displacement[2 * n], displacement[2 * n + 1]] for n in range(node_count)],
        "von_mises": nodal_stress,
        "element_von_mises": element_stress,
        "thickness": thickness,
        "summary": {
            "max_von_mises": max(element_stress),
            "tip_deflection": tip_deflection,
            "beam_theory_tip_deflection": beam_deflection,
            "beam_theory_root_stress": beam_stress,
            "span": span,
            "applied_load": load,
        },
    }
