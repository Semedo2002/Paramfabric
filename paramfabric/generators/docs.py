"""Markdown and LaTeX parameter table generation"""

from __future__ import annotations

from paramfabric.core.graph import ParameterGraph
from paramfabric.generators.common import rendered_value


def generate_markdown(graph: ParameterGraph) -> str:
    lines = [
        "| Parameter | Value | Unit | Description | Rationale |",
        "|---|---:|---|---|---|",
    ]
    for parameter in graph.fabric.parameters:
        description = parameter.description.replace("|", "\\|").replace("\n", " ")
        rationale = parameter.rationale.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| `{parameter.name}` | {rendered_value(graph, parameter.name)} | "
            f"{parameter.unit} | {description} | {rationale} |"
        )
    return "\n".join(lines) + "\n"


def generate_latex(graph: ParameterGraph) -> str:
    """Generate a LaTeX tabular parameter table"""
    def escape(value: str) -> str:
        for source, replacement in (
            ("\\", r"\textbackslash{}"),
            ("&", r"\&"),
            ("%", r"\%"),
            ("_", r"\_"),
            ("#", r"\#"),
        ):
            value = value.replace(source, replacement)
        return value

    lines = [
        r"\begin{tabular}{lllll}",
        r"Parameter & Value & Unit & Description & Rationale \\",
        r"\hline",
    ]
    for parameter in graph.fabric.parameters:
        fields = (
            escape(parameter.name),
            escape(rendered_value(graph, parameter.name)),
            escape(parameter.unit),
            escape(parameter.description),
            escape(parameter.rationale),
        )
        lines.append(" & ".join(fields) + r" \\")
    lines.append(r"\end{tabular}")
    return "\n".join(lines) + "\n"
