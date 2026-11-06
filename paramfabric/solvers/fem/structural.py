"""Native 3D linear-elastic finite-element solver (numpy + scipy.sparse).

Linear systems up to 300k free DOF use a direct sparse factorization with
symmetric minimum-degree ordering (~30 s at 80k DOF); larger
ones use smoothed-aggregation AMG-preconditioned CG (pyamg) seeded with the
six rigid-body modes, falling back to Jacobi-CG.

Supports Tet4/Tet10/Hex8/Hex20 solid elements (mixed meshes allowed), fixed
supports, resultant forces distributed as uniform traction, normal pressure,
gravity body load, and uniform thermal expansion. Loads on faces are
integrated consistently with the face element shape functions; pressure acts
along the outward normal determined from the owning volume element.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

try:  # optional: algebraic multigrid for large models
    import pyamg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on installed extras
    pyamg = None

from paramfabric.meshing.mesh import CORNERS, VolumeMesh
from paramfabric.solvers.fem.elements import REFERENCE_NODES, element

Progress = Callable[[int, dict[str, float]], None]
SOLID_TYPES = ("tet4", "tet10", "hex8", "hex20")
_CHUNK = 3000
_DIRECT_LIMIT = 300_000


class StructuralError(ValueError):
    """Raised for an ill-posed structural case"""


class Cancelled(RuntimeError):
    """Raised when a solve is cancelled"""


@dataclass
class StructuralCase:
    mesh: VolumeMesh
    young: float
    poisson: float
    fixed: list[str] = field(default_factory=list)
    loads: list[dict[str, Any]] = field(default_factory=list)
    density: float = 0.0
    gravity: tuple[float, float, float] = (0.0, 0.0, 0.0)
    expansion: float = 0.0
    delta_temperature: float = 0.0


def elasticity_matrix(young: float, poisson: float) -> np.ndarray:
    """Isotropic 3D constitutive matrix in Voigt order xx, yy, zz, xy, yz, zx"""
    lam = young * poisson / ((1 + poisson) * (1 - 2 * poisson))
    mu = young / (2 * (1 + poisson))
    matrix = np.zeros((6, 6))
    matrix[:3, :3] = lam
    matrix[np.arange(3), np.arange(3)] += 2 * mu
    matrix[3:, 3:] = np.eye(3) * mu
    return matrix


def _derivatives(kind: str, coordinates: np.ndarray, points: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    ref = element(kind)
    gradient = ref.gradient(ref.quad_points if points is None else points)  # (q, 3, n)
    jacobian = np.einsum("qan,mnb->mqab", gradient, coordinates)
    determinant = np.linalg.det(jacobian)
    if np.any(determinant <= 0):
        bad=int((determinant <= 0).any(axis=1).sum())
        raise StructuralError(f"{bad} {kind} element(s) are inverted or degenerate")
    inverse = np.linalg.inv(jacobian)
    return np.einsum("mqba,qan->mqbn", inverse, gradient), determinant


def _strain_matrix(dndx: np.ndarray) -> np.ndarray:
    m, q, _, n = dndx.shape
    b = np.zeros((m, q, 6, 3 * n))
    dx, dy, dz = dndx[:, :, 0, :], dndx[:, :, 1, :], dndx[:, :, 2, :]
    b[:, :, 0, 0::3] = dx
    b[:, :, 1, 1::3] = dy
    b[:, :, 2, 2::3] = dz
    b[:, :, 3, 0::3] = dy
    b[:, :, 3, 1::3] = dx
    b[:, :, 4, 1::3] = dz
    b[:, :, 4, 2::3] = dy
    b[:, :, 5, 0::3] = dz
    b[:, :, 5, 2::3] = dx
    return b


def _face_keys(mesh: VolumeMesh) -> dict[tuple[int, ...], np.ndarray]:
    faces = {
        "tet": [(0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)],
        "hex": [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
    }
    owners: dict[tuple[int, ...], np.ndarray] = {}
    for kind, conn in mesh.cells.items():
        if kind not in SOLID_TYPES:
            continue
        corners = conn[:, : CORNERS[kind]]
        centroids = mesh.nodes[corners].mean(axis=1)
        for local in faces[kind[:3]]:
            for row, centroid in zip(np.sort(corners[:, local], axis=1), centroids):
                owners[tuple(row)] = centroid
    return owners


def _surface_loads(case: StructuralCase, force: np.ndarray) -> dict[str, float]:
    mesh = case.mesh
    owners: dict[tuple[int, ...], np.ndarray] | None = None
    areas: dict[str, float] = {}
    for load in case.loads:
        group = mesh.boundary(load["group"])
        integrals = []
        total_area=0.0
        for kind, conn in group.elements.items():
            ref = element(kind)
            shape = ref.shape(ref.quad_points)  # (q, n)
            gradient = ref.gradient(ref.quad_points)  # (q, 2, n)
            coordinates = mesh.nodes[conn]  # (M, n, 3)
            tangents=np.einsum("qan,mnb->mqab", gradient, coordinates)  # (M, q, 2, 3)
            normal = np.cross(tangents[:, :, 0, :], tangents[:, :, 1, :])  # area-scaled
            weights = ref.quad_weights
            if load["type"] == "pressure":
                if owners is None:
                    owners = _face_keys(mesh)
                corners = np.sort(conn[:, : CORNERS[kind]], axis=1)
                face_centroids = mesh.nodes[conn[:, : CORNERS[kind]]].mean(axis=1)
                sign = np.ones(len(conn))
                for index, key in enumerate(map(tuple, corners)):
                    owner = owners.get(key)
                    if owner is not None:
                        mean_normal = normal[index].sum(axis=0)
                        sign[index] = 1.0 if np.dot(mean_normal, face_centroids[index] - owner) >= 0 else -1.0
                normal = normal * sign[:, None, None]
            area = np.linalg.norm(normal, axis=2)  # (M, q)
            total_area += float(np.einsum("mq,q->", area, weights))
            integrals.append((conn, shape, weights, normal, area))
        areas[load["group"]] = total_area
        if total_area <= 0:
            raise StructuralError(f"load face group {load['group']!r} has no area")
        for conn, shape, weights, normal, area in integrals:
            if load["type"] == "force":
                traction = np.asarray(load["vector"], float) / total_area
                nodal=np.einsum("qn,mq,q,b->mnb", shape, area, weights, traction)
            elif load["type"] == "pressure":
                nodal=-float(load["value"]) * np.einsum("qn,mqb,q->mnb", shape, normal, weights)
            else:
                raise StructuralError(f"unknown load type {load['type']!r}")
            np.add.at(force, (conn[:, :, None] * 3 + np.arange(3)[None, None, :]).ravel(), nodal.ravel())
    return areas


def solve_structural(
    case: StructuralCase,
    *,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    mesh = case.mesh
    say = log or (lambda message: None)
    if case.young <= 0 or not -1 < case.poisson < 0.5:
        raise StructuralError("Young's modulus must be positive and Poisson's ratio in (-1, 0.5)")
    if not case.fixed:
        raise StructuralError("the structure is unconstrained; assign at least one fixed support")
    solid_kinds=[kind for kind in SOLID_TYPES if kind in mesh.cells and len(mesh.cells[kind])]
    if not solid_kinds:
        raise StructuralError("the mesh contains no solid elements")
    started = time.perf_counter()
    dof_count = 3 * len(mesh.nodes)
    elasticity = elasticity_matrix(case.young, case.poisson)
    thermal_strain = case.expansion * case.delta_temperature * np.array([1, 1, 1, 0, 0, 0], float)
    rows, cols, values = [], [], []
    force = np.zeros(dof_count)
    gravity = case.density * np.asarray(case.gravity, float)
    volume = 0.0
    for kind in solid_kinds:
        conn = mesh.cells[kind]
        ref = element(kind)
        weights = ref.quad_weights
        shape = ref.shape(ref.quad_points)
        for start in range(0, len(conn), _CHUNK):
            if cancel is not None and cancel.is_set():
                raise Cancelled("solve cancelled")
            block = conn[start : start + _CHUNK]
            dndx, det = _derivatives(kind, mesh.nodes[block])
            b = _strain_matrix(dndx)
            scale = det * weights[None, :]
            stiffness = np.einsum("mqia,ij,mqjb,mq->mab", b, elasticity, b, scale, optimize=True)
            dofs = (block[:, :, None] * 3 + np.arange(3)[None, None, :]).reshape(len(block), -1)
            rows.append(np.repeat(dofs, dofs.shape[1], axis=1).ravel())
            cols.append(np.tile(dofs, (1, dofs.shape[1])).ravel())
            values.append(stiffness.ravel())
            volume += float(scale.sum())
            if np.any(thermal_strain):
                nodal = np.einsum("mqia,ij,j,mq->ma", b, elasticity, thermal_strain, scale)
                np.add.at(force, dofs.ravel(), nodal.ravel())
            if np.any(gravity):
                nodal = np.einsum("qn,mq,b->mnb", shape, scale, gravity)
                np.add.at(force, dofs.ravel(), nodal.ravel())
    say(f"Assembled {sum(len(mesh.cells[k]) for k in solid_kinds):,} elements, {dof_count:,} DOF")
    stiffness_matrix = sp.csr_matrix(
        (np.concatenate(values), (np.concatenate(rows), np.concatenate(cols))), shape=(dof_count, dof_count)
    )
    areas = _surface_loads(case, force)
    fixed_nodes = np.unique(np.concatenate([mesh.boundary(name).nodes for name in case.fixed]))
    fixed = np.zeros(dof_count, bool)
    fixed[(fixed_nodes[:, None] * 3 + np.arange(3)).ravel()] = True
    used = np.zeros(dof_count, bool)
    for kind in solid_kinds:
        used[(mesh.cells[kind][:, :, None] * 3 + np.arange(3)).ravel()] = True
    free = np.flatnonzero(used & ~fixed)
    reduced = stiffness_matrix[free][:, free].tocsc()
    rhs = force[free]
    displacement = np.zeros(dof_count)
    if progress:
        progress(0, {"residual": 1.0})
    iteration = [0]
    reference = np.linalg.norm(rhs) or 1.0

    def report(xk: np.ndarray) -> None:
        iteration[0] += 1
        if progress and iteration[0] % 5 == 0:
            progress(iteration[0], {"residual": float(np.linalg.norm(rhs - reduced @ xk) / reference)})
        if cancel is not None and cancel.is_set():
            raise Cancelled("solve cancelled")

    if len(free) <= _DIRECT_LIMIT:
        say(f"Direct sparse solve (SuperLU, symmetric ordering), {len(free):,} free DOF")
        solution = spla.splu(reduced, permc_spec="MMD_AT_PLUS_A").solve(rhs)
    elif pyamg is not None:
        say(f"Smoothed-aggregation AMG–CG with rigid-body modes, {len(free):,} free DOF")
        coordinates = mesh.nodes[free // 3]
        component = free % 3
        modes = np.zeros((len(free), 6))
        modes[np.arange(len(free)), component] = 1.0
        x, y, z = coordinates.T
        modes[:, 3] = np.where(component == 0, -y, np.where(component == 1, x, 0.0))
        modes[:, 4] = np.where(component == 1, -z, np.where(component == 2, y, 0.0))
        modes[:, 5] = np.where(component == 2, -x, np.where(component == 0, z, 0.0))
        hierarchy = pyamg.smoothed_aggregation_solver(reduced.tocsr(), B=modes, strength=("symmetric", {"theta": 0.0}))
        solution = hierarchy.solve(rhs, tol=1e-10, accel="cg", maxiter=2000, callback=report)
    else:
        say(f"Jacobi–CG (install pyamg for large models), {len(free):,} free DOF")
        diagonal = reduced.diagonal()
        preconditioner = spla.LinearOperator(reduced.shape, matvec=lambda vector: vector / diagonal)
        solution, info = spla.cg(reduced, rhs, rtol=1e-9, maxiter=50_000, M=preconditioner, callback=report)
        if info:
            raise StructuralError(f"conjugate gradients did not converge (info={info})")
    displacement[free] = solution
    residual = float(np.linalg.norm(reduced @ solution - rhs) / (np.linalg.norm(rhs) or 1.0))
    if progress:
        progress(1, {"residual": max(residual, 1e-16)})
    reactions = stiffness_matrix @ displacement - force
    reaction_total=reactions.reshape(-1, 3)[fixed_nodes].sum(axis=0)

    # Nodal stresses: evaluate at each element's own nodes and average
    stress_sum = np.zeros((len(mesh.nodes), 6))
    stress_count = np.zeros(len(mesh.nodes))
    for kind in solid_kinds:
        conn = mesh.cells[kind]
        for start in range(0, len(conn), _CHUNK):
            block = conn[start : start + _CHUNK]
            dndx, _ = _derivatives(kind, mesh.nodes[block], points=REFERENCE_NODES[kind])
            b = _strain_matrix(dndx)
            local = displacement[(block[:, :, None] * 3 + np.arange(3)).reshape(len(block), -1)]
            strain = np.einsum("mqia,ma->mqi", b, local) - thermal_strain[None, None, :]
            stress=np.einsum("ij,mqj->mqi", elasticity, strain)
            np.add.at(stress_sum, block.ravel(), stress.reshape(-1, 6))
            np.add.at(stress_count, block.ravel(), 1)
    nodal_stress = stress_sum / np.maximum(stress_count, 1)[:, None]
    sx, sy, sz, txy, tyz, tzx = nodal_stress.T
    von_mises = np.sqrt(np.maximum(0.0, 0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2) + 3 * (txy ** 2 + tyz ** 2 + tzx ** 2)))
    vectors = displacement.reshape(-1, 3)
    magnitude = np.linalg.norm(vectors, axis=1)
    say(f"Solved in {time.perf_counter() - started:.2f} s, relative residual {residual:.2e}")
    return {
        "displacement": vectors,
        "von_mises": von_mises,
        "stress": nodal_stress,
        "residual": residual,
        "volume": volume,
        "load_areas": areas,
        "reaction_force": reaction_total,
        "applied_force": force.reshape(-1, 3)[~np.isin(np.arange(len(mesh.nodes)), fixed_nodes)].sum(axis=0),
        "summary": {
            "max_displacement": float(magnitude.max()),
            "max_von_mises": float(von_mises.max()),
            "dof": int(dof_count),
            "free_dof": int(len(free)),
            "elements": {kind: int(len(mesh.cells[kind])) for kind in solid_kinds},
            "relative_residual": residual,
            "reaction_force": [float(v) for v in reaction_total],
            "solve_seconds": time.perf_counter() - started,
        },
    }


# modal analysis

_MASS_ORDER = {"tet4": 2, "tet10": 4, "hex8": 2, "hex20": 3}  # exact for N_i N_j


def assemble_stiffness_mass(mesh: VolumeMesh, young: float, poisson: float, density: float) -> tuple[sp.csr_matrix, sp.csr_matrix]:
    """Global stiffness and consistent mass matrices of the solid elements"""
    dof_count = 3 * len(mesh.nodes)
    elasticity = elasticity_matrix(young, poisson)
    k_parts: tuple[list, list, list] = ([], [], [])
    m_parts: tuple[list, list, list] = ([], [], [])
    for kind in SOLID_TYPES:
        if kind not in mesh.cells or not len(mesh.cells[kind]):
            continue
        conn = mesh.cells[kind]
        mass_rule = element(kind, _MASS_ORDER[kind])
        shape = mass_rule.shape(mass_rule.quad_points)  # (q, n)
        for start in range(0, len(conn), _CHUNK):
            block=conn[start:start + _CHUNK]
            dndx, det = _derivatives(kind, mesh.nodes[block])
            b = _strain_matrix(dndx)
            stiffness = np.einsum("mqia,ij,mqjb,mq->mab", b, elasticity, b, det * element(kind).quad_weights[None, :], optimize=True)
            _, mass_det = _derivatives(kind, mesh.nodes[block], points=mass_rule.quad_points)
            scalar = density * np.einsum("qa,qb,mq,q->mab", shape, shape, mass_det, mass_rule.quad_weights, optimize=True)
            n=shape.shape[1]
            mass = np.zeros((len(block), 3 * n, 3 * n))
            for axis in range(3):
                mass[:, axis::3, axis::3] = scalar
            dofs = (block[:, :, None] * 3 + np.arange(3)[None, None, :]).reshape(len(block), -1)
            for parts, values in ((k_parts, stiffness), (m_parts, mass)):
                parts[0].append(np.repeat(dofs, dofs.shape[1], axis=1).ravel())
                parts[1].append(np.tile(dofs, (1, dofs.shape[1])).ravel())
                parts[2].append(values.ravel())
    if not k_parts[0]:
        raise StructuralError("the mesh contains no solid elements")

    def build(parts: tuple[list, list, list]) -> sp.csr_matrix:
        return sp.csr_matrix((np.concatenate(parts[2]), (np.concatenate(parts[0]), np.concatenate(parts[1]))), shape=(dof_count, dof_count))

    return build(k_parts), build(m_parts)


def eigen_modes(stiffness: sp.spmatrix, mass: sp.spmatrix, free: np.ndarray, count: int, dof_per_node: int,
                translation: np.ndarray | None = None) -> dict[str, Any]:
    """Lowest `count` modes of K φ = ω² M φ on the free DOFs (shift-invert Lanczos).

    Returns frequencies (Hz), mass-normalized mode shapes on all DOFs, and
    effective modal mass fractions per global direction (participation).
    """
    k=stiffness[free][:, free].tocsc()
    m = mass[free][:, free].tocsc()
    count = max(1, min(count, len(free) - 2))
    # A small negative shift keeps the factorization regular even for free-free (rigid-body) models
    shift = -1e-6 * float(np.mean(k.diagonal())) / max(float(np.mean(m.diagonal())), 1e-300)
    values, vectors = spla.eigsh(k, k=count, M=m, sigma=shift, which="LM")
    order = np.argsort(values)
    values, vectors = np.clip(values[order], 0.0, None), vectors[:, order]
    shapes = np.zeros((stiffness.shape[0], count))
    shapes[free] = vectors / np.sqrt(np.einsum("ij,ij->j", vectors, m @ vectors))
    total = stiffness.shape[0]
    participation = []
    for axis in range(3):
        influence = np.zeros(total)
        influence[axis::dof_per_node] = 1.0  # unit rigid translation along the axis
        r = influence[free]
        gamma = shapes[free].T @ (m @ r)
        total_mass = float(influence @ (mass @ influence))  # whole model, supports included
        participation.append(gamma ** 2 / total_mass if total_mass > 0 else np.zeros_like(gamma))
    return {
        "frequencies": np.sqrt(values) / (2 * np.pi),
        "shapes": shapes,
        "effective_mass_fraction": np.array(participation).T,  # (modes, 3)
    }


def solve_modal(mesh: VolumeMesh, young: float, poisson: float, density: float, fixed: list[str], modes: int = 6) -> dict[str, Any]:
    """Natural frequencies and mode shapes of a solid mesh with fixed face groups"""
    if density <= 0:
        raise StructuralError("modal analysis needs a positive density")
    stiffness, mass = assemble_stiffness_mass(mesh, young, poisson, density)
    dof_count = stiffness.shape[0]
    used = np.zeros(dof_count, bool)
    for kind in SOLID_TYPES:
        if kind in mesh.cells:
            used[(mesh.cells[kind][:, :, None] * 3 + np.arange(3)).ravel()] = True
    held = np.zeros(dof_count, bool)
    if fixed:
        nodes = np.unique(np.concatenate([mesh.boundary(name).nodes for name in fixed]))
        held[(nodes[:, None] * 3 + np.arange(3)).ravel()] = True
    result = eigen_modes(stiffness, mass, np.flatnonzero(used & ~held), modes, 3)
    shapes = result["shapes"].T.reshape(len(result["frequencies"]), -1, 3)
    peaks = np.linalg.norm(shapes, axis=2).max(axis=1)
    return {
        "frequencies": result["frequencies"],
        "mode_shapes": shapes / np.maximum(peaks, 1e-300)[:, None, None],  # unit max displacement
        "effective_mass_fraction": result["effective_mass_fraction"],
        "total_mass": float(mass.sum() / 3),
    }


# frictionless node-to-surface penalty contact

@dataclass
class ContactPair:
    """Slave nodes of one face group may not penetrate the master face group"""

    slave: str
    master: str
    penalty_factor: float = 50.0  # penalty stiffness = factor * E * (slave node area) / (master element size)


@dataclass
class ContactCase:
    mesh: VolumeMesh
    young: float
    poisson: float
    fixed: list[str] = field(default_factory=list)  # all three displacements held
    constraints: list[dict[str, Any]] = field(default_factory=list)  # {"group": name, "dofs": [0, 1]}: rollers, symmetry
    loads: list[dict[str, Any]] = field(default_factory=list)  # same format as StructuralCase.loads
    pairs: list[ContactPair] = field(default_factory=list)
    max_iterations: int = 40


def _master_triangles(mesh: VolumeMesh, group: str) -> np.ndarray:
    triangles = []
    for kind, conn in mesh.boundary(group).elements.items():
        corners = conn[:, : CORNERS[kind]]
        if corners.shape[1] == 3:
            triangles.append(corners)
        else:  # quadrilateral faces: two triangles
            triangles += [corners[:, [0, 1, 2]], corners[:, [0, 2, 3]]]
    return np.concatenate(triangles)


def _closest_on_triangle(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = ab @ ap, ac @ ap
    if d1 <= 0 and d2 <= 0:
        return a, np.array([1.0, 0.0, 0.0])
    bp = p - b
    d3, d4 = ab @ bp, ac @ bp
    if d3 >= 0 and d4 <= d3:
        return b, np.array([0.0, 1.0, 0.0])
    vc = d1 * d4 - d3 * d2
    if vc <= 0 <= d1 and d3 <= 0:
        v = d1 / (d1 - d3)
        return a + v * ab, np.array([1 - v, v, 0.0])
    cp = p - c
    d5, d6 = ab @ cp, ac @ cp
    if d6 >= 0 and d5 <= d6:
        return c, np.array([0.0, 0.0, 1.0])
    vb = d5 * d2 - d1 * d6
    if vb <= 0 <= d2 and d6 <= 0:
        w = d2 / (d2 - d6)
        return a + w * ac, np.array([1 - w, 0.0, w])
    va = d3 * d6 - d5 * d4
    if va <= 0 and d4 - d3 >= 0 and d5 - d6 >= 0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return b + w * (c - b), np.array([0.0, 1 - w, w])
    denominator = 1.0 / (va + vb + vc)
    v, w = vb * denominator, vc * denominator
    return a + v * ab + w * ac, np.array([1 - v - w, v, w])


def _contact_constraints(mesh: VolumeMesh, pair: ContactPair, young: float) -> dict[str, np.ndarray]:
    from scipy.spatial import cKDTree

    nodes=mesh.nodes
    triangles = _master_triangles(mesh, pair.master)
    slave_group = mesh.boundary(pair.slave)
    area = np.zeros(len(nodes))  # tributary area of each slave corner node
    for kind, conn in slave_group.elements.items():
        corners = conn[:, : CORNERS[kind]]
        p = nodes[corners]
        face_area = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
        if corners.shape[1] == 4:
            face_area += 0.5 * np.linalg.norm(np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 0]), axis=1)
        np.add.at(area, corners.ravel(), np.repeat(face_area / corners.shape[1], corners.shape[1]))
    slave_nodes = np.flatnonzero(area > 0)
    corners = nodes[triangles]
    centroids=corners.mean(axis=1)
    normals=np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    normals /= np.linalg.norm(normals, axis=1, keepdims=True)
    toward_slave = nodes[slave_nodes].mean(axis=0) - centroids
    normals *= np.where(np.einsum("ij,ij->i", normals, toward_slave) < 0, -1.0, 1.0)[:, None]
    size = float(np.mean(np.linalg.norm(corners[:, 1] - corners[:, 0], axis=1)))
    tree=cKDTree(centroids)
    rows=[]
    for node in slave_nodes:
        candidates = np.atleast_1d(tree.query(nodes[node], k=min(12, len(triangles)))[1])
        best = None
        for index in candidates:
            point, weights = _closest_on_triangle(nodes[node], *corners[index])
            distance=float(np.linalg.norm(nodes[node] - point))
            if best is None or distance < best[0]:
                best = (distance, index, weights, point)
        _, index, weights, point = best
        rows.append((node, triangles[index], weights, normals[index], float((nodes[node] - point) @ normals[index])))
    return {
        "slave": np.array([r[0] for r in rows]), "master": np.array([r[1] for r in rows]),
        "weights": np.array([r[2] for r in rows]), "normal": np.array([r[3] for r in rows]),
        "gap": np.array([r[4] for r in rows]), "area": area[slave_nodes],
        "penalty": pair.penalty_factor * young * area[slave_nodes] / size,
    }


def solve_contact(case: ContactCase, *, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Linear elasticity with frictionless penalty contact, solved by an active-set iteration.

    For each slave node the gap to its master facet is `g = g0 + n·(u_s − Σ wᵢ u_mᵢ)`;
    penetrating nodes (g < 0) carry a penalty spring of energy `k g²/2`. The
    active set is updated until it stops changing (contact closes or opens).
    """
    say = log or (lambda message: None)
    mesh = case.mesh
    stiffness, _ = assemble_stiffness_mass(mesh, case.young, case.poisson, 1.0)
    dof_count = stiffness.shape[0]
    force = np.zeros(dof_count)
    _surface_loads(StructuralCase(mesh, case.young, case.poisson, fixed=case.fixed, loads=case.loads), force)
    held = np.zeros(dof_count, bool)
    for name in case.fixed:
        nodes = mesh.boundary(name).nodes
        held[(nodes[:, None] * 3 + np.arange(3)).ravel()] = True
    for constraint in case.constraints:
        nodes=mesh.boundary(constraint["group"]).nodes
        for dof in constraint["dofs"]:
            held[nodes * 3 + int(dof)] = True
    used = np.zeros(dof_count, bool)
    for kind in SOLID_TYPES:
        if kind in mesh.cells:
            used[(mesh.cells[kind][:, :, None] * 3 + np.arange(3)).ravel()] = True
    free = np.flatnonzero(used & ~held)
    pairs = [_contact_constraints(mesh, pair, case.young) for pair in case.pairs]
    rows, cols, vals, offsets = [], [], [], []
    gaps, penalties, areas = [], [], []
    row = 0
    for data in pairs:
        for node, master, weights, normal, gap, k, a in zip(data["slave"], data["master"], data["weights"], data["normal"],
                                                           data["gap"], data["penalty"], data["area"]):
            for axis in range(3):  # gap = g0 + c·u with c = [n on the slave node, −wᵢ n on the master facet nodes]
                rows.append(row)
                cols.append(node * 3 + axis)
                vals.append(normal[axis])
                for m, w in zip(master, weights):
                    rows.append(row)
                    cols.append(m * 3 + axis)
                    vals.append(-w * normal[axis])
            gaps.append(gap)
            penalties.append(k)
            areas.append(a)
            row += 1
        offsets.append(row)
    constraint = sp.csr_matrix((vals, (rows, cols)), shape=(row, dof_count))
    gap0, penalty, area = np.array(gaps), np.array(penalties), np.array(areas)
    tolerance = 1e-9 * float(np.ptp(mesh.nodes, axis=0).max())
    active = gap0 <= tolerance
    displacement = np.zeros(dof_count)
    history = []
    for iteration in range(1, case.max_iterations + 1):
        c_active = constraint[active]
        k_contact = (c_active.T @ sp.diags(penalty[active]) @ c_active).tocsr()
        rhs = force - c_active.T @ (penalty[active] * gap0[active])
        system = (stiffness + k_contact)[free][:, free].tocsc()
        try:
            solution = spla.splu(system, permc_spec="MMD_AT_PLUS_A").solve(rhs[free])
        except RuntimeError as exc:
            raise StructuralError(f"the contact model is unstable (a body is held neither by supports nor by contact): {exc}") from exc
        if not np.all(np.isfinite(solution)):
            raise StructuralError("the contact model is unstable (a body is held neither by supports nor by contact)")
        displacement[:] = 0.0
        displacement[free] = solution
        gap = gap0 + constraint @ displacement
        updated = gap < 0.0
        history.append(int(active.sum()))
        say(f"Contact iteration {iteration}: {int(active.sum())} of {len(active)} slave nodes in contact")
        if np.array_equal(updated, active):
            break
        active = updated
    else:
        raise StructuralError(f"the contact active set did not settle in {case.max_iterations} iterations")
    gap = gap0 + constraint @ displacement
    contact_force = np.where(active, -penalty * gap, 0.0)  # compressive normal force per slave node
    resultants=[(contact_force[start:end, None] * data["normal"]).sum(axis=0)
                  for data, start, end in zip(pairs, [0, *offsets[:-1]], offsets)]
    return {
        "displacement": displacement.reshape(-1, 3),
        "contact_pressure": contact_force / np.maximum(area, 1e-300),
        "contact_force": contact_force,
        "active": active,
        "gap": gap,
        "slave_nodes": np.concatenate([data["slave"] for data in pairs]) if pairs else np.zeros(0, int),
        "summary": {
            "active_nodes": int(active.sum()), "slave_nodes": int(len(active)), "iterations": len(history),
            "max_penetration": float(max(0.0, -gap[active].min())) if active.any() else 0.0,
            "contact_resultants": [r.tolist() for r in resultants],
            "total_contact_force": float(contact_force.sum()),
        },
    }
