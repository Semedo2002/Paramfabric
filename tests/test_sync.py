from pathlib import Path

import pytest

from paramfabric.auditors import audit_sources
from paramfabric.auditors.sync import SyncEngine, format_literal, inject, scan_text
from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric


def test_scanners_return_exact_literal_spans_for_each_language():
    python = "SPAN: float = -2.5  # [m]\nNOT_CONST = foo()\ndef f():\n    LOCAL = 3\n"
    (constant,) = scan_text(python, "python")
    assert (constant.name, constant.value, constant.unit, python[constant.start:constant.end]) == ("SPAN", -2.5, "m", "-2.5")
    cpp = "static constexpr float GAIN = 2.5f; // [1]\n#define COUNT 12\nconst double SPAN{4.0};\nint mutable_value = 3;\n"
    names = {c.name: (c.value, c.literal) for c in scan_text(cpp, "cpp")}
    assert names == {"GAIN": (2.5, "2.5"), "COUNT": (12.0, "12"), "SPAN": (4.0, "4.0")}
    rust = "pub const SPAN_MM: f64 = 1_250.5;\nstatic COUNT: u32 = 7;\nlet x = 3.0;\n"
    assert {(c.name, c.value) for c in scan_text(rust, "rust")} == {("SPAN_MM", 1250.5), ("COUNT", 7.0)}
    scad = "width = 20; // [mm]\nmodule m() { inner = 3; }\n"
    (scad_constant,) = scan_text(scad, "openscad")
    assert (scad_constant.name, scad_constant.unit) == ("width", "mm")


def test_injection_preserves_formatting_suffixes_and_float_style():
    cpp = "constexpr float GAIN = 2.5f;  // [1]\r\n"
    (constant,) = scan_text(cpp, "cpp")
    assert inject(cpp, [(constant, format_literal(3, constant, False))]) == "constexpr float GAIN = 3.0f;  // [1]\r\n"
    rust = "pub const N: f64 = 2.0;\n"
    (rust_constant,) = scan_text(rust, "rust")
    assert format_literal(4, rust_constant, False) == "4.0"
    python = "COUNT = 3\n"
    (python_constant,) = scan_text(python, "python")
    assert format_literal(5, python_constant, True) == "5"


