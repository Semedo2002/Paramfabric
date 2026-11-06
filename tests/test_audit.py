from pathlib import Path

from paramfabric.auditors import audit_sources
from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric


def test_detects_python_value_drift_and_missing_unit(tmp_path: Path):
    source = tmp_path / "config.py"
    source.write_text("AIR_DENSITY = 1.204\n", encoding="utf-8")
    fabric = Fabric.model_validate(
        {
            "parameters": [
                {"name": "air_density", "value": 1.225, "unit": "kg/m^3"},
                {"name": "missing_value", "value": 2, "unit": "m"},
            ]
        }
    )
    findings=audit_sources(tmp_path, ParameterGraph(fabric))
    statuses = {finding.status for finding in findings}
    assert {"drift", "missing-unit", "missing"} <= statuses


def test_scans_cpp_constants_and_unit_comments(tmp_path: Path):
    source = tmp_path / "constants.hpp"
    source.write_text("constexpr double AIR_DENSITY = 1.204; // [kg/m^3]\n", encoding="utf-8")
    fabric = Fabric.model_validate(
        {"parameters": [{"name": "air_density", "value": 1.204, "unit": "kg/m^3"}]}
    )
    assert audit_sources(tmp_path, ParameterGraph(fabric)) == []
