"""Body-fitted unstructured meshing with gmsh.

* `mesh_solid`    - tetrahedral (Tet4/Tet10) or all-hex (Hex8/Hex20, by
  subdivision) meshes of solid bodies for structural analysis; every CAD face
  becomes a boundary group so loads and supports can be applied to it.
* `mesh_external` - fluid domain = box minus bodies, with optional prismatic
  inflation layers grown from the body surface (discrete boundary-layer
  extrusion), tetrahedra elsewhere; patches `inlet`/`outlet`/`sides`/`walls`.
* `mesh_internal` - the body itself is the fluid volume (pipes, manifolds);
  selected faces become inlet/outlet and the remaining faces walls.

Sizing combines a global target size, curvature-based refinement (elements
per 2π of surface curvature), and distance-threshold refinement around
selected faces. Geometry is read from STEP/IGES/BREP files through gmsh's own
OpenCASCADE kernel; coordinates are metres.
"""

from __future__ import annotations

import math
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from paramfabric.gmsh_session import GmshSession, import_shapes
from paramfabric.meshing.mesh import BoundaryGroup, VolumeMesh
from paramfabric.solvers.fem.elements import GMSH_TYPES

Selector = tuple[Sequence[float], Sequence[float] | None]


class MeshingFailed(RuntimeError):
    """Raised when gmsh cannot produce a valid mesh"""


@dataclass
class BoundaryLayer:
    first_height: float
    layers: int = 5
    growth: float = 1.2

    @property
    def heights(self) -> list[float]:
        return [self.first_height * sum(self.growth ** k for k in range(i + 1)) for i in range(self.layers)]

    @property
    def thickness(self) -> float:
        return self.heights[-1]


@dataclass
class MeshOptions:
    size: float
    min_size: float | None = None
    curvature: int = 16
    order: int = 1
    element: str = "tet"
    refinements: list[dict[str, Any]] = field(default_factory=list)
    boundary_layer: BoundaryLayer | None = None
    optimize: bool = True
    max_nodes: int = 600_000


def first_layer_height(y_plus: float, velocity: float, length: float, kinematic_viscosity: float) -> dict[str, float]:
    """First-cell height for a target y⁺ (turbulent flat-plate skin friction)"""
    reynolds = max(velocity * length / kinematic_viscosity, 1.0)
    skin_friction = 0.058 * reynolds ** -0.2 if reynolds > 5e5 else 1.328 / math.sqrt(reynolds)
    friction_velocity = velocity * math.sqrt(skin_friction / 2)
    return {
        "first_height": y_plus * kinematic_viscosity / max(friction_velocity, 1e-12),
        "reynolds": reynolds,
        "skin_friction": skin_friction,
        "friction_velocity": friction_velocity,
    }


def _import(gmsh: Any, cad_files: Sequence[Path], fragment: bool = True) -> list[tuple[int, int]]:
    volumes: list[tuple[int, int]] = []
    for path in cad_files:
        volumes.extend(import_shapes(gmsh, path))
    if not volumes:
        raise MeshingFailed("the CAD input contains no closed solids")
    if len(volumes) > 1 and fragment:
        volumes, _ = gmsh.model.occ.fragment(volumes[:1], volumes[1:])
        gmsh.model.occ.synchronize()
        volumes = [entity for entity in volumes if entity[0] == 3]
    return volumes


def _closest_surface(gmsh: Any, surfaces: list[int], selector: Selector) -> tuple[int, float]:
    point, normal = selector
    best, best_distance = -1, math.inf
    for tag in surfaces:
        closest, parametric = gmsh.model.getClosestPoint(2, tag, list(point))
        distance = float(np.linalg.norm(np.asarray(closest) - np.asarray(point)))
        if normal is not None:
            surface_normal = np.asarray(gmsh.model.getNormal(tag, parametric))
            if abs(float(np.dot(surface_normal, normal))) < 0.7:
                distance += 1e6
        if distance < best_distance:
            best, best_distance = tag, distance
    return best, best_distance


