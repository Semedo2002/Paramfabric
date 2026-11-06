from pathlib import Path

import yaml

from paramfabric.viz.server import create_app


def test_graph_endpoint_returns_parameter_nodes_and_dependencies(tmp_path: Path):
    fabric = {
        "parameters": [
            {"name": "chord", "value": 2, "unit": "m", "tags": ["geometry"]},
            {"name": "span", "value": "5 * chord", "unit": "m"},
        ]
    }
    path = tmp_path / "params.yaml"
    path.write_text(yaml.safe_dump(fabric), encoding="utf-8")
    app = create_app(path)
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/graph")

    result = route.endpoint()
    assert result["nodes"][1]["id"] == "span"
    assert result["links"] == [{"source": "chord", "target": "span"}]
