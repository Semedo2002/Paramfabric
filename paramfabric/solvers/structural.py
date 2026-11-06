"""Native structural FEA: beams, shells, and solids (numpy + scipy.sparse).

* **Beam**: 3D Euler-Bernoulli frame element, 2 nodes × 6 DOF (axial, two
  bending planes, St-Venant torsion), any cross-section given by A, Iy, Iz, J.
* **Shell**: flat 3-node facet shell, 6 DOF per node: CST membrane, DKT
  (discrete Kirchhoff triangle) bending, and a small drilling stiffness.
  Thin shells; transverse shear deformation is neglected.
* **Solid**: isoparametric Tet4/Tet10/Hex8/Hex20 on gmsh meshes, re-exported
  from :mod:`paramfabric.solvers.fem.structural`.

All three are linear static, isotropic, small strain. Beams and shells are
Python-API models (nodes plus connectivity); the Studio drives the solid
solver from the exact B-Rep.

DOF order per node is `(ux, uy, uz, rx, ry, rz)`; rotations follow the
right-hand rule. `fixed` maps a node to the DOFs held at zero (`"all"`,
`"translations"`, or a list of indices); `loads` maps a node to a 6-vector
of forces and moments.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from paramfabric.solvers.fem.structural import Cancelled, StructuralCase, StructuralError, solve_structural

__all__ = [
    "BeamGroup", "Cancelled", "FrameModel", "MemberModel", "Section", "ShellGroup", "ShellModel", "StructuralCase",
    "StructuralError", "modal_members", "solve_frame", "solve_members", "solve_shell", "solve_structural",
]

Fixity = dict[int, Any]
Loads = dict[int, Sequence[float]]


# shared assembly

def _dof_mask(fixed: Fixity, nodes: int) -> np.ndarray:
    held = np.zeros(nodes * 6, bool)
    for node, spec in fixed.items():
        if spec == "all":
            dofs = range(6)
        elif spec == "translations":
            dofs = range(3)
        else:
            dofs=spec
        for dof in dofs:
            held[6 * int(node) + int(dof)] = True
    return held


def _solve(stiffness: sp.csr_matrix, force: np.ndarray, held: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    free = np.flatnonzero(~held)
    if not held.any():
        raise StructuralError("the model has no supports")
    reduced = stiffness[free][:, free].tocsc()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", spla.MatrixRankWarning)
            solution = spla.spsolve(reduced, force[free])
    except (RuntimeError, spla.MatrixRankWarning) as exc:  # singular factor
        raise StructuralError(f"the model is a mechanism (insufficient supports): {exc}") from exc
    if not np.all(np.isfinite(solution)):
        raise StructuralError("the model is a mechanism (insufficient supports)")
    displacement=np.zeros(len(force))
    displacement[free] = solution
    reactions = stiffness @ displacement - force
    reactions[~held] = 0.0
    return displacement, reactions


def _load_vector(loads: Loads, nodes: int) -> np.ndarray:
    force = np.zeros(nodes * 6)
    for node, vector in loads.items():
        values = np.asarray(vector, float)
        force[6 * int(node) : 6 * int(node) + len(values)] += values
    return force


def _assemble(blocks: list[tuple[np.ndarray, np.ndarray]], size: int) -> sp.csr_matrix:
    rows, cols, vals = [], [], []
    for dofs, matrix in blocks:
        rows.append(np.repeat(dofs, len(dofs)))
        cols.append(np.tile(dofs, len(dofs)))
        vals.append(matrix.ravel())
    return sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(size, size))


def _element_dofs(nodes: Sequence[int]) -> np.ndarray:
    return np.concatenate([np.arange(6 * n, 6 * n + 6) for n in nodes])


# beams

@dataclass
class Section:
    """Cross-section properties (SI): area, second moments about local y and z, torsion constant"""

    area: float
    iy: float
    iz: float
    j: float
    cy: float | None = None  # extreme-fibre distances along local y and z (for stress recovery)
    cz: float | None = None

    @classmethod
    def rectangle(cls, width: float, height: float) -> Section:
        a, b = max(width, height), min(width, height)
        torsion = a * b ** 3 * (1 / 3 - 0.21 * b / a * (1 - b ** 4 / (12 * a ** 4)))
        return cls(width * height, width * height ** 3 / 12, height * width ** 3 / 12, torsion, width / 2, height / 2)

    @classmethod
    def circle(cls, diameter: float) -> Section:
        inertia = math.pi * diameter ** 4 / 64
        return cls(math.pi * diameter ** 2 / 4, inertia, inertia, 2 * inertia, diameter / 2, diameter / 2)

    @classmethod
    def tube(cls, outer: float, wall: float) -> Section:
        inner = outer - 2 * wall
        inertia = math.pi * (outer ** 4 - inner ** 4) / 64
        return cls(math.pi * (outer ** 2 - inner ** 2) / 4, inertia, inertia, 2 * inertia, outer / 2, outer / 2)

    @classmethod
    def i_beam(cls, height: float, flange_width: float, web: float, flange: float) -> Section:
        """Doubly symmetric I-section: `height` along local z (strong-axis bending about y)"""
        if not 0 < 2 * flange < height or not 0 < web <= flange_width:
            raise StructuralError("I-beam dimensions are inconsistent")
        clear = height - 2 * flange
        area=2 * flange_width * flange + clear * web
        iy = (flange_width * height ** 3 - (flange_width - web) * clear ** 3) / 12
        iz = (2 * flange * flange_width ** 3 + clear * web ** 3) / 12
        torsion = (2 * flange_width * flange ** 3 + (height - flange) * web ** 3) / 3  # open thin-walled section
        return cls(area, iy, iz, torsion, flange_width / 2, height / 2)

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> Section:
        kind=spec.get("type", "rectangular")
        try:
            if kind == "rectangular":
                return cls.rectangle(float(spec["width"]), float(spec["height"]))
            if kind == "circular":
                return cls.circle(float(spec["diameter"]))
            if kind == "tube":
                return cls.tube(float(spec["diameter"]), float(spec["wall"]))
            if kind == "i_beam":
                return cls.i_beam(float(spec["height"]), float(spec["flange_width"]), float(spec["web"]), float(spec["flange"]))
        except KeyError as exc:
            raise StructuralError(f"{kind} section needs {exc.args[0]!r}") from exc
        raise StructuralError(f"unknown section type {kind!r}")


@dataclass
class FrameModel:
    nodes: np.ndarray  # (N, 3)
    elements: np.ndarray  # (M, 2)
    section: Section | list[Section]
    young: float
    poisson: float = 0.3
    orientation: np.ndarray | None = None  # (M, 3) vectors in each element's local x-z plane

    def section_of(self, index: int) -> Section:
        return self.section[index] if isinstance(self.section, list) else self.section


def _frame_axes(start: np.ndarray, end: np.ndarray, hint: np.ndarray | None) -> tuple[np.ndarray, float]:
    axis = end - start
    length = float(np.linalg.norm(axis))
    if length <= 0:
        raise StructuralError("beam element has zero length")
    ex=axis / length
    if hint is None:
        hint = np.array([0.0, 0.0, 1.0]) if abs(ex[2]) < 0.99 else np.array([0.0, 1.0, 0.0])
    ey=np.cross(hint, ex)
    if np.linalg.norm(ey) < 1e-9:
        raise StructuralError("beam orientation vector is parallel to the element")
    ey /= np.linalg.norm(ey)
    ez=np.cross(ex, ey)
    return np.vstack([ex, ey, ez]), length


def frame_stiffness(section: Section, young: float, shear: float, length: float) -> np.ndarray:
    """12×12 local stiffness of a 3D Euler-Bernoulli frame element"""
    k = np.zeros((12, 12))
    L = length
    axial = young * section.area / L
    torsion = shear * section.j / L
    k[0, 0] = k[6, 6] = axial
    k[0, 6] = k[6, 0] = -axial
    k[3, 3] = k[9, 9] = torsion
    k[3, 9] = k[9, 3] = -torsion
    # Bending in the local x-y plane (uy, rz) uses Iz; in x-z (uz, ry) uses Iy
    for inertia, (u1, r1, u2, r2), sign in ((section.iz, (1, 5, 7, 11), 1.0), (section.iy, (2, 4, 8, 10), -1.0)):
        c = young * inertia / L ** 3
        block = c * np.array([
            [12, 6 * L * sign, -12, 6 * L * sign],
            [6 * L * sign, 4 * L * L, -6 * L * sign, 2 * L * L],
            [-12, -6 * L * sign, 12, -6 * L * sign],
            [6 * L * sign, 2 * L * L, -6 * L * sign, 4 * L * L],
        ])
        index = [u1, r1, u2, r2]
        k[np.ix_(index, index)] += block
    return k


@dataclass
class FrameResult:
    displacement: np.ndarray  # (N, 6)
    reactions: np.ndarray  # (N, 6)
    end_forces: np.ndarray  # (M, 12) local: N, Vy, Vz, T, My, Mz at each end
    summary: dict[str, Any] = field(default_factory=dict)


def solve_frame(model: FrameModel, fixed: Fixity, loads: Loads) -> FrameResult:
    nodes = np.asarray(model.nodes, float)
    elements = np.asarray(model.elements, int)
    shear = model.young / (2 * (1 + model.poisson))
    blocks, transforms, locals_ = [], [], []
    for index, (a, b) in enumerate(elements):
        hint = None if model.orientation is None else np.asarray(model.orientation[index], float)
        rotation, length = _frame_axes(nodes[a], nodes[b], hint)
        transform = np.kron(np.eye(4), rotation)
        local = frame_stiffness(model.section_of(index), model.young, shear, length)
        blocks.append((_element_dofs([a, b]), transform.T @ local @ transform))
        transforms.append(transform)
        locals_.append(local)
    size = len(nodes) * 6
    stiffness = _assemble(blocks, size)
    displacement, reactions = _solve(stiffness, _load_vector(loads, len(nodes)), _dof_mask(fixed, len(nodes)))
    end_forces = np.array([
        local @ (transform @ displacement[_element_dofs(element)])
        for local, transform, element in zip(locals_, transforms, elements)
    ])
    disp = displacement.reshape(-1, 6)
    return FrameResult(
        disp, reactions.reshape(-1, 6), end_forces,
        {"max_displacement": float(np.linalg.norm(disp[:, :3], axis=1).max()), "elements": len(elements), "dofs": size},
    )


# shells

@dataclass
class ShellModel:
    nodes: np.ndarray  # (N, 3)
    triangles: np.ndarray  # (M, 3)
    thickness: float
    young: float
    poisson: float = 0.3
    drilling: float = 1e-3  # drilling stiffness relative to the membrane stiffness


def _dkt_shape_derivatives(x: np.ndarray, y: np.ndarray, xi: float, eta: float) -> tuple[np.ndarray, ...]:
    xij = np.array([x[1] - x[2], x[2] - x[0], x[0] - x[1]])  # sides 23, 31, 12 -> k = 4, 5, 6
    yij = np.array([y[1] - y[2], y[2] - y[0], y[0] - y[1]])
    l2 = xij ** 2 + yij ** 2
    P = -6 * xij / l2
    q = 3 * xij * yij / l2
    t=-6 * yij / l2
    r = 3 * yij ** 2 / l2
    P4, P5, P6 = P
    q4, q5, q6 = q
    t4, t5, t6 = t
    r4, r5, r6 = r
    a = 1 - 2 * xi
    b = 1 - 2 * eta
    hx_xi = np.array([
        P6 * a + (P5 - P6) * eta, q6 * a - (q5 + q6) * eta, -4 + 6 * (xi + eta) + r6 * a - eta * (r5 + r6),
        -P6 * a + eta * (P4 + P6), q6 * a - eta * (q6 - q4), -2 + 6 * xi + r6 * a + eta * (r4 - r6),
        -eta * (P5 + P4), eta * (q4 - q5), -eta * (r5 - r4),
    ])
    hy_xi = np.array([
        t6 * a + eta * (t5 - t6), 1 + r6 * a - eta * (r5 + r6), -q6 * a + eta * (q5 + q6),
        -t6 * a + eta * (t4 + t6), -1 + r6 * a + eta * (r4 - r6), -q6 * a - eta * (q4 - q6),
        -eta * (t4 + t5), eta * (r4 - r5), -eta * (q4 - q5),
    ])
    hx_eta = np.array([
        -P5 * b - xi * (P6 - P5), q5 * b - xi * (q5 + q6), -4 + 6 * (xi + eta) + r5 * b - xi * (r5 + r6),
        xi * (P4 + P6), xi * (q4 - q6), -xi * (r6 - r4),
        P5 * b - xi * (P4 + P5), q5 * b + xi * (q4 - q5), -2 + 6 * eta + r5 * b + xi * (r4 - r5),
    ])
    hy_eta=np.array([
        -t5 * b - xi * (t6 - t5), 1 + r5 * b - xi * (r5 + r6), -q5 * b + xi * (q5 + q6),
        xi * (t4 + t6), xi * (r4 - r6), -xi * (q4 - q6),
        t5 * b - xi * (t4 + t5), -1 + r5 * b + xi * (r4 - r5), -q5 * b - xi * (q4 - q5),
    ])
    return hx_xi, hy_xi, hx_eta, hy_eta


_DKT_POINTS = ((0.5, 0.0), (0.5, 0.5), (0.0, 0.5))


def _dkt_curvature(x: np.ndarray, y: np.ndarray, xi: float, eta: float) -> tuple[np.ndarray, float]:
    x31, x12 = x[2] - x[0], x[0] - x[1]
    y31, y12 = y[2] - y[0], y[0] - y[1]
    area2 = x31 * y12 - x12 * y31  # 2A = x21*y31 - x31*y21
    hx_xi, hy_xi, hx_eta, hy_eta = _dkt_shape_derivatives(x, y, xi, eta)
    curvature = np.vstack([
        y31 * hx_xi + y12 * hx_eta,
        -x31 * hy_xi - x12 * hy_eta,
        -x31 * hx_xi - x12 * hx_eta + y31 * hy_xi + y12 * hy_eta,
    ]) / area2
    return curvature, area2


def dkt_stiffness(x: np.ndarray, y: np.ndarray, rigidity: np.ndarray) -> np.ndarray:
    stiffness = np.zeros((9, 9))
    for xi, eta in _DKT_POINTS:
        curvature, area2 = _dkt_curvature(x, y, xi, eta)
        stiffness += curvature.T @ rigidity @ curvature * area2 / 6
    return stiffness


def cst_stiffness(x: np.ndarray, y: np.ndarray, membrane: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    area = 0.5 * ((x[1] - x[0]) * (y[2] - y[0]) - (x[2] - x[0]) * (y[1] - y[0]))
    b = np.array([y[1] - y[2], y[2] - y[0], y[0] - y[1]])
    c = np.array([x[2] - x[1], x[0] - x[2], x[1] - x[0]])
    strain = np.zeros((3, 6))
    strain[0, 0::2] = b
    strain[1, 1::2] = c
    strain[2, 0::2] = c
    strain[2, 1::2] = b
    strain /= 2 * area
    return strain.T @ membrane @ strain * area, strain, area


def _shell_frame(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ex = points[1] - points[0]
    ex /= np.linalg.norm(ex)
    normal = np.cross(points[1] - points[0], points[2] - points[0])
    if np.linalg.norm(normal) < 1e-300:
        raise StructuralError("degenerate shell triangle")
    ez = normal / np.linalg.norm(normal)
    ey=np.cross(ez, ex)
    rotation = np.vstack([ex, ey, ez])
    local = (points - points[0]) @ rotation.T
    return rotation, local


@dataclass
class ShellResult:
    displacement: np.ndarray  # (N, 6)
    reactions: np.ndarray  # (N, 6)
    von_mises: np.ndarray  # (M,) max over top/bottom fibres at the element centroid
    summary: dict[str, Any] = field(default_factory=dict)


def solve_shell(model: ShellModel, fixed: Fixity, loads: Loads | None = None, pressure: float = 0.0) -> ShellResult:
    """Linear static facet-shell solve. `pressure` acts along each triangle's normal (+ = along n)."""
    nodes = np.asarray(model.nodes, float)
    triangles = np.asarray(model.triangles, int)
    E, nu, h = model.young, model.poisson, model.thickness
    plane = E / (1 - nu ** 2) * np.array([[1, nu, 0], [nu, 1, 0], [0, 0, (1 - nu) / 2]])
    membrane, bending = plane * h, plane * h ** 3 / 12
    force = _load_vector(loads or {}, len(nodes))
    blocks, cache = [], []
    for triangle in triangles:
        rotation, local = _shell_frame(nodes[triangle])
        x, y = local[:, 0], local[:, 1]
        km, strain, area = cst_stiffness(x, y, membrane)
        kb = dkt_stiffness(x, y, bending)
        k = np.zeros((18, 18))
        mem = np.array([[6 * i, 6 * i + 1] for i in range(3)]).ravel()
        ben=np.array([[6 * i + 2, 6 * i + 3, 6 * i + 4] for i in range(3)]).ravel()
        k[np.ix_(mem, mem)] += km
        k[np.ix_(ben, ben)] += kb
        drill=model.drilling * E * h * area
        for i in range(3):
            k[6 * i + 5, 6 * i + 5] += drill
        transform = np.kron(np.eye(6), rotation)
        blocks.append((_element_dofs(triangle), transform.T @ k @ transform))
        cache.append((transform, strain, x, y))
        if pressure:
            for node in triangle:
                force[6 * node : 6 * node + 3] += pressure * area / 3 * rotation[2]
    size = len(nodes) * 6
    stiffness = _assemble(blocks, size)
    displacement, reactions = _solve(stiffness, force, _dof_mask(fixed, len(nodes)))
    stresses = np.zeros(len(triangles))
    for index, (triangle, (transform, strain, x, y)) in enumerate(zip(triangles, cache)):
        local = transform @ displacement[_element_dofs(triangle)]
        in_plane = membrane @ strain @ local[[0, 1, 6, 7, 12, 13]] / h
        curvature, _ = _dkt_curvature(x, y, 1 / 3, 1 / 3)
        moments = bending @ curvature @ local[[2, 3, 4, 8, 9, 10, 14, 15, 16]]
        fibre = 6 * moments / h ** 2
        stresses[index] = max(_von_mises_plane(in_plane + fibre), _von_mises_plane(in_plane - fibre))
    disp = displacement.reshape(-1, 6)
    return ShellResult(
        disp, reactions.reshape(-1, 6), stresses,
        {"max_displacement": float(np.linalg.norm(disp[:, :3], axis=1).max()), "max_von_mises": float(stresses.max()),
         "elements": len(triangles), "dofs": size},
    )