def _apply_sizing(gmsh: Any, options: MeshOptions, surfaces: list[int]) -> None:
    gmsh.option.setNumber("Mesh.MeshSizeMax", options.size)
    gmsh.option.setNumber("Mesh.MeshSizeMin", options.min_size or options.size / 20)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", max(0, int(options.curvature)))
    gmsh.option.setNumber("Mesh.Algorithm", 6)
    gmsh.option.setNumber("Mesh.Algorithm3D", 1)
    gmsh.option.setNumber("Mesh.Optimize", 1 if options.optimize else 0)
    fields = []
    for refinement in options.refinements:
        tags = sorted(set(refinement.get("surfaces", [])) | {_closest_surface(gmsh, surfaces, selector)[0] for selector in refinement["selectors"]})
        if not tags:
            continue
        distance = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(distance, "SurfacesList", tags)
        threshold = gmsh.model.mesh.field.add("Threshold")
        gmsh.model.mesh.field.setNumber(threshold, "InField", distance)
        gmsh.model.mesh.field.setNumber(threshold, "SizeMin", refinement["size"])
        gmsh.model.mesh.field.setNumber(threshold, "SizeMax", options.size)
        gmsh.model.mesh.field.setNumber(threshold, "DistMin", refinement.get("distance", 0.0))
        gmsh.model.mesh.field.setNumber(threshold, "DistMax", refinement.get("distance", 0.0) + refinement.get("grading", 4 * options.size))
        fields.append(threshold)
    if fields:
        minimum = gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(minimum, "FieldsList", fields)
        gmsh.model.mesh.field.setAsBackgroundMesh(minimum)


def _generate(gmsh: Any, options: MeshOptions, dimension: int = 3) -> None:
    # options persist in the process-global gmsh session, so set every one explicitly
    gmsh.option.setNumber("Mesh.SubdivisionAlgorithm", 2 if options.element == "hex" else 0)
    gmsh.option.setNumber("Mesh.ElementOrder", options.order)
    gmsh.option.setNumber("Mesh.SecondOrderIncomplete", 1 if options.order == 2 else 0)
    gmsh.option.setNumber("Mesh.HighOrderOptimize", 0)
    try:
        gmsh.model.mesh.generate(dimension)
    except Exception as exc:  # noqa: BLE001 - gmsh raises bare Exception
        raise MeshingFailed(f"gmsh failed: {exc}") from exc
    if options.order == 2:
        gmsh.model.mesh.setOrder(2)


