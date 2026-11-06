from paramfabric.core.schema import load_fabric


def test_loads_yaml_with_utf8_bom(tmp_path):
    path = tmp_path / "params.yaml"
    path.write_text(
        "\ufeffparameters:\n  - name: length\n    value: 2\n    unit: m\n",
        encoding="utf-8",
    )

    fabric = load_fabric(path)

    assert fabric.parameters[0].name == "length"