def _von_mises_plane(stress: np.ndarray) -> float:
    sx, sy, txy = stress
    return float(math.sqrt(max(sx * sx - sx * sy + sy * sy + 3 * txy * txy, 0.0)))


def rectangle_mesh(width: float, height: float, nx: int, ny: int) -> tuple[np.ndarray, np.ndarray]:
    """Structured triangle mesh of a rectangle in the z = 0 plane (alternating diagonals)"""
    xs, ys = np.linspace(0, width, nx + 1), np.linspace(0, height, ny + 1)
    nodes = np.array([[x, y, 0.0] for y in ys for x in xs])
    triangles = []
    for j in range(ny):
        for i in range(nx):
            a, b = j * (nx + 1) + i, j * (nx + 1) + i + 1
            c, d = a + nx + 1, b + nx + 1
            triangles += [[a, b, d], [a, d, c]] if (i + j) % 2 == 0 else [[a, b, c], [b, d, c]]
    return nodes, np.array(triangles)


# combined beam + shell models

@dataclass
class BeamGroup:
    elements: np.ndarray  # (M, 2) node indices
    section: Section
    name: str = "beam"
    orientation: Sequence[float] | None = None  # vector in the local x-z plane (default: global z)


@dataclass
class ShellGroup:
    triangles: np.ndarray  # (M, 3) node indices
    thickness: float
    name: str = "shell"
    offset: float = 0.0  # mid-surface offset along the triangle normal from the node plane


