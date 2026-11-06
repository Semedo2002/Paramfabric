"""Neutral CAD import and CAD-to-fabric associativity.

Supported: STEP (`.step`/`.stp`) B-rep solids and shells, IGES
(`.iges`/`.igs`) wireframe, and STL (`.stl`) meshes. Parasolid
(`.x_t`/`.x_b`) needs a licensed Parasolid kernel and is rejected with
guidance to export STEP instead.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePath

from paramfabric.core.schema import Fabric, Parameter
from paramfabric.core.units import ureg
from paramfabric.geometry.iges import read_iges
from paramfabric.geometry.model import AXES, GeometryModel
from paramfabric.geometry.step import read_step
from paramfabric.geometry.stl import read_stl

MAX_IMPORT_BYTES = 50 * 1024 * 1024
SUPPORTED_SUFFIXES = {".step", ".stp", ".iges", ".igs", ".stl"}
_DIMENSION_NAMES = {"x": "length", "y": "width", "z": "height"}
_STL_UNITS = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "inch": 0.0254, "in": 0.0254, "ft": 0.3048}


class GeometryImportError(ValueError):
    """Raised when a CAD file cannot be imported"""


def import_geometry(filename: str, data: bytes, units: str | None = None) -> GeometryModel:
    """Read a neutral CAD file into a :class:`GeometryModel` (metre coordinates)"""
    if len(data) > MAX_IMPORT_BYTES:
        raise GeometryImportError(f"CAD file exceeds {MAX_IMPORT_BYTES // (1024 * 1024)} MB")
    suffix = PurePath(filename).suffix.lower()
    try:
        if suffix in {".step", ".stp"}:
            model = read_step(data.decode("utf-8", "replace"), filename)
        elif suffix in {".iges", ".igs"}:
            model = read_iges(data.decode("latin-1"), filename)
        elif suffix == ".stl":
            unit = (units or "mm").lower()
            if unit not in _STL_UNITS:
                raise GeometryImportError(f"unsupported STL unit {units!r}; choose one of {', '.join(_STL_UNITS)}")
            model = read_stl(data, filename, "inch" if unit == "in" else unit, _STL_UNITS[unit])
        elif suffix in {".x_t", ".x_b", ".xmt_txt", ".xmt_bin"}:
            raise GeometryImportError(
                "Parasolid files require a licensed Parasolid kernel. Export the part as STEP "
                "(AP214/AP242) from your CAD system and import that file."
            )
        else:
            raise GeometryImportError(f"unsupported CAD format {suffix or '(none)'}; use STEP, IGES, or STL")
    except GeometryImportError:
        raise
    except ValueError as exc:
        raise GeometryImportError(str(exc)) from exc
    model.id = uuid.uuid4().hex[:12]
    model.created_at = datetime.now(timezone.utc).isoformat()
    return model


def identifier(text: str, fallback: str = "body") -> str:
    """Turn a CAD label into a fabric-safe parameter prefix"""
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}"
    return cleaned[:40]


def unique_name(base: str, taken: set[str]) -> str:
    name, counter = base, 2
    while name in taken:
        name = f"{base}_{counter}"
        counter += 1
    taken.add(name)
    return name


def associate_dimensions(model: GeometryModel, fabric: Fabric) -> list[Parameter]:
    """Create one length parameter per body axis and bind it to the body.

    Values are expressed in the file's length unit and rounded to six
    significant digits, so a 200 mm part becomes `200 mm` in the fabric.
    Degenerate (zero-thickness) axes are not parameterized.
    """
    taken = set(fabric.by_name())
    unit = model.length_unit if model.length_unit in {"mm", "cm", "m", "inch", "ft", "um", "km", "dm"} else "m"
    to_unit = ureg.Quantity(1, "m").to(unit).magnitude
    created: list[Parameter] = []
    for body in model.bodies:
        prefix = identifier(body.name)
        for axis_index, axis in enumerate(AXES):
            extent = body.extents[axis_index]
            if extent <= 1e-12:
                continue
            value = float(f"{extent * to_unit:.6g}")
            name = unique_name(f"{prefix}_{_DIMENSION_NAMES[axis]}", taken)
            created.append(
                Parameter(
                    name=name,
                    value=value,
                    unit=unit,
                    type="float",
                    description=f"{axis.upper()} extent of {body.name} ({model.filename}).",
                    rationale="Extracted by the CAD associativity engine on import.",
                    tags=["geometry", "cad", f"cad.{prefix}"],
                )
            )
            body.dimension_parameters[axis] = name
    return created