def _workspace(tmp_path: Path) -> tuple[SyncEngine, Fabric]:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "params.hpp").write_text("constexpr double ROOT_CHORD = 2.0; // [m]\nconstexpr double WING_SPAN = 10.0; // [m]\n")
    (tmp_path / "src" / "config.py").write_text("WING_SPAN_ALT = 1\nROOT_CHORD = 2000.0  # [mm]\n")
    (tmp_path / "src" / "lib.rs").write_text("pub const WING_SPAN: f64 = 10.0;\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("ROOT_CHORD = 99\n")
    fabric = Fabric.model_validate({"parameters": [
        {"name": "root_chord", "value": 2.0, "unit": "m"},
        {"name": "wing_span", "value": "5 * root_chord", "unit": "m"},
    ]})
    return SyncEngine(tmp_path), fabric


def values(fabric: Fabric):
    return ParameterGraph(fabric).evaluate()


def test_status_converts_units_and_skips_virtualenvs(tmp_path: Path):
    engine, fabric = _workspace(tmp_path)
    items={item["key"]: item for item in engine.status(fabric, values(fabric))}
    assert set(items) == {
        "src/params.hpp::ROOT_CHORD", "src/params.hpp::WING_SPAN", "src/config.py::ROOT_CHORD", "src/lib.rs::WING_SPAN",
    }
    assert items["src/config.py::ROOT_CHORD"]["state"] == "in_sync"
    assert items["src/config.py::ROOT_CHORD"]["fabric_value"] == pytest.approx(2000.0)
    assert all(item["state"] == "in_sync" for item in items.values())


def test_reconcile_pulls_code_edits_then_pushes_dependent_formulas(tmp_path: Path):
    engine, fabric = _workspace(tmp_path)
    first = engine.reconcile(fabric, values(fabric))
    assert first["pulled"] == first["pushed"] == first["conflicts"] == []
    header = tmp_path / "src" / "params.hpp"
    header.write_text(header.read_text().replace("ROOT_CHORD = 2.0", "ROOT_CHORD = 3.0"))
    pulled = engine.reconcile(fabric, values(fabric))
    updated = pulled["fabric"]
    assert updated.by_name()["root_chord"].value == 3.0
    assert {item["path"] for item in pulled["pulled"]} == {"src/params.hpp"}
    pushed = engine.reconcile(updated, values(updated))
    assert {(item["path"], item["name"], item["value"]) for item in pushed["pushed"]} == {
        ("src/config.py", "ROOT_CHORD", "3000.0"),
        ("src/params.hpp", "WING_SPAN", "15.0"),
        ("src/lib.rs", "WING_SPAN", "15.0"),
    }
    assert "constexpr double WING_SPAN = 15.0; // [m]" in header.read_text()
    assert (tmp_path / "src" / "lib.rs").read_text() == "pub const WING_SPAN: f64 = 15.0;\n"
    assert all(item["state"] == "in_sync" for item in engine.status(updated, values(updated)))


def test_reconcile_holds_conflicts_and_refuses_formula_pulls(tmp_path: Path):
    engine, fabric = _workspace(tmp_path)
    engine.reconcile(fabric, values(fabric))
    header = tmp_path / "src" / "params.hpp"
    header.write_text(header.read_text().replace("ROOT_CHORD = 2.0", "ROOT_CHORD = 4.0").replace("WING_SPAN = 10.0", "WING_SPAN = 11.0"))
    edited = Fabric.model_validate({"parameters": [
        {"name": "root_chord", "value": 2.5, "unit": "m"},
        {"name": "wing_span", "value": "5 * root_chord", "unit": "m"},
    ]})
    result=engine.reconcile(edited, values(edited))
    conflicts = {item["key"] for item in result["conflicts"]}
    assert "src/params.hpp::ROOT_CHORD" in conflicts and "src/params.hpp::WING_SPAN" in conflicts
    assert "ROOT_CHORD = 4.0" in header.read_text()
    with pytest.raises(ValueError, match="formula"):
        engine.pull(edited, values(edited), {"src/params.hpp::WING_SPAN"})


def test_auditor_now_covers_rust_and_openscad(tmp_path: Path):
    (tmp_path / "lib.rs").write_text("pub const AIR_DENSITY: f64 = 1.204; // [kg/m^3]\n")
    (tmp_path / "part.scad").write_text("air_density = 1.225; // [kg/m^3]\n")
    fabric = Fabric.model_validate({"parameters": [{"name": "air_density", "value": 1.225, "unit": "kg/m^3"}]})
    findings = audit_sources(tmp_path, ParameterGraph(fabric))
    assert [(finding.status, finding.file.name) for finding in findings] == [("drift", "lib.rs")]


from paramfabric.auditors import expressions as ex  # noqa: E402


def test_expression_trees_normalize_and_render_every_language():
    for text, language in [
        ("5.0f * ROOT_CHORD + std::pow(GAIN, 2)", "cpp"),
        ("5.0_f64 * ROOT_CHORD + GAIN.powf(2.0)", "rust"),
        ("5 * ROOT_CHORD + pow(GAIN, 2)", "openscad"),
        ("5 * ROOT_CHORD + GAIN ** 2", "python"),
    ]:
        assert ex.canonical(ex.parse(text, language)) == "5 * ROOT_CHORD + GAIN ** 2"
    tree=ex.parse("(a + b) ** 2 / -c", "python")
    assert ex.render(tree, "cpp") == "std::pow(a + b, 2) / -c"
    assert ex.render(tree, "rust") == "(a + b).powf(2.0) / -c"
    assert ex.render(tree, "openscad") == "pow(a + b, 2) / -c"
    assert ex.render(ex.parse("a - (b - c)", "python"), "python") == "a - (b - c)"
    with pytest.raises(ex.ExpressionError):
        ex.parse("sqrt(a)", "python")


def _formula_workspace(tmp_path: Path):
    (tmp_path / "geom.hpp").write_text("constexpr double ROOT_CHORD = 2.0;  // [m]\nconstexpr double WING_SPAN = 5.0 * ROOT_CHORD;  // span\n")
    (tmp_path / "geom.rs").write_text("pub const ROOT_CHORD: f64 = 2.0;\npub const WING_SPAN: f64 = 5.0 * ROOT_CHORD;\n")
    (tmp_path / "geom.py").write_text("ROOT_CHORD = 2.0\nWING_SPAN = 5 * ROOT_CHORD  # mirrors fabric\n")
    fabric = Fabric.model_validate({"parameters": [
        {"name": "root_chord", "value": 2.0, "unit": "m"},
        {"name": "wing_span", "value": "5 * root_chord", "unit": "m"},
    ]})
    return SyncEngine(tmp_path), fabric


def test_formula_constants_sync_structurally_in_both_directions(tmp_path: Path):
    engine, fabric = _formula_workspace(tmp_path)
    items={item["key"]: item for item in engine.status(fabric, values(fabric))}
    assert items["geom.hpp::WING_SPAN"]["state"] == "in_sync"
    assert items["geom.hpp::WING_SPAN"]["value"] == pytest.approx(10.0)
    engine.reconcile(fabric, values(fabric))
    # edit the formula in Python source -> the fabric formula follows
    source = tmp_path / "geom.py"
    source.write_text(source.read_text().replace("5 * ROOT_CHORD", "6 * ROOT_CHORD"))
    pulled = engine.reconcile(fabric, values(fabric))
    updated = pulled["fabric"]
    assert updated.by_name()["wing_span"].value == "6 * root_chord"
    # next round renders the new formula into C++ and Rust, preserving comments
    pushed = engine.reconcile(updated, values(updated))
    assert {(item["path"], item["value"]) for item in pushed["pushed"]} == {
        ("geom.hpp", "6.0 * ROOT_CHORD"), ("geom.rs", "6.0 * ROOT_CHORD"),
    }
    assert "constexpr double WING_SPAN = 6.0 * ROOT_CHORD;  // span" in (tmp_path / "geom.hpp").read_text()
    # A fabric edit with a power renders per language
    document=updated.model_dump()
    document["parameters"][1]["value"] = "6 * root_chord ** 2 / root_chord"
    edited = Fabric.model_validate(document)
    engine.reconcile(edited, values(edited))
    assert "WING_SPAN: f64 = 6.0 * ROOT_CHORD.powf(2.0) / ROOT_CHORD;" in (tmp_path / "geom.rs").read_text()
    assert "6.0 * std::pow(ROOT_CHORD, 2.0) / ROOT_CHORD;  // span" in (tmp_path / "geom.hpp").read_text()
    assert "WING_SPAN = 6 * ROOT_CHORD ** 2 / ROOT_CHORD  # mirrors fabric" in source.read_text()


def test_diff_reports_cross_file_drift_and_strategies_resolve_it(tmp_path: Path):
    engine, fabric = _formula_workspace(tmp_path)
    rust = tmp_path / "geom.rs"
    rust.write_text(rust.read_text().replace("5.0 * ROOT_CHORD", "4.0 * ROOT_CHORD"))
    report = {entry["parameter"]: entry for entry in engine.diff(fabric, values(fabric))}
    assert report["wing_span"]["status"] == "cross_file_drift"
    assert report["root_chord"]["status"] == "in_sync"
    result = engine.resolve(fabric, values(fabric), "wing_span", "source:geom.rs")
    assert result["fabric"].by_name()["wing_span"].value == "4 * root_chord"
    assert {item["path"] for item in result["pushed"]} == {"geom.hpp", "geom.py"}
    assert engine.diff(result["fabric"], values(result["fabric"]))[1]["status"] == "in_sync"
    with pytest.raises(ValueError, match="unknown strategy"):
        engine.resolve(fabric, values(fabric), "wing_span", "coin-flip")


def test_unknown_identifiers_are_reported_not_synced(tmp_path: Path):
    (tmp_path / "a.hpp").write_text("constexpr double WING_SPAN = 5.0 * CHORD_FROM_ELSEWHERE;\n")
    fabric = Fabric.model_validate({"parameters": [{"name": "wing_span", "value": 1.0, "unit": "m"}]})
    engine = SyncEngine(tmp_path)
    (item,) = engine.status(fabric, values(fabric))
    assert item["state"] == "unresolved" and "CHORD_FROM_ELSEWHERE" in item["detail"]
    assert engine.reconcile(fabric, values(fabric))["pushed"] == []
