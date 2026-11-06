"""Isoparametric element library (gmsh node ordering).

Shape functions are built generically: each element type declares its
reference node coordinates (identical to gmsh's `getElementProperties`) and
its polynomial space; the nodal basis is obtained by inverting the
Vandermonde matrix. This keeps node ordering and basis in one place and is
verified against gmsh in the test-suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

# gmsh element type codes
GMSH_TYPES = {
    2: "tri3", 3: "quad4", 4: "tet4", 5: "hex8", 6: "prism6", 7: "pyramid5",
    9: "tri6", 11: "tet10", 16: "quad8", 17: "hex20",
}
GMSH_CODES = {name: code for code, name in GMSH_TYPES.items()}

_HEX_CORNERS = [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
_HEX20_EDGES = [(0, 1), (0, 3), (0, 4), (1, 2), (1, 5), (2, 3), (2, 6), (3, 7), (4, 5), (4, 7), (5, 6), (6, 7)]
_TET_CORNERS = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
_TET10_EDGES = [(0, 1), (1, 2), (0, 2), (0, 3), (2, 3), (1, 3)]
_QUAD_CORNERS = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
_QUAD8_EDGES = [(0, 1), (1, 2), (2, 3), (3, 0)]
_TRI_CORNERS = [(0, 0), (1, 0), (0, 1)]
_TRI6_EDGES = [(0, 1), (1, 2), (2, 0)]


def _midpoints(corners, edges):
    return [tuple((np.array(corners[a]) + np.array(corners[b])) / 2) for a, b in edges]


REFERENCE_NODES: dict[str, np.ndarray] = {
    "tet4": np.array(_TET_CORNERS, float),
    "tet10": np.array(_TET_CORNERS + _midpoints(_TET_CORNERS, _TET10_EDGES), float),
    "hex8": np.array(_HEX_CORNERS, float),
    "hex20": np.array(_HEX_CORNERS + _midpoints(_HEX_CORNERS, _HEX20_EDGES), float),
    "tri3": np.array(_TRI_CORNERS, float),
    "tri6": np.array(_TRI_CORNERS + _midpoints(_TRI_CORNERS, _TRI6_EDGES), float),
    "quad4": np.array(_QUAD_CORNERS, float),
    "quad8": np.array(_QUAD_CORNERS + _midpoints(_QUAD_CORNERS, _QUAD8_EDGES), float),
}

# polynomial spaces as exponent tuples
_SPACES: dict[str, list[tuple[int, ...]]] = {
    "tet4": [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)],
    "tet10": [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (2, 0, 0), (0, 2, 0), (0, 0, 2), (1, 1, 0), (1, 0, 1), (0, 1, 1)],
    "hex8": [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1), (1, 1, 1)],
    "hex20": [
        (0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (2, 0, 0), (0, 2, 0), (0, 0, 2), (1, 1, 0), (1, 0, 1), (0, 1, 1),
        (1, 1, 1), (2, 1, 0), (2, 0, 1), (1, 2, 0), (0, 2, 1), (1, 0, 2), (0, 1, 2), (2, 1, 1), (1, 2, 1), (1, 1, 2),
    ],
    "tri3": [(0, 0), (1, 0), (0, 1)],
    "tri6": [(0, 0), (1, 0), (0, 1), (2, 0), (0, 2), (1, 1)],
    "quad4": [(0, 0), (1, 0), (0, 1), (1, 1)],
    "quad8": [(0, 0), (1, 0), (0, 1), (2, 0), (1, 1), (0, 2), (2, 1), (1, 2)],
}


def _gauss_line(n: int) -> tuple[np.ndarray, np.ndarray]:
    points, weights = np.polynomial.legendre.leggauss(n)
    return points, weights


def _tensor_rule(n: int, dim: int) -> tuple[np.ndarray, np.ndarray]:
    points, weights = _gauss_line(n)
    grids = np.meshgrid(*([points] * dim), indexing="ij")
    weight_grids = np.meshgrid(*([weights] * dim), indexing="ij")
    return np.stack([g.ravel() for g in grids], axis=1), np.prod(np.stack([w.ravel() for w in weight_grids], axis=1), axis=1)


def _tet_rule(order: int) -> tuple[np.ndarray, np.ndarray]:
    if order <= 1:
        return np.array([[0.25, 0.25, 0.25]]), np.array([1 / 6])
    if order == 2:
        a, b = 0.5854101966249685, 0.1381966011250105
        points = np.array([[b, b, b], [a, b, b], [b, a, b], [b, b, a]])
        return points, np.full(4, 1 / 24)
    # Keast degree-4 (11 points, one negative weight) for quadratic products
    points=[[0.25, 0.25, 0.25]]
    weights = [-0.01315555555555556]
    a, b = 0.0714285714285714, 0.7857142857142857
    for p in ([a, a, a], [b, a, a], [a, b, a], [a, a, b]):
        points.append(p)
        weights.append(0.007622222222222222)
    c, d = 0.399403576166799, 0.100596423833201
    for p in ([c, c, d], [c, d, c], [c, d, d], [d, c, c], [d, c, d], [d, d, c]):
        points.append(p)
        weights.append(0.02488888888888889)
    return np.array(points), np.array(weights)


def _tri_rule(order: int) -> tuple[np.ndarray, np.ndarray]:
    if order <= 1:
        return np.array([[1 / 3, 1 / 3]]), np.array([0.5])
    return np.array([[1 / 6, 1 / 6], [2 / 3, 1 / 6], [1 / 6, 2 / 3]]), np.full(3, 1 / 6)


@dataclass(frozen=True)
class ElementType:
    name: str
    dim: int
    nodes: int
    corners: int
    quad_points: np.ndarray
    quad_weights: np.ndarray
    coefficients: np.ndarray
    exponents: np.ndarray

    def _monomials(self, points: np.ndarray) -> np.ndarray:
        return np.prod(points[:, None, :] ** self.exponents[None, :, :], axis=2)

    def shape(self, points: np.ndarray) -> np.ndarray:
        return self._monomials(np.atleast_2d(points)) @ self.coefficients

    def gradient(self, points: np.ndarray) -> np.ndarray:
        """Reference gradients: (n_points, dim, n_nodes)"""
        points = np.atleast_2d(points)
        result = np.zeros((len(points), self.dim, self.nodes))
        for axis in range(self.dim):
            exponents = self.exponents.copy()
            factor = exponents[:, axis].astype(float)
            exponents[:, axis] = np.maximum(exponents[:, axis] - 1, 0)
            monomials = np.prod(points[:, None, :] ** exponents[None, :, :], axis=2) * factor[None, :]
            result[:, axis, :] = monomials @ self.coefficients
        return result


@lru_cache(maxsize=None)
def element(name: str, integration_order: int | None = None) -> ElementType:
    reference = REFERENCE_NODES[name]
    exponents = np.array(_SPACES[name], dtype=int)
    vandermonde = np.prod(reference[:, None, :] ** exponents[None, :, :], axis=2)
    coefficients = np.linalg.inv(vandermonde)
    dim = reference.shape[1]
    corners = {"tet4": 4, "tet10": 4, "hex8": 8, "hex20": 8, "tri3": 3, "tri6": 3, "quad4": 4, "quad8": 4}[name]
    quadratic = name in {"tet10", "hex20", "tri6", "quad8"}
    if name.startswith("tet"):
        points, weights = _tet_rule(integration_order or (2 if quadratic else 1))
    elif name.startswith("tri"):
        points, weights = _tri_rule(integration_order or (2 if quadratic else 1))
    else:
        points, weights = _tensor_rule(integration_order or (3 if quadratic else 2), dim)
    return ElementType(name, dim, len(reference), corners, points, weights, coefficients, exponents)


def face_of(volume_type: str) -> list[str]:
    return {"tet4": ["tri3"], "tet10": ["tri6"], "hex8": ["quad4"], "hex20": ["quad8"]}[volume_type]
