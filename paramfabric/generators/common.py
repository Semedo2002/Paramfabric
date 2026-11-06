"""Shared helpers for deterministic output generation"""

from __future__ import annotations

from paramfabric.core.graph import ParameterGraph


def rendered_value(graph: ParameterGraph, name: str) -> str:
    """Render a resolved value while preserving strings and booleans"""
    parameter = graph.parameters[name]
    if parameter.type == "str":
        return repr(parameter.value)
    if parameter.type == "bool":
        return "true" if parameter.value else "false"
    magnitude = graph.value(name).magnitude
    return format(float(magnitude), ".15g")


def comment_text(text: str) -> str:
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())
