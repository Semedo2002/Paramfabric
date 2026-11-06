"""DAG construction and dimensionally checked parameter evaluation"""

from __future__ import annotations

import networkx as nx
import pint

from paramfabric.core.schema import Fabric, Parameter
from paramfabric.core.units import (
    Q_,
    evaluate_expression,
    expression_names,
    unit_quantity,
    validate_unit,
)


def evaluate_parameter(parameter: Parameter, values: dict[str, pint.Quantity]) -> pint.Quantity:
    """Evaluate one parameter given already-resolved prerequisite values"""
    raw = parameter.value
    if isinstance(raw, str) and parameter.type != "str":
        quantity = evaluate_expression(raw, values)
    elif parameter.type == "str":
        quantity = Q_(0, "dimensionless")
    elif parameter.type == "bool":
        quantity = Q_(int(raw), "dimensionless")
    else:
        quantity = unit_quantity(float(raw), parameter.unit)
    if parameter.type not in {"str", "bool"}:
        quantity = validate_unit(quantity, parameter.unit, parameter.name)
        if parameter.type == "int" and not float(quantity.magnitude).is_integer():
            raise ValueError(f"{parameter.name}: integer parameter expression resolved to a non-integer value")
    return quantity


class ParameterGraph:
    """Resolve all fabric values and expose their dependency graph"""

    def __init__(self, fabric: Fabric) -> None:
        self.fabric = fabric
        self.parameters = fabric.by_name()
        self.graph = nx.DiGraph()
        self.graph.add_nodes_from(self.parameters)
        for parameter in fabric.parameters:
            if isinstance(parameter.value, str) and parameter.type != "str":
                for dependency in expression_names(parameter.value):
                    if dependency not in self.parameters:
                        raise ValueError(
                            f"{parameter.name}: unknown parameter reference {dependency!r}"
                        )
                    self.graph.add_edge(dependency, parameter.name)
        self._values: dict[str, pint.Quantity] = {}

    def evaluate(self) -> dict[str, pint.Quantity]:
        """Evaluate values in dependency order, reporting a concrete cycle"""
        try:
            order = list(nx.topological_sort(self.graph))
        except nx.NetworkXUnfeasible as exc:
            cycle = nx.find_cycle(self.graph)
            route = [cycle[0][0], *(edge[1] for edge in cycle)]
            raise ValueError(f"circular dependency detected: {' -> '.join(route)}") from exc

        values: dict[str, pint.Quantity] = {}
        for name in order:
            values[name] = evaluate_parameter(self.parameters[name], values)
        self._values = values
        return dict(values)

    @property
    def values(self) -> dict[str, pint.Quantity]:
        return dict(self._values) if self._values else self.evaluate()

    def value(self, name: str) -> pint.Quantity:
        return self.values[name]

    def dependencies(self, name: str) -> list[str]:
        return sorted(self.graph.predecessors(name))
