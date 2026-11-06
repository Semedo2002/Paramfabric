import base64
import time

from fastapi.testclient import TestClient

from paramfabric.geometry.scene import box_triangles
from paramfabric.geometry.stl import write_stl
from paramfabric.server.app import create_app


def _client(tmp_path, monkeypatch, auth_required=False):
    monkeypatch.chdir(tmp_path)
    fabric_file=tmp_path / "params.yaml"
    fabric_file.write_text(
        "parameters:\n"
        "  - name: root_chord\n    value: 2\n    unit: m\n    bindings: {cpp_header: generated/params.hpp}\n"
        "  - name: wing_span\n    value: '5 * root_chord'\n    unit: m\n",
        encoding="utf-8",
    )
    kwargs = {"jwt_secret": "x" * 40, "audit_key": "y" * 40} if auth_required else {}
    app=create_app(
        fabric_file,
        database_url=f"sqlite:///{(tmp_path / 'wb.db').as_posix()}",
        auth_required=auth_required,
        audit_path=tmp_path / ".paramfabric" / "audit.jsonl",
        **kwargs,
    )
    return TestClient(app, client=("127.0.0.1", 40000))


def _wait(client, job_id):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        snapshot=client.get(f"/api/workbench/solver/jobs/{job_id}").json()
        if snapshot["status"] not in {"queued", "running"}:
            return snapshot
        time.sleep(0.1)
    raise AssertionError("solver job did not finish")


def test_cad_to_results_workflow(tmp_path, monkeypatch):
    client=_client(tmp_path, monkeypatch)
    demo = client.post("/api/workbench/geometry/demo")
    assert demo.status_code == 200
    created = {p["name"]: (p["value"], p["unit"]) for p in demo.json()["created_parameters"]}
    assert created["cantilever_bracket_length"] == (200.0, "mm")
    model = demo.json()["model"]
    key = f"{model['id']}/{model['bodies'][0]['id']}"

    geometry = client.get(f"/api/workbench/geometry/{model['id']}").json()
    assert len(geometry["bodies"][0]["triangles"]) > 0

    fabric = client.get("/api/local/fabric").json()
    for parameter in fabric["parameters"]:
        if parameter["name"] == "cantilever_bracket_length":
            parameter["value"] = "0.15 * root_chord"  # 0.3 m = 300 mm, live morph through a formula
    evaluated = client.post("/api/workbench/evaluate", json=fabric).json()
    assert evaluated["diagnostics"]["ok"]
    assert abs(evaluated["scene"]["bodies"][0]["bbox_max"][0] - 0.3) < 1e-12

    fabric["parameters"][1]["value"] = "5 * root_chord * cantilever_bracket_width"  # m² declared as m
    broken = client.post("/api/workbench/evaluate", json=fabric).json()["diagnostics"]
    assert broken["units_ok"] is False
    assert broken["issues"][0]["parameter"] == "wing_span"

    assert client.post("/api/workbench/geometry/bind", json={"body": key, "axis": "x", "parameter": "root_chord"}).status_code == 200
    rejected = client.post("/api/workbench/geometry/bind", json={"body": key, "axis": "y", "parameter": "nonexistent"})
    assert rejected.status_code == 422

    physics = client.post("/api/workbench/physics", json={"analysis": "cfd"}).json()
    assert "inlet_velocity" in physics["created_parameters"]
    feature=client.post("/api/workbench/features", json={"type": "flow_volume"})
    assert feature.status_code == 200
    assert feature.json()["workbench"]["scene"]["flow_domain"] is not None

    mesh = client.post("/api/workbench/mesh", json={"mesher": "cartesian", "wait": True})
    assert mesh.status_code == 200, mesh.json()
    assert mesh.json()["quality"]["solid_cells"] > 0

    run = client.post("/api/workbench/solver/run", json={"engine": "builtin"})
    assert run.status_code == 200, run.json()
    snapshot=_wait(client, run.json()["job"]["id"])
    assert snapshot["status"] == "completed", snapshot["error"]
    assert snapshot["residual_count"] > 0
    result = snapshot["result"]
    assert result["analysis"] == "cfd" and result["streamlines"]
    assert len(result["fields"]["pressure"]) == result["grid"]["shape"][0] * result["grid"]["shape"][1]

    case = client.post("/api/workbench/solver/case", json={"engine": "openfoam"}).json()
    assert {"constant/polyMesh/faces", "system/controlDict", "Allrun"} <= set(case["files"])  # body-fitted gmsh case
    stl = client.get("/api/workbench/geometry-export.stl")
    assert stl.status_code == 200 and stl.text.startswith("solid cantilever_bracket")

    client.post("/api/workbench/physics", json={"analysis": "fea"})
    fea = _wait(client, client.post("/api/workbench/solver/run", json={"engine": "builtin"}).json()["job"]["id"])
    assert fea["engine"] == "builtin" and fea["result"]["summary"]["max_von_mises"] > 0

    ledger = (tmp_path / ".paramfabric" / "audit.jsonl").read_text()
    assert "geometry.imported" in ledger and "physics.configured" in ledger and "feature.created" in ledger


