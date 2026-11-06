"""Workspace geometry store, parametric features, and scene resolution.

Associativity model: each body keeps its imported tessellation. A body's
axis extents may be bound to fabric length parameters; resolving the scene
scales the body about its original minimum corner so its extents equal the
parameter values. Features add parameter-driven operations on top:

`extrude`      box grown from a body's bounding face by a `depth` length
`split`        cutting plane at a dimensionless `fraction` of an axis
`scale`        uniform dimensionless `factor` about the body anchor
`boolean`      `subtract`/`union` of a tool body, applied at mesh level
`flow_volume`  fluid domain around all bodies with upstream/downstream/lateral margins
`offset_face`  push-pull of a selected planar face by a signed `distance` (exact kernel)
`fillet`       constant-radius fillet on selected edges (exact kernel)
`chamfer`      equal-distance chamfer on selected edges (exact kernel)

Exact-kernel features reference faces and edges by geometric selectors
(`{"point": [x, y, z], "normal": [nx, ny, nz]}`) captured on selection.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from paramfabric.core.schema import Fabric, Parameter
from paramfabric.core.units import ureg
from paramfabric.geometry.importer import identifier, unique_name
from paramfabric.geometry.model import AXES, Body, GeometryModel

FEATURE_TYPES = ("extrude", "split", "scale", "boolean", "flow_volume", "offset_face", "fillet", "chamfer")
EXACT_ONLY = {"offset_face", "fillet", "chamfer"}
FACES = ("+x", "-x", "+y", "-y", "+z", "-z")


class GeometryStore:
    """Persist imported models and workbench settings under `.paramfabric`"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.directory = self.root / ".paramfabric" / "geometry"
        self.settings_path = self.root / ".paramfabric" / "workbench.json"

    def models(self) -> list[GeometryModel]:
        if not self.directory.is_dir():
            return []
        models = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                models.append(GeometryModel.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, TypeError, KeyError):
                continue
        return sorted(models, key=lambda model: model.created_at)

    def get(self, model_id: str) -> GeometryModel:
        if not model_id.isalnum():
            raise KeyError(model_id)
        path = self.directory / f"{model_id}.json"
        if not path.is_file():
            raise KeyError(model_id)
        return GeometryModel.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, model: GeometryModel) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{model.id}.json").write_text(json.dumps(model.to_dict()), encoding="utf-8")

    def delete(self, model_id: str) -> None:
        if not model_id.isalnum():
            raise KeyError(model_id)
        path = self.directory / f"{model_id}.json"
        if not path.is_file():
            raise KeyError(model_id)
        path.unlink()
        settings = self.settings()
        settings["features"] = [
            feature for feature in settings["features"]
            if not any(str(feature.get(key, "")).startswith(f"{model_id}/") for key in ("body", "tool"))
        ]
        self.save_settings(settings)

    def settings(self) -> dict[str, Any]:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        data.setdefault("analysis", "cfd")
        data.setdefault("features", [])
        return data

    def save_settings(self, settings: dict[str, Any]) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def si_values(values: dict[str, Any]) -> dict[str, float]:
    result = {}
    for name, quantity in values.items():
        try:
            result[name] = float(quantity.to_base_units().magnitude)
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def body_key(model: GeometryModel, body: Body) -> str:
    return f"{model.id}/{body.id}"


def _find_body(models: list[GeometryModel], key: str) -> tuple[GeometryModel, Body]:
    model_id, _, body_id = key.partition("/")
    for model in models:
        if model.id == model_id:
            for body in model.bodies:
                if body.id == body_id:
                    return model, body
    raise KeyError(f"unknown body {key!r}")


