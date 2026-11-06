"""Source-code parameter drift auditors"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paramfabric.core.graph import ParameterGraph


@dataclass(frozen=True)
class AuditFinding:
    """One parameter audit observation"""

    status: str
    parameter: str
    file: Path | None
    message: str


def audit_sources(root: Path, graph: "ParameterGraph") -> list[AuditFinding]:
    """Audit Python, C/C++, Rust, and OpenSCAD sources beneath a project directory"""
    from paramfabric.auditors.ast_cpp import scan_cpp
    from paramfabric.auditors.ast_python import scan_python
    from paramfabric.auditors.sync import scan_text

    findings: list[AuditFinding] = []
    observed: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in {".git", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        if path.suffix == ".py":
            records = scan_python(path)
        elif path.suffix.lower() in {".h", ".hpp", ".hh", ".hxx", ".cpp"}:
            records = scan_cpp(path)
        elif path.suffix.lower() in {".rs", ".scad"}:
            language="rust" if path.suffix.lower() == ".rs" else "openscad"
            records = [
                (constant.name.lower(), constant.value, constant.unit)
                for constant in scan_text(path.read_text(encoding="utf-8"), language)
            ]
        else:
            continue
        for name, value, declared_unit in records:
            key = name.lower()
            if key not in graph.parameters:
                if path.suffix == ".py" and not name.isupper():
                    continue
                findings.append(
                    AuditFinding(
                        "undeclared",
                        name,
                        path,
                        "numeric source constant is absent from the parameter fabric",
                    )
                )
                continue
            observed.add(key)
            parameter = graph.parameters[key]
            if parameter.type in {"str", "bool"}:
                continue
            expected = float(graph.value(key).magnitude)
            if abs(float(value) - expected) > max(1e-12, abs(expected) * 1e-9):
                findings.append(
                    AuditFinding(
                        "drift",
                        key,
                        path,
                        f"source value {value:g} differs from fabric value {expected:g} {parameter.unit}",
                    )
                )
            if declared_unit is None and parameter.unit not in {"", "1", "dimensionless"}:
                findings.append(
                    AuditFinding("missing-unit", key, path, f"source constant has no unit annotation (expected {parameter.unit})")
                )
            elif declared_unit is not None and declared_unit != parameter.unit:
                findings.append(
                    AuditFinding(
                        "unit-mismatch",
                        key,
                        path,
                        f"source declares unit {declared_unit}, fabric declares {parameter.unit}",
                    )
                )
    for name in sorted(graph.parameters.keys() - observed):
        findings.append(
            AuditFinding("missing", name, None, "parameter was not found in any scanned Python, C/C++, Rust, or OpenSCAD source")
        )
    return findings