def _extract(gmsh: Any, groups: dict[str, tuple[str, list[int]]], source_faces: dict[str, list[int]] | None = None) -> VolumeMesh:
    node_tags, coordinates, _ = gmsh.model.mesh.getNodes()
    lookup = np.full(int(node_tags.max()) + 1, -1, dtype=np.int64)
    lookup[node_tags.astype(np.int64)] = np.arange(len(node_tags))
    nodes = coordinates.reshape(-1, 3)
    cells: dict[str, list[np.ndarray]] = {}
    for kind_code, _, element_nodes in zip(*gmsh.model.mesh.getElements(3)):
        name = GMSH_TYPES.get(int(kind_code))
        if name is None:
            raise MeshingFailed(f"unsupported gmsh volume element type {kind_code}")
        width = gmsh.model.mesh.getElementProperties(kind_code)[3]
        cells.setdefault(name, []).append(lookup[element_nodes.astype(np.int64)].reshape(-1, width))
    boundaries = []
    for name, (kind, surfaces) in groups.items():
        elements: dict[str, list[np.ndarray]] = {}
        for surface in surfaces:
            for kind_code, _, element_nodes in zip(*gmsh.model.mesh.getElements(2, surface)):
                surface_name = GMSH_TYPES[int(kind_code)]
                width = gmsh.model.mesh.getElementProperties(kind_code)[3]
                elements.setdefault(surface_name, []).append(lookup[element_nodes.astype(np.int64)].reshape(-1, width))
        boundaries.append(BoundaryGroup(
            name, kind, {key: np.concatenate(value) for key, value in elements.items()},
            (source_faces or {}).get(name, []),
        ))
    merged = {key: np.concatenate(value) for key, value in cells.items()}
    used = np.unique(np.concatenate([conn.ravel() for conn in merged.values()]))
    if len(used) != len(nodes):  # drop nodes that only belong to construction entities
        remap = np.full(len(nodes), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        nodes = nodes[used]
        merged={key: remap[value] for key, value in merged.items()}
        for group in boundaries:
            group.elements = {key: remap[value] for key, value in group.elements.items()}
    order = 2 if any(key in merged for key in ("tet10", "hex20")) else 1
    return VolumeMesh(nodes=np.ascontiguousarray(nodes), cells=merged, boundaries=boundaries, order=order)


def mesh_solid(
    cad_files: Sequence[Path],
    options: MeshOptions,
    named_faces: dict[str, list[Selector]] | None = None,
    *,
    separate_bodies: bool = False,
) -> VolumeMesh:
    """Mesh solids; boundary groups `face:<n>` per CAD face plus each named selection.

    Bodies are fragmented into one conforming mesh by default (bonded parts).
    `separate_bodies=True` keeps every body's mesh independent (no shared
    nodes), as needed for contact between touching parts.
    """
    with GmshSession() as gmsh:
        gmsh.model.add("solid")
        _import(gmsh, cad_files, fragment=not separate_bodies)
        surfaces = [tag for _, tag in gmsh.model.getEntities(2)]
        _apply_sizing(gmsh, options, surfaces)
        _generate(gmsh, options)
        groups: dict[str, tuple[str, list[int]]] = {f"face:{tag}": ("face", [tag]) for tag in surfaces}
        for name, selectors in (named_faces or {}).items():
            tags = sorted({_closest_surface(gmsh, surfaces, selector)[0] for selector in selectors})
            groups[name] = ("named", tags)
        mesh = _extract(gmsh, groups)
        mesh.statistics.update(mesher="gmsh", element=options.element, order=options.order, cad_faces=len(surfaces))
        _check_size(mesh, options)
        return mesh


def _box_surfaces(gmsh: Any, surfaces: list[int], low: Sequence[float], high: Sequence[float]) -> dict[str, list[int]]:
    patches: dict[str, list[int]] = {"inlet": [], "outlet": [], "sides": [], "walls": []}
    span = max(high[i] - low[i] for i in range(3))
    tolerance = 1e-6 * span
    for tag in surfaces:
        x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(2, tag)
        if abs(x0 - low[0]) < tolerance and abs(x1 - low[0]) < tolerance:
            patches["inlet"].append(tag)
        elif abs(x0 - high[0]) < tolerance and abs(x1 - high[0]) < tolerance:
            patches["outlet"].append(tag)
        elif any(
            abs(a - b) < tolerance and abs(c - b) < tolerance
            for a, c, b in ((y0, y1, low[1]), (y0, y1, high[1]), (z0, z1, low[2]), (z0, z1, high[2]))
        ):
            patches["sides"].append(tag)
        else:
            patches["walls"].append(tag)
    return patches


_PATCH_KINDS = {"inlet": "inlet", "outlet": "outlet", "sides": "slip", "walls": "wall"}


def mesh_external(
    cad_files: Sequence[Path],
    domain_low: Sequence[float],
    domain_high: Sequence[float],
    options: MeshOptions,
    log: Callable[[str], None] | None = None,
) -> VolumeMesh:
    say = log or (lambda message: None)
    if options.boundary_layer is not None:
        try:
            return _mesh_external_layers(cad_files, domain_low, domain_high, options, say)
        except MeshingFailed as exc:
            say(f"Inflation layers failed ({exc}); meshing without them.")
            options = MeshOptions(**{**options.__dict__, "boundary_layer": None})
    with GmshSession() as gmsh:
        gmsh.model.add("fluid")
        bodies = _import(gmsh, cad_files)
        box = gmsh.model.occ.addBox(*domain_low, *(domain_high[i] - domain_low[i] for i in range(3)))
        fluid, _ = gmsh.model.occ.cut([(3, box)], bodies)
        gmsh.model.occ.synchronize()
        if not fluid:
            raise MeshingFailed("subtracting the bodies left no fluid volume; enlarge the flow domain")
        surfaces = [tag for _, tag in gmsh.model.getEntities(2)]
        patches = _box_surfaces(gmsh, surfaces, domain_low, domain_high)
        # Graded sizing: target size on the bodies, growing to 4x in the far field
        far = MeshOptions(**{**options.__dict__, "size": 4 * options.size, "refinements": [
            *options.refinements,
            {"selectors": [], "size": options.size, "distance": 0.0, "surfaces": patches["walls"],
             "grading": max(domain_high[i] - domain_low[i] for i in range(3)) / 4},
        ]})
        _apply_sizing(gmsh, far, surfaces)
        _generate(gmsh, MeshOptions(**{**options.__dict__, "order": 1, "element": "tet"}))
        mesh = _extract(gmsh, {name: (_PATCH_KINDS[name], tags) for name, tags in patches.items() if tags})
        mesh.statistics.update(mesher="gmsh", boundary_layers=0)
        _check_size(mesh, options)
        return mesh


def _mesh_external_layers(cad_files, domain_low, domain_high, options: MeshOptions, say) -> VolumeMesh:
    layer = options.boundary_layer
    assert layer is not None
    with GmshSession() as gmsh:
        # stage 1: surface mesh of the bodies on the exact geometry
        gmsh.model.add("surface")
        _import(gmsh, cad_files)
        surfaces = [tag for _, tag in gmsh.model.getEntities(2)]
        _apply_sizing(gmsh, options, surfaces)
        gmsh.option.setNumber("Mesh.SubdivisionAlgorithm", 0)
        gmsh.option.setNumber("Mesh.ElementOrder", 1)
        try:
            gmsh.model.mesh.generate(2)
        except Exception as exc:  # noqa: BLE001
            raise MeshingFailed(f"surface meshing failed: {exc}") from exc
        with tempfile.TemporaryDirectory() as scratch:
            surface_file = Path(scratch) / "surface.msh"
            gmsh.write(str(surface_file))
            gmsh.clear()
            # Stage 2: discrete surface -> prismatic extrusion -> outer tetrahedra
            gmsh.model.add("fluid")
            gmsh.merge(str(surface_file))
        # the merged file carries the solid's (empty) volume entity: drop it, or
        # the body interior would be meshed as fluid
        gmsh.model.removeEntities(gmsh.model.getEntities(3))
        # one smoothed normal field across the body surfaces. No createGeometry():
        # reparametrized surfaces would be re-meshed onto their own facets,
        # moving wall nodes off the exact geometry.
        gmsh.model.mesh.classifySurfaces(math.pi, True, False, math.pi)
        body_surfaces = gmsh.model.getEntities(2)
        gmsh.option.setNumber("Geometry.ExtrudeReturnLateralEntities", 0)
        try:
            extruded=gmsh.model.geo.extrudeBoundaryLayer(body_surfaces, [1] * layer.layers, layer.heights, True)
        except Exception as exc:  # noqa: BLE001
            raise MeshingFailed(f"boundary-layer extrusion failed: {exc}") from exc
        gmsh.model.geo.synchronize()
        tops = [entity[1] for entity in extruded if entity[0] == 2]
        geo = gmsh.model.geo
        low, high = domain_low, domain_high
        points = [geo.addPoint(x, y, z) for z in (low[2], high[2]) for y in (low[1], high[1]) for x in (low[0], high[0])]
        lines: dict[tuple[int, int], int] = {}
        for a, b in [(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)]:
            lines[(a, b)] = geo.addLine(points[a], points[b])

        def face(*edges: tuple[int, int]) -> int:
            curve = [lines[edge] if edge in lines else -lines[(edge[1], edge[0])] for edge in edges]
            return geo.addPlaneSurface([geo.addCurveLoop(curve)])

        box_faces = {
            "inlet": [face((0, 2), (2, 6), (6, 4), (4, 0))],
            "outlet": [face((1, 3), (3, 7), (7, 5), (5, 1))],
            "sides": [
                face((0, 1), (1, 5), (5, 4), (4, 0)), face((2, 3), (3, 7), (7, 6), (6, 2)),
                face((0, 1), (1, 3), (3, 2), (2, 0)), face((4, 5), (5, 7), (7, 6), (6, 4)),
            ],
        }
        outer_loop=geo.addSurfaceLoop([tag for tags in box_faces.values() for tag in tags])
        geo.addVolume([outer_loop, geo.addSurfaceLoop(tops)])
        geo.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 4 * options.size)
        gmsh.option.setNumber("Mesh.MeshSizeMin", options.min_size or options.size / 20)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 1)
        gmsh.option.setNumber("Mesh.SubdivisionAlgorithm", 0)
        gmsh.option.setNumber("Mesh.ElementOrder", 1)
        # keep the body surface mesh from stage 1: re-meshing the classified
        # surfaces would use the far-field size
        gmsh.option.setNumber("Mesh.MeshOnlyEmpty", 1)
        try:
            gmsh.model.mesh.generate(3)
        except Exception as exc:  # noqa: BLE001
            raise MeshingFailed(f"volume meshing with inflation layers failed: {exc}") from exc
        finally:
            gmsh.option.setNumber("Mesh.MeshOnlyEmpty", 0)
        groups = {name: (_PATCH_KINDS[name], tags) for name, tags in box_faces.items()}
        groups["walls"] = ("wall", [tag for _, tag in body_surfaces])
        mesh = _extract(gmsh, groups)
        mesh.statistics.update(
            mesher="gmsh", boundary_layers=layer.layers, first_layer_height=layer.first_height,
            layer_growth=layer.growth, layer_thickness=layer.thickness,
        )
        say(f"Inflation: {layer.layers} prism layers, first height {layer.first_height:.3g} m, growth {layer.growth}")
        _check_size(mesh, options)
        return mesh