@dataclass
class MemberModel:
    nodes: np.ndarray
    young: float
    poisson: float = 0.3
    beams: list[BeamGroup] = field(default_factory=list)
    shells: list[ShellGroup] = field(default_factory=list)
    drilling: float = 1e-3
    density: float = 7850.0  # kg/m³, used by modal analysis


@dataclass
class MemberResult:
    displacement: np.ndarray  # (N, 6)
    reactions: np.ndarray  # (N, 6)
    beam_stress: list[np.ndarray]  # per beam group: max combined axial + bending stress per element
    beam_forces: list[np.ndarray]  # per beam group: (M, 12) local end forces
    shell_von_mises: list[np.ndarray]  # per shell group: per element
    summary: dict[str, Any] = field(default_factory=dict)


def _skew(vector: np.ndarray) -> np.ndarray:
    x, y, z = vector
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])


def _assemble_members(model: MemberModel) -> tuple[np.ndarray, float, sp.csr_matrix, list, list]:
    nodes = np.asarray(model.nodes, float)
    E, nu = model.young, model.poisson
    shear = E / (2 * (1 + nu))
    plane = E / (1 - nu ** 2) * np.array([[1, nu, 0], [nu, 1, 0], [0, 0, (1 - nu) / 2]])
    blocks: list[tuple[np.ndarray, np.ndarray]] = []
    beam_cache: list[list[tuple[np.ndarray, np.ndarray]]] = []
    for group in model.beams:
        hint = None if group.orientation is None else np.asarray(group.orientation, float)
        cache = []
        for a, b in np.asarray(group.elements, int):
            rotation, length = _frame_axes(nodes[a], nodes[b], hint)
            transform = np.kron(np.eye(4), rotation)
            local = frame_stiffness(group.section, E, shear, length)
            blocks.append((_element_dofs([a, b]), transform.T @ local @ transform))
            cache.append((local, transform))
        beam_cache.append(cache)
    shell_cache: list[list[tuple[Any, ...]]] = []
    for group in model.shells:
        h = group.thickness
        if h <= 0:
            raise StructuralError(f"shell {group.name!r} needs a positive thickness")
        membrane, bending = plane * h, plane * h ** 3 / 12
        cache = []
        for triangle in np.asarray(group.triangles, int):
            rotation, local_xy = _shell_frame(nodes[triangle])
            x, y = local_xy[:, 0], local_xy[:, 1]
            km, strain, area = cst_stiffness(x, y, membrane)
            k = np.zeros((18, 18))
            mem = np.array([[6 * i, 6 * i + 1] for i in range(3)]).ravel()
            ben=np.array([[6 * i + 2, 6 * i + 3, 6 * i + 4] for i in range(3)]).ravel()
            k[np.ix_(mem, mem)] += km
            k[np.ix_(ben, ben)] += dkt_stiffness(x, y, bending)
            for i in range(3):
                k[6 * i + 5, 6 * i + 5] += model.drilling * E * h * area
            link = np.eye(18)
            if group.offset:
                eccentricity = -_skew(group.offset * rotation[2])
                for i in range(3):
                    link[6 * i:6 * i + 3, 6 * i + 3:6 * i + 6] = eccentricity
            operator = np.kron(np.eye(6), rotation) @ link  # node DOFs -> local mid-surface DOFs
            blocks.append((_element_dofs(triangle), operator.T @ k @ operator))
            cache.append((operator, strain, x, y, membrane, bending))
        shell_cache.append(cache)
    if not blocks:
        raise StructuralError("the model has no beam or shell elements")
    size = len(nodes) * 6
    stiffness = _assemble(blocks, size)
    return nodes, E, stiffness, beam_cache, shell_cache