def resolve_scene(
    models: list[GeometryModel], features: list[dict[str, Any]], values: dict[str, float]
) -> dict[str, Any]:
    """Resolve body transforms, derived bodies, and the flow domain (metres)"""
    errors: list[str] = []
    resolved: dict[str, dict[str, Any]] = {}
    for model in models:
        for body in model.bodies:
            extents = body.extents
            factors = [1.0, 1.0, 1.0]
            for axis_index, axis in enumerate(AXES):
                name = body.dimension_parameters.get(axis)
                if not name:
                    continue
                if name not in values:
                    errors.append(f"{body.name}: bound parameter {name!r} is missing or non-numeric")
                    continue
                if extents[axis_index] > 1e-15:
                    target=values[name]
                    if target <= 0:
                        errors.append(f"{body.name}: {name} must be positive to drive geometry")
                        continue
                    factors[axis_index] = target / extents[axis_index]
            resolved[body_key(model, body)] = {
                "key": body_key(model, body),
                "model": model.id,
                "body": body.id,
                "name": body.name,
                "kind": body.kind,
                "anchor": list(body.bbox_min),
                "scale": factors,
                "role": "solid",
                "split": None,
            }
    derived: list[dict[str, Any]] = []
    for feature in features:
        kind = feature.get("type")
        params = feature.get("params", {})
        try:
            if kind == "scale":
                entry = resolved[feature["body"]]
                factor = values[params["factor"]]
                if factor <= 0:
                    raise ValueError("scale factor must be positive")
                entry["scale"] = [value * factor for value in entry["scale"]]
            elif kind == "boolean" and feature.get("op") == "subtract":
                resolved[feature["tool"]]["role"] = "tool"
        except (KeyError, ValueError) as exc:
            errors.append(f"feature {feature.get('id')}: {exc}")

    def bbox(entry: dict[str, Any]) -> tuple[list[float], list[float]]:
        _, body = _find_body(models, entry["key"])
        low = [entry["anchor"][i] + (body.bbox_min[i] - entry["anchor"][i]) * entry["scale"][i] for i in range(3)]
        high = [entry["anchor"][i] + (body.bbox_max[i] - entry["anchor"][i]) * entry["scale"][i] for i in range(3)]
        return low, high

    for entry in resolved.values():
        entry["bbox_min"], entry["bbox_max"] = bbox(entry)

    domain = None
    for feature in features:
        kind = feature.get("type")
        params = feature.get("params", {})
        try:
            if kind == "extrude":
                entry = resolved[feature["body"]]
                depth = values[params["depth"]]
                if depth <= 0:
                    raise ValueError("extrude depth must be positive")
                axis = "xyz".index(feature["face"][1])
                low, high = list(entry["bbox_min"]), list(entry["bbox_max"])
                if feature["face"][0] == "+":
                    low[axis], high[axis] = high[axis], high[axis] + depth
                else:
                    low[axis], high[axis] = low[axis] - depth, low[axis]
                derived.append({
                    "key": f"feature/{feature['id']}", "name": feature.get("name", "Extrusion"),
                    "kind": "extrusion", "source": entry["key"], "bbox_min": low, "bbox_max": high, "role": "solid",
                })
            elif kind == "split":
                entry = resolved[feature["body"]]
                fraction = values[params["fraction"]]
                if not 0 < fraction < 1:
                    raise ValueError("split fraction must be between 0 and 1")
                axis = "xyz".index(feature["axis"])
                position = entry["bbox_min"][axis] + fraction * (entry["bbox_max"][axis] - entry["bbox_min"][axis])
                entry["split"] = {"axis": axis, "position": position, "feature": feature["id"]}
        except (KeyError, ValueError, IndexError) as exc:
            errors.append(f"feature {feature.get('id')}: {exc}")

    solids = [entry for entry in resolved.values() if entry["role"] == "solid"] + derived
    for feature in features:
        if feature.get("type") != "flow_volume" or not solids:
            continue
        params = feature.get("params", {})
        try:
            upstream, downstream, lateral = (values[params[key]] for key in ("upstream", "downstream", "lateral"))
            if min(upstream, downstream, lateral) < 0:
                raise ValueError("flow volume margins cannot be negative")
        except (KeyError, ValueError) as exc:
            errors.append(f"feature {feature.get('id')}: {exc}")
            continue
        low = [min(entry["bbox_min"][i] for entry in solids) for i in range(3)]
        high = [max(entry["bbox_max"][i] for entry in solids) for i in range(3)]
        domain = {
            "bbox_min": [low[0] - upstream, low[1] - lateral, low[2] - lateral],
            "bbox_max": [high[0] + downstream, high[1] + lateral, high[2] + lateral],
            "feature": feature["id"],
        }
    return {
        "bodies": list(resolved.values()),
        "derived": derived,
        "flow_domain": domain,
        "booleans": [feature for feature in features if feature.get("type") == "boolean"],
        "errors": errors,
    }