def mesh_internal(cad_files: Sequence[Path], options: MeshOptions, inlets: list[Selector], outlets: list[Selector]) -> VolumeMesh:
    if not inlets or not outlets:
        raise MeshingFailed("internal flow needs at least one inlet face and one outlet face")
    with GmshSession() as gmsh:
        gmsh.model.add("internal")
        _import(gmsh, cad_files)
        surfaces = [tag for _, tag in gmsh.model.getEntities(2)]
        _apply_sizing(gmsh, options, surfaces)
        _generate(gmsh, MeshOptions(**{**options.__dict__, "order": 1, "element": "tet"}))
        inlet=sorted({_closest_surface(gmsh, surfaces, s)[0] for s in inlets})
        outlet = sorted({_closest_surface(gmsh, surfaces, s)[0] for s in outlets} - set(inlet))
        walls = [tag for tag in surfaces if tag not in inlet and tag not in outlet]
        mesh = _extract(gmsh, {"inlet": ("inlet", inlet), "outlet": ("outlet", outlet), "walls": ("wall", walls)})
        mesh.statistics.update(mesher="gmsh", boundary_layers=0)
        _check_size(mesh, options)
        return mesh


class MeshTooLarge(MeshingFailed):
    """Raised when a mesh exceeds the node budget (callers may coarsen and retry)"""


