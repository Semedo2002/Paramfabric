import ast

from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric
from paramfabric.generators.cpp import generate_cpp
from paramfabric.generators.docs import generate_markdown
from paramfabric.generators.python import generate_python
from paramfabric.generators.rust import generate_rust


def test_cpp_and_python_generation():
    fabric = Fabric.model_validate(
        {
            "parameters": [
                {"name": "chord", "value": 2.0, "unit": "m", "description": "Root chord"},
                {"name": "span", "value": "5 * chord", "unit": "m"},
            ]
        }
    )
    graph = ParameterGraph(fabric)
    cpp = generate_cpp(graph)
    python_module = generate_python(graph)
    assert "inline constexpr double CHORD = 2;" in cpp
    assert "inline constexpr double SPAN = 10;" in cpp
    assert "CHORD: float = 2" in python_module
    assert "SPAN: float = 10" in python_module
    ast.parse(python_module)


def test_other_generators_include_parameter_data():
    graph = ParameterGraph(
        Fabric.model_validate(
            {"parameters": [{"name": "mass", "value": 3, "unit": "kg", "rationale": "Test mass"}]}
        )
    )
    assert "mass" in generate_markdown(graph)
    assert "pub const MASS: f64 = 3;" in generate_rust(graph)
