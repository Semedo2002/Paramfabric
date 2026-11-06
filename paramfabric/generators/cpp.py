"""C++ header generation"""

from __future__ import annotations

import json

from paramfabric.core.graph import ParameterGraph
from paramfabric.generators.common import comment_text, rendered_value


def generate_cpp(graph: ParameterGraph, namespace: str = "paramfabric") -> str:
    """Generate a self-contained C++20 header for all resolved parameters"""
    lines = [
        "#pragma once",
        "",
        "#include <string_view>",
        "",
        f"namespace {namespace} {{",
        "",
    ]
    for parameter in graph.fabric.parameters:
        name = parameter.name.upper()
        if parameter.description:
            lines.append(f"// {comment_text(parameter.description)}")
        lines.append(f"// Unit: {parameter.unit}")
        if parameter.type == "str":
            declaration = f"inline constexpr std::string_view {name} = {json.dumps(str(parameter.value))};"
        elif parameter.type == "bool":
            declaration=f"inline constexpr bool {name} = {rendered_value(graph, parameter.name)};"
        elif parameter.type == "int":
            number = int(graph.value(parameter.name).magnitude)
            declaration = f"inline constexpr int {name} = {number};"
        else:
            declaration = f"inline constexpr double {name} = {rendered_value(graph, parameter.name)};"
        lines.extend((f"{declaration}  // [{parameter.unit}]", ""))
    lines.extend((f"}}  // namespace {namespace}", ""))
    return "\n".join(lines)