def solve_members(model: MemberModel, fixed: Fixity, loads: Loads | None = None) -> MemberResult:
    """Beams and shells sharing nodes, solved together (6 DOF per node).

    A shell offset `e` places the mid-surface at `node + e·n`; the shell's
    DOFs are tied to the node with a rigid link (`u_mid = u + θ × e·n`), so an
    offset shell and a beam sharing its nodes act compositely (stiffened panel).
    """
    nodes, E, stiffness, beam_cache, shell_cache = _assemble_members(model)
    size = len(nodes) * 6
    displacement, reactions = _solve(stiffness, _load_vector(loads or {}, len(nodes)), _dof_mask(fixed, len(nodes)))

    beam_stress, beam_forces = [], []
    for group, cache in zip(model.beams, beam_cache):
        elements=np.asarray(group.elements, int)
        forces = np.array([local @ (transform @ displacement[_element_dofs(element)])
                           for (local, transform), element in zip(cache, elements)]).reshape(-1, 12)
        section = group.section
        stress = np.abs(forces[:, [0, 6]]).max(axis=1) / section.area
        if section.cy and section.cz:
            stress=stress + np.abs(forces[:, [4, 10]]).max(axis=1) * section.cz / section.iy \
                + np.abs(forces[:, [5, 11]]).max(axis=1) * section.cy / section.iz
        beam_forces.append(forces)
        beam_stress.append(stress)
    shell_stress=[]
    for group, cache in zip(model.shells, shell_cache):
        values = np.zeros(len(cache))
        for index, (triangle, (operator, strain, x, y, membrane, bending)) in enumerate(zip(np.asarray(group.triangles, int), cache)):
            local = operator @ displacement[_element_dofs(triangle)]
            in_plane = membrane @ strain @ local[[0, 1, 6, 7, 12, 13]] / group.thickness
            curvature, _ = _dkt_curvature(x, y, 1 / 3, 1 / 3)
            fibre = 6 * (bending @ curvature @ local[[2, 3, 4, 8, 9, 10, 14, 15, 16]]) / group.thickness ** 2
            values[index] = max(_von_mises_plane(in_plane + fibre), _von_mises_plane(in_plane - fibre))
        shell_stress.append(values)
    disp = displacement.reshape(-1, 6)
    peaks = [float(v.max()) for v in beam_stress + shell_stress if len(v)]
    return MemberResult(
        disp, reactions.reshape(-1, 6), beam_stress, beam_forces, shell_stress,
        {"max_displacement": float(np.linalg.norm(disp[:, :3], axis=1).max()), "max_stress": max(peaks) if peaks else 0.0,
         "beam_elements": int(sum(len(g.elements) for g in model.beams)),
         "shell_elements": int(sum(len(g.triangles) for g in model.shells)), "dofs": size},
    )


