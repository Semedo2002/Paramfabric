import pytest

from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric
from paramfabric.core.units import UnitMismatchError


def test_rejects_addition_of_incompatible_units():
    fabric = Fabric.model_validate(
        {
            "parameters": [
                {"name": "distance", "value": 2, "unit": "m"},
                {"name": "duration", "value": 3, "unit": "s"},
                {"name": "invalid", "value": "distance + duration", "unit": "m"},
            ]
        }
    )
    with pytest.raises(UnitMismatchError, match="cannot apply Add"):
        ParameterGraph(fabric).evaluate()


def test_rejects_formula_with_wrong_result_dimension():
    fabric = Fabric.model_validate(
        {
            "parameters": [
                {"name": "mass", "value": 2, "unit": "kg"},
                {"name": "velocity", "value": 3, "unit": "m/s"},
                {"name": "force", "value": "mass * velocity", "unit": "N"},
            ]
        }
    )
    with pytest.raises(UnitMismatchError, match="expression has units"):
        ParameterGraph(fabric).evaluate()
