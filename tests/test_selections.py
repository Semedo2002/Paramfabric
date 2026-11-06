"""Named selections and renaming: persistent across parameter changes, and driving boundary conditions"""

import pytest

pytest.importorskip("gmsh")

from paramfabric.core.units import Q_  # noqa: E402
from paramfabric.geometry import exact, selections  # noqa: E402
from paramfabric.geometry.scene import GeometryStore  # noqa: E402
from paramfabric.gmsh_session import GmshSession  # noqa: E402
from tests.test_native3d import _client, _wait  # noqa: E402


@pytest.fixture
def stepped(tmp_path):
    # Lower step ends at x = 0.2, upper step at x = 0.1; both end faces point +x
    with GmshSession() as session:
        occ = session.model.occ
        lower, upper = occ.addBox(0, 0, 0, 0.2, 0.1, 0.05), occ.addBox(0, 0, 0.05, 0.1, 0.1, 0.05)
        occ.fuse([(3, lower)], [(3, upper)])
        occ.synchronize()
        session.write(str(tmp_path / "step.step"))
    data = (tmp_path / "step.step").read_bytes()
    store = GeometryStore(tmp_path)
    model = exact.import_cad("step.step", data)
    exact.store_source(store, model, data)
    model.bodies[0].dimension_parameters = {"x": "length"}
    store.save(model)
    return store, model


def _face_items(model):
    return [{"body": f"{model.id}/{model.bodies[0].id}", "selector": {"point": [0.2, 0.05, 0.02], "normal": [1, 0, 0]}}]


def test_named_face_follows_a_dimension_change(stepped):
    store, model = stepped
    settings = store.settings()
    selection = selections.create(settings, "Lower end", "face", _face_items(model))
    selections.annotate(store, [model], settings, "face", selection["items"], {"length": Q_(0.2, "m")})
    assert selection["items"][0]["selector"]["topology"]["count"] == 1
    # lengthen the part to 0.5 m: the stored point (x = 0.2) is now nearer the upper step's face (x = 0.25),
    # but the persistent name still finds the lower end face
    moved = selections.resolve(store, [model], settings, selection, {"length": Q_(0.5, "m")})
    assert moved[0]["point"][0] == pytest.approx(0.5, abs=1e-9)
    assert moved[0]["point"][2] == pytest.approx(0.025, abs=1e-9)
    assert moved[0]["normal"] == pytest.approx([1, 0, 0], abs=1e-9)


def test_named_edge_resolves_to_the_current_edge(stepped):
    store, model = stepped
    settings = store.settings()
    items = [{"body": f"{model.id}/{model.bodies[0].id}", "selector": {"point": [0.2, 0.05, 0.0]}}]
    selection=selections.create(settings, "Bottom end edge", "edge", items)
    selections.annotate(store, [model], settings, "edge", selection["items"], {"length": Q_(0.2, "m")})
    moved = selections.resolve(store, [model], settings, selection, {"length": Q_(0.5, "m")})
    assert moved[0]["point"] == pytest.approx([0.5, 0.05, 0.0], abs=1e-9)


def test_selection_rules():
    settings = {}
    items = [{"body": "m/b", "selector": {"point": [0, 0, 0]}}]
    first = selections.create(settings, "Clamp end", "face", items)
    for bad in ("", "  ", "_x", "a/b", "x" * 61):
        with pytest.raises(selections.SelectionError):
            selections.create(settings, bad, "face", items)
    with pytest.raises(selections.SelectionError, match="already exists"):
        selections.create(settings, "clamp END", "face", items)  # case-insensitive
    with pytest.raises(selections.SelectionError):
        selections.create(settings, "No items", "face", [])
    with pytest.raises(selections.SelectionError):
        selections.create(settings, "No point", "face", [{"body": "m/b"}])
    with pytest.raises(selections.SelectionError):
        selections.create(settings, "Vertex", "vertex", items)
    assert selections.add_items(settings, first["id"], items) == []  # already present
    assert len(selections.add_items(settings, first["id"], [{"body": "m/b", "selector": {"point": [1, 0, 0]}}])) == 1
    settings["boundary_conditions"] = [{"id": "x", "type": "fixed", "selectors": [], "named_selection": first["id"]}]
    selections.remove(settings, first["id"])
    assert settings["named_selections"] == [] and "named_selection" not in settings["boundary_conditions"][0]