def _check_size(mesh: VolumeMesh, options: MeshOptions) -> None:
    if len(mesh.nodes) > options.max_nodes:
        raise MeshTooLarge(
            f"mesh has {len(mesh.nodes):,} nodes, above the {options.max_nodes:,}-node limit; increase the element size"
        )


def mesh_cht(
    solid_files: Sequence[Path],
    domain_low: Sequence[float],
    domain_high: Sequence[float],
    options: MeshOptions,
    heater: list[Selector] | None = None,
) -> dict[str, VolumeMesh]:
    """Conforming multi-region mesh for conjugate heat transfer.

    The flow box and the solids are fragmented together, so fluid and solid
    share identical interface faces. Returns `{"fluid": mesh, "solid": mesh}`
    (several solid bodies form one solid region). Boundary groups:

    * fluid: `inlet` (x-min), `outlet` (x-max), `sides` (slip), `walls`
      (box faces touched by solids and other non-box walls), `fluid_to_solid`;
    * solid: `solid_to_fluid`, `heater` (faces closest to the `heater`
      selectors, on the solid's outer boundary), `adiabatic` (the rest).
    """
    with GmshSession() as gmsh:
        gmsh.model.add("cht")
        solids = []
        for path in solid_files:
            solids.extend(import_shapes(gmsh, path))
        if not solids:
            raise MeshingFailed("the CAD input contains no closed solids")
        box = gmsh.model.occ.addBox(*domain_low, *(domain_high[i] - domain_low[i] for i in range(3)))
        _, mapping = gmsh.model.occ.fragment([(3, box)], solids)
        gmsh.model.occ.synchronize()
        solid_volumes = sorted({tag for children in mapping[1:] for dim, tag in children if dim == 3})
        fluid_volumes = sorted({tag for dim, tag in mapping[0] if dim == 3} - set(solid_volumes))
        if not fluid_volumes:
            raise MeshingFailed("the solids fill the whole flow box; enlarge the domain")

        def boundary(volumes: list[int]) -> set[int]:
            return {abs(tag) for _, tag in gmsh.model.getBoundary([(3, v) for v in volumes], oriented=False, combined=False)}

        fluid_faces, solid_faces = boundary(fluid_volumes), boundary(solid_volumes)
        interface = sorted(fluid_faces & solid_faces)
        if not interface:
            raise MeshingFailed("the solids do not touch the flow domain (no fluid-solid interface)")
        surfaces = [tag for _, tag in gmsh.model.getEntities(2)]
        _apply_sizing(gmsh, MeshOptions(**{**options.__dict__, "refinements": [
            *options.refinements,
            {"selectors": [], "size": options.size, "distance": 0.0, "surfaces": interface,
             "grading": max(domain_high[i] - domain_low[i] for i in range(3)) / 4},
        ], "size": 2.5 * options.size}), surfaces)
        _generate(gmsh, MeshOptions(**{**options.__dict__, "order": 1, "element": "tet"}))
        box_patches=_box_surfaces(gmsh, sorted(fluid_faces - set(interface)), domain_low, domain_high)
        solid_outer = sorted(solid_faces - set(interface))
        heater_faces = sorted({_closest_surface(gmsh, solid_outer, selector)[0] for selector in heater or []}) if solid_outer else []
        regions = {
            "fluid": (fluid_volumes, {
                "inlet": ("inlet", box_patches["inlet"]), "outlet": ("outlet", box_patches["outlet"]),
                "sides": ("slip", box_patches["sides"]), "walls": ("wall", box_patches["walls"]),
                "fluid_to_solid": ("interface", interface),
            }),
            "solid": (solid_volumes, {
                "solid_to_fluid": ("interface", interface), "heater": ("heatflux", heater_faces),
                "adiabatic": ("adiabatic", [t for t in solid_outer if t not in heater_faces]),
            }),
        }
        meshes = {}
        for name, (volumes, groups) in regions.items():
            meshes[name] = _extract_region(gmsh, volumes, {k: v for k, v in groups.items() if v[1]})
            meshes[name].statistics.update(mesher="gmsh", region=name, interface_faces=len(interface))
        return meshes