def transformed_triangles(models: list[GeometryModel], entry: dict[str, Any]) -> list[float]:
    """Return a body's triangles after its parametric morph (metres)"""
    if entry.get("kind") == "extrusion" or "anchor" not in entry:
        return box_triangles(entry["bbox_min"], entry["bbox_max"])
    _, body = _find_body(models, entry["key"])
    if not body.triangles:
        return box_triangles(entry["bbox_min"], entry["bbox_max"])
    anchor, factors = entry["anchor"], entry["scale"]
    return [
        anchor[index % 3] + (value - anchor[index % 3]) * factors[index % 3]
        for index, value in enumerate(body.triangles)
    ]


def box_triangles(low: list[float], high: list[float]) -> list[float]:
    x0, y0, z0 = low
    x1, y1, z1 = high
    corners = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (3, 0, 4, 7)]
    triangles: list[float] = []
    for a, b, c, d in quads:
        for i, j, k in ((a, b, c), (a, c, d)):
            for corner in (corners[i], corners[j], corners[k]):
                triangles.extend(corner)
    return triangles


def _length_unit(models: list[GeometryModel]) -> str:
    for model in models:
        if model.length_unit in {"mm", "cm", "m", "inch", "ft"}:
            return model.length_unit
    return "m"


def _length_parameter(name: str, metres: float, unit: str, description: str, tag: str) -> Parameter:
    value = ureg.Quantity(metres, "m").to(unit).magnitude
    return Parameter(
        name=name, value=float(f"{value:.4g}"), unit=unit, type="float", description=description,
        rationale="Created by a parametric CAD feature.", tags=["geometry", "feature", tag],
    )


