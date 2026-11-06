"""Stable public façade for source and documentation exports"""

from __future__ import annotations

from paramfabric.core.graph import ParameterGraph
from paramfabric.generators.cpp import generate_cpp
from paramfabric.generators.docs import generate_latex, generate_markdown
from paramfabric.generators.python import generate_python
from paramfabric.generators.rust import generate_rust
from paramfabric.integrations.openscad import generate_openscad


def export_code(graph: ParameterGraph, kind: str) -> str:
    generators = {
        "cpp_header": generate_cpp,
        "py_module": generate_python,
        "rust": generate_rust,
        "latex": generate_latex,
        "markdown": generate_markdown,
        "openscad": generate_openscad,
    }
    try:
        return generators[kind](graph)
    except KeyError as exc:
        raise ValueError(f"unsupported code export format: {kind}") from exc