def test_stl_import_boolean_feature_and_wrong_physics_units(tmp_path, monkeypatch):
    client=_client(tmp_path, monkeypatch)
    block = write_stl("block", box_triangles([0, 0, 0], [100, 50, 50]), 1.0)
    tool = write_stl("pin", box_triangles([40, 20, 0], [60, 30, 50]), 1.0)
    keys = []
    for name, text in (("block.stl", block), ("pin.stl", tool)):
        response = client.post("/api/workbench/geometry/import", json={
            "filename": name, "content_base64": base64.b64encode(text.encode()).decode(), "units": "mm",
        })
        assert response.status_code == 200, response.json()
        model = response.json()["model"]
        keys.append(f"{model['id']}/{model['bodies'][0]['id']}")
    boolean = client.post("/api/workbench/features", json={"type": "boolean", "options": {"body": keys[0], "tool": keys[1], "op": "subtract"}})
    assert boolean.status_code == 200
    roles = {entry["key"]: entry["role"] for entry in boolean.json()["workbench"]["scene"]["bodies"]}
    assert roles == {keys[0]: "solid", keys[1]: "tool"}
    client.post("/api/workbench/physics", json={"analysis": "fea"})
    mesh = client.post("/api/workbench/mesh", json={"mesher": "cartesian", "wait": True}).json()
    full = 100 * 50 * 50
    hole = 20 * 10 * 50
    cell = 1
    for size in mesh["display"]["spacing"]:
        cell *= size * 1000
    assert abs(mesh["quality"]["solid_cells"] * cell - (full - hole)) / (full - hole) < 0.1

    fabric = client.get("/api/local/fabric").json()
    for parameter in fabric["parameters"]:
        if parameter["name"] == "applied_load":
            parameter["unit"] = "m"
    assert client.post("/api/local/fabric", json=fabric).status_code == 200
    wrong = client.post("/api/workbench/solver/run", json={"engine": "builtin"})
    assert wrong.status_code == 422
    assert "applied_load must have dimensions" in wrong.json()["detail"]
    parasolid = client.post("/api/workbench/geometry/import", json={"filename": "a.x_t", "content_base64": base64.b64encode(b"x").decode()})
    assert parasolid.status_code == 422 and "Parasolid" in parasolid.json()["detail"]


def test_code_sync_endpoints_round_trip(tmp_path, monkeypatch):
    client=_client(tmp_path, monkeypatch)
    assert client.post("/api/local/build", json=["cpp_header"]).json()["count"] == 1
    status = client.get("/api/local/sync/status").json()["items"]
    assert {item["name"] for item in status} == {"ROOT_CHORD", "WING_SPAN"}
    client.post("/api/local/sync/reconcile")
    header = tmp_path / "generated" / "params.hpp"
    header.write_text(header.read_text().replace("ROOT_CHORD = 2", "ROOT_CHORD = 4"))
    pulled = client.post("/api/local/sync/reconcile").json()
    assert pulled["pulled"][0]["parameter"] == "root_chord"
    assert client.get("/api/local/fabric").json()["parameters"][0]["value"] == 4
    pushed = client.post("/api/local/sync/reconcile").json()
    assert [(item["name"], item["value"]) for item in pushed["pushed"]] == [("WING_SPAN", "20.0")]  # declared double
    assert "fabric.synced_from_source" in (tmp_path / ".paramfabric" / "audit.jsonl").read_text()


def test_enterprise_workbench_requires_auth_and_sync_stays_local(tmp_path, monkeypatch):
    monkeypatch.setenv("PARAMFABRIC_DATA_DIR", str(tmp_path / "data"))
    client = _client(tmp_path, monkeypatch, auth_required=True)
    assert client.post("/api/workbench/geometry/demo").status_code == 401
    assert client.post("/api/workbench/solver/run", json={}).status_code == 401
    assert client.get("/api/local/sync/status").status_code == 404
