"""Exact B-Rep parametric kernel (OpenCASCADE, through gmsh's OCC binding).

Import
    STEP/IGES are read by OpenCASCADE (XCAF labels on), so assembly placements
    are applied and part paths/colors are kept. Each model's B-Rep is stored as
    `<id>.brep`; bodies map to its volumes in order.

Regeneration
    Geometry is rebuilt from the stored B-Rep on every parameter change by
    replaying operations on the exact topology - never by moving mesh vertices:

    * body extents bound to parameters -> push-pull of the extreme planar faces
      (extrude + fuse, or cut), so holes and fillets keep their true shape; an
      exact affine dilation is used only when no planar extreme face exists
    * `extrude`/`offset_face` -> face prism fused or cut (signed distance)
    * `fillet`/`chamfer` -> OpenCASCADE rolling-ball fillets / chamfers
    * `split` -> fragment with a cutting plane (two exact solids)
    * `scale` -> exact dilation; `boolean` -> exact cut/fuse with a tool body

    Faces and edges carry **persistent names**: when a feature is created, the
    referenced entity is fingerprinted on the geometry it was picked from
    (surface/curve type, outward normal, edge count, and the types and normals
    of its neighbours) and ranked among entities with the same fingerprint
    (by offset along the normal). Regeneration resolves the name first, so a
    dimension change that moves the face past a parallel one still modifies
    the right face. When the topology itself changed (the fingerprint count
    differs), the geometric selector (point and normal) is used instead.

Export
    The regenerated workspace is written as ISO 10303-21 STEP, IGES, or BREP.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any

from paramfabric.geometry.importer import GeometryImportError, import_geometry
from paramfabric.geometry.model import AXES, Body, Face, GeometryModel
from paramfabric.gmsh_session import GmshSession, import_shapes, write_model
from paramfabric.gmsh_session import available as gmsh_available

EXACT_SUFFIXES = {".step", ".stp", ".iges", ".igs"}
_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_CACHE_SIZE = 32
_SURFACE_TYPES = {
    "Plane": "plane", "Cylinder": "cylinder", "Cone": "cone", "Sphere": "sphere", "Torus": "torus",
    "BSplineSurface": "bspline", "BezierSurface": "bezier", "SurfaceOfRevolution": "revolution",
    "SurfaceOfExtrusion": "extrusion", "OffsetSurface": "offset", "Trimmed surface": "trimmed",
}


class ExactKernelUnavailable(RuntimeError):
    """Raised when an exact-geometry operation is requested without the kernel"""


class RegenerationError(ValueError):
    """Raised when a feature cannot be rebuilt on the exact geometry"""


def available() -> bool:
    return gmsh_available()


# import & persistence


def _length_unit(suffix: str, text: str) -> str:
    if suffix in {".step", ".stp"}:
        match = re.search(r"SI_UNIT\s*\(\s*(\.\w+\.|\$)\s*,\s*\.METRE\.\s*\)", text, re.IGNORECASE)
        if match:
            return {".MILLI.": "mm", ".CENTI.": "cm", "$": "m", ".KILO.": "km", ".MICRO.": "um"}.get(match.group(1).upper(), "mm")
        if re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'INCH'", text, re.IGNORECASE):
            return "inch"
        return "mm"
    lines = [line for line in text.splitlines() if len(line) >= 73 and line[72] == "G"]
    global_text = "".join(line[:72] for line in lines)
    fields = re.split(r",(?=(?:[^H]|\d+H)*)", global_text)
    try:
        flag = int(float(fields[13]))
    except (IndexError, ValueError):
        flag = 2
    return {1: "inch", 2: "mm", 4: "ft", 6: "m", 10: "cm"}.get(flag, "mm")


def model_brep(store: Any, model_id: str) -> Path:
    return store.directory / f"{model_id}.brep"


def import_cad(filename: str, data: bytes, units: str | None = None) -> GeometryModel:
    """Exact import for STEP/IGES when the kernel is present; faceted otherwise"""
    suffix = PurePath(filename).suffix.lower()
    if suffix not in EXACT_SUFFIXES or not available():
        return import_geometry(filename, data, units)
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / f"source{suffix}"
        source.write_bytes(data)
        try:
            with GmshSession() as gmsh:
                gmsh.option.setNumber("Geometry.OCCImportLabels", 1)
                gmsh.model.add("import")
                volumes=[tag for _, tag in import_shapes(gmsh, source)]
                warnings: list[str] = []
                if not volumes:
                    raise GeometryImportError("no closed solids were found; the file may contain only surfaces or curves")
                bodies = []
                _surface_mesh(gmsh, volumes)
                for index, tag in enumerate(volumes, start=1):
                    label = gmsh.model.getEntityName(3, tag) or ""
                    parts=[part for part in label.split("/") if part and part.lower() != "shapes"]
                    try:
                        r, g, b, _ = gmsh.model.getColor(3, tag)
                        color=[r / 255, g / 255, b / 255] if (r, g, b) != (0, 0, 255) else None
                    except Exception:  # noqa: BLE001 - colorless entities
                        color = None
                    body = _tessellate_volume(gmsh, tag, f"b{index}", parts[-1] if parts else f"{PurePath(filename).stem} body {index}")
                    body.path = "/".join(parts)
                    body.color = color
                    bodies.append(body)
                brep=Path(scratch) / "model.brep"
                gmsh.write(str(brep))
                brep_bytes = brep.read_bytes()
        except GeometryImportError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to the faceted reader
            model = import_geometry(filename, data, units)
            model.warnings.append(f"Exact kernel could not read the file ({exc}); using the faceted reader.")
            return model
    assemblies = sorted({body.path.rsplit("/", 1)[0] for body in bodies if "/" in body.path})
    if len(volumes) > 1 and not assemblies:
        warnings.append("Multiple solids without assembly labels.")
    model=GeometryModel(
        id=uuid.uuid4().hex[:12], name=bodies[0].path.split("/")[0] if bodies[0].path else PurePath(filename).stem,
        filename=filename, format=suffix.lstrip(".").replace("stp", "step").replace("igs", "iges"),
        length_unit=_length_unit(suffix, data.decode("latin-1", "replace")),
        bodies=bodies, warnings=warnings, assembly=assemblies[0] if assemblies else "",
        created_at=datetime.now(timezone.utc).isoformat(), kernel="occ",
    )
    model._brep_bytes = brep_bytes  # type: ignore[attr-defined]
    return model


def store_source(store: Any, model: GeometryModel, data: bytes) -> None:
    """Persist the original file and, for exact models, the B-Rep"""
    store.directory.mkdir(parents=True, exist_ok=True)
    suffix=PurePath(model.filename).suffix.lower() or ".bin"
    (store.directory / f"{model.id}{suffix}").write_bytes(data)
    brep = getattr(model, "_brep_bytes", None)
    if brep is not None:
        model_brep(store, model.id).write_bytes(brep)


# tessellation


def _surface_mesh(gmsh: Any, volumes: list[int]) -> None:
    box = _bbox(gmsh, volumes)
    diagonal = math.dist(box[:3], box[3:]) or 1.0
    gmsh.option.setNumber("Mesh.MeshSizeMax", diagonal / 30)
    gmsh.option.setNumber("Mesh.MeshSizeMin", diagonal / 1500)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 24)
    gmsh.option.setNumber("Mesh.Algorithm", 6)
    gmsh.option.setNumber("Mesh.SubdivisionAlgorithm", 0)
    gmsh.option.setNumber("Mesh.ElementOrder", 1)
    try:
        gmsh.model.mesh.generate(2)
    except Exception as exc:  # noqa: BLE001 - gmsh raises bare Exception
        raise RegenerationError(f"display tessellation failed: {exc}") from exc


def _tessellate_volume(gmsh: Any, volume: int, body_id: str, name: str) -> Body:
    occ = gmsh.model.occ
    surfaces = [abs(tag) for _, tag in gmsh.model.getBoundary([(3, volume)], oriented=False, combined=False)]
    curves = sorted({abs(tag) for _, tag in gmsh.model.getBoundary([(2, s) for s in surfaces], oriented=False, combined=False)})
    body = Body(id=body_id, name=name, kind="solid")
    exact: dict[int, tuple[float, list[float]]] = {}
    for face_index, surface in enumerate(surfaces):
        kind = _SURFACE_TYPES.get(gmsh.model.getType(2, surface), gmsh.model.getType(2, surface).lower())
        node_tags, coords, _ = gmsh.model.mesh.getNodes(2, surface, includeBoundary=True)
        lookup = {int(tag): coords[3 * i : 3 * i + 3] for i, tag in enumerate(node_tags)}
        element_types, _, element_nodes = gmsh.model.mesh.getElements(2, surface)
        for kind_code, nodes in zip(element_types, element_nodes):
            width = 3 if kind_code == 2 else gmsh.model.mesh.getElementProperties(kind_code)[3]
            for i in range(0, len(nodes), width):
                for node in nodes[i : i + 3]:
                    body.triangles.extend(float(c) for c in lookup[int(node)])
                body.face_ids.append(face_index)
        centre = occ.getCenterOfMass(2, surface)
        exact[face_index] = (float(occ.getMass(2, surface)), [float(c) for c in centre])
        face = Face(id=face_index, name=f"Face {face_index + 1}", surface=kind)
        if kind == "plane":
            _, parametric = gmsh.model.getClosestPoint(2, surface, list(centre))
            face.normal = [round(float(c), 9) for c in gmsh.model.getNormal(surface, parametric)]
        face.source_tag = surface  # type: ignore[attr-defined]
        body.faces.append(face)
    for curve in curves:
        node_tags, coords, parametric = gmsh.model.mesh.getNodes(1, curve, includeBoundary=True)
        if len(node_tags) < 2:
            continue
        order = sorted(range(len(node_tags)), key=lambda i: parametric[i] if i < len(parametric) else 0.0)
        body.edges.append([float(coords[3 * i + k]) for i in order for k in range(3)])
    body.vertex_count = len({tag for _, tag in gmsh.model.getBoundary([(1, c) for c in curves], oriented=False, combined=True)})
    normals = {face.id: face.normal for face in body.faces}
    body.finalize()
    for face in body.faces:  # exact B-Rep properties win over tessellation estimates
        face.area, face.centroid = exact[face.id]
        face.normal = normals[face.id] or face.normal
    return body


# regeneration


def _volume_faces(gmsh: Any, volume: int) -> list[int]:
    return [abs(tag) for _, tag in gmsh.model.getBoundary([(3, volume)], oriented=False, combined=False)]


# persistent naming

def _rounded(vector: Any) -> list[float]:
    return [0.0 if abs(x) < 5e-4 else round(float(x), 3) for x in vector]


def _face_frame(gmsh: Any, volume: int, surface: int) -> tuple[list[float], list[float]]:
    centre = list(gmsh.model.occ.getCenterOfMass(2, surface))
    point, parametric = gmsh.model.getClosestPoint(2, surface, centre)
    normal = list(gmsh.model.getNormal(surface, parametric))
    box=gmsh.model.getBoundingBox(3, volume)
    step = 1e-5 * max(math.dist(box[:3], box[3:]), 1e-9)
    try:
        if gmsh.model.isInside(3, volume, [p + step * n for p, n in zip(point, normal)]):
            normal = [-n for n in normal]
    except Exception:  # noqa: BLE001 - older gmsh without isInside: OCC normals are outward
        pass
    return centre, normal


def _face_token(gmsh: Any, volume: int, surface: int) -> str:
    kind = gmsh.model.getType(2, surface)
    if kind == "Plane":
        return f"{kind}{_rounded(_face_frame(gmsh, volume, surface)[1])}"
    return kind


def _digest(payload: Any) -> str:
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def face_signature(gmsh: Any, volume: int, surface: int) -> str:
    """Topological fingerprint of a face: invariant under dimension changes"""
    faces = set(_volume_faces(gmsh, volume))
    curves = {abs(tag) for _, tag in gmsh.model.getBoundary([(2, surface)], oriented=False, combined=False)}
    neighbours=[]
    for curve in curves:
        upward, _ = gmsh.model.getAdjacencies(1, curve)
        neighbours += [_face_token(gmsh, volume, int(other)) for other in upward if int(other) != surface and int(other) in faces]
    return _digest([_face_token(gmsh, volume, surface), len(curves), sorted(neighbours)])


def _face_rank(gmsh: Any, volume: int, surface: int) -> tuple[Any, ...]:
    centre, normal = _face_frame(gmsh, volume, surface)
    return (round(sum(c * n for c, n in zip(centre, normal)), 9), *(round(c, 9) for c in centre))


def edge_signature(gmsh: Any, volume: int, curve: int) -> str:
    faces = set(_volume_faces(gmsh, volume))
    upward, _ = gmsh.model.getAdjacencies(1, curve)
    return _digest([gmsh.model.getType(1, curve), sorted(_face_token(gmsh, volume, int(s)) for s in upward if int(s) in faces)])


def _edge_rank(gmsh: Any, volume: int, curve: int) -> tuple[float, ...]:
    return tuple(round(c, 9) for c in gmsh.model.occ.getCenterOfMass(1, curve))


def _volume_curves(gmsh: Any, volume: int) -> list[int]:
    return sorted({abs(tag) for _, tag in gmsh.model.getBoundary([(2, s) for s in _volume_faces(gmsh, volume)], oriented=False, combined=False)})


def _name(gmsh: Any, candidates: list[tuple[int, int]], chosen: tuple[int, int], signature: Any, rank: Any) -> dict[str, Any]:
    digest=signature(gmsh, *chosen)
    same = sorted((c for c in candidates if signature(gmsh, *c) == digest), key=lambda c: rank(gmsh, *c))
    return {"hash": digest, "rank": same.index(chosen), "count": len(same)}


def _resolve(gmsh: Any, candidates: list[tuple[int, int]], name: dict[str, Any], signature: Any, rank: Any) -> tuple[int, int] | None:
    same = [c for c in candidates if signature(gmsh, *c) == name["hash"]]
    if len(same) != name.get("count") or not 0 <= name.get("rank", -1) < len(same):
        return None  # topology changed: fall back to the geometric selector
    return sorted(same, key=lambda c: rank(gmsh, *c))[name["rank"]]


def _match_face(gmsh: Any, volumes: list[int], selector: dict[str, Any]) -> tuple[int, int]:
    if selector.get("topology"):
        candidates = [(volume, surface) for volume in volumes for surface in _volume_faces(gmsh, volume)]
        found = _resolve(gmsh, candidates, selector["topology"], face_signature, _face_rank)
        if found is not None:
            return found
    return _match_face_geometric(gmsh, volumes, selector)


def _match_face_geometric(gmsh: Any, volumes: list[int], selector: dict[str, Any]) -> tuple[int, int]:
    point, normal = selector["point"], selector.get("normal")
    best: tuple[float, int, int] | None = None
    for volume in volumes:
        for surface in _volume_faces(gmsh, volume):
            closest, parametric = gmsh.model.getClosestPoint(2, surface, list(point))
            distance = math.dist(closest, point)
            if normal is not None:
                if gmsh.model.getType(2, surface) != "Plane" and selector.get("planar"):
                    continue
                surface_normal=gmsh.model.getNormal(surface, parametric)
                if abs(sum(a * b for a, b in zip(surface_normal, normal))) < 0.95:
                    continue
            if best is None or distance < best[0]:
                best = (distance, volume, surface)
    if best is None:
        raise RegenerationError("a referenced face no longer exists after regeneration")
    return best[1], best[2]


def _match_edges(gmsh: Any, volume: int, selectors: list[dict[str, Any]]) -> list[int]:
    curves = _volume_curves(gmsh, volume)
    chosen = []
    for selector in selectors:
        if selector.get("topology"):
            found = _resolve(gmsh, [(volume, c) for c in curves], selector["topology"], edge_signature, _edge_rank)
            if found is not None:
                chosen.append(found[1])
                continue
        point = selector["point"]
        best = min(curves, key=lambda curve: math.dist(gmsh.model.getClosestPoint(1, curve, list(point))[0], point))
        chosen.append(best)
    if not chosen:
        raise RegenerationError("no edges referenced")
    return sorted(set(chosen))


def _outward(gmsh: Any, volume: int, surface: int) -> list[float]:
    centre = gmsh.model.occ.getCenterOfMass(2, surface)
    _, parametric = gmsh.model.getClosestPoint(2, surface, list(centre))
    normal = list(gmsh.model.getNormal(surface, parametric))
    inside=gmsh.model.occ.getCenterOfMass(3, volume)
    if sum((c - i) * n for c, i, n in zip(centre, inside, normal)) < 0:
        normal = [-n for n in normal]
    return normal


def _push_pull(gmsh: Any, volumes: list[int], volume: int, surface: int, distance: float) -> list[int]:
    if abs(distance) < 1e-12:
        return volumes
    occ = gmsh.model.occ
    normal = _outward(gmsh, volume, surface)
    copy = occ.copy([(2, surface)])
    prism = [entity for entity in occ.extrude(copy, *(n * distance for n in normal)) if entity[0] == 3]
    occ.synchronize()
    if distance > 0:
        result, _ = occ.fuse([(3, volume)], prism)
    else:
        result, _ = occ.cut([(3, volume)], prism)
    occ.synchronize()
    new = [tag for dim, tag in result if dim == 3]
    if not new:
        raise RegenerationError("push-pull removed the whole body")
    return [tag for tag in volumes if tag != volume] + new


def _extreme_planar_faces(gmsh: Any, volume: int, axis: int) -> list[int]:
    occ = gmsh.model.occ
    box = occ.getBoundingBox(3, volume)
    high, low = box[3 + axis], box[axis]
    tolerance = 1e-6 * max(abs(high - low), 1e-9)
    faces=[]
    for surface in _volume_faces(gmsh, volume):
        if gmsh.model.getType(2, surface) != "Plane":
            continue
        normal = _outward(gmsh, volume, surface)
        face_box = occ.getBoundingBox(2, surface)
        if normal[axis] > 0.999 and abs(face_box[axis] - high) < tolerance and abs(face_box[3 + axis] - high) < tolerance:
            faces.append(surface)
    return faces


def _bbox(gmsh: Any, volumes: list[int]) -> list[float]:
    boxes = [gmsh.model.occ.getBoundingBox(3, tag) for tag in volumes]
    return [min(b[i] for b in boxes) for i in range(3)] + [max(b[3 + i] for b in boxes) for i in range(3)]


def _apply_extent(gmsh: Any, body_volumes: list[int], axis: int, target: float) -> tuple[list[int], str]:
    box=_bbox(gmsh, body_volumes)
    current = box[3 + axis] - box[axis]
    delta = target - current
    if abs(delta) <= 1e-9 * max(current, 1e-9):
        return body_volumes, "unchanged"
    if target <= 0:
        raise RegenerationError("dimension parameters must be positive")
    if len(body_volumes) == 1:
        faces = _extreme_planar_faces(gmsh, body_volumes[0], axis)
        if faces and (delta > 0 or -delta < 0.999 * current):
            volumes = body_volumes
            for surface in faces:
                current_volume=volumes[0]
                volumes = _push_pull(gmsh, [current_volume], current_volume, surface, delta)
                if len(volumes) != 1:
                    break
            return volumes, "push-pull"
    factors=[1.0, 1.0, 1.0]
    factors[axis] = target / current
    gmsh.model.occ.dilate([(3, tag) for tag in body_volumes], box[0], box[1], box[2], *factors)
    gmsh.model.occ.synchronize()
    return body_volumes, "dilate"


def _regeneration_key(model: GeometryModel, features: list[dict[str, Any]], values: dict[str, float]) -> str:
    names = sorted({name for body in model.bodies for name in body.dimension_parameters.values()} |
                   {name for feature in features for name in feature.get("params", {}).values()})
    payload = {
        "model": model.id, "bindings": [body.dimension_parameters for body in model.bodies], "features": features,
        "values": {name: round(values.get(name, float("nan")), 12) for name in names},
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def regenerate(store: Any, model: GeometryModel, features: list[dict[str, Any]], values: dict[str, float], *, tessellate: bool = True) -> dict[str, Any]:
    """Rebuild one exact model; returns per-body tessellations, B-Rep file, and log"""
    if model.kernel != "occ":
        raise ExactKernelUnavailable("model has no exact B-Rep")
    own = [f for f in features if str(f.get("body", "")).startswith(f"{model.id}/") or str(f.get("tool", "")).startswith(f"{model.id}/")]
    key = _regeneration_key(model, own, values)
    cached = _CACHE.get(key)
    if cached is not None and (cached["bodies"] or not tessellate):
        _CACHE.move_to_end(key)
        return cached
    brep = model_brep(store, model.id)
    if not brep.is_file():
        raise ExactKernelUnavailable(f"exact B-Rep for {model.filename} is missing")
    cache_dir = store.directory / "regenerated"
    cache_dir.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    errors: list[str] = []
    with GmshSession() as gmsh:
        gmsh.model.add("regenerate")
        volumes = [tag for _, tag in import_shapes(gmsh, brep)]
        body_volumes: dict[str, list[int]] = {body.id: [volumes[i]] for i, body in enumerate(model.bodies) if i < len(volumes)}
        # 1. Associativity: parameter-driven extents.
        for body in model.bodies:
            for axis_name, parameter in body.dimension_parameters.items():
                if parameter not in values or body.id not in body_volumes:
                    if parameter not in values:
                        errors.append(f"{body.name}: bound parameter {parameter!r} is missing or non-numeric")
                    continue
                try:
                    body_volumes[body.id], method = _apply_extent(gmsh, body_volumes[body.id], AXES.index(axis_name), values[parameter])
                    if method != "unchanged":
                        log.append(f"{body.name}: {axis_name.upper()} = {values[parameter]:.6g} m by {method}")
                except (RegenerationError, Exception) as exc:  # noqa: BLE001 - OCC failures are reported per feature
                    errors.append(f"{body.name} {axis_name.upper()} extent: {exc}")
        # 2. Features, in creation order.
        for feature in own:
            try:
                _apply_feature(gmsh, model, feature, values, body_volumes, log)
            except (RegenerationError, KeyError, Exception) as exc:  # noqa: BLE001
                errors.append(f"{feature.get('name', feature.get('type'))}: {exc}")
        gmsh.model.occ.synchronize()
        out = cache_dir / f"{key}.brep"
        gmsh.write(str(out))
        result: dict[str, Any] = {"brep": str(out), "log": log, "errors": errors, "bodies": {}}
        owners = {tag: body for body, tags in body_volumes.items() for tag in tags}
        result["volume_bodies"] = [owners.get(tag) for _, tag in sorted(gmsh.model.getEntities(3))]
        all_volumes = [tag for tags in body_volumes.values() for tag in tags]
        result["bbox"] = _bbox(gmsh, all_volumes) if all_volumes else None
        if tessellate and all_volumes:
            _surface_mesh(gmsh, all_volumes)
            for body in model.bodies:
                tags = body_volumes.get(body.id, [])
                if not tags:
                    continue
                merged: Body | None = None
                for tag in tags:
                    piece = _tessellate_volume(gmsh, tag, body.id, body.name)
                    if merged is None:
                        merged = piece
                    else:
                        offset = len(merged.faces)
                        for face in piece.faces:
                            face.id += offset
                        merged.faces.extend(piece.faces)
                        merged.triangles.extend(piece.triangles)
                        merged.face_ids.extend(face_id + offset for face_id in piece.face_ids)
                        merged.edges.extend(piece.edges)
                assert merged is not None
                merged.finalize()
                result["bodies"][body.id] = {
                    "triangles": merged.triangles, "face_ids": merged.face_ids, "edges": merged.edges,
                    "faces": [{k: v for k, v in face.__dict__.items() if k != "source_tag"} for face in merged.faces],
                    "bbox_min": merged.bbox_min, "bbox_max": merged.bbox_max, "volumes": len(tags),
                    "volume": float(sum(gmsh.model.occ.getMass(3, tag) for tag in tags)),
                }
    _CACHE[key] = result
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return result


def _apply_feature(gmsh: Any, model: GeometryModel, feature: dict[str, Any], values: dict[str, float],
                   body_volumes: dict[str, list[int]], log: list[str]) -> None:
    occ = gmsh.model.occ
    kind = feature["type"]
    body_id = str(feature.get("body", "")).partition("/")[2]
    params = feature.get("params", {})
    if kind == "flow_volume" or body_id not in body_volumes:
        return
    volumes = body_volumes[body_id]
    if kind == "scale":
        factor = values[params["factor"]]
        if factor <= 0:
            raise RegenerationError("scale factor must be positive")
        box = _bbox(gmsh, volumes)
        occ.dilate([(3, tag) for tag in volumes], box[0], box[1], box[2], factor, factor, factor)
        occ.synchronize()
    elif kind in {"extrude", "offset_face"}:
        distance = values[params["depth" if kind == "extrude" else "distance"]]
        if kind == "extrude" and distance <= 0:
            raise RegenerationError("extrude depth must be positive")
        if feature.get("selector"):
            volume, surface = _match_face(gmsh, volumes, feature["selector"] | {"planar": True})
        else:  # legacy bounding-face reference such as "+x"
            face = feature.get("face", "+x")
            axis = "xyz".index(face[1])
            if face[0] == "-":
                raise RegenerationError("bounding-face extrusions on negative sides need a face selection")
            candidates = _extreme_planar_faces(gmsh, volumes[0], axis)
            if not candidates:
                raise RegenerationError(f"no planar face on the {face} side")
            volume, surface = volumes[0], candidates[0]
        body_volumes[body_id] = _push_pull(gmsh, volumes, volume, surface, distance)
    elif kind in {"fillet", "chamfer"}:
        size = values[params["radius" if kind == "fillet" else "distance"]]
        if size <= 0:
            raise RegenerationError(f"{kind} size must be positive")
        volume = volumes[0]
        curves = _match_edges(gmsh, volume, feature["edges"])
        if kind == "fillet":
            result = occ.fillet([volume], curves, [size])
        else:
            surfaces = []
            for curve in curves:
                upward, _ = gmsh.model.getAdjacencies(1, curve)
                surfaces.append(int(upward[0]))
            result = occ.chamfer([volume], curves, surfaces, [size])
        occ.synchronize()
        new = [tag for dim, tag in result if dim == 3]
        if not new:
            raise RegenerationError(f"{kind} failed (size too large for the adjacent faces?)")
        body_volumes[body_id] = [tag for tag in volumes if tag != volume] + new
    elif kind == "split":
        fraction = values[params["fraction"]]
        if not 0 < fraction < 1:
            raise RegenerationError("split fraction must be between 0 and 1")
        axis = "xyz".index(feature["axis"])
        box = _bbox(gmsh, volumes)
        position = box[axis] + fraction * (box[3 + axis] - box[axis])
        span = max(box[3 + i] - box[i] for i in range(3)) * 4
        centre = [(box[i] + box[3 + i]) / 2 for i in range(3)]
        corner = [centre[i] - span / 2 for i in range(3)]
        corner[axis] = position
        if axis == 0:
            plane = occ.addRectangle(0, 0, 0, span, span)
            occ.rotate([(2, plane)], 0, 0, 0, 0, 1, 0, math.pi / 2)
            occ.translate([(2, plane)], position, corner[1], corner[2] + span)
        elif axis == 1:
            plane = occ.addRectangle(0, 0, 0, span, span)
            occ.rotate([(2, plane)], 0, 0, 0, 1, 0, 0, math.pi / 2)
            occ.translate([(2, plane)], corner[0], position, corner[2])
        else:
            plane = occ.addRectangle(corner[0], corner[1], position, span, span)
        result, _ = occ.fragment([(3, tag) for tag in volumes], [(2, plane)])
        occ.synchronize()
        new = [tag for dim, tag in result if dim == 3]
        loose = [(dim, tag) for dim, tag in result if dim == 2 and not gmsh.model.getAdjacencies(2, tag)[0].size]
        if loose:
            occ.remove(loose, recursive=True)
            occ.synchronize()
        body_volumes[body_id] = new
        log.append(f"split into {len(new)} solids at {position:.6g} m")
    elif kind == "boolean":
        tool_id=str(feature.get("tool", "")).partition("/")[2]
        tool_volumes = body_volumes.get(tool_id)
        if not tool_volumes:
            raise RegenerationError("boolean tool body is not in this model")
        tools = [(3, tag) for tag in tool_volumes]
        if feature.get("op") == "union":
            result, _ = occ.fuse([(3, tag) for tag in volumes], tools)
            body_volumes[tool_id] = []
        else:
            result, _ = occ.cut([(3, tag) for tag in volumes], tools, removeTool=False)
        occ.synchronize()
        body_volumes[body_id] = [tag for dim, tag in result if dim == 3]


def annotate_feature(store: Any, models: list[GeometryModel], features: list[dict[str, Any]], feature: dict[str, Any],
                     quantities: dict[str, Any]) -> bool:
    """Give a new feature's face/edge references persistent topological names.

    The references are resolved on the geometry the user picked them from:
    the model regenerated with every earlier feature.
    """
    if not feature.get("selector") and not feature.get("edges"):
        return False
    model_id, _, body_id = str(feature.get("body", "")).partition("/")
    model = next((m for m in models if m.id == model_id), None)
    if model is None or model.kernel != "occ" or not available():
        return False
    from paramfabric.geometry.scene import si_values

    result = regenerate(store, model, features, si_values(quantities), tessellate=False)
    with GmshSession() as gmsh:
        gmsh.model.add("annotate")
        volumes = sorted(tag for _, tag in import_shapes(gmsh, Path(result["brep"])))
        owners = result.get("volume_bodies", [])
        if len(owners) != len(volumes):
            return False
        mine = [tag for tag, owner in zip(volumes, owners) if owner == body_id]
        if not mine:
            return False
        if feature.get("selector"):
            selector = feature["selector"] | {"planar": feature["type"] in {"extrude", "offset_face"}}
            chosen=_match_face_geometric(gmsh, mine, selector)
            candidates = [(volume, surface) for volume in mine for surface in _volume_faces(gmsh, volume)]
            feature["selector"]["topology"] = _name(gmsh, candidates, chosen, face_signature, _face_rank)
        if feature.get("edges"):
            volume = mine[0]
            curves = _volume_curves(gmsh, volume)
            for selector in feature["edges"]:
                point = selector["point"]
                curve = min(curves, key=lambda c: math.dist(gmsh.model.getClosestPoint(1, c, list(point))[0], point))
                selector["topology"] = _name(gmsh, [(volume, c) for c in curves], (volume, curve), edge_signature, _edge_rank)
    return True


def _entity_volumes(store: Any, gmsh: Any, model: GeometryModel, features: list[dict[str, Any]], values: dict[str, float]) -> dict[str, list[int]]:
    result = regenerate(store, model, features, values, tessellate=False)
    volumes = sorted(tag for _, tag in import_shapes(gmsh, Path(result["brep"])))
    owners = result.get("volume_bodies", [])
    if len(owners) != len(volumes):
        raise RegenerationError("the regenerated bodies do not match the model")
    grouped: dict[str, list[int]] = {}
    for tag, owner in zip(volumes, owners):
        grouped.setdefault(owner, []).append(tag)
    return grouped


def _exact_items(models: list[GeometryModel], items: list[dict[str, Any]]) -> dict[str, tuple[GeometryModel, list[dict[str, Any]]]]:
    by_model: dict[str, tuple[GeometryModel, list[dict[str, Any]]]] = {}
    for item in items:
        model_id = item["body"].partition("/")[0]
        model = next((m for m in models if m.id == model_id), None)
        if model is not None and model.kernel == "occ":
            by_model.setdefault(model_id, (model, []))[1].append(item)
    return by_model


def name_entities(store: Any, models: list[GeometryModel], features: list[dict[str, Any]], kind: str, items: list[dict[str, Any]],
                  quantities: dict[str, Any]) -> None:
    """Add persistent topological names to named-selection items, on the current geometry"""
    from paramfabric.geometry.scene import si_values

    if not available():
        return
    values = si_values(quantities)
    for model, members in _exact_items(models, items).values():
        with GmshSession() as gmsh:
            gmsh.model.add("name-entities")
            grouped=_entity_volumes(store, gmsh, model, features, values)
            for item in members:
                mine = grouped.get(item["body"].partition("/")[2])
                if not mine:
                    continue
                selector = item["selector"]
                if kind == "face":
                    chosen=_match_face_geometric(gmsh, mine, selector)
                    candidates = [(volume, surface) for volume in mine for surface in _volume_faces(gmsh, volume)]
                    selector["topology"] = _name(gmsh, candidates, chosen, face_signature, _face_rank)
                else:
                    volume = mine[0]
                    curves = _volume_curves(gmsh, volume)
                    point = selector["point"]
                    curve = min(curves, key=lambda c: math.dist(gmsh.model.getClosestPoint(1, c, list(point))[0], point))
                    selector["topology"] = _name(gmsh, [(volume, c) for c in curves], (volume, curve), edge_signature, _edge_rank)


def resolve_entities(store: Any, models: list[GeometryModel], features: list[dict[str, Any]], kind: str, items: list[dict[str, Any]],
                     quantities: dict[str, Any]) -> list[dict[str, Any]]:
    """Current point and outward normal of named-selection items, in the order given"""
    from paramfabric.geometry.scene import si_values

    stored = {id(item): {"body": item["body"], **{k: v for k, v in item["selector"].items() if k in {"point", "normal"}}} for item in items}
    if not available():
        return list(stored.values())
    values = si_values(quantities)
    for model, members in _exact_items(models, items).values():
        with GmshSession() as gmsh:
            gmsh.model.add("resolve-entities")
            grouped=_entity_volumes(store, gmsh, model, features, values)
            for item in members:
                mine = grouped.get(item["body"].partition("/")[2])
                if not mine:
                    continue
                selector = item["selector"]
                if kind == "face":
                    volume, surface = _match_face(gmsh, mine, selector)
                    centre, normal = _face_frame(gmsh, volume, surface)
                    stored[id(item)] |= {"point": [float(v) for v in centre], "normal": [float(v) for v in normal]}
                else:
                    curve = _match_edges(gmsh, mine[0], [selector])[0]
                    stored[id(item)]["point"] = [float(v) for v in gmsh.model.occ.getCenterOfMass(1, curve)]
    return [stored[id(item)] for item in items]


# scene integration, meshing input, export


def attach_tessellations(store: Any, models: list[GeometryModel], settings: dict[str, Any], quantities: dict[str, Any],
                         scene: dict[str, Any]) -> None:
    """Replace affine previews with exact regenerated geometry for OCC models"""
    exact_models = [model for model in models if model.kernel == "occ"]
    if not exact_models or not available():
        return
    from paramfabric.geometry.scene import si_values

    values = si_values(quantities)
    by_key = {entry["key"]: entry for entry in scene["bodies"]}
    for model in exact_models:
        result = regenerate(store, model, settings.get("features", []), values)
        scene["errors"] = [e for e in scene["errors"] if not any(e.startswith(f"{body.name}:") for body in model.bodies)] + result["errors"]
        for body in model.bodies:
            entry = by_key.get(f"{model.id}/{body.id}")
            data = result["bodies"].get(body.id)
            if entry is None:
                continue
            if data is None:
                entry["role"] = "consumed"
                continue
            entry.update(
                exact=True, anchor=[0.0, 0.0, 0.0], scale=[1.0, 1.0, 1.0], split=None,
                bbox_min=data["bbox_min"], bbox_max=data["bbox_max"], tessellation=data,
                revision=hashlib.sha1(result["brep"].encode()).hexdigest()[:12],
            )
        scene["derived"] = [entry for entry in scene["derived"] if not str(entry.get("source", "")).startswith(f"{model.id}/")]
        scene.setdefault("regeneration_log", []).extend(result["log"])
    _refresh_domain(scene, settings, values)


def _refresh_domain(scene: dict[str, Any], settings: dict[str, Any], values: dict[str, float]) -> None:
    solids = [entry for entry in scene["bodies"] if entry["role"] == "solid"] + scene["derived"]
    feature = next((f for f in settings.get("features", []) if f.get("type") == "flow_volume"), None)
    if not feature or not solids or not scene.get("flow_domain"):
        return
    try:
        upstream, downstream, lateral = (values[feature["params"][key]] for key in ("upstream", "downstream", "lateral"))
    except KeyError:
        return
    low = [min(entry["bbox_min"][i] for entry in solids) for i in range(3)]
    high = [max(entry["bbox_max"][i] for entry in solids) for i in range(3)]
    scene["flow_domain"].update(
        bbox_min=[low[0] - upstream, low[1] - lateral, low[2] - lateral],
        bbox_max=[high[0] + downstream, high[1] + lateral, high[2] + lateral],
    )


def meshing_inputs(store: Any, models: list[GeometryModel], settings: dict[str, Any], quantities: dict[str, Any]) -> list[Path]:
    """Regenerated B-Rep files for every exact model (tool bodies of subtractions excluded)"""
    from paramfabric.geometry.scene import si_values

    values = si_values(quantities)
    files = []
    for model in models:
        if model.kernel != "occ":
            raise ExactKernelUnavailable(
                f"{model.filename} is a faceted model; unstructured meshing needs STEP/IGES B-Rep geometry"
            )
        result = regenerate(store, model, settings.get("features", []), values, tessellate=False)
        if result["errors"]:
            raise RegenerationError(result["errors"][0])
        tools={str(f["tool"]).partition("/")[2] for f in settings.get("features", [])
                 if f.get("type") == "boolean" and f.get("op") == "subtract" and str(f.get("tool", "")).startswith(f"{model.id}/")}
        if tools:
            files.append(_without_bodies(Path(result["brep"]), model, tools))
        else:
            files.append(Path(result["brep"]))
    return files


def _without_bodies(brep: Path, model: GeometryModel, body_ids: set[str]) -> Path:
    target = brep.with_name(brep.stem + "-solids.brep")
    if target.is_file():
        return target
    with GmshSession() as gmsh:
        gmsh.model.add("strip")
        volumes = [tag for _, tag in import_shapes(gmsh, brep)]
        order = [body.id for body in model.bodies]
        remove = [(3, volumes[order.index(body_id)]) for body_id in body_ids if order.index(body_id) < len(volumes)]
        # Tool bodies keep their position in the volume list only when no fuse reordered it;
        # fall back to keeping everything rather than dropping the wrong solid
        if remove and len(volumes) == len(order):
            gmsh.model.occ.remove(remove, recursive=True)
            gmsh.model.occ.synchronize()
        gmsh.write(str(target))
    return target


def export_workspace(store: Any, models: list[GeometryModel], settings: dict[str, Any], quantities: dict[str, Any], kind: str) -> str:
    if not available():
        raise ExactKernelUnavailable("the OpenCASCADE kernel (gmsh) is not installed")
    files = meshing_inputs(store, models, settings, quantities)
    with tempfile.TemporaryDirectory() as scratch, GmshSession(fresh=True) as gmsh:
        for path in files:
            import_shapes(gmsh, path)
        target = Path(scratch) / ("model.step" if kind == "step" else "model.igs")
        write_model(gmsh, target)
        return target.read_text(encoding="latin-1")
