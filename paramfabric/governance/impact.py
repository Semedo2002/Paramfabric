"""Dependency-aware parameter blast-radius analysis"""

from __future__ import annotations

from typing import TypedDict

import networkx as nx

from paramfabric.core.graph import ParameterGraph


class Impact(TypedDict):
    parameter: str
    distance: int
    tags: list[str]
    bindings: dict[str, str]
    dependencies: list[str]


def blast_radius(graph: ParameterGraph, changed_names: set[str]) -> list[Impact]:
    """List changed parameters and all downstream dependents in DAG order"""
    impacted: set[str] = set()
    for name in changed_names:
        if name not in graph.parameters:
            continue
        impacted.add(name)
        impacted.update(nx.descendants(graph.graph, name))
    result: list[Impact] = []
    for name in sorted(impacted, key=lambda item: (len(nx.ancestors(graph.graph, item)), item)):
        sources = [source for source in changed_names if source in graph.parameters]
        distances = [
            nx.shortest_path_length(graph.graph, source, name)
            for source in sources
            if source == name or nx.has_path(graph.graph, source, name)
        ]
        parameter = graph.parameters[name]
        result.append(
            {
                "parameter": name,
                "distance": min(distances, default=0),
                "tags": parameter.tags,
                "bindings": parameter.bindings,
                "dependencies": graph.dependencies(name),
            }
        )
    return result
