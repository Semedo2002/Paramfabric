"""Legacy ASCII VTK `STRUCTURED_POINTS` writer and reader"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class VtkError(ValueError):
    """Raised for unsupported or malformed VTK files"""


@dataclass
class StructuredPoints:
    dimensions: tuple[int, int, int]
    origin: tuple[float, float, float]
    spacing: tuple[float, float, float]
    scalars: dict[str, list[float]] = field(default_factory=dict)
    vectors: dict[str, list[tuple[float, float, float]]] = field(default_factory=dict)
    title: str = "paramfabric"

    @property
    def point_count(self) -> int:
        return self.dimensions[0] * self.dimensions[1] * self.dimensions[2]


def write_structured_points(path: Path, data: StructuredPoints) -> None:
    count = data.point_count
    lines=[
        "# vtk DataFile Version 3.0",
        data.title.replace("\n", " ")[:255],
        "ASCII",
        "DATASET STRUCTURED_POINTS",
        "DIMENSIONS {} {} {}".format(*data.dimensions),
        "ORIGIN {:.9g} {:.9g} {:.9g}".format(*data.origin),
        "SPACING {:.9g} {:.9g} {:.9g}".format(*data.spacing),
        f"POINT_DATA {count}",
    ]
    for name, values in data.scalars.items():
        if len(values) != count:
            raise VtkError(f"scalar field {name!r} has {len(values)} values, expected {count}")
        lines.extend((f"SCALARS {name} double 1", "LOOKUP_TABLE default"))
        lines.extend(f"{value:.9g}" for value in values)
    for name, vectors in data.vectors.items():
        if len(vectors) != count:
            raise VtkError(f"vector field {name!r} has {len(vectors)} values, expected {count}")
        lines.append(f"VECTORS {name} double")
        lines.extend(f"{x:.9g} {y:.9g} {z:.9g}" for x, y, z in vectors)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def read_structured_points(path: Path) -> StructuredPoints:
    lines = path.read_text(encoding="ascii", errors="replace").splitlines()
    if len(lines) < 5 or not lines[0].startswith("# vtk DataFile"):
        raise VtkError("not a legacy VTK file")
    if lines[2].strip().upper() != "ASCII":
        raise VtkError("only ASCII legacy VTK files are supported")
    if lines[3].split()[-1].upper() != "STRUCTURED_POINTS":
        raise VtkError("only STRUCTURED_POINTS datasets are supported")
    tokens=" ".join(lines[4:]).split()
    position = 0
    dimensions = origin = spacing = None
    scalars: dict[str, list[float]] = {}
    vectors: dict[str, list[tuple[float, float, float]]] = {}
    count = 0
    while position < len(tokens):
        keyword = tokens[position].upper()
        if keyword == "DIMENSIONS":
            dimensions = tuple(int(v) for v in tokens[position + 1 : position + 4])
            position += 4
        elif keyword == "ORIGIN":
            origin = tuple(float(v) for v in tokens[position + 1 : position + 4])
            position += 4
        elif keyword in {"SPACING", "ASPECT_RATIO"}:
            spacing = tuple(float(v) for v in tokens[position + 1 : position + 4])
            position += 4
        elif keyword == "POINT_DATA":
            count = int(tokens[position + 1])
            position += 2
        elif keyword == "SCALARS":
            name = tokens[position + 1]
            components = 1
            position += 3
            if position < len(tokens) and tokens[position].isdigit():
                components = int(tokens[position])
                position += 1
            if tokens[position].upper() == "LOOKUP_TABLE":
                position += 2
            size = count * components
            scalars[name] = [float(v) for v in tokens[position : position + size]]
            position += size
        elif keyword == "VECTORS":
            name = tokens[position + 1]
            position += 3
            flat=[float(v) for v in tokens[position : position + 3 * count]]
            vectors[name] = [(flat[3 * i], flat[3 * i + 1], flat[3 * i + 2]) for i in range(count)]
            position += 3 * count
        elif keyword in {"CELL_DATA", "FIELD"}:
            break
        else:
            position += 1
    if dimensions is None or origin is None or spacing is None:
        raise VtkError("VTK file is missing DIMENSIONS, ORIGIN, or SPACING")
    return StructuredPoints(dimensions, origin, spacing, scalars, vectors, lines[1])  # type: ignore[arg-type]
