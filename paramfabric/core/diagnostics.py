"""Per-parameter fabric diagnostics for live editing.

Unlike :meth:`ParameterGraph.evaluate`, which stops at the first problem,
diagnostics evaluate every parameter independently in dependency order so
one bad formula is pinpointed while the rest of the fabric still resolves.
"""

from __future__ import annotations

from typing import Any

import networkx as nx
import pint

from paramfabric.core.graph import evaluate_parameter
from paramfabric.core.schema import Fabric
from paramfabric.core.units import UnitMismatchError, expression_names


def diagnose(fabric: Fabric) -> dict[str, Any]:
    """Return resolved values, typed issues, and the dependency edges"""
    parameters = fabric.by_name()
    graph = nx.DiGraph()
    graph.add_nodes_from(parameters)
    issues: list[dict[str, Any]] = []
    broken: set[str] = set()
    for parameter in fabric.parameters:
        if not (isinstance(parameter.value, str) and parameter.type != "str"):
            continue
        try:
            names=expression_names(parameter.value)
        except ValueError as exc:
            issues.append({"kind": "syntax", "parameter": parameter.name, "message": str(exc)})
            broken.add(parameter.name)
            continue
        for dependency in sorted(names):
            if dependency not in parameters:
                issues.append({
                    "kind": "reference", "parameter": parameter.name,
                    "message": f"{parameter.name}: unknown parameter reference {dependency!r}",
                })
                broken.add(parameter.name)
            else:
                graph.add_edge(dependency, parameter.name)
    links=[{"source": source, "target": target} for source, target in graph.edges]
    cycle: list[str] | None = None
    try:
        order = list(nx.topological_sort(graph))
    except nx.NetworkXUnfeasible:
        edges = nx.find_cycle(graph)
        cycle = [edges[0][0], *(edge[1] for edge in edges)]
        issues.append({
            "kind": "cycle", "parameter": cycle[0], "cycle": cycle,
            "message": f"circular dependency detected: {' -> '.join(cycle)}",
        })
        cyclic = {node for component in nx.strongly_connected_components(graph) if len(component) > 1 for node in component}
        broken |= cyclic
        acyclic = graph.subgraph(set(graph) - cyclic)
        order = list(nx.topological_sort(acyclic))
    values: dict[str, pint.Quantity] = {}
    resolved: dict[str, dict[str, Any]] = {}
    for name in order:
        if name in broken:
            continue
        blocked = [dependency for dependency in graph.predecessors(name) if dependency not in values]
        if blocked:
            issues.append({
                "kind": "blocked", "parameter": name,
                "message": f"{name}: waiting on invalid input(s) {', '.join(sorted(blocked))}",
            })
            continue
        parameter = parameters[name]
        try:
            quantity=evaluate_parameter(parameter, values)
        except UnitMismatchError as exc:
            issues.append({"kind": "unit", "parameter": name, "message": str(exc)})
            continue
        except (ValueError, TypeError, ZeroDivisionError, pint.errors.PintError) as exc:
            issues.append({"kind": "expression", "parameter": name, "message": f"{name}: {exc}"})
            continue
        values[name] = quantity
        if parameter.type in {"str", "bool"}:
            resolved[name] = {"value": parameter.value, "unit": parameter.unit}
            continue
        base = quantity.to_base_units()
        resolved[name] = {
            "value": float(quantity.magnitude),
            "unit": parameter.unit,
            "si": float(base.magnitude),
            "si_unit": f"{base.units:~}" or "1",
            "dimensionality": str(quantity.dimensionality) if quantity.dimensionality else "dimensionless",
        }
    kinds = {issue["kind"] for issue in issues}
    return {
        "ok": not issues,
        "units_ok": "unit" not in kinds,
        "dag_ok": cycle is None,
        "cycle": cycle,
        "issues": issues,
        "resolved": resolved,
        "links": links,
        "quantities": values,
    }
