from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric
from paramfabric.integrations.cad_nx import generate_nx_journal
from paramfabric.integrations.cad_solidworks import generate_solidworks_macro
from paramfabric.integrations.simulation import (
    generate_ansys_apdl,
    generate_comsol_java,
    generate_openfoam_dictionary,
)


def test_cad_and_simulation_exports_resolve_dependencies():
    graph = ParameterGraph(
        Fabric.model_validate(
            {
                "parameters": [
                    {"name": "chord", "value": 2, "unit": "m"},
                    {"name": "span", "value": "5 * chord", "unit": "m"},
                ]
            }
        )
    )
    assert '"""span"" = 10m"' in generate_solidworks_macro(graph)
    assert "CreateExpression(NXOpen.Expression.Type.Number" in generate_nx_journal(graph)
    assert "SPAN = 10" in generate_ansys_apdl(graph)
    assert "span 10;" in generate_openfoam_dictionary(graph)
    assert '"span", "10[m]"' in generate_comsol_java(graph)