def _extract_region(gmsh: Any, volumes: list[int], groups: dict[str, tuple[str, list[int]]]) -> VolumeMesh:
    node_tags, coordinates, _ = gmsh.model.mesh.getNodes()
    lookup = np.full(int(node_tags.max()) + 1, -1, dtype=np.int64)
    lookup[node_tags.astype(np.int64)] = np.arange(len(node_tags))
    nodes = coordinates.reshape(-1, 3)
    cells: dict[str, list[np.ndarray]] = {}
    for volume in volumes:
        for kind_code, _, element_nodes in zip(*gmsh.model.mesh.getElements(3, volume)):
            width = gmsh.model.mesh.getElementProperties(kind_code)[3]
            cells.setdefault(GMSH_TYPES[int(kind_code)], []).append(lookup[element_nodes.astype(np.int64)].reshape(-1, width))
    merged = {key: np.concatenate(value) for key, value in cells.items()}
    used = np.unique(np.concatenate([conn.ravel() for conn in merged.values()]))
    remap = np.full(len(nodes), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    boundaries = []
    for name, (kind, surfaces) in groups.items():
        elements: dict[str, list[np.ndarray]] = {}
        for surface in surfaces:
            for kind_code, _, element_nodes in zip(*gmsh.model.mesh.getElements(2, surface)):
                width = gmsh.model.mesh.getElementProperties(kind_code)[3]
                elements.setdefault(GMSH_TYPES[int(kind_code)], []).append(remap[lookup[element_nodes.astype(np.int64)]].reshape(-1, width))
        boundaries.append(BoundaryGroup(name, kind, {key: np.concatenate(value) for key, value in elements.items()}))
    return VolumeMesh(nodes=np.ascontiguousarray(nodes[used]), cells={k: remap[v] for k, v in merged.items()}, boundaries=boundaries, order=1)