def frame_mass(section: Section, density: float, length: float) -> np.ndarray:
    """12×12 consistent local mass of a 3D frame element (axial, torsion, two bending planes)"""
    m = density * section.area * length
    mass = np.zeros((12, 12))
    mass[np.ix_([0, 6], [0, 6])] = m / 6 * np.array([[2, 1], [1, 2]])
    polar = density * (section.iy + section.iz) * length  # rotary inertia about the axis
    mass[np.ix_([3, 9], [3, 9])] = polar / 6 * np.array([[2, 1], [1, 2]])
    L = length
    for (u1, r1, u2, r2), sign in (((1, 5, 7, 11), 1.0), ((2, 4, 8, 10), -1.0)):
        block = m / 420 * np.array([
            [156, 22 * L * sign, 54, -13 * L * sign],
            [22 * L * sign, 4 * L * L, 13 * L * sign, -3 * L * L],
            [54, 13 * L * sign, 156, -22 * L * sign],
            [-13 * L * sign, -3 * L * L, -22 * L * sign, 4 * L * L],
        ])
        index = [u1, r1, u2, r2]
        mass[np.ix_(index, index)] += block
    return mass


def _member_mass(model: MemberModel) -> sp.csr_matrix:
    nodes = np.asarray(model.nodes, float)
    blocks: list[tuple[np.ndarray, np.ndarray]] = []
    for group in model.beams:
        hint = None if group.orientation is None else np.asarray(group.orientation, float)
        for a, b in np.asarray(group.elements, int):
            rotation, length = _frame_axes(nodes[a], nodes[b], hint)
            transform = np.kron(np.eye(4), rotation)
            blocks.append((_element_dofs([a, b]), transform.T @ frame_mass(group.section, model.density, length) @ transform))
    for group in model.shells:
        h = group.thickness
        for triangle in np.asarray(group.triangles, int):
            corners = nodes[triangle]
            area = 0.5 * np.linalg.norm(np.cross(corners[1] - corners[0], corners[2] - corners[0]))
            share=model.density * h * area / 3  # lumped: translational mass per node
            rotary = share * (h * h / 12 + group.offset ** 2)  # thickness rotary inertia (and offset)
            diagonal = np.tile([share, share, share, rotary, rotary, rotary * 1e-3], 3)
            blocks.append((_element_dofs(triangle), np.diag(diagonal)))
    return _assemble(blocks, len(nodes) * 6)