def create_feature(
    models: list[GeometryModel],
    features: list[dict[str, Any]],
    fabric: Fabric,
    kind: str,
    options: dict[str, Any],
) -> tuple[dict[str, Any], list[Parameter]]:
    """Build a feature definition and the fabric parameters that drive it"""
    if kind not in FEATURE_TYPES:
        raise ValueError(f"unknown feature type {kind!r}")
    if not models:
        raise ValueError("import or create geometry before adding features")
    taken = set(fabric.by_name())
    unit = _length_unit(models)
    feature_id = uuid.uuid4().hex[:8]
    feature: dict[str, Any] = {"id": feature_id, "type": kind, "params": {}}
    parameters: list[Parameter] = []

    def body_entry(key_name: str = "body") -> tuple[GeometryModel, Body, str]:
        key = str(options.get(key_name) or "")
        if not key:
            first_model = models[0]
            key = body_key(first_model, first_model.bodies[0])
        model, body = _find_body(models, key)
        return model, body, key

    if kind == "flow_volume":
        if any(item.get("type") == "flow_volume" for item in features):
            raise ValueError("the workspace already has a flow volume; edit its parameters instead")
        characteristic = max(max(body.extents) for model in models for body in model.bodies) or 1.0
        for role, multiple, label in (("upstream", 1.5, "Upstream"), ("downstream", 3.0, "Downstream"), ("lateral", 1.0, "Lateral")):
            name = unique_name(f"domain_{role}", taken)
            parameters.append(_length_parameter(
                name, multiple * characteristic, unit, f"{label} margin of the extracted flow volume.", "cfd.domain"
            ))
            feature["params"][role] = name
        feature["name"] = "Flow volume"
    elif kind == "extrude":
        _, body, key = body_entry()
        face = str(options.get("face") or "+x")
        if face not in FACES:
            raise ValueError(f"extrude face must be one of {', '.join(FACES)}")
        axis = "xyz".index(face[1])
        name = unique_name(f"{identifier(body.name)}_extrude_{face[1]}", taken)
        parameters.append(_length_parameter(
            name, 0.25 * (body.extents[axis] or 0.01), unit, f"Extrusion depth from {face} face of {body.name}.", "cad.feature"
        ))
        feature.update(body=key, face=face, name=f"Extrude {face}")
        feature["params"]["depth"] = name
    elif kind == "split":
        _, body, key = body_entry()
        axis = str(options.get("axis") or "x")
        if axis not in AXES:
            raise ValueError("split axis must be x, y, or z")
        name=unique_name(f"{identifier(body.name)}_split_{axis}", taken)
        parameters.append(Parameter(
            name=name, value=0.5, unit="dimensionless", type="float",
            description=f"Split plane position along {axis.upper()} as a fraction of {body.name}.",
            rationale="Created by a parametric CAD feature.", tags=["geometry", "feature", "cad.feature"],
        ))
        feature.update(body=key, axis=axis, name=f"Split {axis.upper()}")
        feature["params"]["fraction"] = name
    elif kind == "scale":
        _, body, key = body_entry()
        name = unique_name(f"{identifier(body.name)}_scale", taken)
        parameters.append(Parameter(
            name=name, value=1.0, unit="dimensionless", type="float",
            description=f"Uniform scale factor applied to {body.name}.",
            rationale="Created by a parametric CAD feature.", tags=["geometry", "feature", "cad.feature"],
        ))
        feature.update(body=key, name="Scale")
        feature["params"]["factor"] = name
    elif kind in EXACT_ONLY:
        model, body, key = body_entry()
        if model.kernel != "occ":
            raise ValueError(f"{kind.replace('_', ' ')} needs exact B-Rep geometry (import STEP or IGES)")
        if kind == "offset_face":
            selector = options.get("selector")
            if not isinstance(selector, dict) or len(selector.get("point", [])) != 3:
                raise ValueError("select a planar face to push or pull")
            name = unique_name(f"{identifier(body.name)}_face_offset", taken)
            parameters.append(_length_parameter(
                name, float(options.get("distance", 0.1 * max(body.extents))), unit,
                f"Push (+) / pull (−) of a face of {body.name}.", "cad.feature",
            ))
            feature.update(body=key, selector=selector, name="Push/Pull face")
            feature["params"]["distance"] = name
        else:
            edges = options.get("edges")
            if not isinstance(edges, list) or not edges or not all(len(edge.get("point", [])) == 3 for edge in edges):
                raise ValueError(f"select one or more edges to {kind}")
            role = "radius" if kind == "fillet" else "distance"
            name = unique_name(f"{identifier(body.name)}_{kind}_{role}", taken)
            parameters.append(_length_parameter(
                name, float(options.get(role, 0.05 * min(e for e in body.extents if e > 0))), unit,
                f"{kind.capitalize()} {role} on {len(edges)} edge(s) of {body.name}.", "cad.feature",
            ))
            feature.update(body=key, edges=edges, name=kind.capitalize())
            feature["params"][role] = name
    elif kind == "boolean":
        _, _, target = body_entry("body")
        _, _, tool = body_entry("tool")
        if target == tool:
            raise ValueError("boolean target and tool must be different bodies")
        operation = str(options.get("op") or "subtract")
        if operation not in {"subtract", "union"}:
            raise ValueError("boolean operation must be subtract or union")
        feature.update(body=target, tool=tool, op=operation, name=f"Boolean {operation}")
    return feature, parameters