def test_named_selection_api_and_linked_boundary_condition(tmp_path, monkeypatch):
    client=_client(tmp_path, monkeypatch)
    client.post("/api/workbench/geometry/demo")
    client.post("/api/workbench/physics", json={"analysis": "fea"})
    state = client.get("/api/workbench").json()
    key = state["scene"]["bodies"][0]["key"]

    def named(name, point, normal):
        return client.post("/api/workbench/named-selections", json={"name": name, "kind": "face", "items": [{"body": key, "selector": {"point": point, "normal": normal}}]})

    root = named("Root", [0.0, 0.02, 0.01], [-1, 0, 0])
    assert root.status_code == 200, root.text
    tip = named("Tip", [0.2, 0.02, 0.01], [1, 0, 0]).json()["selection"]
    assert named("root", [0.0, 0.02, 0.01], [-1, 0, 0]).status_code == 422  # duplicate, case-insensitive
    root_id = root.json()["selection"]["id"]
    assert root.json()["selection"]["items"][0]["selector"]["topology"]

    renamed = client.patch(f"/api/workbench/named-selections/{root_id}", json={"name": "Clamped root"})
    assert renamed.json()["selection"]["name"] == "Clamped root"
    assert client.patch("/api/workbench/named-selections/nope", json={"name": "x"}).status_code == 404

    # lengthen the bracket: the tip selection moves with the geometry
    fabric = client.get("/api/local/fabric").json()
    for parameter in fabric["parameters"]:
        if parameter["name"] == "cantilever_bracket_length":
            parameter["value"] = 300
    assert client.post("/api/local/fabric", json=fabric).status_code == 200
    items=client.get(f"/api/workbench/named-selections/{tip['id']}/resolve").json()["items"]
    assert items[0]["point"][0] == pytest.approx(0.3, abs=1e-9) and items[0]["normal"] == pytest.approx([1, 0, 0], abs=1e-9)

    # Conditions tied to the selections solve on the moved faces: the stale stored selector is not used
    fabric["parameters"].append({"name": "tip_force", "value": 500, "unit": "N", "type": "float", "tags": ["physics"], "description": "Tip load.", "rationale": "Test.", "bindings": {}})
    assert client.post("/api/local/fabric", json=fabric).status_code == 200
    conditions = [
        {"id": "a", "type": "fixed", "named_selection": root_id, "selectors": [{"point": [0.0, 0.02, 0.01], "normal": [-1, 0, 0]}]},
        {"id": "b", "type": "force", "named_selection": tip["id"], "selectors": [{"point": [0.2, 0.02, 0.01], "normal": [1, 0, 0]}],
         "params": {"magnitude": "tip_force"}, "direction": [0, 0, -1]},
    ]
    assert client.post("/api/workbench/settings", json={"values": {"boundary_conditions": conditions}}).status_code == 200
    run = _wait(client, client.post("/api/workbench/solver/run", json={"engine": "native3d"}).json()["job"]["id"])
    assert run["status"] == "completed", run["error"]
    summary = run["result"]["summary"]
    assert summary["reaction_force"][2] == pytest.approx(500, rel=1e-6)
    # 300×40×20 mm steel: δ = PL³/3EI, so the long-bracket deflection proves the moved geometry was solved
    inertia = 0.04 * 0.02 ** 3 / 12
    assert summary["max_displacement"] == pytest.approx(500 * 0.3 ** 3 / (3 * 200e9 * inertia), rel=0.1)

    # Deleting the selection keeps the condition as an ordinary one
    after = client.delete(f"/api/workbench/named-selections/{root_id}").json()
    assert [s["name"] for s in after["settings"]["named_selections"]] == ["Tip"]
    assert "named_selection" not in after["settings"]["boundary_conditions"][0]
    assert client.delete(f"/api/workbench/named-selections/{root_id}").status_code == 404


def test_rename_entities(tmp_path, monkeypatch):
    client=_client(tmp_path, monkeypatch)
    model = client.post("/api/workbench/geometry/demo").json()["model"]
    key = f"{model['id']}/{model['bodies'][0]['id']}"
    state = client.post("/api/workbench/rename", json={"kind": "body", "id": key, "name": "Main bracket"}).json()
    assert state["models"][0]["bodies"][0]["name"] == "Main bracket"
    assert state["scene"]["bodies"][0]["name"] == "Main bracket"
    state = client.post("/api/workbench/rename", json={"kind": "model", "id": model["id"], "name": "Assembly A"}).json()
    assert state["models"][0]["name"] == "Assembly A"
    client.post("/api/workbench/physics", json={"analysis": "cfd"})
    feature = client.post("/api/workbench/features", json={"type": "flow_volume"}).json()["feature"]
    state = client.post("/api/workbench/rename", json={"kind": "feature", "id": feature["id"], "name": "Air box"}).json()
    assert state["settings"]["features"][0]["name"] == "Air box"
    assert client.post("/api/workbench/rename", json={"kind": "body", "id": "nope/x", "name": "A"}).status_code == 404
    assert client.post("/api/workbench/rename", json={"kind": "feature", "id": feature["id"], "name": "/bad"}).status_code == 422
    assert client.post("/api/workbench/rename", json={"kind": "mesh", "id": "x", "name": "A"}).status_code == 422
