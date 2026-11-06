"""Polyhedral finite-volume topology from a linear volume mesh.

Builds the unique face list with owner/neighbour cells, oriented area
vectors, face and cell centroids, cell volumes (tetrahedral decomposition),
boundary patches, and the least-squares gradient operator.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from paramfabric.meshing.mesh import VolumeMesh

# local faces (corner indices) per cell type, gmsh ordering. Triangles are padded with -1.
CELL_FACES: dict[str, list[tuple[int, ...]]] = {
    "tet4": [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)],
    "hex8": [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
    "prism6": [(0, 2, 1), (3, 4, 5), (0, 1, 4, 3), (1, 2, 5, 4), (2, 0, 3, 5)],
    "pyramid5": [(0, 3, 2, 1), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)],
}


@dataclass
class Patch:
    name: str
    kind: str
    faces: np.ndarray  # indices into the boundary-face block (global face ids)


@dataclass
class Topology:
    cell_centres: np.ndarray
    volumes: np.ndarray
    face_nodes: np.ndarray  # (Nf, 4), -1 padded
    owner: np.ndarray
    neighbour: np.ndarray  # -1 for boundary faces
    area: np.ndarray  # (Nf, 3) oriented owner -> neighbour (outward for boundary)
    face_centres: np.ndarray
    patches: list[Patch]
    internal: np.ndarray
    boundary: np.ndarray
    cell_node_index: np.ndarray  # flat node ids of every cell
    cell_node_owner: np.ndarray  # owning cell of each entry above
    node_count: int

    @property
    def cells(self) -> int:
        return len(self.volumes)

    @property
    def magnitude(self) -> np.ndarray:
        return np.linalg.norm(self.area, axis=1)

    def patch(self, name: str) -> Patch:
        for patch in self.patches:
            if patch.name == name:
                return patch
        raise KeyError(name)


def _triangles(nodes: np.ndarray, faces: np.ndarray) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    a, b, c, d = (faces[:, i] for i in range(4))
    quad = d >= 0
    first = (nodes[a], nodes[b], nodes[c])
    second_d = np.where(quad, d, c)
    second = (nodes[a], nodes[c], nodes[second_d])
    return [first, second]


def build_topology(mesh: VolumeMesh) -> Topology:
    nodes = mesh.nodes
    local_faces, local_cells = [], []
    node_index: list[np.ndarray] = []
    node_owner: list[np.ndarray] = []
    offset = 0
    for kind in ("tet4", "hex8", "prism6", "pyramid5"):
        conn = mesh.cells.get(kind)
        if conn is None or not len(conn):
            continue
        conn = conn[:, : {"tet4": 4, "hex8": 8, "prism6": 6, "pyramid5": 5}[kind]]
        node_index.append(conn.ravel())
        node_owner.append(np.repeat(np.arange(offset, offset + len(conn)), conn.shape[1]))
        for local in CELL_FACES[kind]:
            padded = np.full((len(conn), 4), -1, dtype=np.int64)
            padded[:, : len(local)] = conn[:, list(local)]
            local_faces.append(padded)
            local_cells.append(np.arange(offset, offset + len(conn)))
        offset += len(conn)
    if not local_faces:
        raise ValueError("finite-volume mesh has no tet/hex/prism/pyramid cells")
    faces = np.concatenate(local_faces)
    cells = np.concatenate(local_cells)
    keys = np.sort(np.where(faces < 0, np.iinfo(np.int64).max, faces), axis=1)
    _, first_index, inverse, counts = np.unique(keys, axis=0, return_index=True, return_inverse=True, return_counts=True)
    inverse=inverse.ravel()
    if np.any(counts > 2):
        raise ValueError("non-manifold mesh: a face is shared by more than two cells")
    unique_count = len(first_index)
    owner = np.full(unique_count, -1, dtype=np.int64)
    neighbour = np.full(unique_count, -1, dtype=np.int64)
    order = np.argsort(inverse, kind="stable")
    sorted_ids = inverse[order]
    starts = np.r_[0, np.flatnonzero(np.diff(sorted_ids)) + 1]
    owner[sorted_ids[starts]] = cells[order[starts]]
    pair = counts[sorted_ids[starts]] == 2
    neighbour[sorted_ids[starts[pair]]] = cells[order[starts[pair] + 1]]
    face_nodes = faces[first_index]

    # face geometry (area vector and centroid from fan triangles)
    area = np.zeros((unique_count, 3))
    moment = np.zeros((unique_count, 3))
    for a, b, c in _triangles(nodes, face_nodes):
        tri_area = 0.5 * np.cross(b - a, c - a)
        size=np.linalg.norm(tri_area, axis=1)
        area += tri_area
        moment += size[:, None] * (a + b + c) / 3
    face_size = np.linalg.norm(area, axis=1)
    face_centres = moment / np.maximum(face_size, 1e-300)[:, None]

    # cell volumes and centroids via pyramids from the vertex average
    cell_count = offset
    approx = np.zeros((cell_count, 3))
    start = 0
    for kind in ("tet4", "hex8", "prism6", "pyramid5"):
        conn = mesh.cells.get(kind)
        if conn is None or not len(conn):
            continue
        width = {"tet4": 4, "hex8": 8, "prism6": 6, "pyramid5": 5}[kind]
        approx[start : start + len(conn)] = nodes[conn[:, :width]].mean(axis=1)
        start += len(conn)
    volumes = np.zeros(cell_count)
    centroid_moment=np.zeros((cell_count, 3))
    for cell_ids in (owner, neighbour):
        valid=cell_ids >= 0
        ids = cell_ids[valid]
        for a, b, c in _triangles(nodes, face_nodes[valid]):
            apex = approx[ids]
            signed = np.einsum("ij,ij->i", np.cross(b - apex, c - apex), a - apex) / 6.0
            volume = np.abs(signed)
            centre = (apex + a + b + c) / 4
            np.add.at(volumes, ids, volume)
            np.add.at(centroid_moment, ids, volume[:, None] * centre)
    if np.any(volumes <= 0):
        raise ValueError("mesh contains zero-volume cells")
    cell_centres = centroid_moment / volumes[:, None]

    # Orient area vectors owner -> neighbour (outward on the boundary)
    flip = np.einsum("ij,ij->i", area, face_centres - cell_centres[owner]) < 0
    area[flip] *= -1

    internal = np.flatnonzero(neighbour >= 0)
    boundary = np.flatnonzero(neighbour < 0)
    lookup = {tuple(row): face for face, row in zip(boundary, np.sort(np.where(face_nodes[boundary] < 0, -1, face_nodes[boundary]), axis=1))}
    assigned = np.zeros(unique_count, bool)
    patches: list[Patch] = []
    for group in mesh.boundaries:
        ids = []
        for kind, conn in group.elements.items():
            width = 3 if kind.startswith("tri") else 4
            corners = conn[:, :width]
            padded = np.full((len(corners), 4), -1, dtype=np.int64)
            padded[:, :width] = corners
            for row in np.sort(padded, axis=1):
                face = lookup.get(tuple(row))
                if face is not None and not assigned[face]:
                    ids.append(face)
                    assigned[face] = True
        patches.append(Patch(group.name, group.kind, np.array(ids, dtype=np.int64)))
    leftover = boundary[~assigned[boundary]]
    if len(leftover):
        patches.append(Patch("unassigned", "wall", leftover))
    return Topology(
        cell_centres=cell_centres, volumes=volumes, face_nodes=face_nodes, owner=owner, neighbour=neighbour,
        area=area, face_centres=face_centres, patches=[p for p in patches if len(p.faces)],
        internal=internal, boundary=boundary, cell_node_index=np.concatenate(node_index),
        cell_node_owner=np.concatenate(node_owner), node_count=len(nodes),
    )


class LeastSquaresGradient:
    """Weighted least-squares cell gradients with explicit boundary values.

    `include` selects the boundary faces whose *value* takes part (fixed-value
    faces). The others are zero-gradient faces: they contribute the constraint
    ∂φ/∂n = 0 as a normal-derivative row instead of a value. Substituting the
    cell value at a face centroid that is offset tangentially from the cell
    centroid (as on tetrahedral wall cells) would inject a spurious zero
    difference and bias the gradient; dropping the face entirely leaves sliver
    cells ill-conditioned, so the constraint is added only for near-degenerate cells.
    """

    def __init__(self, topology: Topology, include: np.ndarray | None = None) -> None:
        self.t = topology
        self.include = np.ones(len(topology.boundary), bool) if include is None else np.asarray(include, bool)
        owner, neighbour = topology.owner, topology.neighbour
        internal, boundary = topology.internal, topology.boundary
        self.d_internal = topology.cell_centres[neighbour[internal]] - topology.cell_centres[owner[internal]]
        self.d_boundary = topology.face_centres[boundary] - topology.cell_centres[owner[boundary]]
        self.w_internal = 1.0 / np.einsum("ij,ij->i", self.d_internal, self.d_internal)
        self.w_boundary = np.where(self.include, 1.0 / np.maximum(np.einsum("ij,ij->i", self.d_boundary, self.d_boundary), 1e-300), 0.0)
        matrix = np.zeros((topology.cells, 3, 3))
        outer = self.w_internal[:, None, None] * np.einsum("ij,ik->ijk", self.d_internal, self.d_internal)
        np.add.at(matrix, owner[internal], outer)
        np.add.at(matrix, neighbour[internal], outer)
        outer_b = self.w_boundary[:, None, None] * np.einsum("ij,ik->ijk", self.d_boundary, self.d_boundary)
        np.add.at(matrix, owner[boundary], outer_b)
        # zero-gradient faces add ∂φ/∂n = 0 rows only where the cell's own system is
        # near-degenerate (eigenvalue ratio below 1e-2), e.g. slivers with several
        # boundary faces; elsewhere they are left out so they cannot bias the gradient.
        eigenvalues = np.linalg.eigvalsh(matrix)
        degenerate = eigenvalues[:, 0] < 1e-2 * np.maximum(eigenvalues[:, 2], 1e-300)
        normal=topology.area[boundary] / np.linalg.norm(topology.area[boundary], axis=1)[:, None]
        normal_distance = np.abs(np.einsum("ij,ij->i", self.d_boundary, normal))
        active = ~self.include & degenerate[owner[boundary]]
        weight_n = np.where(active, normal_distance ** 2 / np.maximum(np.einsum("ij,ij->i", self.d_boundary, self.d_boundary), 1e-300), 0.0)
        np.add.at(matrix, owner[boundary], weight_n[:, None, None] * np.einsum("ij,ik->ijk", normal, normal))
        self.inverse = np.linalg.pinv(matrix)

    def __call__(self, values: np.ndarray, boundary_values: np.ndarray) -> np.ndarray:
        t = self.t
        owner, neighbour = t.owner, t.neighbour
        internal, boundary = t.internal, t.boundary
        vector = values.ndim == 2
        width = values.shape[1] if vector else 1
        v = values.reshape(len(values), width)
        vb=boundary_values.reshape(len(boundary), width)
        rhs=np.zeros((t.cells, width, 3))
        delta=(v[neighbour[internal]] - v[owner[internal]])[:, :, None] * (self.w_internal[:, None] * self.d_internal)[:, None, :]
        np.add.at(rhs, owner[internal], delta)
        np.add.at(rhs, neighbour[internal], delta)
        delta_b = (vb - v[owner[boundary]])[:, :, None] * (self.w_boundary[:, None] * self.d_boundary)[:, None, :]
        np.add.at(rhs, owner[boundary], delta_b)
        gradient = np.einsum("cij,ckj->cki", self.inverse, rhs)
        return gradient if vector else gradient[:, 0, :]


def cell_to_node(topology: Topology, values: np.ndarray) -> np.ndarray:
    """Volume-weighted average of cell values at mesh nodes"""
    values=np.asarray(values)
    shape = (topology.node_count,) + values.shape[1:]
    total = np.zeros(shape)
    weight = np.zeros(topology.node_count)
    flat, owners = topology.cell_node_index, topology.cell_node_owner
    np.add.at(total, flat, values[owners] * topology.volumes[owners].reshape((-1,) + (1,) * (values.ndim - 1)))
    np.add.at(weight, flat, topology.volumes[owners])
    return total / np.maximum(weight, 1e-300).reshape((-1,) + (1,) * (values.ndim - 1))