@dataclass
class ModalResult:
    frequencies: np.ndarray  # Hz, ascending
    mode_shapes: np.ndarray  # (modes, N, 6), translations normalized to unit maximum
    effective_mass_fraction: np.ndarray  # (modes, 3) along x, y, z
    total_mass: float


def modal_members(model: MemberModel, fixed: Fixity, modes: int = 6) -> ModalResult:
    """Natural frequencies and mode shapes of a beam/shell model (shift-invert Lanczos)"""
    from paramfabric.solvers.fem.structural import eigen_modes

    if model.density <= 0:
        raise StructuralError("modal analysis needs a positive density")
    nodes, _, stiffness, _, _ = _assemble_members(model)
    mass = _member_mass(model)
    held=_dof_mask(fixed, len(nodes)) if fixed else np.zeros(len(nodes) * 6, bool)
    active = np.zeros(len(nodes) * 6, bool)
    for group in model.beams:
        active[np.concatenate([_element_dofs(e) for e in np.asarray(group.elements, int)])] = True
    for group in model.shells:
        active[np.concatenate([_element_dofs(t) for t in np.asarray(group.triangles, int)])] = True
    result = eigen_modes(stiffness, mass, np.flatnonzero(active & ~held), modes, 6)
    shapes = result["shapes"].T.reshape(len(result["frequencies"]), len(nodes), 6)
    peaks = np.linalg.norm(shapes[:, :, :3], axis=2).max(axis=1)
    rigid = np.zeros(len(nodes) * 6)
    rigid[0::6] = 1.0  # unit translation along x: rᵀ M r is the total mass (consistent mass has off-diagonal terms)
    return ModalResult(result["frequencies"], shapes / np.maximum(peaks, 1e-300)[:, None, None],
                       result["effective_mass_fraction"], float(rigid @ (mass @ rigid)))
