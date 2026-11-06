"""Safe import parsers for fabric documents and source constants"""

from __future__ import annotations

import ast
import csv
import io
import json
import re
from pathlib import PurePath
from typing import Any

import yaml

from paramfabric.core.schema import Fabric, Parameter

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def _value(text: str) -> float | int | bool | str:
    value = text.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(_NUMBER, value):
        return float(value)
    return value


def _source_parameters(pairs: list[tuple[str, Any, str]]) -> Fabric:
    parameters: list[Parameter] = []
    for name, value, description in pairs:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        kind = "bool" if isinstance(value, bool) else (
            "int" if isinstance(value, int) else "float"
        )
        parameters.append(
            Parameter(
                name=name,
                value=value,
                unit="dimensionless",
                type=kind,
                description=description,
                rationale="Imported from source constant.",
                tags=["imported"],
            )
        )
    if not parameters:
        raise ValueError("no supported numeric or boolean constants were found")
    return Fabric(parameters=parameters)


def parse_import(filename: str, content: str) -> Fabric:
    """Parse a supported fabric document, CSV, Python/C++ source, or OpenSCAD file"""
    suffix = PurePath(filename).suffix.lower()
    if suffix in {".yaml", ".yml", ".json"}:
        data = json.loads(content) if suffix == ".json" else yaml.safe_load(content)
        if data is None:
            data = {}
        if isinstance(data, list):
            data={"parameters": data}
        if not isinstance(data, dict):
            raise ValueError("fabric file must contain an object or a parameters list")
        return Fabric.model_validate(data)

    if suffix == ".csv":
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames or not {"name", "value", "unit"}.issubset(
            {field.strip().lower() for field in reader.fieldnames}
        ):
            raise ValueError("CSV columns must include name, value, and unit")
        parameters = []
        for row in reader:
            normalized={key.strip().lower(): value for key, value in row.items() if key}
            value = _value(normalized.get("value") or "")
            value_type=(
                "bool" if isinstance(value, bool) else
                "str" if isinstance(value, str) and not re.search(r"[+\-*/()]", value) else
                "int" if isinstance(value, int) else
                "float"
            )
            parameters.append(
                Parameter(
                    name=(normalized.get("name") or "").strip(),
                    value=value,
                    unit=(normalized.get("unit") or "dimensionless").strip(),
                    description=(normalized.get("description") or "").strip(),
                    rationale="Imported from CSV.",
                    type=value_type,
                    tags=[tag.strip() for tag in (normalized.get("tags") or "imported").split(";") if tag.strip()],
                )
            )
        return Fabric(parameters=parameters)

    if suffix == ".py":
        tree = ast.parse(content, filename=filename)
        pairs: list[tuple[str, Any, str]] = []
        for statement in tree.body:
            if isinstance(statement, ast.Assign):
                targets = statement.targets
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
            else:
                continue
            try:
                value = ast.literal_eval(statement.value)
            except (ValueError, TypeError):
                continue
            if isinstance(value, (bool, int, float)) and not isinstance(value, complex):
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        pairs.append((target.id.lower(), value, f"Imported Python constant {target.id}."))
        return _source_parameters(pairs)

    if suffix in {".hpp", ".h", ".hh", ".cpp", ".cc", ".c"}:
        pairs = []
        pattern = re.compile(
            rf"^\s*(?:#\s*define\s+|(?:(?:inline\s+)?constexpr|const)\s+"
            rf"(?:(?:unsigned|signed|long|short)\s+)*(?:int|double|float|auto)\s+)"
            rf"([A-Za-z_]\w*)\s*(?:=|\s)\s*({_NUMBER})\s*;?(?:\s*//\s*(.*))?$",
            re.MULTILINE,
        )
        for match in pattern.finditer(content):
            name, raw_value, description = match.groups()
            pairs.append((name.lower(), _value(raw_value), description or f"Imported C++ constant {name}."))
        return _source_parameters(pairs)

    if suffix == ".scad":
        pairs = []
        pattern = re.compile(
            rf"^\s*([A-Za-z_]\w*)\s*=\s*({_NUMBER})\s*;\s*(?://\s*(.*))?$",
            re.MULTILINE,
        )
        for match in pattern.finditer(content):
            name, raw_value, description = match.groups()
            pairs.append((name, _value(raw_value), description or f"Imported OpenSCAD parameter {name}."))
        return _source_parameters(pairs)

    raise ValueError(f"unsupported import type: {suffix or 'no file extension'}")
