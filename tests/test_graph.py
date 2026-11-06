import pytest

from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric


def test_resolves_dependent_expressions():
    fabric=Fabric.model_validate(
        {
            "parameters": [
                {"name": "chord", "value": 2, "unit": "m"},
                {"name": "span", "value": "5 * chord", "unit": "m"},
                {"name": "area", "value": "span * chord", "unit": "m^2"},
            ]
        }
    )
    graph = ParameterGraph(fabric)
    values=graph.evaluate()
    assert values["span"].magnitude == pytest.approx(10)
    assert values["area"].magnitude == pytest.approx(20)
    assert graph.dependencies("area") == ["chord", "span"]


def test_detects_cycle_with_dependency_route():
    fabric=Fabric.model_validate(
        {
            "parameters": [
                {"name": "first", "value": "second + 1", "unit": "m"},
                {"name": "second", "value": "first + 1", "unit": "m"},
            ]
        }
    )
    with pytest.raises(ValueError, match=r"circular dependency detected: .* -> .*"):
        ParameterGraph(fabric).evaluate()


def test_rejects_unknown_parameter_reference():
    fabric=Fabric.model_validate(
        {"parameters": [{"name": "span", "value": "unknown * 2", "unit": "m"}]}
    )
    with pytest.raises(ValueError, match="unknown parameter reference"):
        ParameterGraph(fabric)


def test_integer_expression_must_resolve_to_an_integer():
    fabric=Fabric.model_validate(
        {
            "parameters": [
                {"name": "length", "value": 3, "unit": "dimensionless"},
                {"name": "count", "value": "length / 2", "unit": "dimensionless", "type": "int"},
            ]
        }
    )
    with pytest.raises(ValueError, match="resolved to a non-integer"):
        ParameterGraph(fabric).evaluate()
