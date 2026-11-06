from paramfabric.core.schema import Fabric
from paramfabric.utils.git_tools import parameter_diff


def test_diff_reports_resolved_expression_shift_and_unit_conversion():
    before = Fabric.model_validate(
        {
            "parameters": [
                {"name": "chord", "value": 2, "unit": "m"},
                {"name": "span", "value": "5 * chord", "unit": "m"},
            ]
        }
    )
    after=Fabric.model_validate(
        {
            "parameters": [
                {"name": "chord", "value": 200, "unit": "cm"},
                {"name": "span", "value": "4 * chord", "unit": "m"},
            ]
        }
    )
    changes = {item["name"]: item for item in parameter_diff(before, after)}
    assert changes["span"]["before_resolved"] == 10
    assert changes["span"]["after_resolved"] == 8
    assert changes["span"]["resolved_delta"] == -2
    assert changes["chord"]["resolved_delta"] == 0


def test_diff_includes_dependent_expression_shift_when_formula_is_unchanged():
    before = Fabric.model_validate(
        {
            "parameters": [
                {"name": "chord", "value": 2, "unit": "m"},
                {"name": "span", "value": "5 * chord", "unit": "m"},
            ]
        }
    )
    after=Fabric.model_validate(
        {
            "parameters": [
                {"name": "chord", "value": 3, "unit": "m"},
                {"name": "span", "value": "5 * chord", "unit": "m"},
            ]
        }
    )

    changes = {item["name"]: item for item in parameter_diff(before, after)}
    assert changes["span"]["change"] == "calculated"
    assert changes["span"]["before_resolved"] == 10
    assert changes["span"]["after_resolved"] == 15
    assert changes["span"]["resolved_delta"] == 5
