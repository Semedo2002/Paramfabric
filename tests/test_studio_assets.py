"""Studio front-end assets: the knowledge registry, help-id coverage, and the browser-side slicer"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "paramfabric" / "studio" / "static"
REGISTRY = json.loads((STATIC / "knowledge.json").read_text(encoding="utf-8"))

REQUIRED = [
    "turbulence.k_omega_sst", "turbulence.k_epsilon", "turbulence.spalart_allmaras", "turbulence.laminar",
    "mesh.tetrahedral", "mesh.hexahedral", "engine.lbm", "engine.fvm_navier_stokes",
    "bc.velocity_inlet", "bc.pressure_outlet", "bc.no_slip_wall", "bc.symmetry",
]


@pytest.mark.parametrize("topic", REQUIRED)
def test_registry_topics_are_complete(topic):
    entry = REGISTRY["topics"][topic]
    assert entry["title"] and entry["category"] and entry["summary"].endswith(".")
    assert entry["formulation"] and all(eq["label"] and eq["html"] for eq in entry["formulation"])
    assert entry["use_cases"] and entry["pitfalls"] and entry["references"]
    if topic.startswith("turbulence.") and topic != "turbulence.laminar":
        assert "y⁺" in entry["yplus"]


def test_comparisons_cover_the_exclusive_selectors():
    comparisons = REGISTRY["comparisons"]
    assert set(comparisons["mesh_type"]["options"]) == {"mesh.tetrahedral", "mesh.hexahedral"}
    assert set(comparisons["solver_engine"]["options"]) == {"engine.lbm", "engine.fvm_navier_stokes"}
    for comparison in comparisons.values():
        assert all(option in REGISTRY["topics"] for option in comparison["options"])
        assert {"Pros", "Cons", "Computational cost", "Accuracy", "Recommended use"} <= set(comparison["rows"])
        assert all(len(cells) == len(comparison["options"]) for cells in comparison["rows"].values())
    assert "Not available" in comparisons["solver_engine"]["rows"]["In Paramfabric"][0]  # LBM is comparison-only


def test_every_help_id_in_the_ui_resolves():
    sources = "".join((STATIC / name).read_text(encoding="utf-8") for name in ("app.js", "index.html"))
    literal = set(re.findall(r'data-help-id="([a-z_]+\.[a-z_]+)"', sources))
    dynamic = set(re.findall(r'"((?:turbulence|mesh|bc|structural|engine|solver)\.[a-z_]+)"', sources))
    assert literal and dynamic
    missing = sorted((literal | dynamic) - set(REGISTRY["topics"]))
    assert not missing, missing


def test_static_assets_are_served(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from paramfabric.server.app import create_app

    monkeypatch.chdir(tmp_path)
    (tmp_path / "params.yaml").write_text("parameters: []\n", encoding="utf-8")
    app = create_app(tmp_path / "params.yaml", database_url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}",
                     auth_required=False, audit_path=tmp_path / "audit.jsonl")
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.get("/studio-static/knowledge.json").json()["topics"]["bc.symmetry"]["title"]
    guide=client.get("/studio-static/guide.js")
    assert guide.status_code == 200 and "export async function initGuide" in guide.text
    page = client.get("/studio").text
    assert 'data-skill="expert"' in page and 'id="cutToggle"' in page and 'data-pick="edge"' in page


SLICE_CHECK = r"""
import { readFileSync } from "node:fs";
const source = readFileSync(process.argv[2], "utf8").replace(/^import \* as THREE.*$/m, "const THREE = {};").replace(/^import \{.*$/mg, "");
const { sliceVolume } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const P = [[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]];
const T = [[0,1,2,6],[0,2,3,6],[0,3,7,6],[0,7,4,6],[0,4,5,6],[0,5,1,6]];
const volume = { positions: Float32Array.from(P.flat()), tets: Uint32Array.from(T.flat()),
  scalars: { f: Float32Array.from(P.map(([x, y, z]) => 2 * x + y - z)) }, vectors: { U: Float32Array.from(P.flatMap(([x]) => [x, 0, 1])) } };
const out = [];
for (const [axis, position] of [[0, 0.3], [1, 0.5], [2, 0.77]]) {
  const cut = sliceVolume(volume, axis, position, "f", "U");
  let area = 0, error = 0;
  for (let i = 0; i < cut.positions.length; i += 9) {
    const a = cut.positions.slice(i, i + 3), b = cut.positions.slice(i + 3, i + 6), c = cut.positions.slice(i + 6, i + 9);
    const u = b.map((v, k) => v - a[k]), w = c.map((v, k) => v - a[k]);
    area += Math.hypot(u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]) / 2;
  }
  for (let i = 0; i < cut.values.length; i += 1) {
    const [x, y, z] = cut.positions.slice(3 * i, 3 * i + 3);
    error = Math.max(error, Math.abs(cut.values[i] - (2 * x + y - z)), Math.abs(cut.vectors[3 * i] - x), Math.abs([x, y, z][axis] - position));
  }
  out.push({ area, error, outlines: cut.outlines.length > 0 });
}
console.log(JSON.stringify(out));
"""


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_browser_slicer_cuts_exact_areas_and_interpolates_fields(tmp_path):
    script = tmp_path / "slice_check.mjs"
    script.write_text(SLICE_CHECK, encoding="utf-8")
    run = subprocess.run([NODE, str(script), str(STATIC / "viewport.js")], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    for cut in json.loads(run.stdout):
        assert cut["area"] == pytest.approx(1.0, abs=1e-6)  # unit-cube cross-section on every axis
        assert cut["error"] < 1e-6 and cut["outlines"]  # linear fields and vectors interpolate exactly


ISO_TRACE_CHECK = r"""
import { readFileSync } from "node:fs";
const source = readFileSync(process.argv[2], "utf8").replace(/^import \* as THREE.*$/m, "const THREE = {};").replace(/^import \{.*$/mg, "");
const { isoSurface, traceStreamlines } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
// Kuhn-subdivided grid on [-1, 1]^3: six tets per cube, conforming across cubes.
const n = 24, h = 2 / n, P = [], T = [];
const id = (i, j, k) => (i * (n + 1) + j) * (n + 1) + k;
for (let i = 0; i <= n; i++) for (let j = 0; j <= n; j++) for (let k = 0; k <= n; k++) P.push([-1 + i * h, -1 + j * h, -1 + k * h]);
const kuhn = [[0,1,2,6],[0,2,3,6],[0,3,7,6],[0,7,4,6],[0,4,5,6],[0,5,1,6]];
const corner = [[0,0,0],[1,0,0],[1,1,0],[0,1,0],[0,0,1],[1,0,1],[1,1,1],[0,1,1]];
for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) for (let k = 0; k < n; k++)
  for (const tet of kuhn) T.push(...tet.map(c => id(i + corner[c][0], j + corner[c][1], k + corner[c][2])));
const make = U => ({ positions: Float32Array.from(P.flat()), tets: Uint32Array.from(T), low: [-1, -1, -1], high: [1, 1, 1],
  scalars: { r: Float32Array.from(P.map(p => Math.hypot(...p))), z: Float32Array.from(P.map(p => p[2])) },
  vectors: { U: Float32Array.from(P.flatMap(U)) } });
const volume = make(([x, y]) => [-y, x, 0]);
const iso = isoSurface(volume, "r", 0.6, "z");
let area = 0, levelError = 0, colorError = 0;
for (let i = 0; i < iso.positions.length; i += 9) {
  const a = iso.positions.slice(i, i + 3), b = iso.positions.slice(i + 3, i + 6), c = iso.positions.slice(i + 6, i + 9);
  const u = b.map((v, k) => v - a[k]), w = c.map((v, k) => v - a[k]);
  area += Math.hypot(u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]) / 2;
}
for (let i = 0; i < iso.values.length; i++) {
  const p = iso.positions.slice(3 * i, 3 * i + 3);
  levelError = Math.max(levelError, Math.abs(Math.hypot(...p) - 0.6));
  colorError = Math.max(colorError, Math.abs(iso.values[i] - p[2]));
}
const rotation = traceStreamlines(volume, "U", [0.5, 0, 0.1], { step: 0.01, maxSteps: 400 })[0];
let radiusDrift = 0;
for (let k = 0; k < rotation.length; k += 4) radiusDrift = Math.max(radiusDrift, Math.abs(Math.hypot(rotation[k], rotation[k + 1]) - 0.5));
const uniform = traceStreamlines(make(() => [1, 0.5, 0]), "U", [-0.95, -0.5, 0.2, -0.95, 0.3, -0.4], { step: 0.02 });
const straight = uniform.map(line => {
  let off = 0;
  for (let k = 0; k < line.length; k += 4) off = Math.max(off, Math.abs((line[k + 1] - line[1]) - 0.5 * (line[k] - line[0])));
  return { off, reach: Math.max(Math.abs(line[line.length - 4]), Math.abs(line[line.length - 3])), speed: line[3] };
});
console.log(JSON.stringify({ area, levelError, colorError, triangles: iso.positions.length / 9, radiusDrift, points: rotation.length / 4, straight }));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_browser_iso_surface_and_particle_tracer(tmp_path):
    script = tmp_path / "iso_check.mjs"
    script.write_text(ISO_TRACE_CHECK, encoding="utf-8")
    run = subprocess.run([NODE, str(script), str(STATIC / "viewport.js")], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    # marching tets on |x| = 0.6: area within 3 % of 4πr², vertices within one cell of the true sphere
    assert out["area"] == pytest.approx(4 * 3.141592653589793 * 0.36, rel=0.03)
    assert out["levelError"] < 2 / 24 and out["colorError"] < 1e-5 and out["triangles"] > 500
    # RK2 in a rigid rotation (linear field: exact interpolation) keeps the orbit radius
    assert out["points"] == 400 and out["radiusDrift"] < 2e-3
    for line in out["straight"]:  # uniform flow: straight lines that run until they leave the domain
        assert line["off"] < 1e-5 and line["reach"] > 0.97 and line["speed"] == pytest.approx(5 ** 0.5 / 2, rel=1e-6)


MEMBERS_CHECK = r"""
import { readFileSync } from "node:fs";
const { sectionProperties, sectionPreviewSvg, shellPreviewSvg } = await import("data:text/javascript;base64," + readFileSync(process.argv[2]).toString("base64"));
const specs = JSON.parse(process.argv[3]);
console.log(JSON.stringify({
  props: specs.map(sectionProperties),
  svgs: specs.map(spec => sectionPreviewSvg(spec)),
  bad: [sectionProperties({ type: "tube", diameter: 0.01, wall: 0.006 }).error, sectionProperties({ type: "i_beam", height: 0.01, flange_width: 0.05, web: 0.004, flange: 0.006 }).error],
  shell: shellPreviewSvg(0.002, -0.001),
}));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_member_modal_section_properties_match_the_solver(tmp_path):
    from paramfabric.solvers.structural import Section

    specs=[{"type": "rectangular", "width": 0.02, "height": 0.04}, {"type": "circular", "diameter": 0.03},
             {"type": "tube", "diameter": 0.04, "wall": 0.003},
             {"type": "i_beam", "height": 0.1, "flange_width": 0.055, "web": 0.0041, "flange": 0.0057}]
    script = tmp_path / "members_check.mjs"
    script.write_text(MEMBERS_CHECK, encoding="utf-8")
    run = subprocess.run([NODE, str(script), str(STATIC / "members.js"), json.dumps(specs)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    for spec, js, svg in zip(specs, out["props"], out["svgs"]):
        section = Section.from_spec(spec)  # the preview shows exactly what the solver will use
        for key, value in (("area", section.area), ("iy", section.iy), ("iz", section.iz), ("j", section.j)):
            assert js[key] == pytest.approx(value, rel=1e-12), (spec["type"], key)
        assert svg.startswith("<svg") and "NaN" not in svg and 'class="dim"' in svg
    assert all(out["bad"]) and "NaN" not in out["shell"] and "mid-surface" in out["shell"]


MENU_CHECK = r"""
import { readFileSync } from "node:fs";
const load = async path => import("data:text/javascript;base64," + Buffer.from(readFileSync(path, "utf8").replace(/^import \* as THREE.*$/m, "const THREE = {};").replace(/^import \{.*$/mg, "")).toString("base64"));
const { menuFor } = await load(process.argv[2]);
const { viewAngles } = await load(process.argv[3]);
const flat = items => items.flatMap(item => item.items ? [item, ...flat(item.items)] : [item]);
const labels = items => flat(items).filter(item => item.label).map(item => item.label);
const face = { kind: "face", key: "m/b", title: "Face 1", subtitle: "plane", surface: "plane", normal: [1, 0, 0] };
const named = [{ id: "n1", name: "Clamp", kind: "face" }, { id: "n2", name: "Rail", kind: "edge" }];
console.log(JSON.stringify({
  fea: labels(menuFor(face, { analysis: "fea", exact: true, named })),
  external: labels(menuFor(face, { analysis: "cfd", internal: false, heat: false, named: [] })),
  internal: labels(menuFor(face, { analysis: "cfd", internal: true, heat: true, named: [] })),
  many: labels(menuFor({ ...face, count: 3 }, { analysis: "fea", named: [] })),
  edge: labels(menuFor({ kind: "edge", key: "m/b", title: "Edge", subtitle: "" }, { analysis: "fea", exact: true, named })),
  faceted: labels(menuFor({ kind: "edge", key: "m/b", title: "Edge", subtitle: "" }, { analysis: "cfd", exact: false, named: [] })),
  hidden: labels(menuFor({ kind: "body", key: "m/b", title: "B", subtitle: "" }, { analysis: "fea", hidden: new Set(["m/b"]), named: [] })),
  probe: labels(menuFor(face, { analysis: "fea", named: [], probe: { text: "75 MPa" }, hasVolume: true, hasCut: true })),
  empty: labels(menuFor({ kind: "empty" }, { analysis: "fea", bodies: 1, hasResult: true, layers: {}, named: [] })),
  named: labels(menuFor({ kind: "named", id: "n1", selectionKind: "face", title: "Clamp", subtitle: "" }, { analysis: "fea", named })),
  shape: flat(menuFor(face, { analysis: "fea", exact: true, named })).filter(item => item.items).every(item => !item.id),
  angles: [[0, 0, 1], [1, 0, 0], [0, 1, 0], [0, 0, -1], [1, 1, 1]].map(viewAngles),
}));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_right_click_menus_follow_the_physics_and_every_command_has_a_handler(tmp_path):
    import math
    import re

    script = tmp_path / "menu_check.mjs"
    script.write_text(MENU_CHECK, encoding="utf-8")
    run = subprocess.run([NODE, str(script), str(STATIC / "menus.js"), str(STATIC / "viewport.js")], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert {"Look at", "Zoom to selection", "Create named selection…", "Clamp", "Fixed support", "Force", "Pressure", "Assign 2D shell…", "Push / pull face",
            "Hide body", "Isolate body", "Coplanar faces", "Properties"} <= set(out["fea"])
    assert "Rail" not in out["fea"]  # only selections of the same kind can be added to
    assert not {"Fixed support", "Velocity inlet", "Heat source", "Assign 2D shell…"} & set(out["external"])
    assert {"Velocity inlet", "Pressure outlet", "Heat source"} <= set(out["internal"]) and "Fixed support" not in out["internal"]
    assert "Create named selection from 3 faces…" in out["many"]
    assert {"Assign 1D beam…", "Fillet", "Chamfer", "Rail"} <= set(out["edge"])
    assert not {"Assign 1D beam…", "Fillet", "Chamfer"} & set(out["faceted"])  # fillets need exact geometry, beams need FEA
    assert "Show body" in out["hidden"] and "Hide body" not in out["hidden"] and "Show all bodies" in out["hidden"]
    assert {"Probe value here (75 MPa)", "Iso-surface at this value", "Normal to X"} <= set(out["probe"])
    assert {"Isometric", "Back", "Zoom to fit", "Faces only", "X-ray", "Contour", "Cutting plane", "Save viewport image", "Import CAD…"} <= set(out["empty"])
    assert {"Select", "Look at", "Fixed support", "Rename…", "Delete"} <= set(out["named"])
    assert out["shape"]  # a submenu parent is never itself a command
    # orbit angles: head-on views along each axis
    angles=out["angles"]
    assert angles[0]["theta"] is None and angles[0]["phi"] == pytest.approx(1e-3)
    assert angles[1] == pytest.approx({"theta": 0, "phi": math.pi / 2})
    assert angles[2] == pytest.approx({"theta": math.pi / 2, "phi": math.pi / 2})
    assert angles[3]["phi"] == pytest.approx(math.pi - 1e-3)
    assert angles[4]["phi"] == pytest.approx(math.acos(1 / math.sqrt(3))) and angles[4]["theta"] == pytest.approx(math.pi / 4)
    # A command in a menu with no handler would be a dead item
    commands = set(re.findall(r'\bid: "(\w+)"', (STATIC / "menus.js").read_text(encoding="utf-8"))) | {"bc", "namedBc"}
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    block=app[app.index("const commands = {"):app.index("await commands[id]?.()")]
    handlers = set(re.findall(r"^    (\w+)(?:: |,)", block, re.M))
    assert commands <= handlers, commands - handlers


CONTROLS_CHECK = r"""
import { readFileSync } from "node:fs";
const load = async path => import("data:text/javascript;base64," + readFileSync(path).toString("base64"));
const c = await load(process.argv[2]);
const m = await load(process.argv[3]);
const a = (scheme, button, shift, ctrl) => c.mouseAction(scheme, { button, shift, ctrl });
const commands = [{ label: "View Top", group: "View" }, { label: "Run simulation", keywords: "solve start", group: "Solve" }, { label: "Mesh geometry", keywords: "generate", group: "Mesh" }, { label: "Show or hide mesh", group: "Mesh" }];
// Regular tetrahedron, a flat sliver, then a prism's three tets (not scored).
const r = [1, 1, 1, -1, -1, 1, -1, 1, -1, 1, -1, -1];
const flat = [0, 0, 5, 1, 0, 5, 0, 1, 5, 0.3, 0.3, 5.001];
const prism = [0, 0, 9, 1, 0, 9, 0, 1, 9, 0, 0, 9.01, 1, 0, 9.01, 0, 1, 9.01];
const volume = { positions: [...r, ...flat, ...prism], tets: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 9, 10, 11, 12, 10, 11, 12, 13],
  cells: [{ kind: "tet4", cells: 2, tets_per_cell: 1 }, { kind: "prism6", cells: 1, tets_per_cell: 3 }] };
const scores = m.tetQualities(volume);
const stats = m.qualityStats(scores);
const geometry = m.cellGeometry(volume, [0, 1], scores.quality);
const noCells = m.tetQualities({ positions: r, tets: [0, 1, 2, 3] });
console.log(JSON.stringify({
  sw: [a("solidworks", 1), a("solidworks", 1, false, true), a("solidworks", 1, true), a("solidworks", 0), a("solidworks", 2)],
  fusion: [a("fusion", 1), a("fusion", 1, true)], nx: [a("nx", 1), a("nx", 1, true)], own: [a("paramfabric", 0), a("paramfabric", 0, true), a("paramfabric", 2)], unknown: a("nope", 0),
  rank: { run: c.rankCommands(commands, "run")[0].label, solve: c.rankCommands(commands, "solve")[0].label, mesh: c.rankCommands(commands, "mesh").map(x => x.label), none: c.rankCommands(commands, "zzzz").length, all: c.rankCommands(commands, " ").length, typo: c.rankCommands(commands, "simultion")[0]?.label },
  pane: [c.clampPane(50, 1600, 300), c.clampPane(5000, 1600, 300), c.clampPane(400, 1600, 300), c.clampPane(5000, 600, 300)],
  float: [c.clampFloat(-50, -50, 100, 100, 800, 600), c.clampFloat(5000, 5000, 100, 100, 800, 600), c.clampFloat(10, 20, 100, 100, 800, 600)],
  progress: { none: c.solveProgress([{ iteration: 1, time: 0 }], 100), slow: c.solveProgress([{ iteration: 0, time: 0 }, { iteration: 10, time: 25 }], 600), fast: c.solveProgress([{ iteration: 0, time: 0 }, { iteration: 50, time: 10 }], null), done: c.solveProgress([{ iteration: 0, time: 0 }, { iteration: 600, time: 60 }], 600), stuck: c.solveProgress([{ iteration: 5, time: 0 }, { iteration: 5, time: 9 }], 100) },
  scale: [c.nextScale(1, 1), c.nextScale(1, -1), c.nextScale(1.5, 1), c.nextScale(0.8, -1)],
  quality: [...scores.quality].slice(0, 2), scored: [...scores.scored], stats,
  verdicts: [m.qualityVerdict(stats), m.qualityVerdict({ total: 1000, poor: 0, min: 0.5 }), m.qualityVerdict({ total: 1000, poor: 2, min: 0.05 }), m.qualityVerdict({ total: 100, poor: 5, min: 0.05 }), m.qualityVerdict({ total: 0 })].map(v => v.level),
  poor: m.cellsInRange(scores, 0, 0.1), crossing: m.cellsCrossing(volume, 2, 2), triangles: geometry.triangles.length, edges: geometry.edges.length / 6, values: geometry.values.length, noCells: [...noCells.scored],
  colors: [m.qualityColor(0), m.qualityColor(1), m.qualityColor(5)],
  rows: m.patchRows([{ name: "face:1", kind: "face", indices: [0, 1, 2] }, { name: "face:2", kind: "face", indices: [0, 1, 2, 0, 2, 3] }, { name: "inlet", kind: "inlet", indices: [0, 1, 2] }, { name: "sides", kind: "slip", indices: [0, 1, 2] }, { name: "my_wall", kind: "wall", indices: [0, 1, 2] }]),
}));
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_mouse_schemes_command_search_layout_rules_and_mesh_quality(tmp_path):
    script = tmp_path / "controls_check.mjs"
    script.write_text(CONTROLS_CHECK, encoding="utf-8")
    run = subprocess.run([NODE, str(script), str(STATIC / "controls.js"), str(STATIC / "meshview.js")], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    # Autodesk's own table: SolidWorks orbit on middle, Ctrl+middle pans; Fusion the reverse of orbit and pan
    assert out["sw"] == ["orbit", "pan", "zoom", None, None]
    assert out["fusion"] == ["pan", "orbit"] and out["nx"] == ["orbit", "pan"] and out["own"] == ["orbit", "pan", "pan"] and out["unknown"] == "orbit"
    rank = out["rank"]
    assert rank["run"] == rank["solve"] == "Run simulation" and rank["none"] == 0 and rank["all"] == 4
    assert rank["mesh"][0] == "Mesh geometry" and "Show or hide mesh" in rank["mesh"]
    assert rank["typo"] == "Run simulation"  # letters in order still find it
    assert out["pane"] == [200, 940, 400, 200]  # minimum, room left for the viewport, as asked, never below minimum
    assert out["float"] == [{"left": 0, "top": 0}, {"left": 700, "top": 500}, {"left": 10, "top": 20}]
    progress = out["progress"]
    assert progress["none"] is None and progress["stuck"] is None
    assert "2.5 s/it" in progress["slow"]["text"] and "10 / 600 it" in progress["slow"]["text"] and "at most 25 min left" in progress["slow"]["text"]
    assert progress["fast"]["text"] == "50 it · 5.0 it/s" and "left" not in progress["done"]["text"]
    assert out["scale"] == [1.1, 0.9, 1.5, 0.8]
    # Quality: 1 for a regular tet, nearly 0 for a sliver, same formula as VolumeMesh.quality()
    assert out["quality"][0] == pytest.approx(1.0, abs=1e-6) and out["quality"][1] < 0.02
    assert out["scored"] == [1, 1, 0, 0, 0] and out["noCells"] == [1]
    stats = out["stats"]
    assert stats["total"] == 2 and stats["unscored"] == 3 and stats["worst"] == 1 and stats["poor"] == 1
    assert sum(stats["bins"]) == 2 and stats["bins"][9] == 1 and stats["bins"][0] == 1
    assert out["verdicts"] == ["bad", "good", "warn", "bad", "good"]
    assert out["poor"] == [1] and out["crossing"] == [] and out["triangles"] == 2 * 36 and out["edges"] == 12 and out["values"] == 24
    assert out["colors"][0][0] > 0.8 and out["colors"][1][2] > out["colors"][1][0] and out["colors"][2] == out["colors"][1]
    rows = {row["id"]: row for row in out["rows"]}
    assert list(rows) == ["part-surface", "inlet", "sides", "my_wall"] and rows["part-surface"]["triangles"] == 3
    assert rows["sides"]["hiddenByDefault"] and not rows["inlet"]["hiddenByDefault"] and rows["my_wall"]["label"] == "my wall"
