import { createViewport, paintLegend } from "/studio-static/viewport.js";
import { compareButton, initGuide } from "/studio-static/guide.js";
import { consumeDismissal, contextMenuOpen, openContextMenu } from "/studio-static/contextmenu.js";
import { MOUSE_SCHEMES, nextScale, rankCommands, solveProgress } from "/studio-static/controls.js";
import { createLayout } from "/studio-static/layout.js";
import { cellCorners, cellsInRange, patchRows, qualityColor, qualityStats, qualityVerdict, tetQualities } from "/studio-static/meshview.js";
import { menuFor } from "/studio-static/menus.js";
import { SECTION_FIELDS, SECTION_TYPES, sectionPreviewSvg, sectionProperties, shellPreviewSvg } from "/studio-static/members.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const OUTPUT_TARGETS = [
  ["cpp_header", "C++", "generated/params.hpp"], ["py_module", "Python", "generated/params.py"],
  ["rust", "Rust", "generated/params.rs"], ["openscad", "OpenSCAD", "generated/params.scad"],
  ["markdown", "Markdown", "docs/parameters.md"], ["latex", "LaTeX", "docs/parameters.tex"],
  ["solidworks", "SolidWorks", "generated/update_solidworks.bas"], ["nx", "Siemens NX", "generated/update_nx.py"],
  ["ansys", "ANSYS", "generated/params.inp"], ["openfoam", "OpenFOAM", "generated/paramfabricDict"],
  ["comsol", "COMSOL", "generated/params.java"],
];
const UNITS = [
  ["m", "length"], ["mm", "length"], ["cm", "length"], ["km", "length"], ["inch", "length"], ["ft", "length"],
  ["m^2", "area"], ["mm^2", "area"], ["m^3", "volume"], ["L", "volume"],
  ["s", "time"], ["ms", "time"], ["min", "time"], ["h", "time"], ["Hz", "frequency"], ["rpm", "angular speed"],
  ["kg", "mass"], ["g", "mass"], ["t", "mass"],
  ["N", "force"], ["kN", "force"], ["lbf", "force"],
  ["Pa", "pressure / stress"], ["kPa", "pressure / stress"], ["MPa", "pressure / stress"], ["GPa", "pressure / stress"],
  ["N/m^2", "pressure / stress"], ["bar", "pressure"], ["psi", "pressure"],
  ["J", "energy"], ["W", "power"], ["kW", "power"], ["N*m", "torque"],
  ["K", "temperature"], ["degC", "temperature"], ["deg", "angle"], ["rad", "angle"],
  ["m/s", "velocity"], ["km/h", "velocity"], ["m/s^2", "acceleration"],
  ["kg/m^3", "density"], ["m^2/s", "kinematic viscosity"], ["Pa*s", "dynamic viscosity"],
  ["W/(m*K)", "thermal conductivity"], ["J/(kg*K)", "specific heat"], ["dimensionless", "ratio"], ["percent", "ratio"],
];
const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"];
const ICONS = { model: "i-folder", body: "i-cube", face: "i-face", feature: "i-extrude", parameter: "i-param", domain: "i-flow", edge: "i-ruler" };
const FEATURE_LABELS = { extrude: "Extrude", split: "Split", boolean: "Boolean", flow_volume: "Flow volume", scale: "Scale", offset_face: "Push/Pull", fillet: "Fillet", chamfer: "Chamfer" };

const state = {
  localMode: false,
  fabric: { version: 1, parameters: [] },
  draft: { version: 1, parameters: [] },
  diagnostics: null,
  workbench: null,
  models: new Map(),
  selection: null,
  expanded: new Set(["features"]),
  step: 1,
  rightTab: "inspector",
  mesh: null,
  job: null,
  residuals: [],
  logCount: 0,
  result: null,
  resultField: null,
  pickSet: [],
  undo: [],
  redo: [],
  meshView: { style: "shaded", hidden: new Set(), range: null, source: null, analysis: null, rows: [] },
  hidden: new Set(),
  sync: { items: [], busy: false },
  importCandidates: [],
  pendingCad: null,
  editing: null,
  saving: false,
};
let viewport = { available: false };
let toastTimer;
let evaluateTimer;
let evaluateSequence = 0;

// helpers

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function fmt(value, digits = 5) {
  if (typeof value !== "number" || !Number.isFinite(value)) return String(value ?? "—");
  const magnitude = Math.abs(value);
  if (magnitude !== 0 && (magnitude >= 1e6 || magnitude < 1e-3)) return value.toExponential(Math.max(1, digits - 2));
  return String(Number(value.toPrecision(digits)));
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }

function notify(message, isError = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), isError ? 5200 : 3000);
}

async function api(path, options = {}) {
  const headers = { ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers };
  const token = localStorage.getItem("paramfabric.access-token");
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map(item => item.msg || JSON.stringify(item)).join("; ") : data.detail;
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return data;
}

function download(name, content, type = "text/plain") {
  const anchor = document.createElement("a");
  anchor.href = URL.createObjectURL(new Blob([content], { type }));
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(anchor.href), 1000);
}

function parameter(name, source = state.draft) {
  return source.parameters.find(item => item.name === name);
}

function isFormula(param) {
  return typeof param?.value === "string" && (param.type === "float" || param.type === "int");
}

function resolved(name) { return state.diagnostics?.resolved?.[name]; }

function workbenchReady() { return Boolean(state.workbench); }

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function issuesFor(name) { return (state.diagnostics?.issues || []).filter(issue => issue.parameter === name); }

function dirty() { return JSON.stringify(state.draft) !== JSON.stringify(state.fabric); }

function parseValue(raw, type) {
  const text = String(raw).trim();
  if (type === "str") return text;
  if (type === "bool") return text.toLowerCase() === "true";
  if (text !== "" && Number.isFinite(Number(text))) {
    if (type === "int" && !Number.isInteger(Number(text))) return text;
    return Number(text);
  }
  return text;
}

function lengthUnitScale(unit) {
  return { mm: 1e-3, cm: 1e-2, m: 1, km: 1e3, inch: 0.0254, in: 0.0254, ft: 0.3048, um: 1e-6, dm: 0.1 }[unit];
}

// session & loading

async function bootstrap() {
  const fromLaunch = new URLSearchParams(location.hash.slice(1)).get("token");
  if (fromLaunch) {
    localStorage.setItem("paramfabric.access-token", fromLaunch);
    history.replaceState(null, "", location.pathname + location.search);
  }
  try {
    const session = await api("/api/local/session");
    state.localMode = true;
    if (session.token) localStorage.setItem("paramfabric.access-token", session.token);
    $("#sessionPill").hidden = false;
    $("#enterpriseLogin").hidden = true;
    await loadAll();
    setInterval(pollSync, 2500);
    pollSync();
  } catch {
    state.localMode = false;
    $("#enterpriseLogin").hidden = false;
    setDiag("diagUnits", "idle", "Sign in");
    setDiag("diagDag", "idle", "Sign in");
    setDiag("diagSync", "idle", "Local mode only");
    if (localStorage.getItem("paramfabric.access-token")) {
      try { await connectEnterprise(); } catch (error) { notify(error.message, true); }
    }
    renderAll();
  }
}

async function connectEnterprise() {
  const typed = $("#tokenInput").value.trim();
  if (typed) localStorage.setItem("paramfabric.access-token", typed);
  const data = await api("/api/parameters");
  state.fabric = { version: 1, parameters: data.parameters };
  state.draft = clone(state.fabric);
  state.diagnostics = {
    ok: true, units_ok: true, dag_ok: true, issues: [], links: [],
    resolved: Object.fromEntries(Object.entries(data.resolved).map(([name, value]) => [name, { value, unit: parameter(name)?.unit }])),
  };
  try { state.diagnostics.links = (await api("/api/graph")).links; } catch { /* graph is optional */ }
  try {
    applyWorkbench(await api("/api/workbench"));
    await loadModels();
    viewport.fit?.();
  } catch (error) {
    notify(`Workbench unavailable: ${error.message}`, true);
  }
  setDiag("diagUnits", "good", "Server verified");
  setDiag("diagDag", "good", "Acyclic");
  $("#sessionPill").hidden = false;
  $("#sessionPill span").textContent = "Enterprise";
  $("#enterpriseLogin").hidden = true;
  renderAll();
  refreshQueue();
  notify("Connected to the enterprise workspace, edits become change requests; meshing and solving run on the task queue.");
}

async function loadAll() {
  const [fabric, workbench] = await Promise.all([api("/api/local/fabric"), api("/api/workbench")]);
  state.fabric = { version: fabric.version || 1, parameters: fabric.parameters || [] };
  state.draft = clone(state.fabric);
  applyWorkbench(workbench);
  await loadModels();
  state.diagnostics = workbench.diagnostics;
  if (workbench.latest_job?.status === "completed") await loadJob(workbench.latest_job.id, true);
  renderAll();
  viewport.fit?.();
  refreshQueue();
}

function applyWorkbench(workbench) {
  state.workbench = workbench;
  state.diagnostics = workbench.diagnostics;
  if (workbench.mesh) state.mesh = { ...(state.mesh || {}), ...workbench.mesh };
}

async function loadModels() {
  const ids = new Set((state.workbench?.models || []).map(model => model.id));
  for (const id of [...state.models.keys()]) if (!ids.has(id)) state.models.delete(id);
  await Promise.all([...ids].filter(id => !state.models.has(id)).map(async id => {
    state.models.set(id, await api(`/api/workbench/geometry/${id}`));
  }));
  if (viewport.available) {
    viewport.setModels(state.models);
    viewport.setScene(state.workbench?.scene || null);
  }
}

function governanceNote(outcome) {
  if (outcome?.change_request) notify(`Change request ${outcome.change_request.slice(0, 8)} opened for review`);
}

async function refreshWorkbench(workbench) {
  const hadContent = viewport.hasContent?.();
  applyWorkbench(workbench || await api("/api/workbench"));
  const fabric = await api("/api/local/fabric");
  state.fabric = { version: fabric.version || 1, parameters: fabric.parameters || [] };
  const unsaved = state.draft.parameters.filter(item => {
    const saved = parameter(item.name, state.fabric);
    return saved && JSON.stringify(saved) !== JSON.stringify(item) && state.editing === item.name;
  });
  state.draft = clone(state.fabric);
  unsaved.forEach(item => { const index = state.draft.parameters.findIndex(p => p.name === item.name); state.draft.parameters[index] = item; });
  await loadModels();
  renderAll();
  if (!hadContent) viewport.fit?.();
}

// live evaluation & persistence

function scheduleEvaluate(delay = 90) {
  if (!workbenchReady()) { renderAll(); return; }
  clearTimeout(evaluateTimer);
  evaluateTimer = setTimeout(evaluateDraft, delay);
}

async function evaluateDraft() {
  const sequence = ++evaluateSequence;
  try {
    const result = await api("/api/workbench/evaluate", { method: "POST", body: JSON.stringify(state.draft) });
    if (sequence !== evaluateSequence) return;
    state.diagnostics = result.diagnostics;
    if (state.workbench) state.workbench.scene = result.scene;
    if (viewport.available) viewport.setScene(result.scene);
    renderDiagnostics();
    renderParamList();
    renderValidation();
    renderEvalLine();
    renderLineage();
    updateMorphValues();
    renderHud();
  } catch (error) {
    if (sequence === evaluateSequence) notify(`Evaluation failed: ${error.message}`, true);
  }
}

async function commit(message = "Fabric saved") {
  if (state.saving) return false;
  state.saving = true;
  try {
    if (state.localMode) {
      const previous = clone(state.fabric);
      await api("/api/local/fabric", { method: "POST", body: JSON.stringify(state.draft) });
      if (JSON.stringify(previous) !== JSON.stringify(state.draft)) recordUndo({ kind: "fabric", fabric: previous, label: message });
      state.fabric = clone(state.draft);
      notify(message);
      if ($("#liveSync").checked) await pollSync(true);
    } else {
      const response = await api("/api/change-requests", {
        method: "POST",
        body: JSON.stringify({ proposed_fabric: state.draft, rationale: "Updated from Paramfabric Studio" }),
      });
      notify(`Change request ${response.id.slice(0, 8)} submitted for review`);
      state.draft = clone(state.fabric);
      refreshQueue();
    }
    renderAll();
    return true;
  } catch (error) {
    notify(`Not saved: ${error.message}`, true);
    renderAll();
    return false;
  } finally {
    state.saving = false;
  }
}

function discard() {
  state.draft = clone(state.fabric);
  state.editing = state.selection?.kind === "parameter" ? state.selection.name : null;
  scheduleEvaluate(0);
  renderInspector();
}

// diagnostics bar & validation

function setDiag(id, status, text) {
  const node = $(`#${id}`);
  node.dataset.state = status;
  node.querySelector("span:last-child").textContent = text;
}

function renderDiagnostics() {
  const diagnostics = state.diagnostics;
  if (state.localMode && diagnostics) {
    const unitIssues = diagnostics.issues.filter(issue => issue.kind === "unit").length;
    setDiag("diagUnits", unitIssues ? "critical" : "good", unitIssues ? `${unitIssues} unit error${unitIssues > 1 ? "s" : ""}` : "Pint verified");
    setDiag("diagDag", diagnostics.dag_ok ? "good" : "critical", diagnostics.dag_ok ? `Acyclic · ${diagnostics.links.length} links` : `Loop: ${diagnostics.cycle.join(" → ")}`);
  }
  const quality = state.mesh?.quality;
  if (quality) {
    const status = { good: "good", fair: "warning", poor: "critical" }[quality.grade];
    setDiag("diagMesh", status, `${quality.grade[0].toUpperCase()}${quality.grade.slice(1)} · ${quality.cells.toLocaleString()} cells`);
  } else {
    setDiag("diagMesh", "idle", "Not meshed");
  }
  const job = state.job;
  if (job) {
    const map = { running: ["running", "Running"], queued: ["running", "Queued"], completed: ["good", "Converged"], failed: ["critical", "Failed"], cancelled: ["warning", "Cancelled"] };
    const [status, label] = map[job.status] || ["idle", job.status];
    const converged = job.status === "completed" && state.result && state.result.converged === false ? ["warning", "Completed · not converged"] : null;
    setDiag("diagSolver", (converged || [status])[0], `${(converged || [0, label])[1]} · ${job.engine}`);
  }
  const syncItems = state.sync.items;
  if (state.localMode) {
    const conflicts = syncItems.filter(item => ["conflict", "unit_mismatch"].includes(item.state)).length;
    const drift = syncItems.filter(item => item.state === "drift").length;
    if (!syncItems.length) setDiag("diagSync", "idle", "No matching constants");
    else if (conflicts) setDiag("diagSync", "critical", `${conflicts} conflict${conflicts > 1 ? "s" : ""}`);
    else if (drift) setDiag("diagSync", "warning", `${drift} drifted`);
    else setDiag("diagSync", "good", `${syncItems.length} in sync${$("#liveSync").checked ? " · live" : ""}`);
  }
  updateSteps();
}

function renderValidation() {
  const list=$("#validationList");
  const entries = [];
  for (const issue of state.diagnostics?.issues || []) entries.push({ level: "error", kind: issue.kind, message: issue.message, parameter: issue.parameter });
  for (const message of state.workbench?.scene?.errors || []) entries.push({ level: "error", kind: "geometry", message });
  for (const message of state.mesh?.warnings || []) entries.push({ level: "warn", kind: "mesh", message });
  for (const model of state.workbench?.models || []) for (const message of model.warnings) entries.push({ level: "warn", kind: model.filename, message });
  for (const item of state.sync.items.filter(i => ["conflict", "unit_mismatch"].includes(i.state))) {
    entries.push({ level: "error", kind: "code sync", message: `${item.path}:${item.line} ${item.name}, ${item.state === "conflict" ? "changed in both source and fabric" : `unit ${item.unit} incompatible with ${item.parameter_unit}`}`, parameter: item.parameter });
  }
  if (state.job?.status === "failed") entries.push({ level: "error", kind: "solver", message: state.job.error || "Solver failed" });
  const errors = entries.filter(entry => entry.level === "error").length;
  $("#validationCount").textContent = String(entries.length);
  $("#validationCount").classList.toggle("alert", errors > 0);
  $("#validationToggle").classList.toggle("has-issues", errors > 0);
  list.replaceChildren();
  if (!entries.length) {
    list.innerHTML = '<div class="issue ok"><svg><use href="#i-check"/></svg><span class="kind">All clear</span><p>Units, dependencies, geometry, and sources validate.</p></div>';
    return;
  }
  for (const entry of entries) {
    const button = document.createElement("button");
    button.className = `issue${entry.level === "warn" ? " warn" : ""}`;
    button.innerHTML = `<svg><use href="#i-alert"/></svg><span class="kind">${escapeHtml(entry.kind)}</span><p>${escapeHtml(entry.message)}</p>`;
    if (entry.parameter) button.addEventListener("click", () => selectParameter(entry.parameter));
    list.append(button);
  }
}

function updateSteps() {
  const hasGeometry = (state.workbench?.models || []).length > 0;
  const analysis = state.workbench?.physics?.analysis;
  const physicsReady = !!analysis && state.workbench.physics.parameters.every(name => parameter(name, state.fabric));
  const done = { 1: hasGeometry, 2: hasGeometry && physicsReady, 3: !!state.mesh?.quality, 4: !!state.result };
  $$(".step").forEach(step => {
    step.classList.toggle("active", Number(step.dataset.step) === state.step);
    step.classList.toggle("done", !!done[step.dataset.step] && Number(step.dataset.step) !== state.step);
  });
}

// left panel: scene tree

function treeRow({ depth, label, meta = "", icon, key, expandable = false, open = false, selected = false, badge = "", badgeWarn = false, onClick, context = null }) {
  const row = document.createElement("button");
  if (context) row.addEventListener("contextmenu", event => { event.preventDefault(); showMenu(context(), event.clientX, event.clientY); });
  row.className = `tree-row${selected ? " selected" : ""}`;
  row.style.setProperty("--depth", depth);
  row.setAttribute("role", "treeitem");
  if (expandable) row.setAttribute("aria-expanded", String(open));
  row.innerHTML = `${expandable ? `<svg class="twisty${open ? "" : " closed"}"><use href="#i-chevron"/></svg>` : '<span class="spacer"></span>'}<svg><use href="#${icon}"/></svg><span class="label">${escapeHtml(label)}</span>${badge ? `<span class="badge${badgeWarn ? " warn" : ""}">${escapeHtml(badge)}</span>` : ""}${meta ? `<span class="meta">${escapeHtml(meta)}</span>` : ""}`;
  row.addEventListener("click", event => {
    if (expandable && (event.target.closest(".twisty") || event.detail === 2 || !onClick)) {
      state.expanded.has(key) ? state.expanded.delete(key) : state.expanded.add(key);
      renderTree();
      if (event.target.closest(".twisty")) return;
    }
    onClick?.();
  });
  return row;
}

function isSelected(kind, extra = {}) {
  const selection = state.selection;
  if (!selection || selection.kind !== kind) return false;
  return Object.entries(extra).every(([key, value]) => selection[key] === value);
}

function renderTree() {
  const tree = $("#sceneTree");
  tree.replaceChildren();
  const models = state.workbench?.models || [];
  if (!workbenchReady()) {
    tree.innerHTML = '<div class="tree-empty">Sign in to load the workbench.</div>';
    return;
  }
  if (!models.length) {
    tree.innerHTML = '<div class="tree-empty">No CAD imported yet. Use <b>Import CAD</b> or load the demo part.</div>';
  }
  for (const summary of models) {
    const modelKey = `model:${summary.id}`;
    const open = !state.expanded.has(`closed:${modelKey}`);
    const row = treeRow({
      depth: 0, label: summary.assembly || summary.name, icon: ICONS.model, key: `closed:${modelKey}`, expandable: true, open,
      badge: `${summary.format.toUpperCase()} · ${summary.length_unit}`, badgeWarn: summary.warnings.length > 0,
      selected: isSelected("model", { id: summary.id }),
      onClick: () => select({ kind: "model", id: summary.id }),
      context: () => modelTarget(summary.id),
    });
    tree.append(row);
    if (!open) continue;
    const full = state.models.get(summary.id);
    for (const body of summary.bodies) {
      const key = `${summary.id}/${body.id}`;
      const bodyOpen = state.expanded.has(key);
      tree.append(treeRow({
        depth: 1, label: body.name, icon: ICONS.body, key, expandable: true, open: bodyOpen,
        meta: body.kind, selected: isSelected("body", { key }) || state.pickSet.some(item => item.kind === "body" && item.key === key), onClick: () => select({ kind: "body", key }),
        badge: state.hidden.has(key) ? "hidden" : "", context: () => entityTarget({ kind: "body", key }),
      }));
      if (!bodyOpen) continue;
      for (const axis of ["x", "y", "z"]) {
        const name = body.dimension_parameters[axis];
        tree.append(treeRow({
          depth: 2, label: name ? `${axis.toUpperCase()} extent → ${name}` : `${axis.toUpperCase()} extent (unbound)`, icon: "i-link",
          selected: name ? isSelected("parameter", { name }) : false,
          meta: name && resolved(name) ? `${fmt(resolved(name).value)} ${resolved(name).unit}` : "",
          onClick: () => name ? selectParameter(name) : select({ kind: "body", key }),
        }));
      }
      const facesKey = `faces:${key}`;
      const facesOpen = state.expanded.has(facesKey);
      tree.append(treeRow({ depth: 2, label: "Faces", icon: ICONS.face, key: facesKey, expandable: true, open: facesOpen, meta: String(body.stats.faces), onClick: null }));
      if (facesOpen && full) {
        const faces = full.bodies.find(item => item.id === body.id)?.faces || [];
        faces.slice(0, 250).forEach(face => tree.append(treeRow({
          depth: 3, label: face.name, icon: ICONS.face, meta: face.surface, badge: face.approximate ? "approx" : "", badgeWarn: true,
          selected: state.pickSet.some(item => item.kind === "face" && item.key === key && item.faceId === face.id), onClick: () => select({ kind: "face", key, faceId: face.id }),
          context: () => entityTarget({ kind: "face", key, faceId: face.id }),
        })));
      }
    }
  }
  const features = state.workbench?.settings?.features || [];
  if (models.length) {
    const open = state.expanded.has("features");
    tree.append(treeRow({ depth: 0, label: "Parametric features", icon: "i-extrude", key: "features", expandable: true, open, meta: String(features.length), onClick: null }));
    if (open) {
      if (!features.length) tree.append(Object.assign(document.createElement("div"), { className: "tree-empty", textContent: "Use the CAD feature tools below." }));
      features.forEach(feature => tree.append(treeRow({
        depth: 1, label: feature.name || FEATURE_LABELS[feature.type], icon: feature.type === "flow_volume" ? ICONS.domain : ICONS.feature,
        meta: Object.values(feature.params || {}).join(", "), selected: isSelected("feature", { id: feature.id }),
        onClick: () => select({ kind: "feature", id: feature.id }),
        context: () => featureTarget(feature),
      })));
    }
    const named = state.workbench?.settings?.named_selections || [];
    if (named.length) {
      const namedOpen=!state.expanded.has("closed:named");
      tree.append(treeRow({ depth: 0, label: "Named selections", icon: "i-link", key: "closed:named", expandable: true, open: namedOpen, meta: String(named.length), onClick: null }));
      if (namedOpen) {
        named.forEach(selection => tree.append(treeRow({
          depth: 1, label: selection.name, icon: selection.kind === "face" ? ICONS.face : selection.kind === "edge" ? "i-ruler" : ICONS.body,
          meta: `${selection.items.length} ${selection.kind}${selection.items.length === 1 ? "" : "s"}`,
          onClick: () => selectNamed(selection.id), context: () => namedTarget(selection),
        })));
      }
    }
    if (state.mesh?.quality) {
      tree.append(treeRow({ depth: 0, label: "Volume mesh", icon: "i-mesh", meta: `${state.mesh.quality.cells.toLocaleString()} cells`, onClick: () => setStep(3),
        context: () => ({ kind: "mesh", subtitle: `${state.mesh.quality.cells.toLocaleString()} cells` }) }));
    }
    if (state.result) {
      tree.append(treeRow({ depth: 0, label: `Results · ${state.result.analysis.toUpperCase()}`, icon: "i-pulse", meta: state.result.engine, onClick: () => setStep(4),
        context: () => ({ kind: "results", subtitle: state.result.method || state.result.engine }) }));
    }
  }
}

// left panel: fabric list, lineage

function geometryUsage() {
  const usage = new Map();
  const add = (name, label) => { if (!name) return; usage.set(name, [...(usage.get(name) || []), label]); };
  for (const model of state.workbench?.models || []) {
    for (const body of model.bodies) for (const [axis, name] of Object.entries(body.dimension_parameters)) add(name, `${body.name} ${axis.toUpperCase()}`);
  }
  for (const feature of state.workbench?.settings?.features || []) for (const [role, name] of Object.entries(feature.params || {})) add(name, `${feature.name} ${role}`);
  return usage;
}

function renderTagFilter() {
  const select=$("#tagFilter");
  const current = select.value;
  const tags = [...new Set(state.draft.parameters.flatMap(item => item.tags || []))].sort();
  select.replaceChildren(new Option("All tags", ""), ...tags.map(tag => new Option(tag, tag)));
  select.value = tags.includes(current) ? current : "";
}

function renderParamList() {
  const list = $("#paramList");
  const query = $("#paramSearch").value.trim().toLowerCase();
  const tag = $("#tagFilter").value;
  const usage = geometryUsage();
  const syncCounts = new Map();
  state.sync.items.forEach(item => syncCounts.set(item.parameter, (syncCounts.get(item.parameter) || 0) + 1));
  $("#paramCount").textContent = String(state.draft.parameters.length);
  list.replaceChildren();
  const visible = state.draft.parameters.filter(item =>
    (!query || `${item.name} ${item.description} ${item.unit} ${item.value}`.toLowerCase().includes(query)) &&
    (!tag || (item.tags || []).includes(tag)));
  if (!visible.length) {
    list.innerHTML = `<div class="tree-empty">${state.draft.parameters.length ? "No parameters match the filter." : "No parameters yet, import CAD or create one."}</div>`;
    return;
  }
  for (const item of visible) {
    const issues = issuesFor(item.name);
    const value = resolved(item.name);
    const row = document.createElement("button");
    row.className = `param-row${isSelected("parameter", { name: item.name }) ? " selected" : ""}${issues.length ? " error" : ""}`;
    row.setAttribute("role", "option");
    const outputs = Object.keys(item.bindings || {}).length;
    const chips = [
      usage.has(item.name) ? `<span class="bind-chip" title="${escapeHtml(usage.get(item.name).join(", "))}"><svg><use href="#i-cube"/></svg>CAD</span>` : "",
      outputs ? `<span class="bind-chip" title="${escapeHtml(Object.keys(item.bindings).join(", "))}"><svg><use href="#i-export"/></svg>${outputs}</span>` : "",
      syncCounts.get(item.name) ? `<span class="bind-chip" title="Matched source constants"><svg><use href="#i-code"/></svg>${syncCounts.get(item.name)}</span>` : "",
    ].join("");
    const display = issues.length ? `<span class="status-icon critical"><svg><use href="#i-alert"/></svg>${escapeHtml(issues[0].kind)}</span>`
      : value && typeof value.value === "number" ? `${fmt(value.value)}` : escapeHtml(String(item.value));
    row.innerHTML = `<span class="param-name">${escapeHtml(item.name)}</span><span class="param-value">${display}</span><span class="param-sub"><span class="unit-pill">${escapeHtml(item.unit)}</span>${isFormula(item) ? `<span class="formula">= ${escapeHtml(item.value)}</span>` : ""}${chips}</span>`;
    row.addEventListener("click", () => selectParameter(item.name));
    row.addEventListener("contextmenu", event => { event.preventDefault(); showMenu(parameterTarget(item), event.clientX, event.clientY); });
    list.append(row);
  }
}

let lineageSimulation;
function renderLineage() {
  const host=$("#lineage");
  if (typeof d3 === "undefined" || host.closest("[hidden]")) return;
  const links = (state.diagnostics?.links || []).map(link => ({ ...link }));
  const nodes = state.draft.parameters.map(item => ({ id: item.name, formula: isFormula(item) }));
  $("#graphCount").textContent = `${links.length} link${links.length === 1 ? "" : "s"}`;
  lineageSimulation?.stop();
  host.replaceChildren();
  if (!nodes.length) return;
  const width = host.clientWidth || 280; const height = host.clientHeight || 210;
  const svg = d3.select(host).append("svg").attr("viewBox", `0 0 ${width} ${height}`);
  const cycle = new Set(state.diagnostics?.cycle || []);
  lineageSimulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(node => node.id).distance(46))
    .force("charge", d3.forceManyBody().strength(-150))
    .force("collide", d3.forceCollide(16))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("x", d3.forceX(width / 2).strength(0.06)).force("y", d3.forceY(height / 2).strength(0.09));
  const link = svg.append("g").selectAll("line").data(links).join("line").attr("class", "link");
  const node = svg.append("g").selectAll("g").data(nodes).join("g").attr("class", "node")
    .on("click", (_, datum) => selectParameter(datum.id));
  node.append("circle").attr("r", 5).attr("fill", datum => cycle.has(datum.id) ? "#d03b3b" : datum.formula ? "#d95926" : "#3987e5");
  node.append("title").text(datum => datum.id);
  node.append("text").attr("x", 8).attr("y", 3).text(datum => datum.id.length > 18 ? `${datum.id.slice(0, 17)}…` : datum.id);
  lineageSimulation.on("tick", () => {
    nodes.forEach(datum => { datum.x = Math.max(8, Math.min(width - 8, datum.x)); datum.y = Math.max(8, Math.min(height - 8, datum.y)); });
    link.attr("x1", d => d.source.x).attr("y1", d => d.source.y).attr("x2", d => d.target.x).attr("y2", d => d.target.y);
    node.attr("transform", d => `translate(${d.x},${d.y})`);
  });
}

// selection & inspector

function isEntity(selection) {
  return Boolean(selection) && ["body", "face", "edge"].includes(selection.kind);
}

function sameEntity(a, b) {
  return a.kind === b.kind && a.key === b.key && (a.kind === "body" || (a.kind === "face" ? a.faceId === b.faceId : a.points === b.points || JSON.stringify(a.points) === JSON.stringify(b.points)));
}

// `keepSet`: the selection is one member of state.pickSet (a multi-selection); otherwise it replaces the set
function select(selection, { keepSet = false } = {}) {
  state.selection = selection;
  state.editing = selection?.kind === "parameter" ? selection.name : null;
  if (!keepSet) state.pickSet = isEntity(selection) ? [selection] : [];
  if (viewport.available) {
    viewport.select(isEntity(selection) ? selection : null, state.pickSet.filter(item => item !== selection));
  }
  if (selection && state.rightTab !== "inspector") setRightTab("inspector");
  renderTree();
  renderParamList();
  renderInspector();
  renderMorphDock();
  renderHud();
}

function selectParameter(name) {
  select({ kind: "parameter", name });
  const row = $$(".param-row").find(node => node.querySelector(".param-name")?.textContent === name);
  row?.scrollIntoView({ block: "nearest" });
}

function bodySummary(key) {
  const [modelId, bodyId] = key.split("/");
  const model=state.workbench?.models.find(item => item.id === modelId);
  const stored = state.models.get(modelId)?.bodies.find(item => item.id === bodyId);
  const entry = sceneEntry(key);
  const full=entry?.tessellation ? { ...stored, ...entry.tessellation } : stored;
  return { model, body: model?.bodies.find(item => item.id === bodyId), full, entry };
}

function sceneEntry(key) {
  return state.workbench?.scene?.bodies.find(entry => entry.key === key);
}

function inspectorHead(icon, title, subtitle) {
  return `<div class="insp-head"><span class="insp-icon"><svg><use href="#${icon}"/></svg></span><div><h2>${escapeHtml(title)}</h2>${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ""}</div></div>`;
}

function renderInspector() {
  const host = $("#inspector");
  const selection = state.selection;
  if (!selection) return renderOverview(host);
  if (selection.kind === "parameter") return renderParameterEditor(host, selection.name);
  if (selection.kind === "body") return renderBodyInspector(host, selection.key);
  if (selection.kind === "face") return renderFaceInspector(host, selection);
  if (selection.kind === "edge") return renderEdgeInspector(host, selection);
  if (selection.kind === "feature") return renderFeatureInspector(host, selection.id);
  if (selection.kind === "model") return renderModelInspector(host, selection.id);
  return renderOverview(host);
}

function renderOverview(host) {
  const models = state.workbench?.models || [];
  const bodies = models.reduce((sum, model) => sum + model.bodies.length, 0);
  host.innerHTML = `${inspectorHead("i-cube", "Workspace", state.localMode ? "Local Admin · data stays on this computer" : "Enterprise · edits become change requests")}
    <table class="kv-table"><tbody>
      <tr><td>CAD documents</td><td>${models.length}</td></tr>
      <tr><td>Bodies</td><td>${bodies}</td></tr>
      <tr><td>Parameters</td><td>${state.draft.parameters.length}</td></tr>
      <tr><td>Dependency links</td><td>${state.diagnostics?.links?.length ?? 0}</td></tr>
      <tr><td>Analysis</td><td>${escapeHtml(state.workbench?.physics?.analysis?.toUpperCase() || "—")}</td></tr>
    </tbody></table>
    <div class="note"><svg><use href="#i-info"/></svg><span>Click a face, edge, or body in the viewport, or a parameter in the Fabric tab, to inspect it. Bound dimensions morph the geometry live as you drag sliders or edit formulas.</span></div>
    <div class="btn-row"><button class="tbtn" id="ovNew"><svg><use href="#i-plus"/></svg><span>New parameter</span></button></div>`;
  $("#ovNew").addEventListener("click", newParameter);
}

function lengthParameters() {
  return state.draft.parameters.filter(item => resolved(item.name)?.dimensionality === "[length]");
}

function renderBodyInspector(host, key) {
  const { model, body, full } = bodySummary(key);
  if (!body) return renderOverview(host);
  const entry = sceneEntry(key);
  const size = entry ? entry.bbox_max.map((v, i) => v - entry.bbox_min[i]) : [0, 0, 0];
  const original = body.bbox_max.map((v, i) => v - body.bbox_min[i]);
  const unit = model.length_unit; const scale = lengthUnitScale(unit) || 1;
  const features = (state.workbench.settings.features || []).filter(feature => feature.body === key || feature.tool === key);
  host.innerHTML = `${inspectorHead("i-cube", body.name, `${body.kind} · ${model.filename}`)}
    <table class="kv-table"><tbody>
      ${["X", "Y", "Z"].map((axis, i) => `<tr><td>${axis} extent</td><td>${fmt(size[i] / scale)} ${unit}${Math.abs(size[i] - original[i]) > 1e-12 ? ` <span class="muted">(was ${fmt(original[i] / scale)})</span>` : ""}</td></tr>`).join("")}
      <tr><td>Faces · edges · vertices</td><td>${body.stats.faces} · ${body.stats.edges} · ${body.stats.vertices}</td></tr>
      <tr><td>Display triangles</td><td>${body.stats.triangles.toLocaleString()}</td></tr>
      ${full ? `<tr><td>Surface area</td><td>${fmt(full.faces.reduce((s, f) => s + f.area, 0) / scale / scale)} ${unit}²</td></tr>` : ""}
    </tbody></table>
    <div class="section-head"><span>Dimension constraints</span></div>
    <div id="bindRows"></div>
    ${features.length ? `<div class="section-head"><span>Features</span></div>${features.map(feature => `<div class="card"><h3>${escapeHtml(feature.name)}</h3><span class="muted">${escapeHtml(Object.values(feature.params || {}).join(", ") || feature.op || "")}</span></div>`).join("")}` : ""}
    <div class="btn-row"><button class="tbtn" data-feature-body="extrude"><svg><use href="#i-extrude"/></svg><span>Extrude</span></button><button class="tbtn" data-feature-body="split"><svg><use href="#i-split"/></svg><span>Split</span></button><button class="tbtn" data-feature-body="scale"><svg><use href="#i-scale"/></svg><span>Scale</span></button></div>`;
  const rows=$("#bindRows", host);
  ["x", "y", "z"].forEach(axis => rows.append(bindControl(key, axis, body.dimension_parameters[axis], original["xyz".indexOf(axis)] / scale, unit)));
  $$("[data-feature-body]", host).forEach(button => button.addEventListener("click", () => openFeature(button.dataset.featureBody, { body: key })));
}

function bindControl(key, axis, current, extent, unit) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const options = lengthParameters().map(item => `<option value="${escapeHtml(item.name)}"${item.name === current ? " selected" : ""}>${escapeHtml(item.name)} (${fmt(resolved(item.name).value)} ${escapeHtml(item.unit)})</option>`).join("");
  wrap.innerHTML = `<span>${axis.toUpperCase()} extent driven by</span><div class="row-unit"><select aria-label="Parameter driving ${axis} extent"><option value="">— not bound —</option>${options}</select><button class="tbtn" type="button" title="Create a parameter from the current extent">New</button></div>`;
  $("select", wrap).addEventListener("change", event => bindDimension(key, axis, event.target.value || null));
  $("button", wrap).addEventListener("click", async () => {
    const base=(bodySummary(key).body?.name || "body").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || "body";
    let name = `${base}_${{ x: "length", y: "width", z: "height" }[axis]}`;
    for (let n = 2; parameter(name); n += 1) name = `${base}_${{ x: "length", y: "width", z: "height" }[axis]}_${n}`;
    state.draft.parameters.push({ name, value: Number(extent.toPrecision(6)), unit, type: "float", tags: ["geometry", "cad"], description: `${axis.toUpperCase()} extent of ${bodySummary(key).body?.name}.`, rationale: "Created from the Inspector.", bindings: {} });
    if (await commit(`Created ${name}`)) await bindDimension(key, axis, name);
  });
  return wrap;
}

async function bindDimension(key, axis, name) {
  try {
    const workbench = await api("/api/workbench/geometry/bind", { method: "POST", body: JSON.stringify({ body: key, axis, parameter: name }) });
    await refreshWorkbench(workbench);
    notify(name ? `${axis.toUpperCase()} extent now driven by ${name}` : `${axis.toUpperCase()} extent unbound`);
  } catch (error) {
    notify(error.message, true);
    renderInspector();
  }
}

function faceAxis(face) {
  if (!face?.normal) return null;
  const index = face.normal.findIndex(component => Math.abs(Math.abs(component) - 1) < 1e-6);
  return index < 0 ? null : { axis: "xyz"[index], sign: face.normal[index] > 0 ? "+" : "-" };
}

function renderFaceInspector(host, selection) {
  const { model, body, full } = bodySummary(selection.key);
  const face = full?.faces.find(item => item.id === selection.faceId);
  if (!face) return renderBodyInspector(host, selection.key);
  const unit = model.length_unit; const scale = lengthUnitScale(unit) || 1;
  const aligned=faceAxis(face);
  const exactModel = model.kernel === "occ";
  const analysis = state.workbench?.physics?.analysis;
  const internal = state.workbench?.settings?.solver_options?.domain === "internal";
  const bcTypes=analysis === "fea" ? [["fixed", "Fixed support"], ["force", "Force"], ["pressure", "Pressure"]]
    : internal ? [["inlet", "Velocity inlet"], ["outlet", "Pressure outlet"]] : [];
  if (analysis === "cfd" && state.workbench?.settings?.solver_options?.heat_transfer?.enabled) bcTypes.push(["heat", "Heat source"]);
  const bcTopic = { inlet: "bc.velocity_inlet", outlet: "bc.pressure_outlet", heat: "physics.cht" };
  host.innerHTML = `${inspectorHead("i-face", face.name, `${face.surface} face of ${body.name}${exactModel ? " · exact B-Rep" : ""}`)}
    <table class="kv-table"><tbody>
      <tr><td>Surface</td><td>${escapeHtml(face.surface)}${face.approximate ? " (approx.)" : ""}</td></tr>
      <tr><td>Area</td><td>${fmt(face.area / scale / scale)} ${unit}²</td></tr>
      ${face.normal ? `<tr><td>Normal</td><td>(${face.normal.map(v => fmt(v, 3)).join(", ")})</td></tr>` : ""}
      ${face.centroid ? `<tr><td>Centroid</td><td>(${face.centroid.map(v => fmt(v / scale, 4)).join(", ")}) ${unit}</td></tr>` : ""}
    </tbody></table>
    ${exactModel && face.surface === "plane" ? `<div class="section-head"><span>Direct modelling</span></div><div class="btn-row"><button class="tbtn" id="pushPull"><svg><use href="#i-extrude"/></svg><span>Push / pull face</span></button></div>` : ""}
    ${bcTypes.length ? `<div class="section-head"><span>Boundary condition</span></div><div class="btn-row">${bcTypes.map(([type, label]) => `<button class="tbtn" data-bc="${type}"${bcTopic[type] ? ` data-help-id="${bcTopic[type]}"` : ""}>${label}</button>`).join("")}</div>` : ""}
    ${analysis === "fea" ? `<div class="section-head" data-help-id="structural.shell"><span>2D shell</span></div>
      <div class="btn-row"><button class="tbtn" id="assignShell" data-help-id="structural.shell"><svg><use href="#i-face"/></svg><span>Assign 2D shell…</span></button></div>` : ""}
    <div class="section-head"><span>Mesh</span></div><div class="btn-row"><button class="tbtn" id="refineFace"><svg><use href="#i-mesh"/></svg><span>Refine mesh at this face</span></button></div>
    ${aligned ? `<div class="section-head"><span>Bind face offset</span></div><p class="hint">This face is normal to ${aligned.axis.toUpperCase()}; its position is set by the body's ${aligned.axis.toUpperCase()} extent${exactModel ? " (exact push-pull on regeneration)" : ""}.</p><div id="faceBind"></div>` : ""}
    <div class="btn-row"><button class="tbtn" id="selectBody"><svg><use href="#i-cube"/></svg><span>Select body</span></button></div>`;
  const selector = { point: face.centroid, normal: face.normal || null };
  if (aligned) {
    const extent=(body.bbox_max["xyz".indexOf(aligned.axis)] - body.bbox_min["xyz".indexOf(aligned.axis)]) / scale;
    $("#faceBind", host).append(bindControl(selection.key, aligned.axis, body.dimension_parameters[aligned.axis], extent, unit));
  }
  $("#pushPull", host)?.addEventListener("click", () => createFeature("offset_face", { body: selection.key, selector }));
  $$("[data-bc]", host).forEach(button => button.addEventListener("click", () => assignBoundaryCondition(button.dataset.bc, selector)));
  $("#refineFace", host).addEventListener("click", () => {
    const options = { ...(state.workbench.settings.mesh_options || {}) };
    options.refinements = [...(options.refinements || []), { selectors: [selector], size_ratio: 0.25 }];
    saveSettings({ mesh_options: options }, "Local refinement added, mesh again to apply");
  });
  $("#assignShell", host)?.addEventListener("click", () => openMemberDialog("shell", selection, `${face.name} of ${body.name}`, selector));
  $("#selectBody", host).addEventListener("click", () => select({ kind: "body", key: selection.key }));
}

const memberDraft = { section: "rectangular", dims: {}, thickness: 2, offset: 0 };

function sectionFieldsHtml(kind) {
  return SECTION_FIELDS[kind].map(([key, label, value]) => `<label class="field">${label}<div class="row-unit"><input data-section="${key}" type="number" min="0" step="any" value="${memberDraft.dims[`${kind}.${key}`] ?? value}"><span class="unit">mm</span></div></label>`).join("");
}

// edge -> 1D beam (cross-section), face -> 2D shell (thickness, offset). Values are in mm in the form, SI in the request.
function openMemberDialog(kind, selection, target, selector = null) {
  const fields = $("#memberFields");
  $("#memberTitle").textContent = kind === "beam" ? "Assign 1D beam" : "Assign 2D shell";
  $("#memberKicker").textContent = kind === "beam" ? "EDGE · BEAM IDEALIZATION" : "FACE · SHELL IDEALIZATION";
  $("#memberTarget").textContent = target;
  $("#memberDialog").dataset.kind = kind;
  if (kind === "beam") {
    fields.innerHTML = `<div class="seg" role="radiogroup" aria-label="Cross-section">${SECTION_TYPES.map(([type, label]) => `<button type="button" class="${type === memberDraft.section ? "active" : ""}" data-section-type="${type}" role="radio" aria-checked="${type === memberDraft.section}">${label}</button>`).join("")}</div>
      <div id="sectionFields" class="member-grid">${sectionFieldsHtml(memberDraft.section)}</div>
      <p class="hint" data-help-id="structural.beam">Euler–Bernoulli frame element along the edge; local z is the section height. Mesh the edge and solve to compare with the solid.</p>`;
    $$("[data-section-type]", fields).forEach(button => button.addEventListener("click", () => {
      memberDraft.section = button.dataset.sectionType;
      $$("[data-section-type]", fields).forEach(other => { other.classList.toggle("active", other === button); other.setAttribute("aria-checked", String(other === button)); });
      $("#sectionFields").innerHTML = sectionFieldsHtml(memberDraft.section);
      refreshMemberPreview();
    }));
  } else {
    fields.innerHTML = `<div class="member-grid"><label class="field" data-help-id="structural.shell">Thickness t<div class="row-unit"><input id="shellThickness" type="number" min="0" step="any" value="${memberDraft.thickness}"><span class="unit">mm</span></div></label>
      <label class="field" data-help-id="structural.shell">Offset e (along the face normal)<div class="row-unit"><input id="shellOffset" type="number" step="any" value="${memberDraft.offset}"><span class="unit">mm</span></div></label></div>
      <p class="hint" data-help-id="structural.shell">Kirchhoff plate + membrane (DKT/CST) on the face; e = 0 puts the mid-surface on the CAD face, e = −t/2 makes the face the outer skin.</p>`;
  }
  fields.oninput = refreshMemberPreview;
  $("#memberForm").onsubmit = event => {
    event.preventDefault();
    const spec = memberSpec();
    if (spec.error) return;
    $("#memberDialog").close();
    if (kind === "beam") assignMember({ type: "beam", body: selection.key, edges: [{ point: selection.point }], section: spec.section });
    else assignMember({ type: "shell", body: selection.key, selector, thickness: spec.thickness, offset: spec.offset });
  };
  refreshMemberPreview();
  $("#memberDialog").showModal();
}

function memberSpec() {
  if ($("#memberDialog").dataset.kind === "beam") {
    const section = { type: memberDraft.section };
    $$("#memberFields [data-section]").forEach(input => {
      section[input.dataset.section] = Number(input.value) / 1e3;
      memberDraft.dims[`${memberDraft.section}.${input.dataset.section}`] = Number(input.value);
    });
    const props=sectionProperties(section);
    return { section, props, error: props.error };
  }
  const thickness = Number($("#shellThickness").value) / 1e3; const offset = Number($("#shellOffset").value || 0) / 1e3;
  memberDraft.thickness = thickness * 1e3; memberDraft.offset = offset * 1e3;
  return { thickness, offset, error: thickness > 0 ? null : "Thickness must be positive" };
}

function refreshMemberPreview() {
  const spec = memberSpec();
  const beam = $("#memberDialog").dataset.kind === "beam";
  $("#memberPreview").innerHTML = beam ? sectionPreviewSvg(spec.section) : shellPreviewSvg(spec.thickness, spec.offset);
  const sci = (value, unit) => `${value.toPrecision(4)} ${unit}`;
  $("#memberProps").innerHTML = spec.error ? "" : beam
    ? `<tbody><tr><td>Area A</td><td>${sci(spec.props.area * 1e6, "mm²")}</td></tr><tr><td>I<sub>y</sub> (strong)</td><td>${sci(spec.props.iy * 1e12, "mm⁴")}</td></tr><tr><td>I<sub>z</sub></td><td>${sci(spec.props.iz * 1e12, "mm⁴")}</td></tr><tr><td>J (torsion)</td><td>${sci(spec.props.j * 1e12, "mm⁴")}</td></tr></tbody>`
    : `<tbody><tr><td>Bending D/E</td><td>${sci(spec.thickness ** 3 / 12 * 1e9 / (1 - 0.09), "mm³")}</td></tr><tr><td>Mass / area (steel)</td><td>${sci(7850 * spec.thickness, "kg/m²")}</td></tr></tbody>`;
  $("#memberError").hidden = !spec.error;
  $("#memberError").textContent = spec.error || "";
  $("#memberSubmit").disabled = Boolean(spec.error);
}

async function assignMember(member) {
  try {
    const result = await api("/api/workbench/members", { method: "POST", body: JSON.stringify(member) });
    await refreshWorkbench(result.workbench);
    notify(`${result.member.type === "beam" ? "Beam" : "Shell"} ${result.member.name} assigned, FEA now solves the beam/shell model`);
  } catch (error) {
    notify(error.message, true);
  }
}

function renderEdgeInspector(host, selection) {
  const { model, body, entry } = bodySummary(selection.key);
  const points = selection.points || [];
  let length = 0;
  for (let i = 3; i < points.length; i += 3) {
    const d = [0, 1, 2].map(k => (points[i + k] - points[i + k - 3]) * (entry?.scale[k] ?? 1));
    length += Math.hypot(...d);
  }
  const scale = lengthUnitScale(model?.length_unit) || 1;
  const exactModel = model?.kernel === "occ";
  host.innerHTML = `${inspectorHead("i-ruler", "Edge", `on ${body?.name || "body"}`)}
    <table class="kv-table"><tbody><tr><td>Length</td><td>${fmt(length / scale)} ${escapeHtml(model?.length_unit || "m")}</td></tr><tr><td>Samples</td><td>${points.length / 3}</td></tr></tbody></table>
    ${exactModel ? '<div class="section-head"><span>Edge features</span></div><div class="btn-row"><button class="tbtn" id="filletEdge"><svg><use href="#i-boolean"/></svg><span>Fillet</span></button><button class="tbtn" id="chamferEdge"><svg><use href="#i-split"/></svg><span>Chamfer</span></button></div><p class="hint">Rolling-ball fillet / equal-distance chamfer on the exact B-Rep; the size becomes a fabric parameter.</p>' : '<div class="note"><svg><use href="#i-info"/></svg><span>Fillets and chamfers need exact geometry, import STEP or IGES.</span></div>'}
    ${state.workbench?.physics?.analysis === "fea" ? `<div class="section-head" data-help-id="structural.beam"><span>1D beam</span></div>
      <div class="btn-row"><button class="tbtn" id="assignBeam" data-help-id="structural.beam"><svg><use href="#i-ruler"/></svg><span>Assign 1D beam…</span></button></div>` : ""}
    <div class="btn-row"><button class="tbtn" id="selectBody"><svg><use href="#i-cube"/></svg><span>Select body</span></button></div>`;
  const edges = [{ point: selection.point }];
  $("#assignBeam", host)?.addEventListener("click", () => openMemberDialog("beam", selection, `${fmt(length / scale)} ${model?.length_unit || "m"} edge of ${body?.name || "body"}`));
  $("#filletEdge", host)?.addEventListener("click", () => createFeature("fillet", { body: selection.key, edges }));
  $("#chamferEdge", host)?.addEventListener("click", () => createFeature("chamfer", { body: selection.key, edges }));
  $("#selectBody", host).addEventListener("click", () => select({ kind: "body", key: selection.key }));
}

async function createFeature(type, options) {
  try {
    const result = await api("/api/workbench/features", { method: "POST", body: JSON.stringify({ type, options }) });
    governanceNote(result.governance);
    await refreshWorkbench(result.workbench);
    select({ kind: "feature", id: result.feature.id });
    notify(`${result.feature.name} created, drag its parameter in the morph dock`);
  } catch (error) {
    notify(error.message, true);
  }
}

async function saveSettings(values, message) {
  try {
    const before = state.workbench?.settings || {};
    const previous = Object.fromEntries(Object.keys(values).map(key => [key, clone(before[key] ?? (["boundary_conditions", "structural_members"].includes(key) ? [] : {}))]));
    await refreshWorkbench(await api("/api/workbench/settings", { method: "POST", body: JSON.stringify({ values }) }));
    recordUndo({ kind: "settings", values: previous, label: message || "Settings change" });
    if (message) notify(message);
  } catch (error) {
    notify(error.message, true);
  }
}

async function assignBoundaryCondition(type, faces, link = {}) {
  const selectors = Array.isArray(faces) ? faces : [faces];
  const selector = selectors[0];
  const conditions = [...(state.workbench.settings.boundary_conditions || [])];
  const id=Math.random().toString(16).slice(2, 10);
  const condition = { id, type, selectors, ...link };
  if (type === "force" || type === "pressure") {
    let name = type === "force" ? "face_force" : "face_pressure";
    for (let n = 2; parameter(name); n += 1) name = `${type === "force" ? "face_force" : "face_pressure"}_${n}`;
    state.draft.parameters.push({
      name, value: type === "force" ? 500 : 1e5, unit: type === "force" ? "N" : "Pa", type: "float",
      tags: ["physics", "physics.fea"], description: type === "force" ? "Resultant force on the selected face." : "Uniform pressure on the selected face.",
      rationale: "Created by boundary-condition assignment.", bindings: {},
    });
    if (!(await commit(`Created ${name}`))) return;
    condition.params = { magnitude: name };
    if (type === "force") condition.direction = selector.normal ? selector.normal.map(v => -v) : [0, 0, -1];
  }
  conditions.push(condition);
  await saveSettings({ boundary_conditions: conditions }, `${type} condition assigned`);
}

function renderModelInspector(host, id) {
  const model = state.workbench?.models.find(item => item.id === id);
  if (!model) return renderOverview(host);
  host.innerHTML = `${inspectorHead("i-folder", model.assembly || model.name, model.filename)}
    <table class="kv-table"><tbody><tr><td>Format</td><td>${model.format.toUpperCase()}</td></tr><tr><td>Length unit</td><td>${escapeHtml(model.length_unit)}</td></tr><tr><td>Bodies</td><td>${model.bodies.length}</td></tr></tbody></table>
    ${model.warnings.map(warning => `<div class="note warn"><svg><use href="#i-alert"/></svg><span>${escapeHtml(warning)}</span></div>`).join("")}
    <div class="btn-row"><button class="tbtn danger" id="removeModel"><svg><use href="#i-trash"/></svg><span>Remove from workspace</span></button></div>
    <p class="hint">Removing keeps its fabric parameters; delete them from the Fabric tab if no longer needed.</p>`;
  $("#removeModel", host).addEventListener("click", async () => {
    if (!confirm(`Remove ${model.filename} from the workspace?`)) return;
    try { await refreshWorkbench(await api(`/api/workbench/geometry/${id}`, { method: "DELETE" })); select(null); viewport.fit?.(); notify("Geometry removed"); } catch (error) { notify(error.message, true); }
  });
}

function renderFeatureInspector(host, id) {
  const feature = (state.workbench?.settings?.features || []).find(item => item.id === id);
  if (!feature) return renderOverview(host);
  host.innerHTML = `${inspectorHead(feature.type === "flow_volume" ? "i-flow" : "i-extrude", feature.name, FEATURE_LABELS[feature.type])}
    <table class="kv-table"><tbody>${feature.body ? `<tr><td>Body</td><td>${escapeHtml(bodySummary(feature.body).body?.name || feature.body)}</td></tr>` : ""}
    ${feature.tool ? `<tr><td>Tool</td><td>${escapeHtml(bodySummary(feature.tool).body?.name || feature.tool)}</td></tr><tr><td>Operation</td><td>${escapeHtml(feature.op)}</td></tr>` : ""}
    ${feature.face ? `<tr><td>Face</td><td>${escapeHtml(feature.face)}</td></tr>` : ""}${feature.axis ? `<tr><td>Axis</td><td>${escapeHtml(feature.axis.toUpperCase())}</td></tr>` : ""}</tbody></table>
    <div class="section-head"><span>Driving parameters</span></div><div class="chips">${Object.entries(feature.params || {}).map(([role, name]) => `<button class="chip" data-param="${escapeHtml(name)}">${escapeHtml(role)}: ${escapeHtml(name)}</button>`).join("") || '<span class="muted">None (topological operation)</span>'}</div>
    ${feature.type === "boolean" ? '<div class="note"><svg><use href="#i-info"/></svg><span>Booleans are applied to the volume mesh (voxel level); the viewport shows the tool body as a ghost.</span></div>' : ""}
    <div class="btn-row"><button class="tbtn danger" id="deleteFeature"><svg><use href="#i-trash"/></svg><span>Delete feature</span></button></div>`;
  $$("[data-param]", host).forEach(chip => chip.addEventListener("click", () => selectParameter(chip.dataset.param)));
  $("#deleteFeature", host).addEventListener("click", async () => {
    try { await refreshWorkbench(await api(`/api/workbench/features/${id}`, { method: "DELETE" })); select(null); notify("Feature deleted"); } catch (error) { notify(error.message, true); }
  });
}

function highlightFormula(text) {
  const names = new Set(state.draft.parameters.map(item => item.name));
  return escapeHtml(text).replace(/([A-Za-z_][A-Za-z0-9_]*)|(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+)|([+\-*/()]+)/g, (match, name, number, operator) => {
    if (name) return `<span class="${names.has(name) ? "tok-ref" : "tok-bad"}">${name}</span>`;
    if (number) return `<span class="tok-num">${number}</span>`;
    return `<span class="tok-op">${operator}</span>`;
  });
}

function unitAutocomplete(input, onPick) {
  let menu; let active = 0;
  const close = () => { menu?.remove(); menu = null; };
  const open = () => {
    const query = input.value.trim().toLowerCase();
    const matches = UNITS.filter(([unit, label]) => !query || unit.toLowerCase().startsWith(query) || label.includes(query)).slice(0, 40);
    close();
    if (!matches.length) return;
    menu = document.createElement("div");
    menu.className = "unit-menu";
    menu.setAttribute("role", "listbox");
    active = Math.min(active, matches.length - 1);
    matches.forEach(([unit, label], index) => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = `unit-option${index === active ? " active" : ""}`;
      option.innerHTML = `<span>${escapeHtml(unit)}</span><span>${escapeHtml(label)}</span>`;
      option.addEventListener("mousedown", event => { event.preventDefault(); input.value = unit; close(); onPick(unit); });
      menu.append(option);
    });
    input.parentElement.append(menu);
  };
  input.addEventListener("focus", () => { active = 0; open(); });
  input.addEventListener("input", () => { active = 0; open(); onPick(input.value.trim(), true); });
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", event => {
    if (!menu) return;
    const options=$$(".unit-option", menu);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      options.forEach((option, index) => option.classList.toggle("active", index === active));
      options[active].scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter") {
      event.preventDefault();
      input.value = options[active].firstChild.textContent;
      close();
      onPick(input.value);
    } else if (event.key === "Escape") close();
  });
}

function renderParameterEditor(host, name) {
  let item=parameter(name);
  const isNew = !parameter(name, state.fabric);
  if (!item) { state.selection = null; return renderOverview(host); }
  const dependents = (state.diagnostics?.links || []).filter(link => (link.source.id || link.source) === name).map(link => link.target.id || link.target);
  const inputs = (state.diagnostics?.links || []).filter(link => (link.target.id || link.target) === name).map(link => link.source.id || link.source);
  const usage = geometryUsage().get(name) || [];
  const syncItems = state.sync.items.filter(entry => entry.parameter === name);
  host.innerHTML = `${inspectorHead("i-param", item.name, isNew ? "New parameter · unsaved" : (item.description || "Fabric parameter"))}
    <label class="field">Name<input id="pName" value="${escapeHtml(item.name)}" pattern="[A-Za-z_][A-Za-z0-9_]*" autocomplete="off" spellcheck="false"></label>
    <div class="row-unit">
      <label class="field">Value or formula<div class="formula-wrap"><pre class="formula-hl" id="pHl" aria-hidden="true"></pre><input id="pValue" value="${escapeHtml(String(item.value))}" autocomplete="off" spellcheck="false"></div></label>
      <label class="field">Unit<input id="pUnit" value="${escapeHtml(item.unit)}" autocomplete="off" spellcheck="false"></label>
    </div>
    <div id="evalLine"></div>
    <div class="row2">
      <label class="field">Type<select id="pType">${["float", "int", "str", "bool"].map(type => `<option${type === item.type ? " selected" : ""}>${type}</option>`).join("")}</select></label>
      <label class="field">Tags<input id="pTags" value="${escapeHtml((item.tags || []).join(", "))}" placeholder="geometry, thermal"></label>
    </div>
    <label class="field">Description<input id="pDesc" value="${escapeHtml(item.description || "")}"></label>
    <label class="field">Rationale<textarea id="pWhy">${escapeHtml(item.rationale || "")}</textarea></label>
    <div class="section-head"><span>Output target bindings</span></div>
    <div class="chips" id="pBindings">${OUTPUT_TARGETS.map(([kind, label]) => `<button type="button" class="chip${item.bindings?.[kind] ? " active" : ""}" data-kind="${kind}" title="${escapeHtml(item.bindings?.[kind] || "Not bound")}">${label}</button>`).join("")}</div>
    <div class="section-head"><span>Lineage</span></div>
    <table class="kv-table"><tbody>
      <tr><td>Inputs</td><td>${inputs.map(escapeHtml).join(", ") || "—"}</td></tr>
      <tr><td>Dependents</td><td>${dependents.map(escapeHtml).join(", ") || "—"}</td></tr>
      <tr><td>Drives geometry</td><td>${usage.map(escapeHtml).join(", ") || "—"}</td></tr>
      <tr><td>Source constants</td><td>${syncItems.map(entry => `${escapeHtml(entry.path)}:${entry.line}`).join(", ") || "—"}</td></tr>
    </tbody></table>
    <div class="btn-row"><button class="tbtn primary" id="pSave"><svg><use href="#i-check"/></svg><span>${state.localMode ? "Save" : "Submit for review"}</span></button><button class="tbtn" id="pDiscard">Discard</button><button class="tbtn danger" id="pDelete"><svg><use href="#i-trash"/></svg><span>Delete</span></button></div>`;
  const update = mutate => {
    item = parameter(state.editing);
    if (!item) return;
    mutate(item);
    scheduleEvaluate();
  };
  const valueInput = $("#pValue", host);
  const highlight = () => { $("#pHl", host).innerHTML = highlightFormula(valueInput.value) || " "; $("#pHl", host).scrollLeft = valueInput.scrollLeft; };
  highlight();
  valueInput.addEventListener("input", () => { highlight(); update(target => { target.value = parseValue(valueInput.value, target.type); }); });
  valueInput.addEventListener("scroll", highlight);
  unitAutocomplete($("#pUnit", host), (unit, typing) => { update(target => { target.unit = unit || "dimensionless"; }); if (!typing) $("#pValue", host).focus(); });
  $("#pType", host).addEventListener("change", event => update(target => { target.type = event.target.value; target.value = parseValue(valueInput.value, target.type); }));
  $("#pTags", host).addEventListener("input", event => update(target => { target.tags = event.target.value.split(",").map(tag => tag.trim()).filter(Boolean); }));
  $("#pDesc", host).addEventListener("input", event => update(target => { target.description = event.target.value; }));
  $("#pWhy", host).addEventListener("input", event => update(target => { target.rationale = event.target.value; }));
  $("#pName", host).addEventListener("change", event => {
    const next = event.target.value.trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(next) || (next !== state.editing && parameter(next))) {
      notify("Names must be unique identifiers (letters, digits, underscores).", true);
      event.target.value = state.editing;
      return;
    }
    update(target => { target.name = next; });
    state.editing = next;
    state.selection = { kind: "parameter", name: next };
  });
  $$("#pBindings .chip", host).forEach(chip => chip.addEventListener("click", () => {
    update(target => {
      target.bindings = target.bindings || {};
      if (target.bindings[chip.dataset.kind]) delete target.bindings[chip.dataset.kind];
      else target.bindings[chip.dataset.kind] = OUTPUT_TARGETS.find(entry => entry[0] === chip.dataset.kind)[2];
    });
    chip.classList.toggle("active");
  }));
  $("#pSave", host).addEventListener("click", () => commit(`${state.editing} saved`));
  $("#pDiscard", host).addEventListener("click", discard);
  $("#pDelete", host).addEventListener("click", async () => {
    if (!confirm(`Delete parameter "${state.editing}"?`)) return;
    state.draft.parameters = state.draft.parameters.filter(entry => entry.name !== state.editing);
    if (await commit(`Deleted ${state.editing}`)) select(null);
  });
  renderEvalLine();
}

function renderEvalLine() {
  const host=$("#evalLine");
  if (!host || state.selection?.kind !== "parameter") return;
  const name = state.editing;
  const issues=issuesFor(name);
  const value = resolved(name);
  if (issues.length) {
    host.innerHTML = `<div class="eval-line bad"><span><b>${escapeHtml(issues[0].kind)}:</b> ${escapeHtml(issues[0].message)}</span></div>`;
  } else if (value && typeof value.value === "number") {
    host.innerHTML = `<div class="eval-line"><span>= ${fmt(value.value, 8)} ${escapeHtml(value.unit)}</span><span class="si">${fmt(value.si, 6)} ${escapeHtml(value.si_unit)} · ${escapeHtml(value.dimensionality)}</span></div>`;
  } else {
    host.innerHTML = "";
  }
  $("#pUnit")?.closest(".field").classList.toggle("invalid", issues.some(issue => issue.kind === "unit"));
}

function newParameter() {
  let name = "new_parameter";
  for (let n = 2; parameter(name); n += 1) name = `new_parameter_${n}`;
  state.draft.parameters.push({ name, value: 1, unit: "dimensionless", type: "float", tags: [], description: "", rationale: "", bindings: {} });
  setLeftTab("fabric");
  selectParameter(name);
  scheduleEvaluate(0);
  $("#pName")?.select();
}

// viewport HUD & morph dock

function renderHud() {
  const hud = $("#selectionHud");
  const selection = state.selection;
  if (!selection || !["body", "face", "edge"].includes(selection.kind)) { hud.hidden = true; return; }
  const { model, body, full } = bodySummary(selection.key);
  const entry = sceneEntry(selection.key);
  if (!body || !entry) { hud.hidden = true; return; }
  const unit = model.length_unit; const scale = lengthUnitScale(unit) || 1;
  const size = entry.bbox_max.map((v, i) => (v - entry.bbox_min[i]) / scale);
  const face = selection.kind === "face" ? full?.faces.find(item => item.id === selection.faceId) : null;
  hud.hidden = false;
  hud.innerHTML = `<strong>${escapeHtml(face ? face.name : body.name)}</strong>
    ${state.pickSet.length > 1 ? `<div class="kv"><span>Selected</span><span>${state.pickSet.length} ${selection.kind}s</span></div>` : ""}
    <div class="kv"><span>Body</span><span>${escapeHtml(body.name)}</span></div>
    <div class="kv"><span>Size</span><span>${size.map(v => fmt(v, 4)).join(" × ")} ${unit}</span></div>
    ${face ? `<div class="kv"><span>Surface</span><span>${escapeHtml(face.surface)}</span></div><div class="kv"><span>Area</span><span>${fmt(face.area / scale / scale)} ${unit}²</span></div>` : ""}
    ${selection.point ? `<div class="kv"><span>Picked at</span><span>${selection.point.map(v => fmt(v / scale, 4)).join(", ")}</span></div>` : ""}`;
}

function morphParameters() {
  const names = [];
  const add = name => { if (name && !names.includes(name) && parameter(name)) names.push(name); };
  const key = state.selection?.key;
  for (const model of state.workbench?.models || []) {
    for (const body of model.bodies) {
      if (!key || key === `${model.id}/${body.id}`) Object.values(body.dimension_parameters).forEach(add);
    }
  }
  for (const feature of state.workbench?.settings?.features || []) {
    if (!key || feature.body === key || feature.type === "flow_volume") Object.values(feature.params || {}).forEach(add);
  }
  return names;
}

function renderMorphDock() {
  const dock=$("#morphDock");
  const names = workbenchReady() ? morphParameters() : [];
  dock.hidden = !names.length;
  if (!names.length) return;
  $("#morphScope").textContent = state.selection?.key ? bodySummary(state.selection.key).body?.name || "" : "All bodies";
  const rows=$("#morphRows");
  rows.replaceChildren();
  for (const name of names) {
    const item = parameter(name);
    const row = document.createElement("div");
    row.className = "morph-row";
    row.dataset.name = name;
    const value=resolved(name)?.value ?? Number(item.value);
    if (isFormula(item)) {
      row.innerHTML = `<label title="${escapeHtml(name)}">${escapeHtml(name)}</label><span class="num"><input type="number" value="${fmt(value)}" disabled><span class="unit">${escapeHtml(item.unit)}</span></span><span class="formula">= ${escapeHtml(item.value)} (driven by formula)</span>`;
    } else {
      const saved = Number(parameter(name, state.fabric)?.value ?? value);
      const base = Math.abs(saved) || 1;
      const low = item.unit === "dimensionless" && saved <= 1 ? 0.01 : base * 0.2;
      const high=item.unit === "dimensionless" && saved <= 1 ? 0.99 : base * 3;
      row.innerHTML = `<label for="morph-${escapeHtml(name)}" title="${escapeHtml(name)}">${escapeHtml(name)}</label><span class="num"><input type="number" step="any" value="${fmt(value)}" aria-label="${escapeHtml(name)} value"><span class="unit">${escapeHtml(item.unit)}</span></span><input id="morph-${escapeHtml(name)}" type="range" min="${low}" max="${high}" step="${(high - low) / 400}" value="${value}">`;
      const slider = $("input[type=range]", row);
      const number = $("input[type=number]", row);
      const apply = (raw, save) => {
        const numeric = Number(raw);
        if (!Number.isFinite(numeric)) return;
        const target = parameter(name);
        target.value = target.type === "int" ? Math.round(numeric) : Number(numeric.toPrecision(8));
        number.value = fmt(target.value);
        scheduleEvaluate(30);
        if (save) commit(`${name} = ${fmt(target.value)} ${target.unit}`);
      };
      slider.addEventListener("input", () => apply(slider.value, false));
      slider.addEventListener("change", () => apply(slider.value, true));
      number.addEventListener("change", () => { slider.value = number.value; apply(number.value, true); });
    }
    rows.append(row);
  }
}

function updateMorphValues() {
  $$("#morphRows .morph-row").forEach(row => {
    const value = resolved(row.dataset.name)?.value;
    const number = $("input[type=number]", row);
    if (typeof value === "number" && document.activeElement !== number && number.disabled) number.value = fmt(value);
  });
}

// features & imports

const FACE_OPTIONS = ["+x", "-x", "+y", "-y", "+z", "-z"];

function bodyOptions(selected) {
  return (state.workbench?.models || []).flatMap(model => model.bodies.map(body => {
    const key = `${model.id}/${body.id}`;
    return `<option value="${key}"${key === selected ? " selected" : ""}>${escapeHtml(body.name)}</option>`;
  })).join("");
}

function openFeature(type, defaults = {}) {
  if (type === "offset_face" || type === "fillet" || type === "chamfer") {
    return notify(type === "offset_face" ? "Select a planar face, then use Push / pull in the Inspector." : "Select an edge, then use Fillet or Chamfer in the Inspector.");
  }
  if (!workbenchReady()) return notify("CAD features are available in the local desktop Studio.", true);
  if (!(state.workbench?.models || []).length) return notify("Import CAD geometry before adding features.", true);
  const body = defaults.body || state.selection?.key || "";
  const fields = $("#featureFields");
  const help = {
    extrude: "Adds a box grown from a body's bounding face; its depth is a new length parameter.",
    split: "Cuts a body at a dimensionless fraction along an axis (shown as two regions).",
    boolean: "Subtracts or unites a tool body. Applied to the volume mesh; the tool is shown as a ghost.",
    flow_volume: "Extracts the fluid domain around all bodies with upstream, downstream, and lateral margin parameters. Flow is along +X.",
    scale: "Adds a uniform scale-factor parameter for a body.",
  }[type];
  $("#featureTitle").textContent = FEATURE_LABELS[type];
  $("#featureHelp").textContent = help;
  const parts = [];
  if (["extrude", "split", "scale", "boolean"].includes(type)) parts.push(`<label class="field">${type === "boolean" ? "Target body" : "Body"}<select name="body">${bodyOptions(body)}</select></label>`);
  if (type === "extrude") parts.push(`<label class="field">Face<select name="face">${FACE_OPTIONS.map(face => `<option${face === (defaults.face || "+x") ? " selected" : ""}>${face}</option>`).join("")}</select></label>`);
  if (type === "split") parts.push('<label class="field">Axis<select name="axis"><option>x</option><option>y</option><option>z</option></select></label>');
  if (type === "boolean") parts.push(`<label class="field">Tool body<select name="tool">${bodyOptions("")}</select></label><label class="field">Operation<select name="op"><option>subtract</option><option>union</option></select></label>`);
  fields.innerHTML = parts.join("");
  $("#featureForm").dataset.type = type;
  $("#featureDialog").showModal();
}

async function submitFeature(event) {
  event.preventDefault();
  const form = event.target;
  const options = Object.fromEntries(new FormData(form).entries());
  $("#featureDialog").close();
  try {
    const result = await api("/api/workbench/features", { method: "POST", body: JSON.stringify({ type: form.dataset.type, options }) });
    governanceNote(result.governance);
    await refreshWorkbench(result.workbench);
    viewport.fit?.();
    select({ kind: "feature", id: result.feature.id });
    notify(`${result.feature.name} created`);
  } catch (error) {
    notify(error.message, true);
  }
}

function routeFile(file) {
  if (!file) return;
  const extension = file.name.split(".").pop().toLowerCase();
  if (["step", "stp", "iges", "igs", "stl", "x_t", "x_b"].includes(extension)) return prepareCad(file);
  return previewFabricImport(file);
}

function prepareCad(file) {
  if (!workbenchReady()) return notify("CAD import is available in the local desktop Studio.", true);
  state.pendingCad = file;
  const extension = file.name.split(".").pop().toLowerCase();
  $("#cadTitle").textContent = file.name;
  $("#cadSummary").textContent = `${(file.size / 1024).toFixed(1)} KB · ${extension.toUpperCase()}`;
  $("#cadUnits").value = extension === "stl" ? "mm" : "";
  $("#cadDialog").showModal();
}

async function submitCad(event) {
  event.preventDefault();
  const file=state.pendingCad;
  $("#cadDialog").close();
  if (!file) return;
  try {
    notify(`Importing ${file.name}…`);
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    const result = await api("/api/workbench/geometry/import", {
      method: "POST",
      body: JSON.stringify({ filename: file.name, content_base64: btoa(binary), units: $("#cadUnits").value || null, associate: $("#cadAssociate").checked }),
    });
    await refreshWorkbench(result.workbench);
    viewport.view?.("iso");
    const created = result.created_parameters.map(item => item.name);
    notify(`Imported ${result.model.bodies.length} bod${result.model.bodies.length === 1 ? "y" : "ies"}${created.length ? ` · ${created.length} associativity parameters` : ""}`);
    if (result.model.bodies[0]) select({ kind: "body", key: `${result.model.id}/${result.model.bodies[0].id}` });
  } catch (error) {
    notify(`Import failed: ${error.message}`, true);
  } finally {
    state.pendingCad = null;
  }
}

async function loadDemo() {
  if (!workbenchReady()) return notify("The demo part is available in the local desktop Studio.", true);
  try {
    const result = await api("/api/workbench/geometry/demo", { method: "POST" });
    await refreshWorkbench(result.workbench);
    viewport.view?.("iso");
    select({ kind: "body", key: `${result.model.id}/${result.model.bodies[0].id}` });
    notify("Demo cantilever bracket loaded with associativity parameters");
  } catch (error) {
    notify(error.message, true);
  }
}

function previewFabricImport(file) {
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const result=await api("/api/import/preview", { method: "POST", body: JSON.stringify({ filename: file.name, content: String(reader.result || "") }) });
      state.importCandidates = result.parameters || [];
      if (!state.importCandidates.length) throw new Error("No parameters were found in this file.");
      $("#importSummary").textContent = `${file.name} · ${state.importCandidates.length} parameter${state.importCandidates.length === 1 ? "" : "s"}`;
      $("#importPreview").innerHTML = state.importCandidates.map(item => `<label class="import-row"><input type="checkbox" checked value="${escapeHtml(item.name)}"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(String(item.value))}</span><span class="unit-pill">${escapeHtml(item.unit)}</span></label>`).join("");
      $("#importDialog").showModal();
    } catch (error) {
      notify(`Import failed: ${error.message}`, true);
    }
  };
  reader.readAsText(file);
}

async function mergeImport() {
  const chosen = new Set($$("#importPreview input:checked").map(input => input.value));
  const incoming = state.importCandidates.filter(item => chosen.has(item.name));
  if (!incoming.length) return notify("Select at least one parameter", true);
  const byName = new Map(state.draft.parameters.map(item => [item.name, item]));
  incoming.forEach(item => byName.set(item.name, item));
  state.draft.parameters = [...byName.values()];
  $("#importDialog").close();
  await commit(`Imported ${incoming.length} parameter${incoming.length === 1 ? "" : "s"}`);
  scheduleEvaluate(0);
}

// physics, mesh & solver

function renderMembersCard() {
  const members = state.workbench.settings.structural_members || [];
  const model=state.workbench.settings.solver_options?.structural_model || "auto";
  const describe = member => member.type === "beam"
    ? `${member.section?.type?.replace("_", "-")} · ${member.edges.length} edge(s)`
    : `t = ${fmt(member.thickness * 1e3, 4)} mm${member.offset ? ` · offset ${fmt(member.offset * 1e3, 4)} mm` : ""}`;
  const options = state.workbench.settings.solver_options || {};
  const modal = options.structural_analysis === "modal";
  return `<div class="section-head" data-help-id="structural.modal"><span>Structural analysis</span></div>
    <div class="row2"><label class="field" data-help-id="structural.modal">Type<select data-structural-analysis><option value="static"${modal ? "" : " selected"}>Linear static</option><option value="modal"${modal ? " selected" : ""}>Modal (natural frequencies)</option></select></label>
    <label class="field" data-help-id="structural.modal">Modes<input data-structural-modes type="number" min="1" max="50" step="1" value="${options.modes || 6}"${modal ? "" : " disabled"}></label></div>
    ${modal ? '<p class="hint">Modal analysis uses the supports only (loads are ignored) and the optional <b>material_density</b> parameter (default 7850 kg/m³).</p>' : ""}
    <div class="section-head"><span>Structural idealization</span></div>
    <label class="field">Model<select data-structural-model><option value="auto"${model === "auto" ? " selected" : ""}>Beams &amp; shells when assigned, else solid</option><option value="solid"${model === "solid" ? " selected" : ""}>Solid (full volume mesh)</option></select></label>
    <div class="card">${members.length ? members.map(member => `<div class="member-row" data-help-id="structural.${member.type}"><span><span class="kind">${member.type === "beam" ? "1D beam" : "2D shell"}</span> ${escapeHtml(member.name)}</span><span class="muted">${escapeHtml(describe(member))}</span><button class="mini-btn" data-remove-member="${escapeHtml(member.id)}" title="Remove">✕</button></div>`).join("")
      : '<p class="hint">No members. Use the viewport\'s <b>Edge</b> selection to assign a beam, or <b>Face</b> to assign a shell.</p>'}</div>`;
}

function renderHeatCard() {
  const heat = state.workbench.settings.solver_options?.heat_transfer || {};
  const on = Boolean(heat.enabled);
  const options = (list, value) => list.map(([key, label]) => `<option value="${key}"${key === value ? " selected" : ""}>${label}</option>`).join("");
  return `<div class="section-head" data-help-id="physics.cht"><span>Conjugate heat transfer</span></div>
    <label class="check" data-help-id="physics.cht"><input type="checkbox" data-heat="enabled"${on ? " checked" : ""}> Solve solid conduction + coolant (chtMultiRegionSimpleFoam)</label>
    ${on ? `<div class="row2">
      <label class="field">Coolant<select data-heat="fluid">${options([["water", "Water"], ["air", "Air"], ["glycol50", "Ethylene glycol 50 %"]], heat.fluid || "water")}</select></label>
      <label class="field">Solid<select data-heat="solid">${options([["aluminium", "Aluminium"], ["copper", "Copper"], ["steel", "Steel"]], heat.solid || "aluminium")}</select></label>
      <label class="field">Heater power<div class="row-unit"><input data-heat="heater_power" type="number" min="0" step="any" value="${heat.heater_power ?? 50}"><span class="unit">W</span></div></label>
      <label class="field">Inlet temperature<div class="row-unit"><input data-heat="inlet_temperature" type="number" min="1" step="any" value="${heat.inlet_temperature ?? 293.15}"><span class="unit">K</span></div></label></div>
      <p class="hint">${(state.workbench.settings.boundary_conditions || []).some(bc => bc.type === "heat") ? "Heater faces assigned." : "No heater face yet: the bodies' bottom face is heated. Select a face → <b>Heat source</b> to choose."} Needs OpenFOAM and a flow volume; the inlet velocity is the coolant speed.</p>` : ""}`;
}

function physicsRow(name) {
  const item = parameter(name);
  if (!item) return `<div class="bc-row"><span>${escapeHtml(name)}</span><span class="muted">missing</span></div>`;
  const issue = issuesFor(name)[0];
  return `<div class="field${issue ? " invalid" : ""}"><span>${escapeHtml(name)} <small>${escapeHtml(item.description || "")}</small></span>
    <div class="row-unit"><input data-physics-value="${escapeHtml(name)}" value="${escapeHtml(String(item.value))}" spellcheck="false"><input data-physics-unit="${escapeHtml(name)}" value="${escapeHtml(item.unit)}" spellcheck="false"></div>
    ${issue ? `<small style="color:#f3a6a6">${escapeHtml(issue.message)}</small>` : ""}</div>`;
}

function renderPhysics() {
  const host = $("#physicsPanel");
  if (!workbenchReady()) { host.innerHTML = '<div class="note"><svg><use href="#i-info"/></svg><span>Physics setup runs in the local desktop Studio.</span></div>'; return; }
  const physics = state.workbench?.physics;
  if (!physics) return;
  const missing=physics.parameters.filter(name => !parameter(name, state.fabric));
  host.innerHTML = `<div class="kicker">ANALYSIS TYPE</div>
    <div class="seg" role="radiogroup">${physics.analyses.map(name => `<button role="radio" aria-checked="${name === physics.analysis}" class="${name === physics.analysis ? "active" : ""}" data-analysis="${name}">${name === "cfd" ? "CFD · external flow" : "FEA · linear static"}</button>`).join("")}</div>
    ${missing.length ? `<div class="note warn"><svg><use href="#i-alert"/></svg><span>Missing inputs: ${missing.map(escapeHtml).join(", ")}.</span></div><div class="btn-row"><button class="tbtn primary" id="createPhysics"><svg><use href="#i-plus"/></svg><span>Create default physics parameters</span></button></div>` : ""}
    <div class="section-head"><span>Material &amp; flow inputs (fabric parameters)</span></div>
    ${physics.parameters.filter(name => parameter(name)).map(physicsRow).join("")}
    <div class="section-head"><span>Boundary conditions</span></div>
    <div class="card">${(state.workbench.settings.boundary_conditions || []).length ? state.workbench.settings.boundary_conditions.map(bc => `<div class="bc-row"><span>${escapeHtml(bc.type)} · ${bc.named_selection ? escapeHtml((state.workbench.settings.named_selections || []).find(item => item.id === bc.named_selection)?.name || "named selection") : `${bc.selectors.length} face(s)`}</span><span class="val">${escapeHtml(bc.params?.magnitude || "")}<button class="mini-btn" data-remove-bc="${escapeHtml(bc.id)}" title="Remove">✕</button></span><span class="type muted">${bc.direction ? `direction (${bc.direction.map(v => fmt(v, 2)).join(", ")})` : "assigned in the viewport"}</span></div>`).join("") : physics.boundary_conditions.map(bc => `<div class="bc-row"><span>${escapeHtml(bc.patch)}</span><span class="val">${escapeHtml(bc.value)}</span><span class="type muted">${escapeHtml(bc.type)} (default)</span></div>`).join("")}</div>
    <p class="hint">Select a face in the viewport to assign ${physics.analysis === "fea" ? "supports, forces, or pressures" : "inlets and outlets (internal flow)"}.</p>
    ${physics.analysis === "cfd" ? `<div class="section-head"><span>Boundary types in this setup</span></div>
      <div class="card">
        <div class="bc-row" data-help-id="bc.velocity_inlet"><span>Velocity inlet</span><span class="type muted">${state.workbench.settings.solver_options?.domain === "internal" ? "selected inlet faces" : "upstream box face"}</span></div>
        <div class="bc-row" data-help-id="bc.pressure_outlet"><span>Pressure outlet</span><span class="type muted">${state.workbench.settings.solver_options?.domain === "internal" ? "selected outlet faces" : "downstream box face"}</span></div>
        <div class="bc-row" data-help-id="bc.no_slip_wall"><span>No-slip wall</span><span class="type muted">all body surfaces</span></div>
        ${state.workbench.settings.solver_options?.domain === "internal" ? "" : '<div class="bc-row" data-help-id="bc.symmetry"><span>Symmetry / slip</span><span class="type muted">box sides</span></div>'}
      </div>${renderHeatCard()}` : renderMembersCard()}
    ${physics.analysis === "cfd" && !(state.workbench.settings.features || []).some(f => f.type === "flow_volume") ? '<div class="note warn"><svg><use href="#i-alert"/></svg><span>No flow volume yet, CFD will use a tight box around the bodies. Add one with the Flow Volume tool.</span></div><div class="btn-row"><button class="tbtn" id="addFlow"><svg><use href="#i-flow"/></svg><span>Extract flow volume</span></button></div>' : ""}
    <div class="note"><svg><use href="#i-info"/></svg><span>${physics.analysis === "cfd"
      ? "CFD runs on OpenFOAM (steady simpleFoam, body-fitted gmsh mesh with prism layers) when it is installed; otherwise on the native experimental solver, whose results are flagged as not reliable."
      : "FEA runs the native 3D solver: the full solid (Tet10/Hex20), or the beams and shells assigned below. Without supports, the min-X face is clamped and the max-X face carries applied_load in −Z."}</span></div>`;
  $$("[data-analysis]", host).forEach(button => button.addEventListener("click", () => setAnalysis(button.dataset.analysis)));
  $$("[data-remove-bc]", host).forEach(button => button.addEventListener("click", () => saveSettings({
    boundary_conditions: state.workbench.settings.boundary_conditions.filter(bc => bc.id !== button.dataset.removeBc),
  }, "Boundary condition removed")));
  $("#createPhysics", host)?.addEventListener("click", () => setAnalysis(physics.analysis));
  $$("[data-remove-member]", host).forEach(button => button.addEventListener("click", async () => {
    try {
      await refreshWorkbench(await api(`/api/workbench/members/${button.dataset.removeMember}`, { method: "DELETE" }));
      notify("Member removed");
    } catch (error) { notify(error.message, true); }
  }));
  const solverOptions = () => state.workbench.settings.solver_options || {};
  $("[data-structural-analysis]", host)?.addEventListener("change", event => saveSettings({
    solver_options: { ...solverOptions(), structural_analysis: event.target.value },
  }, event.target.value === "modal" ? "Modal analysis: solve to get natural frequencies and mode shapes" : "Linear static analysis"));
  $("[data-structural-modes]", host)?.addEventListener("change", event => saveSettings({
    solver_options: { ...solverOptions(), modes: Math.max(1, Math.min(50, Math.round(Number(event.target.value) || 6))) },
  }, "Mode count updated"));
  $$("[data-heat]", host).forEach(input => input.addEventListener("change", () => {
    const heat={ ...(solverOptions().heat_transfer || {}) };
    const key = input.dataset.heat;
    heat[key] = input.type === "checkbox" ? input.checked : input.tagName === "SELECT" ? input.value : Number(input.value);
    saveSettings({ solver_options: { ...solverOptions(), heat_transfer: heat } },
      key === "enabled" ? (input.checked ? "Conjugate heat transfer on: select a face and add a Heat source" : "Conjugate heat transfer off") : "Heat-transfer settings updated");
  }));
  $("[data-structural-model]", host)?.addEventListener("change", event => saveSettings({
    solver_options: { ...(state.workbench.settings.solver_options || {}), structural_model: event.target.value },
  }, "Structural model updated"));
  $("#addFlow", host)?.addEventListener("click", () => openFeature("flow_volume"));
  $$("[data-physics-value]", host).forEach(input => input.addEventListener("change", () => {
    const target = parameter(input.dataset.physicsValue);
    target.value = parseValue(input.value, target.type);
    commit(`${target.name} updated`).then(() => scheduleEvaluate(0));
  }));
  $$("[data-physics-unit]", host).forEach(input => input.addEventListener("change", () => {
    parameter(input.dataset.physicsUnit).unit = input.value.trim() || "dimensionless";
    commit(`${input.dataset.physicsUnit} unit updated`).then(() => scheduleEvaluate(0));
  }));
}

async function setAnalysis(analysis) {
  try {
    const result = await api("/api/workbench/physics", { method: "POST", body: JSON.stringify({ analysis }) });
    governanceNote(result.governance);
    state.mesh = null;
    if (viewport.available) viewport.setMesh(null);
    await refreshWorkbench(result.workbench);
    if (result.created_parameters.length) notify(`Created ${result.created_parameters.join(", ")}`);
  } catch (error) {
    notify(error.message, true);
  }
}

function turbulenceTopic(value) {
  return { laminar: "turbulence.laminar", k_epsilon: "turbulence.k_epsilon", "k-epsilon": "turbulence.k_epsilon", spalart_allmaras: "turbulence.spalart_allmaras" }[value] || "turbulence.k_omega_sst";
}

function renderMeshPanel() {
  const host = $("#meshPanel");
  if (!workbenchReady()) { host.innerHTML = ""; return; }
  const fineness = parameter("mesh_fineness_ratio");
  const quality = state.mesh?.quality;
  const options=state.workbench.settings.mesh_options || {};
  const layer = options.boundary_layer || {};
  const analysis = state.workbench.physics.analysis;
  const solver = state.workbench.settings.solver_options || {};
  const gradeIcon = { good: "i-check", fair: "i-alert", poor: "i-alert" };
  host.innerHTML = `<div class="section-head"><span>Mesh</span></div>
    <div class="row2">
      <label class="field" data-help-id="mesh.tetrahedral">Mesher<select data-mesh="mesher"><option value="auto">Automatic</option><option value="unstructured">Unstructured (gmsh)</option><option value="cartesian">Cartesian voxel</option></select></label>
      <label class="field" data-help-id="${(options.element || "tet") === "hex" ? "mesh.hexahedral" : "mesh.tetrahedral"}"><span class="label-line">Elements ${compareButton("mesh_type", (options.element || "tet") === "hex" ? "mesh.hexahedral" : "mesh.tetrahedral")}</span><select data-mesh="element"${analysis === "cfd" ? " disabled" : ""}><option value="tet">Tetrahedra</option><option value="hex">Hexahedra (subdivision)</option></select></label>
    </div>
    <div class="row2">
      <label class="field">Order<select data-mesh="order"${analysis === "cfd" ? " disabled" : ""}><option value="2">Quadratic (Tet10/Hex20)</option><option value="1">Linear</option></select></label>
      <label class="field">Curvature refinement<input data-mesh="curvature" type="number" min="0" max="64" value="${options.curvature ?? 16}"><small>elements per 2π</small></label>
    </div>
    ${fineness && !isFormula(fineness) ? `<div class="morph-row" style="margin-bottom:8px"><label for="finenessRange">mesh_fineness_ratio</label><span class="num"><input type="number" id="finenessNumber" value="${fmt(Number(fineness.value))}" step="1" min="4" max="80"><span class="unit">L/h</span></span><input type="range" id="finenessRange" min="6" max="64" step="1" value="${Number(fineness.value)}"></div>` : '<p class="hint">Run the Physics step to create <code>mesh_fineness_ratio</code>.</p>'}
    ${analysis === "cfd" ? `<label class="check" data-help-id="mesh.inflation"><input type="checkbox" data-layer-enabled${layer.enabled ? " checked" : ""}> Prism inflation layers on walls</label>
    <div class="row2"${layer.enabled ? "" : " hidden"} id="layerControls">
      <label class="field" data-help-id="bc.no_slip_wall">Target y⁺<input data-layer="y_plus" type="number" min="0.5" step="0.5" value="${layer.y_plus ?? 30}"></label>
      <label class="field">Layers · growth<div class="row2"><input data-layer="layers" type="number" min="1" max="20" value="${layer.layers ?? 5}"><input data-layer="growth" type="number" min="1" max="2" step="0.05" value="${layer.growth ?? 1.2}"></div></label>
    </div>
    <div class="row2">
      <label class="field" data-help-id="${solver.domain === "internal" ? "bc.velocity_inlet" : "bc.symmetry"}">Flow domain<select data-solver="domain"><option value="external">External (flow volume)</option><option value="internal">Internal (body is fluid)</option></select></label>
      <label class="field" data-help-id="${turbulenceTopic(solver.turbulence)}"><span class="label-line">Turbulence ${compareButton("turbulence", turbulenceTopic(solver.turbulence))}</span><select data-solver="turbulence"><option value="auto">Automatic (by Reynolds number)</option><option value="laminar">Laminar</option><option value="k_omega_sst">k-ω SST</option><option value="k_epsilon">k-ε (wall functions)</option><option value="spalart_allmaras">Spalart–Allmaras (OpenFOAM)</option></select></label>
    </div>
    <div class="expert-only">
      <div class="section-head" data-help-id="solver.relaxation"><span>Advanced solver controls</span><span class="muted">Expert</span></div>
      <div class="row2">
        <label class="field" data-help-id="solver.relaxation"><span class="label-line">α<sub>U</sub> (velocity)</span><input data-advanced="relax_velocity" type="number" min="0.05" max="1" step="0.05" placeholder="0.9" value="${solver.relax_velocity ?? ""}"></label>
        <label class="field" data-help-id="solver.relaxation"><span class="label-line">α<sub>p</sub> (pressure)</span><input data-advanced="relax_pressure" type="number" min="0.05" max="1" step="0.05" placeholder="none (SIMPLEC)" value="${solver.relax_pressure ?? ""}"></label>
      </div>
      <div class="row2">
        <label class="field" data-help-id="solver.relaxation"><span class="label-line">α<sub>turb</sub></span><input data-advanced="relax_turbulence" type="number" min="0.05" max="1" step="0.05" placeholder="0.7" value="${solver.relax_turbulence ?? ""}"></label>
        <label class="field" data-help-id="solver.tolerance"><span class="label-line">Residual target</span><input data-advanced="tolerance" type="number" min="1e-9" step="any" placeholder="1e-5" value="${solver.tolerance ?? ""}"></label>
      </div>
      <div class="row2">
        <label class="field" data-help-id="solver.tolerance"><span class="label-line">p matrix tolerance</span><input data-advanced="linear_tolerance_p" type="number" min="1e-14" step="any" placeholder="1e-7" value="${solver.linear_tolerance_p ?? ""}"></label>
        <label class="field" data-help-id="solver.tolerance"><span class="label-line">U, turb. matrix tolerance</span><input data-advanced="linear_tolerance_u" type="number" min="1e-14" step="any" placeholder="1e-8" value="${solver.linear_tolerance_u ?? ""}"></label>
      </div>
      <div class="row2">
        <label class="field">Max iterations<input data-advanced="max_iterations" type="number" min="1" step="1" placeholder="500" value="${solver.max_iterations ?? ""}"></label>
        <label class="field">MPI processes<input data-advanced="processors" type="number" min="1" step="1" placeholder="1" value="${solver.processors ?? ""}"></label>
      </div>
    </div>` : ""}
    ${(options.refinements || []).length ? `<p class="hint">${options.refinements.length} local refinement region(s). <button class="link-btn" id="clearRefinements">Clear</button></p>` : ""}
    <div class="btn-row"><button class="tbtn" id="meshNow"><svg><use href="#i-mesh"/></svg><span>Mesh geometry</span></button></div>
    ${quality ? `<div class="card"><h3>${state.mesh.mesher === "gmsh" ? "Unstructured body-fitted mesh" : "Body-aligned Cartesian hex mesh"} · <span class="grade ${quality.grade}"><svg><use href="#${gradeIcon[quality.grade]}"/></svg>${quality.grade}</span></h3>
      <table class="kv-table"><tbody>
        <tr><td>Cells</td><td>${quality.cells.toLocaleString()}</td></tr>
        ${quality.nodes ? `<tr><td>Nodes</td><td>${quality.nodes.toLocaleString()}</td></tr>` : ""}
        ${quality.cell_types ? Object.entries(quality.cell_types).map(([type, count]) => `<tr><td>${escapeHtml(type)}</td><td>${count.toLocaleString()}</td></tr>`).join("") : ""}
        ${quality.min_tet_quality !== undefined ? `<tr><td>Min / mean tet quality</td><td>${fmt(quality.min_tet_quality, 3)} / ${fmt(quality.mean_tet_quality, 3)}</td></tr>` : ""}
        ${quality.boundary_layers ? `<tr><td>Inflation layers</td><td>${quality.boundary_layers} · first ${fmt(quality.first_layer_height * 1e3, 3)} mm</td></tr>` : ""}
        ${quality.fluid_cells !== undefined ? `<tr><td>Fluid / solid</td><td>${quality.fluid_cells.toLocaleString()} / ${quality.solid_cells.toLocaleString()}</td></tr><tr><td>Max aspect ratio</td><td>${fmt(quality.max_aspect_ratio, 3)}</td></tr>` : ""}
      </tbody></table></div>` : ""}${meshViewerHtml()}`;
  bindMeshViewer(host);
  $$("[data-mesh]", host).forEach(control => {
    const key = control.dataset.mesh;
    if (key !== "curvature") control.value = String(options[key] ?? { mesher: "auto", element: "tet", order: 2 }[key]);
    control.addEventListener("change", () => saveSettings({ mesh_options: { ...options, [key]: key === "mesher" || key === "element" ? control.value : Number(control.value) } }));
  });
  $("[data-layer-enabled]", host)?.addEventListener("change", event => saveSettings({ mesh_options: { ...options, boundary_layer: { y_plus: 30, layers: 5, growth: 1.2, ...layer, enabled: event.target.checked } } }));
  $$("[data-layer]", host).forEach(control => control.addEventListener("change", () => saveSettings({ mesh_options: { ...options, boundary_layer: { ...layer, enabled: true, [control.dataset.layer]: Number(control.value) } } })));
  $$("[data-advanced]", host).forEach(control => control.addEventListener("change", () => {
    const next={ ...solver };
    if (control.value === "") delete next[control.dataset.advanced];
    else next[control.dataset.advanced] = Number(control.value);
    saveSettings({ solver_options: next }, `${control.dataset.advanced.replaceAll("_", " ")} updated`);
  }));
  $$("[data-solver]", host).forEach(control => {
    const legacy = { "k-epsilon": "k_epsilon" };
    control.value = legacy[solver[control.dataset.solver]] || solver[control.dataset.solver] || control.options[0].value;
    control.addEventListener("change", () => saveSettings({ solver_options: { ...solver, [control.dataset.solver]: control.value } }));
  });
  $("#clearRefinements", host)?.addEventListener("click", () => saveSettings({ mesh_options: { ...options, refinements: [] } }, "Refinements cleared"));
  const range = $("#finenessRange", host); const number = $("#finenessNumber", host);
  const apply = (value, save) => {
    parameter("mesh_fineness_ratio").value = Number(value);
    number.value = value; range.value = value;
    if (save) commit(`mesh_fineness_ratio = ${value}`);
  };
  range?.addEventListener("input", () => apply(range.value, false));
  range?.addEventListener("change", () => apply(range.value, true));
  number?.addEventListener("change", () => apply(number.value, true));
  $("#meshNow", host).addEventListener("click", meshGeometry);
}

async function meshGeometry() {
  if (!workbenchReady()) return notify("Sign in to mesh.", true);
  if (dirty() && !(await commit("Fabric saved before meshing"))) return;
  setDiag("diagMesh", "running", "Meshing…");
  try {
    const mesher = state.workbench.settings.mesh_options?.mesher || "auto";
    const { task } = await api("/api/workbench/mesh", { method: "POST", body: JSON.stringify({ mesher }) });
    const done = await waitTask(task.id, snapshot => {
      const last = snapshot.log.at(-1);
      if (last) setDiag("diagMesh", "running", last.slice(0, 64));
    });
    if (done.status !== "completed") throw new Error(done.error || `meshing ${done.status}`);
    state.mesh = done.result.mesh;
    if (viewport.available) {
      showMesh();
      setLayerToggle("mesh", true);
      refreshCutReadout();
    }
    notify(`Mesh: ${state.mesh.quality.cells.toLocaleString()} cells · ${state.mesh.quality.grade}`);
    renderAll();
  } catch (error) {
    showFailure("Meshing failed", error.message);
    renderDiagnostics();
  }
}

async function waitTask(id, onTick) {
  let log = 0;
  for (;;) {
    const snapshot = await api(`/api/workbench/solver/jobs/${id}?log=${log}`);
    log = snapshot.log_count;
    onTick?.(snapshot);
    if (["completed", "failed", "cancelled"].includes(snapshot.status)) return snapshot;
    await sleep(400);
  }
}

function renderSolverPanel() {
  const host = $("#solverPanel");
  if (!workbenchReady()) { host.innerHTML = ""; return; }
  const engines = state.workbench?.engines || {};
  const analysis = state.workbench?.physics?.analysis;
  const chosen = host.dataset.engine || "auto";
  const options = [["auto", { available: true, label: "Automatic", detail: analysis === "cfd" ? "OpenFOAM when installed, else the native experimental solver." : "Native 3D FEA (beams and shells when assigned), or CalculiX when installed." }],
    ...Object.entries(engines).filter(([, engine]) => engine.analyses.includes(analysis) || !engine.available)];
  const running = ["queued", "running"].includes(state.job?.status);
  const engineTopic = key => (analysis === "cfd" && ["auto", "openfoam", "native3d"].includes(key) ? "engine.fvm_navier_stokes" : "");
  host.innerHTML = (analysis === "cfd" ? `<div class="label-line" data-help-id="engine.fvm_navier_stokes"><span class="muted">Solver engine</span>${compareButton("solver_engine", "engine.fvm_navier_stokes")}</div>` : "") +
    options.map(([key, engine]) => `<label class="engine${engine.available ? "" : " unavailable"}"${engineTopic(key) ? ` data-help-id="${engineTopic(key)}"` : ""}><input type="radio" name="engine" value="${key}"${key === chosen ? " checked" : ""}${engine.available ? "" : " disabled"}><span>${escapeHtml(engine.label)}</span><small>${escapeHtml(engine.detail)}</small></label>`).join("") +
    `<div class="btn-row"><button class="tbtn primary" id="runNow"${running ? " disabled" : ""}><svg><use href="#i-play"/></svg><span>Run simulation</span></button><button class="tbtn" id="cancelRun"${running ? "" : " disabled"}><svg><use href="#i-stop"/></svg><span>Cancel</span></button></div>
    ${state.job ? `<p class="hint">Job ${escapeHtml(state.job.id)} · ${escapeHtml(state.job.engine)} · <b>${escapeHtml(state.job.status)}</b>${state.job.error ? `, ${escapeHtml(state.job.error)}` : ""}</p>` : ""}`;
  $$("input[name=engine]", host).forEach(input => input.addEventListener("change", () => { host.dataset.engine = input.value; }));
  $("#runNow", host).addEventListener("click", runSimulation);
  $("#cancelRun", host).addEventListener("click", async () => { if (state.job) await api(`/api/workbench/solver/jobs/${state.job.id}/cancel`, { method: "POST" }); });
}

async function runSimulation() {
  if (!workbenchReady()) return notify("Solvers run in the local desktop Studio.", true);
  if (dirty() && !(await commit("Fabric saved before solving"))) return;
  setStep(3);
  try {
    const engine=$("#solverPanel").dataset.engine || "auto";
    const response = await api("/api/workbench/solver/run", { method: "POST", body: JSON.stringify({ engine }) });
    state.job = response.job;
    state.residuals = [];
    state.logCount = 0;
    state.iterationTimes = [];
    hideFailure();
    $("#solverLog").textContent = "";
    renderAll();
    pollJob();
  } catch (error) {
    showFailure("The simulation could not start", error.message);
  }
}

async function pollJob() {
  if (!state.job) return;
  try {
    const snapshot=await api(`/api/workbench/solver/jobs/${state.job.id}?residuals=${state.residuals.length}&log=${state.logCount}`);
    state.residuals.push(...snapshot.residuals);
    if (snapshot.residuals.length) (state.iterationTimes ||= []).push({ iteration: snapshot.residuals.at(-1).iteration, time: performance.now() / 1000 });
    state.logCount = snapshot.log_count;
    if (snapshot.log.length) {
      const log = $("#solverLog");
      log.textContent = (log.textContent + snapshot.log.join("\n") + "\n").slice(-60000);
      log.scrollTop = log.scrollHeight;
    }
    state.job = snapshot;
    drawResiduals();
    renderDiagnostics();
    if (["queued", "running"].includes(snapshot.status)) {
      setTimeout(pollJob, 350);
      return;
    }
    renderSolverPanel();
    if (snapshot.status === "completed") {
      if (snapshot.result?.mesh) state.mesh = snapshot.result.mesh;
      setResult(snapshot.result);
      notify(`Solver finished · ${snapshot.result.iterations} iterations${snapshot.result.converged ? "" : " (not converged)"}`);
      setStep(4);
    } else if (snapshot.status === "failed") {
      showFailure("The simulation failed", snapshot.error);
    }
    renderAll();
  } catch (error) {
    notify(error.message, true);
  }
}

async function loadJob(id, silent = false) {
  const snapshot = await api(`/api/workbench/solver/jobs/${id}`);
  state.job = snapshot;
  state.residuals = snapshot.residuals;
  state.logCount = snapshot.log_count;
  $("#solverLog").textContent = snapshot.log.join("\n");
  if (snapshot.result) setResult(snapshot.result);
  if (!silent) renderAll();
}

function setResult(result) {
  state.result = result;
  state.resultField = result?.field_names?.[0] || (result?.analysis === "cfd" ? "pressure" : "von_mises");
  if (result?.mesh) state.mesh = result.mesh;
  if (result?.mesh?.display && viewport.available) showMesh(result.mesh);
  viewport.clearProbes?.();
  if (viewport.available) updateLegend(viewport.setResult(result, state.resultField));
  refreshCutReadout();
  const hasVolume = Boolean(result?.volume?.positions?.length);
  $("#isoToggle").disabled = !hasVolume;
  if (!hasVolume) { $("#isoToggle").setAttribute("aria-pressed", "false"); $("#isoControls").hidden = true; }
  $("#isoField").replaceChildren();
  queueMicrotask(() => refreshIso());
}

const FIELD_LABELS = { pressure: "Gauge pressure", speed: "Velocity magnitude", von_mises: "Von Mises stress", wall_shear: "Wall shear stress", displacement: "Displacement", temperature: "Temperature" };
const FIELD_UNITS = { pressure: "Pa", speed: "m/s", temperature: "K", von_mises: "Pa", displacement: "m" };

function refreshIso(options = {}) {
  if (!viewport.available || !viewport.setIso) return;
  const info=viewport.setIso({ ...options, enabled: $("#isoToggle").getAttribute("aria-pressed") === "true" });
  const select = $("#isoField");
  if (info?.fields && select.options.length !== info.fields.length) {
    select.replaceChildren(...info.fields.map(field => new Option(FIELD_LABELS[field] || field, field)));
    select.value = info.field;
  }
  $("#isoReadout").textContent = info ? `${fmt(info.level, 4)} ${state.result?.units?.[info.field] || FIELD_UNITS[info.field] || ""} · ${info.triangles.toLocaleString()} tri` : "";
}

function updateLegend(legend) {
  state.legend = legend;
  const host = $("#legend");
  if (!legend) { host.hidden = true; return; }
  host.hidden = false;
  const labels = { ...FIELD_LABELS, ...(legend.labels || {}) };
  $("#legendTitle").textContent = legend.unit ? `${labels[legend.field] || legend.field} [${legend.unit}]` : `${labels[legend.field] || legend.field} · |φ| normalized`;
  const select = $("#legendField");
  const fields = legend.fields || (state.result?.analysis === "cfd" ? ["pressure", "speed"] : ["von_mises"]);
  select.replaceChildren(...fields.map(field => new Option(labels[field] || field, field)));
  select.value = legend.field;
  select.hidden = fields.length < 2;
  paintLegend($("#legendBar"), legend.kind);
  const mid = legend.kind === "diverging" ? 0 : (legend.min + legend.max) / 2;
  const bound=Math.max(Math.abs(legend.min), Math.abs(legend.max));
  $("#legendMin").textContent = fmt(legend.kind === "diverging" ? -bound : legend.min, 4);
  $("#legendMid").textContent = fmt(mid, 4);
  $("#legendMax").textContent = fmt(legend.kind === "diverging" ? bound : legend.max, 4);
}

function renderResults() {
  const host = $("#resultsPanel");
  const result=state.result;
  if (!result) {
    host.innerHTML = `<div class="note"><svg><use href="#i-info"/></svg><span>${workbenchReady() ? "Run a simulation to post-process results here and in the viewport." : "Sign in to run simulations."}</span></div>`;
    return;
  }
  const s = result.summary || {};
  const rows = result.analysis === "cfd" && result.fields
    ? [["Inlet velocity", `${fmt(s.inlet_velocity)} m/s`], ["Max speed", `${fmt(s.max_speed)} m/s`], ["Pressure range", `${fmt(s.min_pressure)} … ${fmt(s.max_pressure)} Pa`], ["Min pressure coefficient", fmt(s.min_pressure_coefficient, 4)]]
    : result.surfaces && result.analysis === "cfd"
      ? [["Cells", (s.cells || 0).toLocaleString()], ["Turbulence", s.turbulence],
        ...(s.convergence ? [["Convergence", s.convergence]] : []),
        ["Max velocity", `${fmt(s.max_velocity)} m/s`],
        ...(s.pressure_range ? [["Pressure range", `${fmt(s.pressure_range[0])} … ${fmt(s.pressure_range[1])} Pa`]] : []),
        ...(s.pressure_drop !== undefined ? [["Pressure drop", `${fmt(s.pressure_drop)} Pa`]] : []),
        ...(s.drag !== undefined ? [["Drag force", `${fmt(s.drag)} N`]] : []),
        ...(s.drag_coefficient !== undefined ? [["Drag coefficient (bbox frontal area)", fmt(s.drag_coefficient, 4)]] : []),
        ...(s.lift_coefficient !== undefined ? [["Lift coefficient (+z)", fmt(s.lift_coefficient, 4)]] : []),
        ...(s.mean_wall_y_plus !== null && s.mean_wall_y_plus !== undefined ? [["Wall y⁺ mean / max", `${fmt(s.mean_wall_y_plus, 3)} / ${fmt(s.max_wall_y_plus, 3)}`]] : []),
        ...(s.mass_imbalance !== null && s.mass_imbalance !== undefined ? [["Mass imbalance", fmt(s.mass_imbalance, 3)]] : [])]
    : result.physics === "modal"
      ? [["Total mass", `${fmt(s.total_mass, 4)} kg`], ["Σ effective mass x / y / z", s.effective_mass_total.map(v => fmt(v, 3)).join(" / ")]]
    : result.physics === "cht"
      ? [["Coolant / solid", `${s.fluid} / ${s.solid}`], ["Cells fluid / solid", `${s.fluid_cells.toLocaleString()} / ${s.solid_cells.toLocaleString()}`], ["Max solid temperature", `${fmt(s.max_solid_temperature, 5)} K`], ["Outlet temperature", `${fmt(s.outlet_temperature, 5)} K`], ["Heat to coolant ṁ·cp·ΔT", `${fmt(s.heat_to_fluid, 4)} W`], ["Energy balance error", fmt(s.energy_balance_error, 3)], ["Thermal resistance", `${fmt(s.thermal_resistance, 4)} K/W`]]
    : result.surfaces && result.analysis === "fea"
      ? [["Elements", Object.entries(s.elements).map(([k, v]) => `${v.toLocaleString()} ${k}`).join(", ")], ["Degrees of freedom", s.dof.toLocaleString()], ["Max von Mises", `${fmt(s.max_von_mises / 1e6)} MPa`], ["Max displacement", `${fmt(s.max_displacement * 1e3)} mm`], ["Reaction force", `(${s.reaction_force.map(v => fmt(v, 4)).join(", ")}) N`], ["Relative residual", fmt(s.relative_residual, 3)]]
    : result.analysis === "fea" && result.nodes
      ? [["Applied load", `${fmt(s.applied_load)} N`], ["Max von Mises (element centroid)", `${fmt(s.max_von_mises / 1e6)} MPa`], ["Tip deflection (FE)", `${fmt(s.tip_deflection * 1e3)} mm`], ["Euler–Bernoulli reference", `${fmt(s.beam_theory_tip_deflection * 1e3)} mm`], ["Beam root stress reference", `${fmt(s.beam_theory_root_stress / 1e6)} MPa`]]
      : Object.entries(s).map(([key, value]) => [key, typeof value === "object" && value ? Object.entries(value).map(([k, v]) => `${k} ${fmt(v)}`).join(", ") : fmt(value)]);
  host.innerHTML = `${inspectorHead("i-pulse", result.analysis === "cfd" ? "Flow results" : "Structural results", result.method)}
    <table class="kv-table"><tbody>
      <tr><td>Engine</td><td>${escapeHtml(result.engine)}</td></tr>
      <tr><td>Converged</td><td>${result.converged ? "yes" : "no"} · ${result.iterations} it.</td></tr>
      ${rows.map(([key, value]) => `<tr><td>${escapeHtml(key)}</td><td>${escapeHtml(value)}</td></tr>`).join("")}
    </tbody></table>
    ${result.physics === "modal" ? `<table class="kv-table modes"><thead><tr><th>Mode</th><th>f (Hz)</th><th>m<sub>eff</sub> x</th><th>y</th><th>z</th></tr></thead><tbody>${s.modes.map(mode => `<tr data-mode="mode_${mode.mode}" class="${state.resultField === `mode_${mode.mode}` ? "active" : ""}"><td>${mode.mode}</td><td>${fmt(mode.frequency, 5)}</td>${mode.effective_mass.map(v => `<td>${fmt(v, 3)}</td>`).join("")}</tr>`).join("")}</tbody></table><p class="hint">Click a mode to show its shape; effective mass is the fraction of the total mass along each axis.</p>` : ""}
    ${result.analysis === "fea" && (result.nodes || result.surfaces) ? `<div class="morph-row"><label for="deformRange">Deformation scale</label><span class="num"><input type="number" id="deformNumber" value="1" step="0.1" min="0"><span class="unit">×</span></span><input type="range" id="deformRange" min="0" max="4" step="0.05" value="1"></div>` : ""}
    ${result.summary?.reliable === false ? `<div class="note warn"><svg><use href="#i-alert"/></svg><span><b>Results not reliable:</b> ${escapeHtml(result.summary.warnings.join("; "))}. ${result.engine === "openfoam" ? "Run more iterations or refine the mesh." : "Refine the mesh, lower relaxation, or use OpenFOAM."}</span></div>` : ""}
    ${result.note ? `<div class="note"><svg><use href="#i-info"/></svg><span>${escapeHtml(result.note)}</span></div>` : ""}
    ${result.vtk ? `<div class="btn-row"><button class="tbtn" id="downloadVtk"><svg><use href="#i-export"/></svg><span>Download VTK (${escapeHtml(result.vtk.split(/[\\/]/).pop())})</span></button></div>` : ""}${result.case_archive ? `<div class="btn-row"><button class="tbtn" id="downloadCase"><svg><use href="#i-export"/></svg><span>Download OpenFOAM case (.zip)</span></button></div>` : ""}${result.case_dir ? `<p class="hint">Case: <code>${escapeHtml(result.case_dir)}</code></p>` : ""}
    ${result.analysis === "cfd" && result.fields ? '<div class="note warn"><svg><use href="#i-alert"/></svg><span>Potential flow is inviscid: it predicts pressure and velocity around attached flow, not separation, wakes, or drag.</span></div>' : ""}`;
  $("#downloadVtk", host)?.addEventListener("click", () => downloadArtifact(state.job.id, result.vtk.split(/[\\/]/).pop()));
  $("#downloadCase", host)?.addEventListener("click", () => downloadArtifact(state.job.id, result.case_archive));
  $$("[data-mode]", host).forEach(row => row.addEventListener("click", () => {
    state.resultField = row.dataset.mode;
    updateLegend(viewport.setResult?.(result, state.resultField));
    $$("[data-mode]", host).forEach(other => other.classList.toggle("active", other === row));
  }));
  const range=$("#deformRange", host); const number = $("#deformNumber", host);
  const deform = value => { range.value = value; number.value = value; updateLegend(viewport.setDeformation?.(Number(value))); };
  range?.addEventListener("input", () => deform(range.value));
  number?.addEventListener("change", () => deform(number.value));
}

async function exportCompliance(format) {
  if (!workbenchReady()) return notify("Sign in to export a compliance report.", true);
  if (dirty() && !(await commit("Fabric saved before the compliance report"))) return;
  const buttons = $$("#exportCompliancePdf, #exportComplianceHtml");
  buttons.forEach(button => { button.disabled = true; });
  notify(`Preparing the sealed ${format.toUpperCase()} compliance report…`);
  try {
    const report = await api(`/api/workbench/report?format=${format}&download=1`);
    const bytes = Uint8Array.from(atob(report.content_base64), ch => ch.charCodeAt(0));
    if (!bytes.length) throw new Error("the server returned an empty report");
    const blob = new Blob([bytes], { type: format === "pdf" ? "application/pdf" : "text/html" });
    const url = URL.createObjectURL(blob);
    const link = Object.assign(document.createElement("a"), { href: url, download: report.filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    notify(`Compliance report downloaded · ${report.filename} · Ed25519 key ${report.seal.key_id}`);
  } catch (error) {
    notify(`Compliance report failed: ${error.message}`, true);
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

async function downloadArtifact(taskId, name) {
  try {
    const token = localStorage.getItem("paramfabric.access-token");
    const response = await fetch(`/api/workbench/tasks/${taskId}/artifacts/${encodeURIComponent(name)}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    download(name, await response.blob(), "application/octet-stream");
  } catch (error) {
    notify(`Download failed: ${error.message}`, true);
  }
}

// residual chart -----------------------------------------------------------

function residualSeries() {
  const fields = [];
  state.residuals.forEach(point => Object.keys(point.fields).forEach(field => { if (!fields.includes(field)) fields.push(field); }));
  return fields.slice(0, SERIES.length);
}

function drawResiduals(hoverX = null) {
  const canvas = $("#residualChart");
  if (!canvas || canvas.closest("[hidden]")) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth; const height = canvas.clientHeight;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, width, height);
  const fields = residualSeries();
  const legend = $("#residualLegend");
  legend.innerHTML = fields.length > 1 ? fields.map((field, index) => `<span><i style="background:${SERIES[index]}"></i>${escapeHtml(field)}</span>`).join("") : "";
  const points=state.residuals.filter(point => Object.values(point.fields).some(value => value > 0));
  const timing = ["queued", "running"].includes(state.job?.status) ? solveProgress(state.iterationTimes || [], state.workbench?.settings?.solver_options?.max_iterations) : null;
  $("#residualMeta").textContent = timing ? timing.text : points.length ? `${fields.length === 1 ? `${fields[0]} · ` : ""}${points.at(-1).iteration} it.` : "";
  const pad = { left: 44, right: 10, top: 10, bottom: 22 };
  context.font = "10.5px system-ui, sans-serif";
  context.fillStyle = "#7f8c9b";
  if (!points.length) {
    context.textAlign = "center";
    context.fillText("Residuals stream here while the solver runs", width / 2, height / 2);
    return;
  }
  const iterations = points.map(point => point.iteration);
  const values = points.flatMap(point => Object.values(point.fields)).filter(value => value > 0);
  const xMin = Math.min(...iterations); const xMax = Math.max(...iterations, xMin + 1);
  const yMin = Math.floor(Math.log10(Math.min(...values))); const yMax = Math.ceil(Math.log10(Math.max(...values))) || yMin + 1;
  const x=value => pad.left + (value - xMin) / (xMax - xMin) * (width - pad.left - pad.right);
  const y = value => pad.top + (yMax - Math.log10(value)) / Math.max(1, yMax - yMin) * (height - pad.top - pad.bottom);
  context.strokeStyle = "rgba(148,170,196,0.12)";
  context.lineWidth = 1;
  context.textAlign = "right";
  const decadeStep = Math.max(1, Math.ceil((yMax - yMin) / 5));
  for (let decade = yMax; decade >= yMin; decade -= decadeStep) {
    const py = y(10 ** decade);
    context.beginPath(); context.moveTo(pad.left, py); context.lineTo(width - pad.right, py); context.stroke();
    context.fillText(`1e${decade}`, pad.left - 6, py + 3);
  }
  context.textAlign = "center";
  context.fillText(String(xMin), pad.left, height - 6);
  context.fillText(String(xMax), width - pad.right, height - 6);
  context.fillText("iteration", (pad.left + width - pad.right) / 2, height - 6);
  fields.forEach((field, index) => {
    context.strokeStyle = SERIES[index];
    context.lineWidth = 2;
    context.lineJoin = "round";
    context.beginPath();
    let started = false;
    for (const point of points) {
      const value = point.fields[field];
      if (!(value > 0)) continue;
      if (!started) { context.moveTo(x(point.iteration), y(value)); started = true; } else context.lineTo(x(point.iteration), y(value));
    }
    context.stroke();
  });
  const tip = $("#residualTip");
  if (hoverX === null) { tip.hidden = true; return; }
  const target = xMin + (hoverX - pad.left) / (width - pad.left - pad.right) * (xMax - xMin);
  const nearest = points.reduce((best, point) => Math.abs(point.iteration - target) < Math.abs(best.iteration - target) ? point : best, points[0]);
  const px=x(nearest.iteration);
  context.strokeStyle = "rgba(232,237,243,0.35)";
  context.lineWidth = 1;
  context.beginPath(); context.moveTo(px, pad.top); context.lineTo(px, height - pad.bottom); context.stroke();
  fields.forEach((field, index) => {
    const value = nearest.fields[field];
    if (!(value > 0)) return;
    context.fillStyle = SERIES[index];
    context.strokeStyle = "#10161f";
    context.lineWidth = 2;
    context.beginPath(); context.arc(px, y(value), 4, 0, Math.PI * 2); context.fill(); context.stroke();
  });
  tip.hidden = false;
  tip.innerHTML = `<div>iteration ${nearest.iteration}</div>${fields.map((field, index) => nearest.fields[field] > 0 ? `<div><span class="sw" style="background:${SERIES[index]}"></span>${escapeHtml(field)} ${nearest.fields[field].toExponential(2)}</div>` : "").join("")}`;
  tip.style.left = `${Math.min(px + 10, width - tip.offsetWidth - 4)}px`;
  tip.style.top = "8px";
}

// code sync & reviews

async function pollSync(force = false) {
  if (!state.localMode || state.sync.busy || (state.saving && !force)) return;
  if (!force && !$("#liveSync").checked) {
    if (!state.sync.items.length || force) await refreshSyncStatus();
    return;
  }
  state.sync.busy = true;
  try {
    const result = $("#liveSync").checked ? await api("/api/local/sync/reconcile", { method: "POST" }) : await api("/api/local/sync/status");
    state.sync.items = result.items;
    if (result.pulled?.length) {
      const fabric = await api("/api/local/fabric");
      const pulledNames=new Set(result.pulled.map(item => item.parameter));
      state.fabric = { version: fabric.version || 1, parameters: fabric.parameters };
      for (const name of pulledNames) {
        const index = state.draft.parameters.findIndex(item => item.name === name);
        if (index >= 0) state.draft.parameters[index] = clone(parameter(name, state.fabric));
      }
      notify(`Synced from code: ${result.pulled.map(item => `${item.parameter} = ${fmt(item.value)} (${item.path}:${item.line})`).join(", ")}`);
      scheduleEvaluate(0);
      if (state.selection?.kind === "parameter" && pulledNames.has(state.selection.name)) renderInspector();
    }
    if (result.pushed?.length) notify(`Wrote ${result.pushed.length} constant${result.pushed.length === 1 ? "" : "s"} to source: ${[...new Set(result.pushed.map(item => item.path))].join(", ")}`);
    renderSync();
    renderDiagnostics();
    renderValidation();
  } catch {
    // transient polling failures are retried
  } finally {
    state.sync.busy = false;
  }
}

async function refreshSyncStatus() {
  try {
    state.sync.items = (await api("/api/local/sync/status")).items;
    renderSync();
    renderDiagnostics();
  } catch { /* ignore */ }
}

function renderSync() {
  const list = $("#syncList");
  const items = state.sync.items;
  const attention = items.filter(item => item.state !== "in_sync").length;
  $("#syncCount").hidden = !attention;
  $("#syncCount").textContent = String(attention);
  $("#syncCount").classList.toggle("alert", items.some(item => item.state === "conflict"));
  if (!state.localMode) { list.innerHTML = '<div class="tree-empty">Code sync runs in the local desktop Studio.</div>'; return; }
  if (!items.length) { list.innerHTML = '<div class="tree-empty">No source constants match parameter names yet. Build code targets or name constants after parameters (e.g. <code>ROOT_CHORD</code>).</div>'; return; }
  const icons = { in_sync: "i-check", drift: "i-alert", structural_drift: "i-alert", conflict: "i-alert", unit_mismatch: "i-alert", unresolved: "i-alert", unsupported: "i-info" };
  list.innerHTML = items.map(item => `<div class="sync-item">
      <div class="top"><span class="lang">${escapeHtml(item.language)}</span><strong>${escapeHtml(item.name)}</strong><span class="state-tag ${item.state}"><svg><use href="#${icons[item.state]}"/></svg>${escapeHtml(item.state.replace("_", " "))}</span></div>
      <div class="file" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}:${item.line}</div>
      <div class="values"><span>source ${item.source_formula ? `= ${escapeHtml(item.source_formula)}` : fmt(item.value, 8)}${item.unit ? ` ${escapeHtml(item.unit)}` : ""}</span><span>fabric ${item.fabric_formula ? `= ${escapeHtml(item.fabric_formula)}` : item.fabric_value === null ? "—" : fmt(item.fabric_value, 8)}</span></div>
      ${item.detail ? `<div class="file">${escapeHtml(item.detail)}</div>` : ""}
      ${["drift", "structural_drift", "conflict"].includes(item.state) ? `<div class="row-actions">${item.expression ? "" : `<button class="mini-btn" data-pull="${escapeHtml(item.key)}"><svg><use href="#i-arrow-down"/></svg>Use source</button>`}<button class="mini-btn" data-push="${escapeHtml(item.key)}"><svg><use href="#i-arrow-up"/></svg>Use fabric</button></div>` : ""}
    </div>`).join("");
  $$("[data-pull]", list).forEach(button => button.addEventListener("click", () => syncAction("pull", [button.dataset.pull])));
  renderDriftReport();
  $$("[data-push]", list).forEach(button => button.addEventListener("click", () => syncAction("push", [button.dataset.push])));
}

async function renderDriftReport() {
  const host=$("#driftReport");
  if (!host || !state.localMode) return;
  try {
    const report = (await api("/api/local/sync/diff")).filter(entry => entry.status !== "in_sync");
    if (!report.length) { host.innerHTML = ""; return; }
    host.innerHTML = `<div class="section-head"><span>Drift report</span><span class="muted">${report.length} parameter(s)</span></div>` + report.map(entry => `
      <div class="sync-item"><div class="top"><strong>${escapeHtml(entry.parameter)}</strong><span class="state-tag conflict">${escapeHtml(entry.status.replace(/_/g, " "))}</span></div>
      <div class="file">fabric ${escapeHtml((entry.fabric || "").replace(/^[vf]:/, ""))}</div>
      ${entry.definitions.map(d => `<div class="file">${escapeHtml(d.path)}:${d.line}, ${escapeHtml(d.text)}</div>`).join("")}
      <div class="row-actions"><button class="mini-btn" data-resolve="${escapeHtml(entry.parameter)}" data-strategy="fabric">Use fabric</button>${entry.definitions.map(d => `<button class="mini-btn" data-resolve="${escapeHtml(entry.parameter)}" data-strategy="source:${escapeHtml(d.path)}">Use ${escapeHtml(d.path.split("/").pop())}</button>`).join("")}<button class="mini-btn" data-resolve="${escapeHtml(entry.parameter)}" data-strategy="newest">Newest</button><button class="mini-btn" data-resolve="${escapeHtml(entry.parameter)}" data-strategy="accept">Accept as-is</button></div></div>`).join("");
    $$("[data-resolve]", host).forEach(button => button.addEventListener("click", async () => {
      try {
        const result = await api("/api/local/sync/resolve", { method: "POST", body: JSON.stringify({ parameter: button.dataset.resolve, strategy: button.dataset.strategy }) });
        state.sync.items = result.items;
        const fabric = await api("/api/local/fabric");
        state.fabric = { version: fabric.version || 1, parameters: fabric.parameters };
        state.draft = clone(state.fabric);
        scheduleEvaluate(0);
        notify(`Resolved ${button.dataset.resolve}: ${result.pulled.length} pulled, ${result.pushed.length} written`);
        renderAll();
      } catch (error) {
        notify(error.message, true);
      }
    }));
  } catch {
    host.innerHTML = "";
  }
}

async function syncAction(kind, keys) {
  try {
    const result = await api(`/api/local/sync/${kind}`, { method: "POST", body: JSON.stringify({ keys }) });
    state.sync.items = result.items;
    if (kind === "pull") {
      const fabric = await api("/api/local/fabric");
      state.fabric = { version: fabric.version || 1, parameters: fabric.parameters };
      state.draft = clone(state.fabric);
      scheduleEvaluate(0);
      notify(`Pulled ${result.pulled.length} value${result.pulled.length === 1 ? "" : "s"} from source`);
    } else {
      notify(`Wrote ${result.pushed.length} constant${result.pushed.length === 1 ? "" : "s"} to source`);
    }
    renderAll();
  } catch (error) {
    notify(error.message, true);
  }
}

async function refreshQueue() {
  const host=$("#requestList");
  try {
    const requests = await api("/api/change-requests");
    if (!requests.length) { host.innerHTML = `<div class="tree-empty">No change requests.${state.localMode ? " Local edits save directly; enterprise deployments route edits through review." : ""}</div>`; return; }
    host.replaceChildren();
    for (const request of requests) {
      const card = document.createElement("div");
      card.className = "request";
      card.innerHTML = `<div><strong>${escapeHtml(request.author)}</strong> <span class="muted">${escapeHtml(request.id.slice(0, 8))} · ${escapeHtml(request.status)}</span></div><div>${escapeHtml(request.rationale)}</div><div class="muted">Changes: ${escapeHtml((request.changes || []).map(change => change.name).join(", ") || "—")} · Impact: ${escapeHtml((request.impact || []).map(item => item.parameter).join(", ") || "—")}</div><div class="actions"></div>`;
      const actions = $(".actions", card);
      const act = (label, path, body, primary) => {
        const button = document.createElement("button");
        button.className = `mini-btn${primary ? " primary" : ""}`;
        button.textContent = label;
        button.addEventListener("click", async () => {
          try { await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }); notify(`${label} recorded`); refreshQueue(); if (label === "Merge") (state.localMode ? loadAll() : connectEnterprise()); } catch (error) { notify(error.message, true); }
        });
        actions.append(button);
      };
      if (request.status === "pending" && request.can_review) {
        act("Approve", `/api/change-requests/${request.id}/review`, { decision: "approve", comment: "" }, true);
        act("Reject", `/api/change-requests/${request.id}/review`, { decision: "reject", comment: "" });
      } else if (request.status === "approved") act("Merge", `/api/change-requests/${request.id}/merge`, null, true);
      host.append(card);
    }
  } catch (error) {
    host.innerHTML = `<div class="tree-empty">Change-request service unavailable: ${escapeHtml(error.message)}</div>`;
  }
}

// export

async function runExport(kind) {
  $("#exportMenu").hidden = true;
  $("#exportMenuButton").setAttribute("aria-expanded", "false");
  try {
    if (kind === "build") {
      if (!state.localMode) throw new Error("Building into the workspace is a local desktop feature; use the individual downloads.");
      const result = await api("/api/local/build", { method: "POST", body: "null" });
      notify(result.count ? `Built ${result.count} file${result.count === 1 ? "" : "s"}: ${result.files.map(file => file.path).join(", ")}` : "No output bindings are configured, enable targets in the parameter Inspector.");
      setTimeout(() => pollSync(true), 300);
    } else if (kind === "stl" || kind === "step" || kind === "iges") {
      const token = localStorage.getItem("paramfabric.access-token");
      const response = await fetch(`/api/workbench/geometry-export.${kind}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (!response.ok) throw new Error((await response.json()).detail);
      const names = { stl: "paramfabric_morphed_mm.stl", step: "paramfabric_model.step", iges: "paramfabric_model.igs" };
      download(names[kind], await response.text(), "application/octet-stream");
    } else if (kind === "compliance-pdf" || kind === "compliance-html") {
      await exportCompliance(kind.split("-")[1]);
    } else if (kind === "openfoam" || kind === "calculix") {
      const result = await api("/api/workbench/solver/case", { method: "POST", body: JSON.stringify({ engine: kind }) });
      const detail = result.cells ? ` (${result.cells.toLocaleString()} cells, ${result.turbulence}; run ./Allrun)` : "";
      notify(`Wrote ${result.files.length} file${result.files.length === 1 ? "" : "s"} to ${result.case_dir}${detail}`);
    } else {
      const names = { cpp_header: "params.hpp", py_module: "params.py", rust: "params.rs", openscad: "params.scad", solidworks: "update_solidworks.bas", nx: "update_nx.py" };
      const result = await api(`/api/export/${kind}`);
      download(names[kind] || `${kind}.txt`, result.content);
    }
  } catch (error) {
    notify(`Export failed: ${error.message}`, true);
  }
}

// layout plumbing

function setLeftTab(name) {
  $$("[data-left]").forEach(tab => tab.classList.toggle("active", tab.dataset.left === name));
  $$("[data-left-body]").forEach(body => { body.hidden = body.dataset.leftBody !== name; });
  if (name === "fabric") renderLineage();
  if (name === "reviews") refreshQueue();
}

function setRightTab(name) {
  state.rightTab = name;
  $$("[data-right]").forEach(tab => tab.classList.toggle("active", tab.dataset.right === name));
  $$("[data-right-body]").forEach(body => { body.hidden = body.dataset.rightBody !== name; });
  if (name === "solve") requestAnimationFrame(() => drawResiduals());
}

function setStep(step) {
  state.step = step;
  setRightTab({ 1: "inspector", 2: "physics", 3: "solve", 4: "results" }[step]);
  updateSteps();
}

function setLayerToggle(layer, on) {
  const button = $(`[data-layer="${layer}"]`);
  button.setAttribute("aria-pressed", String(on));
  viewport.setLayer?.(layer, on);
}

function renderAll() {
  renderDiagnostics();
  renderTagFilter();
  renderParamList();
  renderTree();
  renderInspector();
  renderPhysics();
  renderMeshPanel();
  renderSolverPanel();
  renderResults();
  renderSync();
  renderValidation();
  renderMorphDock();
  renderHud();
  renderLineage();
  const hasGeometry = (state.workbench?.models || []).length > 0;
  $("#viewportEmpty").hidden = hasGeometry || !workbenchReady();
  $$(".tool").forEach(tool => { tool.disabled = !workbenchReady() || !hasGeometry; });
  const unit = state.workbench?.models?.[0]?.length_unit;
  $("#vpUnits").textContent = `Viewport: m${unit && unit !== "m" ? ` · model unit ${unit}` : ""} · Z up`;
}

function refreshCutReadout(info = null) {
  if (!viewport.available || $("#cutControls").hidden) return;
  const current = info || viewport.setClip({});
  const unit = state.workbench?.models?.[0]?.length_unit || "m";
  const scale = lengthUnitScale(unit) || 1;
  const axis = "XYZ"[Number($("[data-cut-axis].active")?.dataset.cutAxis || 0)];
  $("#cutReadout").textContent = `${axis} = ${fmt(current.position / scale, 4)} ${unit}`;
}

async function signingOrganization() {
  return (await api("/api/signing-keys")).organization;
}

// verification goes through the public endpoint, exactly as a third-party auditor would use it
async function verifyReportFile(file) {
  const host = $("#verifyResult");
  host.innerHTML = `<p class="hint">Verifying ${escapeHtml(file.name)}…</p>`;
  try {
    const organization = await signingOrganization();
    const response = await fetch(`/api/public/organizations/${encodeURIComponent(organization)}/verify-report`, { method: "POST", body: await file.arrayBuffer() });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || response.statusText);
    const good = result.valid;
    const reason = !result.payload_intact ? "the report content was changed after signing"
      : result.document_intact === false ? "the document's pages or metadata were edited after signing"
      : !result.signature_valid ? `the signature does not match any public key of ${organization}` : "";
    host.innerHTML = `<div class="note ${good ? "good" : "warn"}" data-verify="${good ? "valid" : "invalid"}"><svg><use href="#i-${good ? "check" : "alert"}"/></svg><span><b>${good ? "Signature valid" : "Not verified"}</b> · ${escapeHtml(file.name)}<br>
      ${good ? `${escapeHtml(result.algorithm)} · key ${escapeHtml(result.verified_key_id)} (${escapeHtml(organization)}) · payload SHA-256 intact${result.document_intact ? " · pages match the signed data" : ""}${result.generated_at ? ` · generated ${escapeHtml(result.generated_at)}` : ""}` : escapeHtml(reason)}</span></div>`;
  } catch (error) {
    host.innerHTML = `<div class="note warn" data-verify="error"><svg><use href="#i-alert"/></svg><span>Could not verify ${escapeHtml(file.name)}: ${escapeHtml(error.message)}</span></div>`;
  }
}

async function downloadPublicKey() {
  try {
    const organization = await signingOrganization();
    const response = await fetch(`/api/public/organizations/${encodeURIComponent(organization)}/signing-keys?format=pem`);
    if (!response.ok) throw new Error(response.statusText);
    download(`${organization}-signing-keys.pem`, await response.text(), "application/x-pem-file");
    notify(`Public key downloaded: paramfabric report-verify report.pdf --public-key ${organization}-signing-keys.pem`);
  } catch (error) {
    notify(`Public key download failed: ${error.message}`, true);
  }
}

function initComplianceTools() {
  $("#exportCompliancePdf").addEventListener("click", () => exportCompliance("pdf"));
  $("#exportComplianceHtml").addEventListener("click", () => exportCompliance("html"));
  $("#verifyReport").addEventListener("click", () => $("#verifyReportFile").click());
  $("#verifyReportFile").addEventListener("change", event => {
    const [file] = event.target.files;
    if (file) verifyReportFile(file);
    event.target.value = "";
  });
  $("#downloadPublicKey").addEventListener("click", downloadPublicKey);
}

function initViewportTools() {
  $$("[data-pick]").forEach(button => button.addEventListener("click", () => {
    $$("[data-pick]").forEach(other => other.classList.toggle("active", other === button));
    viewport.setPickMode?.(button.dataset.pick);
    state.pickMode = button.dataset.pick;
    if (button.dataset.pick !== "auto") notify(`Selecting ${button.dataset.pick}s only`);
  }));
  $("#cutToggle").addEventListener("click", event => {
    const on = event.currentTarget.getAttribute("aria-pressed") !== "true";
    event.currentTarget.setAttribute("aria-pressed", String(on));
    $("#cutControls").hidden = !on;
    refreshCutReadout(viewport.setClip?.({ enabled: on }));
  });
  $$("[data-cut-axis]").forEach(button => button.addEventListener("click", () => {
    $$("[data-cut-axis]").forEach(other => other.classList.toggle("active", other === button));
    refreshCutReadout(viewport.setClip?.({ axis: Number(button.dataset.cutAxis) }));
  }));
  $("#cutPosition").addEventListener("input", event => refreshCutReadout(viewport.setClip?.({ fraction: Number(event.target.value) })));
  $("#cutFlip").addEventListener("click", event => {
    const on = event.currentTarget.getAttribute("aria-pressed") !== "true";
    event.currentTarget.setAttribute("aria-pressed", String(on));
    refreshCutReadout(viewport.setClip?.({ flip: on }));
  });
  $("#isoToggle").addEventListener("click", event => {
    const on = event.currentTarget.getAttribute("aria-pressed") !== "true";
    event.currentTarget.setAttribute("aria-pressed", String(on));
    $("#isoControls").hidden = !on;
    refreshIso();
  });
  $("#isoField").addEventListener("change", event => refreshIso({ field: event.target.value }));
  $("#isoLevel").addEventListener("input", event => refreshIso({ fraction: Number(event.target.value) }));
  $("#glyphToggle").addEventListener("click", event => {
    const on = event.currentTarget.getAttribute("aria-pressed") !== "true";
    event.currentTarget.setAttribute("aria-pressed", String(on));
    viewport.setGlyphs?.(on);
  });
}

async function applySkillDefaults(level) {
  if (level === "beginner" && workbenchReady() && state.workbench.physics?.analysis === "cfd") {
    // Conservative defaults: resolved boundary layer (y⁺ = 1) with k-ω SST
    const options=state.workbench.settings.mesh_options || {};
    const solver={ ...(state.workbench.settings.solver_options || {}), turbulence: "k_omega_sst" };
    for (const key of ["relax_velocity", "relax_pressure", "relax_turbulence", "linear_tolerance_p", "linear_tolerance_u"]) delete solver[key];
    await saveSettings({
      mesh_options: { ...options, boundary_layer: { enabled: true, y_plus: 1, layers: 10, growth: 1.2 } },
      solver_options: solver,
    }, "Beginner defaults: 10 inflation layers at y⁺ = 1, k-ω SST, default relaxation");
  } else {
    notify(level === "expert" ? "Expert: help hidden; relaxation factors and matrix tolerances unlocked in Mesh & Solve" : "Intermediate: hover a setting for guidance");
  }
}

// right-click menus, named selections, visibility

function faceOf(entity) {
  return bodySummary(entity.key).full?.faces?.find(face => face.id === entity.faceId) || null;
}

function selectorOf(entity) {
  if (entity.kind === "face") {
    const face = faceOf(entity);
    return { point: face?.centroid || entity.point, normal: face?.normal || null };
  }
  return { point: entity.point };
}

function entityTarget(entity, probe = null) {
  const { model, body } = bodySummary(entity.key);
  const unit=model?.length_unit || "m";
  const scale = lengthUnitScale(unit) || 1;
  const inSet = state.pickSet.some(item => sameEntity(item, entity));
  const base = { ...entity, count: inSet ? state.pickSet.length : 1, probe };
  if (entity.kind === "face") {
    const face = faceOf(entity);
    return { ...base, title: face?.name || "Face", subtitle: face ? `${face.surface} · ${fmt(face.area / scale / scale)} ${unit}² · ${body?.name || ""}` : body?.name, surface: face?.surface, normal: face?.normal };
  }
  if (entity.kind === "edge") {
    const points = entity.points || [];
    let length = 0;
    for (let i = 3; i < points.length; i += 3) length += Math.hypot(points[i] - points[i - 3], points[i + 1] - points[i - 2], points[i + 2] - points[i - 1]);
    return { ...base, title: "Edge", subtitle: `${fmt(length / scale)} ${unit} · ${body?.name || ""}` };
  }
  return { ...base, title: body?.name || "Body", subtitle: `${body?.kind || "solid"} · ${model?.filename || ""}` };
}

function modelTarget(id) {
  const model = state.workbench.models.find(item => item.id === id);
  return { kind: "model", id, title: model.assembly || model.name, subtitle: `${model.format.toUpperCase()} · ${model.bodies.length} bodies` };
}

function featureTarget(feature) {
  return { kind: "feature", id: feature.id, title: feature.name || FEATURE_LABELS[feature.type], subtitle: FEATURE_LABELS[feature.type] };
}

function namedTarget(selection) {
  return { kind: "named", id: selection.id, selectionKind: selection.kind, title: selection.name, subtitle: `Named selection · ${selection.items.length} ${selection.kind}${selection.items.length === 1 ? "" : "s"}` };
}

function parameterTarget(item) {
  const value = resolved(item.name);
  return { kind: "parameter", name: item.name, title: item.name, subtitle: value && typeof value.value === "number" ? `${fmt(value.value)} ${value.unit || item.unit}` : String(item.value), item };
}

function probeText(probe) {
  const unit = probe.unit ? ` ${probe.unit}` : "";
  return `${fmt(probe.value, 4)}${unit}`;
}

function menuContext(target, probe) {
  const physics = state.workbench?.physics;
  const options = state.workbench?.settings?.solver_options || {};
  const legend=state.legend;
  return {
    analysis: physics?.analysis, internal: options.domain === "internal", heat: Boolean(options.heat_transfer?.enabled),
    exact: target.key ? bodySummary(target.key).model?.kernel === "occ" : false,
    named: state.workbench?.settings?.named_selections || [], hidden: state.hidden,
    probe: probe ? { text: probeText(probe) } : null, probeCount: viewport.probeCount?.() || 0,
    hasResult: Boolean(state.result), hasVolume: Boolean(state.result?.volume?.positions?.length), hasCut: Boolean(viewport.clipBounds?.()),
    fields: (legend?.fields || []).map(id => ({ id, label: legend.labels?.[id] || FIELD_LABELS[id] || id })), activeField: legend?.field,
    pickMode: state.pickMode || "auto", display: $("[data-display].active")?.dataset.display || "shaded",
    layers: Object.fromEntries(["mesh", "contour", "streamlines"].map(layer => [layer, $(`button[data-layer="${layer}"]`)?.getAttribute("aria-pressed") === "true"])),
    cutOn: $("#cutToggle").getAttribute("aria-pressed") === "true",
    bodies: (state.workbench?.scene?.bodies || []).length, hasSelection: Boolean(state.selection), canGoBack: viewport.canGoBack?.(),
    mouseSchemes: Object.entries(MOUSE_SCHEMES).map(([scheme, item]) => [scheme, item.label]), mouseScheme: prefs.mouse,
  };
}

function showMenu(target, x, y, probe = target.probe || null) {
  if (!viewport.available && ["face", "edge", "empty"].includes(target.kind)) return;
  if (isEntity(target) && !state.pickSet.some(item => sameEntity(item, target))) {
    const { kind, key, faceId, point, points } = target;
    select({ kind, key, ...(faceId !== undefined ? { faceId } : {}), ...(point ? { point } : {}), ...(points ? { points } : {}) });
  }
  openContextMenu(menuFor(target, menuContext(target, probe)), x, y, item => runCommand(item.id, item.args || {}, target, probe));
}

function togglePick(entity) {
  const others = state.pickSet.filter(item => item.kind === entity.kind);
  const present = others.find(item => sameEntity(item, entity));
  state.pickSet = present ? others.filter(item => item !== present) : [...others, entity];
  select(state.pickSet.at(-1) || null, { keepSet: true });
}

function setPickSet(list) {
  state.pickSet = list;
  select(list.at(-1) || null, { keepSet: true });
  if (list.length > 1) notify(`${list.length} ${list[0].kind}s selected`);
}

function selectedEntities(target) {
  return state.pickSet.some(item => sameEntity(item, target)) ? state.pickSet : [target];
}

function selectFacesLike(target, mode) {
  const faces = bodySummary(target.key).full?.faces || [];
  const reference = faceOf(target);
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const parallel = face => face.normal && reference?.normal && Math.abs(dot(face.normal, reference.normal)) > 0.999;
  const matches={
    all: () => true,
    surface: face => face.surface === reference?.surface,
    parallel,
    coplanar: face => parallel(face) && Math.abs(dot(reference.normal, face.centroid) - dot(reference.normal, reference.centroid)) < 1e-7,
  }[mode];
  setPickSet(faces.filter(matches).map(face => ({ kind: "face", key: target.key, faceId: face.id })));
}

// single dialog for "name this" and "rename": onSubmit may throw; the message appears under the field
let nameSubmitHandler = null;
function askName({ kicker, title, hint = "", label = "Name", value = "", action = "Save", onSubmit }) {
  $("#nameKicker").textContent = kicker;
  $("#nameTitle").textContent = title;
  $("#nameHint").textContent = hint;
  $("#nameLabel").textContent = label;
  $("#nameSubmit").textContent = action;
  $("#nameError").hidden = true;
  const input = $("#nameInput");
  input.value = value;
  nameSubmitHandler = onSubmit;
  $("#nameDialog").showModal();
  input.focus();
  input.select();
}

async function submitName(event) {
  event.preventDefault();
  try {
    await nameSubmitHandler($("#nameInput").value);
    $("#nameDialog").close();
  } catch (error) {
    $("#nameError").textContent = error.message;
    $("#nameError").hidden = false;
  }
}

function defaultSelectionName(kind) {
  const taken = new Set((state.workbench.settings.named_selections || []).map(item => item.name.toLowerCase()));
  const base = { face: "Faces", edge: "Edges", body: "Bodies" }[kind];
  for (let n = 1; ; n += 1) if (!taken.has(`${base} ${n}`.toLowerCase())) return `${base} ${n}`;
}

function entityItem(entity) {
  return entity.kind === "body" ? { body: entity.key } : { body: entity.key, selector: selectorOf(entity) };
}

function nameSelection(target) {
  const entities = selectedEntities(target);
  const { kind } = target;
  askName({
    kicker: "NAMED SELECTION", title: `Name ${entities.length > 1 ? `${entities.length} ${kind}s` : `this ${kind}`}`,
    hint: kind === "body" ? "Find the bodies again from the Scene tree." : "The name keeps pointing at the same geometry when parameters change, and boundary conditions can use it.",
    value: defaultSelectionName(kind), action: "Create",
    onSubmit: async name => {
      const result = await api("/api/workbench/named-selections", { method: "POST", body: JSON.stringify({ name, kind, items: entities.map(entityItem) }) });
      await refreshWorkbench(result.workbench);
      notify(`Named selection “${result.selection.name}” created`);
    },
  });
}

function renameEntity(target) {
  const body = target.kind === "body" ? { kind: "body", id: target.key } : { kind: target.kind, id: target.id };
  askName({
    kicker: "RENAME", title: `Rename ${target.kind}`, value: target.title, hint: "Only the display name changes. Parameters and results keep their names.",
    onSubmit: async name => {
      await refreshWorkbench(await api("/api/workbench/rename", { method: "POST", body: JSON.stringify({ ...body, name }) }));
      notify(`Renamed to ${name}`);
    },
  });
}

function renameNamed(id) {
  const selection = state.workbench.settings.named_selections.find(item => item.id === id);
  askName({
    kicker: "NAMED SELECTION", title: "Rename named selection", value: selection.name,
    onSubmit: async name => {
      const result = await api(`/api/workbench/named-selections/${id}`, { method: "PATCH", body: JSON.stringify({ name }) });
      await refreshWorkbench(result.workbench);
      notify(`Renamed to ${result.selection.name}`);
    },
  });
}

// maps a named selection to the entities on screen now (the server knows where they moved to)
async function namedEntities(id) {
  const { selection, items } = await api(`/api/workbench/named-selections/${id}/resolve`);
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const entities = items.map(item => {
    if (selection.kind === "body") return { kind: "body", key: item.body };
    if (selection.kind === "edge") return { kind: "edge", key: item.body, point: item.point, points: viewport.edgeAt?.(item.body, item.point) };
    const faces=bodySummary(item.body).full?.faces || [];
    const ranked = faces.filter(face => !item.normal || !face.normal || Math.abs(dot(face.normal, item.normal)) > 0.95)
      .map(face => ({ face, distance: Math.hypot(...face.centroid.map((v, k) => v - item.point[k])) })).sort((a, b) => a.distance - b.distance);
    return ranked[0] ? { kind: "face", key: item.body, faceId: ranked[0].face.id } : null;
  }).filter(Boolean);
  return { selection, items, entities };
}

async function selectNamed(id) {
  try {
    const { entities } = await namedEntities(id);
    if (!entities.length) return notify("This named selection no longer matches any geometry", true);
    state.pickSet = entities;
    select(entities.at(-1), { keepSet: true });
  } catch (error) { notify(error.message, true); }
}

async function lookAtEntities(entities) {
  const first = entities[0];
  const normal=first.kind === "face" ? faceOf(first)?.normal : null;
  if (first.kind === "body") return viewport.zoomTo?.(entities);
  return viewport.lookAt?.(entities, normal);
}

function setHiddenBodies(keys) {
  state.hidden = new Set(keys);
  viewport.setHidden?.(state.hidden);
  renderTree();
  if (state.hidden.size && state.result && $('button[data-layer="contour"]')?.getAttribute("aria-pressed") === "true") {
    notify("Hiding applies to CAD bodies. Results stay visible until you turn Contour off, and hidden bodies are still solved.");
  }
}

function setCutPlane(axis, position) {
  const bounds = viewport.clipBounds?.();
  if (!bounds) return;
  const span = bounds.high[axis] - bounds.low[axis];
  const fraction=span > 0 ? Math.min(1, Math.max(0, (position - bounds.low[axis]) / span)) : 0.5;
  const toggle = $("#cutToggle");
  toggle.setAttribute("aria-pressed", "true");
  $("#cutControls").hidden = false;
  $$("[data-cut-axis]").forEach(other => other.classList.toggle("active", Number(other.dataset.cutAxis) === axis));
  $("#cutPosition").value = fraction;
  refreshCutReadout(viewport.setClip({ enabled: true, axis, fraction }));
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    notify(`${label} copied`);
  } catch {
    notify(`${label}: ${text}`);
  }
}

function saveViewportImage() {
  const data = viewport.snapshot?.();
  if (!data) return;
  const bytes = Uint8Array.from(atob(data.split(",")[1]), ch => ch.charCodeAt(0));
  download("paramfabric-viewport.png", bytes, "image/png");
  notify("Viewport image saved");
}

async function runCommand(id, args, target, probe) {
  const edit = async (promise, message) => {
    try { await refreshWorkbench(await promise); if (message) notify(message); } catch (error) { notify(error.message, true); }
  };
  const entities = isEntity(target) ? selectedEntities(target) : [];
  const unit = target.key ? bodySummary(target.key).model?.length_unit || "m" : "m";
  const scale = lengthUnitScale(unit) || 1;
  const commands = {
    lookAt: () => lookAtEntities(entities),
    zoomTo: () => viewport.zoomTo?.(entities),
    zoomModel: () => viewport.zoomTo?.(state.workbench.models.find(model => model.id === target.id).bodies.map(body => ({ key: `${target.id}/${body.id}` }))),
    selectBody: () => select({ kind: "body", key: target.key }),
    selectFaces: () => selectFacesLike(target, args.mode),
    selectBodies: () => setPickSet((state.workbench.scene.bodies || []).map(entry => ({ kind: "body", key: entry.key }))),
    clearSelection: () => select(null),
    nameSelection: () => nameSelection(target),
    addToNamed: async () => {
      try {
        const result = await api(`/api/workbench/named-selections/${args.selection}`, { method: "PATCH", body: JSON.stringify({ add_items: entities.map(entityItem) }) });
        await refreshWorkbench(result.workbench);
        notify(`Added to “${result.selection.name}”`);
      } catch (error) { notify(error.message, true); }
    },
    bc: () => assignBoundaryCondition(args.type, entities.map(selectorOf)),
    shell: () => openMemberDialog("shell", target, target.title, selectorOf(target)),
    beam: () => openMemberDialog("beam", target, target.subtitle),
    edgeFeature: () => createFeature(args.type, { body: target.key, edges: entities.map(entity => ({ point: entity.point })) }),
    bodyFeature: () => openFeature(args.type, { body: target.key }),
    pushPull: () => createFeature("offset_face", { body: target.key, selector: selectorOf(target) }),
    refine: () => {
      const options = { ...(state.workbench.settings.mesh_options || {}) };
      options.refinements = [...(options.refinements || []), { selectors: entities.map(selectorOf), size_ratio: 0.25 }];
      saveSettings({ mesh_options: options }, "Local refinement added: mesh again to apply");
    },
    hide: () => setHiddenBodies([...state.hidden, args.key]),
    show: () => setHiddenBodies([...state.hidden].filter(key => key !== args.key)),
    isolate: () => setHiddenBodies((state.workbench.scene.bodies || []).map(entry => entry.key).filter(key => key !== args.key)),
    showAll: () => setHiddenBodies([]),
    copyPoint: () => {
      const point=target.point || faceOf(target)?.centroid;
      if (point) copyText(`${point.map(v => fmt(v / scale, 6)).join(", ")} ${unit}`, "Coordinates");
    },
    properties: () => {
      if (target.kind === "parameter") return selectParameter(target.name);
      if (target.kind === "model" || target.kind === "feature") return select({ kind: target.kind, id: target.id });
      setRightTab("inspector");
    },
    probe: () => { viewport.addProbe(probe.point, probeText(probe)); },
    clearProbes: () => viewport.clearProbes?.(),
    isoHere: () => {
      const info = viewport.setIso({ enabled: true, field: probe.field });
      const fraction = info && info.max > info.min ? (probe.value - info.min) / (info.max - info.min) : 0.5;
      $("#isoToggle").setAttribute("aria-pressed", "true");
      $("#isoControls").hidden = false;
      $("#isoLevel").value = fraction;
      refreshIso({ field: probe.field, fraction });
      $("#isoField").value = probe.field;
    },
    cutHere: () => setCutPlane(args.axis, probe.point[args.axis]),
    view: () => viewport.view?.(args.preset),
    fit: () => viewport.fit?.(),
    previousView: () => viewport.previousView?.(),
    pickMode: () => $(`[data-pick="${args.mode}"]`)?.click(),
    display: () => $(`[data-display="${args.mode}"]`)?.click(),
    layer: () => setLayerToggle(args.layer, $(`button[data-layer="${args.layer}"]`).getAttribute("aria-pressed") !== "true"),
    field: () => { const field = $("#legendField"); field.value = args.field; field.dispatchEvent(new Event("change")); },
    cut: () => $("#cutToggle").click(),
    saveImage: saveViewportImage,
    importCad: () => $("#cadInput").click(),
    loadDemo,
    rename: () => renameEntity(target),
    removeModel: () => { select({ kind: "model", id: target.id }); setRightTab("inspector"); $("#removeModel")?.focus(); notify("Press Remove from workspace to confirm"); },
    deleteFeature: () => edit(api(`/api/workbench/features/${target.id}`, { method: "DELETE" }), "Feature deleted"),
    copyName: () => copyText(target.name, "Name"),
    copyValue: () => copyText(target.subtitle, "Value"),
    goStep: () => setStep(args.step),
    namedSelect: () => selectNamed(args.selection),
    namedLook: async () => {
      try {
        const { entities: found } = await namedEntities(args.selection);
        if (found.length) { state.pickSet = found; select(found.at(-1), { keepSet: true }); await lookAtEntities(found); }
      } catch (error) { notify(error.message, true); }
    },
    namedBc: async () => {
      try {
        const { items } = await namedEntities(args.selection);
        await assignBoundaryCondition(args.type, items.map(item => ({ point: item.point, normal: item.normal || null })), { named_selection: args.selection });
      } catch (error) { notify(error.message, true); }
    },
    mouseScheme: () => setMouseScheme(args.scheme),
    palette: openPalette,
    resetLayout: () => { layout.reset(); notify("Window layout reset"); },
    meshWorst: goToWorstCell,
    namedRename: () => renameNamed(args.selection),
    namedDelete: () => edit(api(`/api/workbench/named-selections/${args.selection}`, { method: "DELETE" }), "Named selection deleted"),
  };
  await commands[id]?.();
}

// L look at · Z zoom · H hide · I isolate · A show all · N name · F2 rename · F fit · Esc clear
function entityShortcut(event) {
  if (contextMenuOpen()) return;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (key === "f") return viewport.fit?.();
  if (key === "Escape") return select(null);
  if (key === "a" && state.hidden.size) return setHiddenBodies([]);
  const selection = state.selection;
  if (key === "F2") {
    event.preventDefault();
    if (selection?.kind === "feature") return renameEntity(featureTarget(state.workbench.settings.features.find(item => item.id === selection.id)));
    if (selection?.kind === "model") return renameEntity(modelTarget(selection.id));
    if (selection?.kind === "body") return renameEntity(entityTarget(selection));
    return;
  }
  if (!isEntity(selection)) return;
  const target = entityTarget(selection);
  if (key === "l") runCommand("lookAt", {}, target);
  else if (key === "z") runCommand("zoomTo", {}, target);
  else if (key === "n") nameSelection(target);
  else if (key === "h") setHiddenBodies([...state.hidden, selection.key]);
  else if (key === "i") setHiddenBodies((state.workbench.scene.bodies || []).map(entry => entry.key).filter(body => body !== selection.key));
}

function initViewport() {
  viewport = createViewport($("#viewportCanvas"), {
    onContext: (picked, probe, event) => {
      const target = picked && picked.kind !== "feature" ? entityTarget(picked, probe) : { kind: "empty", probe };
      showMenu(target, event.clientX, event.clientY, probe);
    },
    onSelect: (picked, { add = false } = {}) => {
      if (consumeDismissal()) return;
      if (!picked) return add ? undefined : select(null);
      if (picked.kind === "feature") return select({ kind: "feature", id: picked.key.split("/")[1] });
      if (add) return togglePick(picked);
      select(picked);
      // edge / Face selection modes are the beam / shell assignment tools in structural analyses
      if (state.workbench?.physics?.analysis === "fea" && state.pickMode === picked.kind) {
        queueMicrotask(() => $(picked.kind === "edge" ? "#assignBeam" : "#assignShell")?.click());
      }
    },
  });
  const webgpu = "gpu" in navigator ? "WebGPU adapter present" : "WebGPU not exposed";
  if (viewport.available) {
    $("#diagRenderer").dataset.state = "good";
    $("#diagRenderer span").textContent = `${viewport.renderer} renderer · ${webgpu}`;
  } else {
    $("#diagRenderer").dataset.state = "critical";
    $("#diagRenderer span").textContent = "3D unavailable (WebGL disabled)";
  }
}

function initEvents() {
  $$(".step").forEach(button => button.addEventListener("click", () => setStep(Number(button.dataset.step))));
  $$("[data-left]").forEach(tab => tab.addEventListener("click", () => setLeftTab(tab.dataset.left)));
  $$("[data-right]").forEach(tab => tab.addEventListener("click", () => {
    setRightTab(tab.dataset.right);
    state.step = { inspector: 1, physics: 2, solve: 3, results: 4 }[tab.dataset.right];
    updateSteps();
  }));
  $("#importCad").addEventListener("click", () => $("#cadInput").click());
  $("#emptyImport").addEventListener("click", () => $("#cadInput").click());
  $("#cadInput").addEventListener("change", event => { routeFile(event.target.files[0]); event.target.value = ""; });
  $("#importFabric").addEventListener("click", () => $("#fabricInput").click());
  $("#fabricInput").addEventListener("change", event => { routeFile(event.target.files[0]); event.target.value = ""; });
  $("#cadForm").addEventListener("submit", submitCad);
  $("#featureForm").addEventListener("submit", submitFeature);
  $("#mergeImport").addEventListener("click", mergeImport);
  $$("[data-close]").forEach(button => button.addEventListener("click", () => $(`#${button.dataset.close}`).close()));
  $("#loadDemo").addEventListener("click", loadDemo);
  $("#emptyDemo").addEventListener("click", loadDemo);
  $("#checkFabric").addEventListener("click", async () => {
    try {
      if (state.localMode) {
        await evaluateDraft();
        const result = await api("/api/local/check", { method: "POST" });
        notify(`Fabric check passed · ${result.parameters} parameters · units and DAG verified`);
      } else {
        await api("/api/parameters");
        notify("Fabric check passed on the server");
      }
    } catch (error) {
      notify(`Fabric check failed: ${error.message}`, true);
      $("#validationList").hidden = false;
      $("#validationToggle").setAttribute("aria-expanded", "true");
    }
  });
  $("#meshGeometry").addEventListener("click", () => { setStep(3); meshGeometry(); });
  $("#runSimulation").addEventListener("click", runSimulation);
  $("#exportMenuButton").addEventListener("click", event => {
    event.stopPropagation();
    const menu = $("#exportMenu");
    menu.hidden = !menu.hidden;
    event.currentTarget.setAttribute("aria-expanded", String(!menu.hidden));
  });
  document.addEventListener("click", event => { if (!event.target.closest(".menu-wrap")) $("#exportMenu").hidden = true; });
  $$("[data-export]").forEach(button => button.addEventListener("click", () => runExport(button.dataset.export)));
  $("#connectEnterprise").addEventListener("click", async () => { try { await connectEnterprise(); } catch (error) { notify(`Sign-in failed: ${error.message}`, true); } });
  $("#paramSearch").addEventListener("input", renderParamList);
  $("#tagFilter").addEventListener("change", renderParamList);
  $("#newParameter").addEventListener("click", newParameter);
  $$("[data-feature]").forEach(button => button.addEventListener("click", () => openFeature(button.dataset.feature)));
  $("#bindTool").addEventListener("click", () => {
    if (state.selection?.kind === "face" || state.selection?.kind === "body") { setRightTab("inspector"); renderInspector(); }
    else notify("Select a planar face or a body in the viewport, then bind its dimension in the Inspector.");
  });
  $("#liveSync").addEventListener("change", () => { pollSync(true); renderDiagnostics(); });
  $("#pullAll").addEventListener("click", () => {
    const keys = state.sync.items.filter(item => item.state === "drift" && !item.expression).map(item => item.key);
    keys.length ? syncAction("pull", keys) : notify("Nothing to pull");
  });
  $("#pushAll").addEventListener("click", () => syncAction("push", state.sync.items.filter(item => item.state === "drift").map(item => item.key)));
  $("#refreshQueue").addEventListener("click", refreshQueue);
  $("#validationToggle").addEventListener("click", event => {
    const open = $("#validationList").hidden;
    $("#validationList").hidden = !open;
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  $$("[data-view]").forEach(button => button.addEventListener("click", () => viewport.view?.(button.dataset.view)));
  $$("[data-display]").forEach(button => button.addEventListener("click", () => {
    $$("[data-display]").forEach(other => other.classList.toggle("active", other === button));
    viewport.setDisplay?.(button.dataset.display);
    if (state.selection) viewport.select?.(["body", "face", "edge"].includes(state.selection.kind) ? state.selection : null);
  }));
  $$("[data-layer]").forEach(button => button.addEventListener("click", () => setLayerToggle(button.dataset.layer, button.getAttribute("aria-pressed") !== "true")));
  $("#morphCollapse").addEventListener("click", event => {
    const open = $("#morphRows").hidden;
    $("#morphRows").hidden = !open;
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  $("#legendField").addEventListener("change", event => {
    state.resultField = event.target.value;
    updateLegend(viewport.setResult?.(state.result, state.resultField));
  });
  const chart = $("#residualChart");
  chart.addEventListener("pointermove", event => drawResiduals(event.offsetX));
  chart.addEventListener("pointerleave", () => drawResiduals());
  window.addEventListener("resize", () => drawResiduals());
  let dragDepth = 0;
  window.addEventListener("dragenter", event => { if ([...event.dataTransfer.types].includes("Files")) { dragDepth += 1; $("#dropOverlay").hidden = false; } });
  window.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("#dropOverlay").hidden = true; });
  window.addEventListener("dragover", event => event.preventDefault());
  window.addEventListener("drop", event => { event.preventDefault(); dragDepth = 0; $("#dropOverlay").hidden = true; routeFile(event.dataTransfer.files[0]); });
  document.addEventListener("keydown", event => {
    const typing = event.target.closest("input, textarea, select");
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.key.toLowerCase() === "o") { event.preventDefault(); $("#cadInput").click(); }
    else if (mod && event.key === "Enter") { event.preventDefault(); runSimulation(); }
    else if (mod && event.key.toLowerCase() === "s") { event.preventDefault(); commit(); }
    else if (mod && event.key.toLowerCase() === "k") { event.preventDefault(); setLeftTab("fabric"); $("#paramSearch").focus(); }
    else if (mod && event.key.toLowerCase() === "p") { event.preventDefault(); openPalette(); }
    else if (mod && event.key.toLowerCase() === "b") { event.preventDefault(); layout.toggle(event.shiftKey ? "right" : "left"); }
    else if (mod && (event.key === "=" || event.key === "+")) { event.preventDefault(); setUiScale(nextScale(prefs.scale, 1)); }
    else if (mod && event.key === "-") { event.preventDefault(); setUiScale(nextScale(prefs.scale, -1)); }
    else if (mod && event.key === "0") { event.preventDefault(); setUiScale(1); }
    else if (!typing && mod && event.key.toLowerCase() === "z") { event.preventDefault(); runUndo(event.shiftKey ? "redo" : "undo"); }
    else if (!typing && mod && event.key.toLowerCase() === "y") { event.preventDefault(); runUndo("redo"); }
    else if (!typing && event.key === "?" && !document.querySelector("dialog[open]")) openShortcuts();
    else if (!typing && !mod && !document.querySelector("dialog[open]")) entityShortcut(event);
  });
}

// Automation hook (UI tests and scripting from the devtools console)
window.paramfabricStudio = { get viewport() { return viewport; }, get layout() { return layout; }, select, state, showMenu, showFailure, get prefs() { return prefs; } };

// preferences, undo, failures, mesh viewer, command search

const PREFS_KEY = "paramfabric.prefs";
const prefs = (() => {
  try { return { mouse: "paramfabric", invertWheel: false, scale: 1, ...JSON.parse(localStorage.getItem(PREFS_KEY)) }; } catch { return { mouse: "paramfabric", invertWheel: false, scale: 1 }; }
})();
let layout;

function savePrefs() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch { /* private mode: lasts this session */ }
}

function setMouseScheme(scheme) {
  prefs.mouse = scheme;
  viewport.setMouseScheme?.(scheme, prefs.invertWheel);
  savePrefs();
  notify(`Mouse: ${MOUSE_SCHEMES[scheme].label}. ${MOUSE_SCHEMES[scheme].summary}`);
}

function setUiScale(scale) {
  prefs.scale = scale;
  document.documentElement.style.zoom = String(scale);
  savePrefs();
  notify(`Interface size ${Math.round(scale * 100)}%`);
}

// Settings and parameter edits can be undone. Creating features, members and named selections cannot yet.
function recordUndo(entry) {
  if (state.applyingUndo) return;
  state.undo.push(entry);
  if (state.undo.length > 50) state.undo.shift();
  state.redo = [];
}

async function runUndo(direction) {
  const [from, to] = direction === "undo" ? [state.undo, state.redo] : [state.redo, state.undo];
  const entry = from.pop();
  if (!entry) return notify(direction === "undo" ? "Nothing to undo" : "Nothing to redo");
  state.applyingUndo = true;
  try {
    if (entry.kind === "fabric") {
      to.push({ kind: "fabric", fabric: clone(state.fabric), label: entry.label });
      state.draft = clone(entry.fabric);
      await api("/api/local/fabric", { method: "POST", body: JSON.stringify(state.draft) });
      state.fabric = clone(state.draft);
      await refreshWorkbench();
      scheduleEvaluate(0);
    } else {
      const current = Object.fromEntries(Object.keys(entry.values).map(key => [key, clone(state.workbench.settings[key] ?? (["boundary_conditions", "structural_members"].includes(key) ? [] : {}))]));
      to.push({ kind: "settings", values: current, label: entry.label });
      await refreshWorkbench(await api("/api/workbench/settings", { method: "POST", body: JSON.stringify({ values: entry.values }) }));
    }
    notify(`${direction === "undo" ? "Undid" : "Redid"}: ${entry.label}`);
  } catch (error) {
    notify(`Could not ${direction}: ${error.message}`, true);
  } finally {
    state.applyingUndo = false;
  }
}

// A failure stays on screen until dismissed, leads with what to do, and keeps the raw output one click away
function showFailure(context, message) {
  const text = String(message || "Unknown error");
  const [lead, ...rest] = text.split("\n\n");
  const hasDetails=rest.length > 0;
  const sentence = lead.indexOf(". ");
  $("#failureTitle").textContent = hasDetails && sentence > 0 ? lead.slice(0, sentence + 1) : context;
  $("#failureFix").textContent = hasDetails ? (sentence > 0 ? lead.slice(sentence + 2) : lead) : text.split("\n")[0];
  $("#failureDetails").textContent = hasDetails ? rest.join("\n\n") : text;
  $("#failureMore").open = false;
  $("#failureMore").hidden = !hasDetails && !text.includes("\n");
  $("#failureCard").hidden = false;
}

function hideFailure() {
  $("#failureCard").hidden = true;
}

function showMesh(mesh = state.mesh) {
  viewport.setMesh(mesh.display, mesh.volume);
  applyMeshView();
}

function meshAnalysis() {
  const view = state.meshView;
  const volume = state.mesh?.volume;
  if (view.source !== state.mesh) {
    view.source = state.mesh;
    view.range = null;
    view.rows = patchRows(state.mesh?.display?.patches);
    view.hidden = new Set(view.rows.filter(row => row.hiddenByDefault).map(row => row.id));
    const scores = volume ? tetQualities(volume) : null;
    view.analysis = scores ? { scores, stats: qualityStats(scores) } : null;
  }
  return view.analysis;
}

function applyMeshView() {
  if (!viewport.available || !state.mesh?.display) return;
  const analysis = meshAnalysis();
  const view = state.meshView;
  viewport.setMeshView({
    style: view.style, hidden: view.hidden, colors: Object.fromEntries(view.rows.map(row => [row.id, row.color])),
    quality: analysis?.scores.quality || null,
    highlight: view.range && analysis ? cellsInRange(analysis.scores, view.range[0], view.range[1]) : null,
  });
}

function meshViewerHtml() {
  if (state.mesh?.display?.kind !== "surface") return "";
  const analysis = meshAnalysis();
  const view = state.meshView;
  const styles=[["shaded", "Solid + edges"], ["wire", "Wireframe"], ["xray", "See-through"]];
  const stats = analysis?.stats;
  const verdict = stats ? qualityVerdict(stats) : null;
  const peak = stats ? Math.max(...stats.bins, 1) : 1;
  const bars = stats ? stats.bins.map((count, bin) => {
    const low = bin / stats.bins.length; const high = (bin + 1) / stats.bins.length;
    const height=count ? Math.max(4, Math.sqrt(count / peak) * 60) : 0;
    const [r, g, b] = qualityColor((low + high) / 2).map(v => Math.round(v * 255));
    const active = view.range && Math.abs(view.range[0] - low) < 1e-9 && Math.abs(view.range[1] - high) < 1e-9;
    return `<button type="button" class="q-bar${active ? " active" : ""}" data-low="${low}" data-high="${high}" ${count ? "" : "disabled"} title="Quality ${low.toFixed(1)}–${high.toFixed(1)}: ${count.toLocaleString()} cells. Click to show them." aria-label="Quality ${low.toFixed(1)} to ${high.toFixed(1)}: ${count} cells"><span style="height:${height}px;background:rgb(${r},${g},${b})"></span></button>`;
  }).join("") : "";
  return `<div class="section-head"><span>Mesh viewer</span></div>
    <div class="seg" role="radiogroup" aria-label="Mesh display">${styles.map(([style, label]) => `<button type="button" role="radio" aria-checked="${view.style === style}" class="${view.style === style ? "active" : ""}" data-mesh-style="${style}">${label}</button>`).join("")}</div>
    ${view.rows.length ? `<div class="patch-list">${view.rows.map(row => `<label class="patch" title="${row.triangles.toLocaleString()} faces"><input type="checkbox" data-patch="${escapeHtml(row.id)}"${view.hidden.has(row.id) ? "" : " checked"}><i style="background:${row.color}"></i><span>${escapeHtml(row.label)}</span></label>`).join("")}</div>` : ""}
    ${stats ? `<div class="note ${verdict.level === "good" ? "good" : "warn"}" data-mesh-verdict="${verdict.level}"><svg><use href="#i-${verdict.level === "good" ? "check" : "alert"}"/></svg><span>${escapeHtml(verdict.text)}</span></div>
    <div class="label-line"><span class="muted">Cell quality (1 is ideal). Click a bar to see those cells.</span></div>
    <div class="q-hist">${bars}</div><div class="q-axis"><span>0 bad</span><span>0.5</span><span>1 ideal</span></div>
    <div class="btn-row"><button type="button" class="tbtn" id="meshWorst"><span>Go to worst cell</span></button><button type="button" class="tbtn" id="meshPoor"${stats.poor ? "" : " disabled"}><span>Show ${stats.poor.toLocaleString()} poor cells</span></button>${view.range ? '<button type="button" class="tbtn" id="meshClearHighlight"><span>Clear</span></button>' : ""}</div>
    ${stats.unscored ? `<p class="hint">${(state.mesh.quality?.cells - stats.total).toLocaleString()} prism or hex cells are not scored: boundary-layer cells are stretched on purpose. checkMesh covers them when you solve.</p>` : ""}` : '<p class="hint">This mesh is too large to send to the browser for cell analysis. Open <b>mesh.vtk</b> in ParaView for per-cell checks.</p>'}
    <p class="hint">Turn on <b>Cut</b> in the viewport to see the actual cells inside, coloured by quality.</p>`;
}

function bindMeshViewer(host) {
  $$("[data-mesh-style]", host).forEach(button => button.addEventListener("click", () => { state.meshView.style = button.dataset.meshStyle; applyMeshView(); renderMeshPanel(); }));
  $$("[data-patch]", host).forEach(input => input.addEventListener("change", () => {
    input.checked ? state.meshView.hidden.delete(input.dataset.patch) : state.meshView.hidden.add(input.dataset.patch);
    applyMeshView();
  }));
  const highlight = range => {
    state.meshView.range = range;
    setLayerToggle("mesh", true);
    applyMeshView();
    renderMeshPanel();
  };
  $$(".q-bar", host).forEach(bar => bar.addEventListener("click", () => {
    const range = [Number(bar.dataset.low), Number(bar.dataset.high)];
    const same = state.meshView.range && state.meshView.range[0] === range[0] && state.meshView.range[1] === range[1];
    highlight(same ? null : range);
  }));
  $("#meshPoor", host)?.addEventListener("click", () => highlight([0, 0.1]));
  $("#meshClearHighlight", host)?.addEventListener("click", () => highlight(null));
  $("#meshWorst", host)?.addEventListener("click", goToWorstCell);
}

function goToWorstCell() {
  const analysis = meshAnalysis();
  if (!analysis || analysis.stats.worst < 0) return notify("No scored cells in this mesh");
  const { worst, min } = analysis.stats;
  const corners = cellCorners(state.mesh.volume, worst);
  setLayerToggle("mesh", true);
  state.meshView.range = [min, min + 1e-6];
  state.meshView.style = state.meshView.style === "shaded" ? "xray" : state.meshView.style;
  applyMeshView();
  renderMeshPanel();
  viewport.zoomTo(corners.map(world => ({ world })));
  notify(`Worst cell: quality ${min.toFixed(3)}. Previous view is in the right-click menu.`);
}

function paletteCommands() {
  const run = (id, args = {}) => () => runCommand(id, args, { kind: "empty" }, null);
  const click=selector => () => $(selector)?.click();
  const views=[["iso", "Isometric"], ["front", "Front"], ["back", "Back"], ["top", "Top"], ["bottom", "Bottom"], ["right", "Right"], ["left", "Left"]];
  const features = state.workbench?.scene?.bodies?.length ? ["extrude", "split", "boolean", "scale", "flow_volume"] : [];
  return [
    { label: "Run simulation", group: "Solve", keywords: "solve start calculate", shortcut: "Ctrl+Enter", run: runSimulation },
    { label: "Cancel simulation", group: "Solve", keywords: "stop abort", run: click("#cancelRun") },
    { label: "Mesh geometry", group: "Mesh", keywords: "generate grid", run: () => { setStep(3); $("#meshNow")?.click(); } },
    { label: "Show or hide mesh", group: "Mesh", run: run("layer", { layer: "mesh" }) },
    { label: "Go to worst cell", group: "Mesh", keywords: "quality bad skewed", run: goToWorstCell },
    { label: "Import CAD", group: "File", keywords: "open step iges stl", shortcut: "Ctrl+O", run: run("importCad") },
    { label: "Load demo part", group: "File", keywords: "bracket example", run: loadDemo },
    { label: "Save fabric", group: "File", keywords: "commit parameters", shortcut: "Ctrl+S", run: () => commit() },
    { label: "Undo", group: "Edit", shortcut: "Ctrl+Z", run: () => runUndo("undo") },
    { label: "Redo", group: "Edit", shortcut: "Ctrl+Y", run: () => runUndo("redo") },
    { label: "New parameter", group: "Edit", run: newParameter },
    { label: "Find a parameter", group: "Edit", keywords: "search fabric", shortcut: "Ctrl+K", run: () => { setLeftTab("fabric"); $("#paramSearch").focus(); } },
    ...features.map(type => ({ label: `Create ${FEATURE_LABELS[type] || type}`, group: "Feature", keywords: "insert add", run: () => openFeature(type) })),
    ...views.map(([preset, label]) => ({ label: `View ${label}`, group: "View", keywords: "camera look", run: run("view", { preset }) })),
    { label: "Zoom to fit", group: "View", keywords: "fit all", shortcut: "F", run: run("fit") },
    { label: "Previous view", group: "View", keywords: "back camera", run: run("previousView") },
    { label: "Toggle cutting plane", group: "View", keywords: "section slice clip", run: run("cut") },
    { label: "Toggle iso-surface", group: "View", keywords: "contour level", run: click("#isoToggle") },
    { label: "Toggle velocity vectors", group: "View", keywords: "arrows glyphs", run: click("#glyphToggle") },
    { label: "Show all bodies", group: "View", keywords: "unhide", shortcut: "A", run: run("showAll") },
    { label: "Save viewport image", group: "View", keywords: "screenshot picture png", run: run("saveImage") },
    { label: "Clear probes", group: "View", run: run("clearProbes") },
    ...[["1", "Geometry & Parameters"], ["2", "Physics"], ["3", "Mesh & Solver"], ["4", "Results"]].map(([step, label]) => ({ label: `Go to ${label}`, group: "Step", run: () => setStep(Number(step)) })),
    { label: "Export compliance report (PDF)", group: "Report", keywords: "audit iso as9100", run: click("#exportCompliancePdf") },
    { label: "Verify a report", group: "Report", keywords: "signature check", run: click("#verifyReport") },
    { label: "Show or hide left panel", group: "Window", keywords: "collapse sidebar scene", shortcut: "Ctrl+B", run: () => layout.toggle("left") },
    { label: "Show or hide right panel", group: "Window", keywords: "collapse sidebar inspector", shortcut: "Ctrl+Shift+B", run: () => layout.toggle("right") },
    { label: "Reset window layout", group: "Window", keywords: "panels default", run: run("resetLayout") },
    { label: "Make interface larger", group: "Window", keywords: "scale zoom font size dpi", shortcut: "Ctrl+=", run: () => setUiScale(nextScale(prefs.scale, 1)) },
    { label: "Make interface smaller", group: "Window", keywords: "scale zoom font size dpi", shortcut: "Ctrl+-", run: () => setUiScale(nextScale(prefs.scale, -1)) },
    { label: "Reset interface size", group: "Window", keywords: "scale zoom", shortcut: "Ctrl+0", run: () => setUiScale(1) },
    ...Object.entries(MOUSE_SCHEMES).map(([scheme, item]) => ({ label: `Mouse controls: ${item.label}`, group: "Preferences", keywords: `navigation orbit pan rotate ${item.summary}`, run: () => setMouseScheme(scheme) })),
    { label: `${prefs.invertWheel ? "Normal" : "Reverse"} mouse wheel zoom`, group: "Preferences", keywords: "scroll direction invert", run: () => { prefs.invertWheel = !prefs.invertWheel; viewport.setMouseScheme?.(prefs.mouse, prefs.invertWheel); savePrefs(); notify(prefs.invertWheel ? "Wheel zoom reversed" : "Wheel zoom normal"); } },
    { label: "Mouse and keyboard help", group: "Help", keywords: "shortcuts keys", shortcut: "?", run: openShortcuts },
  ];
}

function openPalette() {
  const dialog = $("#paletteDialog");
  if (dialog.open) return;
  const input=$("#paletteInput");
  const list = $("#paletteList");
  const commands = paletteCommands();
  let shown=commands;
  let active=0;
  const draw = () => {
    list.innerHTML = shown.length ? shown.slice(0, 60).map((command, index) => `<button type="button" role="option" class="palette-item${index === active ? " active" : ""}" data-index="${index}" aria-selected="${index === active}"><span class="palette-group">${escapeHtml(command.group)}</span><span>${escapeHtml(command.label)}</span>${command.shortcut ? `<kbd>${escapeHtml(command.shortcut)}</kbd>` : "<i></i>"}</button>`).join("") : '<p class="hint palette-empty">No matching command.</p>';
    list.querySelector(".active")?.scrollIntoView({ block: "nearest" });
  };
  const go = index => {
    const command = shown[index];
    if (!command) return;
    dialog.close();
    command.run();
  };
  input.value = "";
  input.oninput = () => { shown = rankCommands(commands, input.value); active = 0; draw(); };
  input.onkeydown = event => {
    if (event.key === "ArrowDown") { event.preventDefault(); active = Math.min(active + 1, Math.min(shown.length, 60) - 1); draw(); }
    else if (event.key === "ArrowUp") { event.preventDefault(); active = Math.max(active - 1, 0); draw(); }
    else if (event.key === "Enter") { event.preventDefault(); go(active); }
  };
  list.onclick = event => { const item = event.target.closest(".palette-item"); if (item) go(Number(item.dataset.index)); };
  dialog.onclick = event => { if (event.target === dialog) dialog.close(); };
  draw();
  dialog.showModal();
  input.focus();
}

function openShortcuts() {
  const scheme = MOUSE_SCHEMES[prefs.mouse];
  const row=([key, text]) => `<div class="shortcut"><kbd>${escapeHtml(key)}</kbd><span>${escapeHtml(text)}</span></div>`;
  const group = (title, rows) => `<section><h3>${title}</h3>${rows.map(row).join("")}</section>`;
  $("#shortcutsBody").innerHTML = group(`Mouse (${escapeHtml(scheme.label)})`, [["Drag", scheme.summary], ["Wheel", "Zoom toward the cursor"], ["Click", "Select"], ["Ctrl+click", "Add to the selection"], ["Right-click", "Commands for what is under the cursor"]])
    + group("Find anything", [["Ctrl+P", "Search all commands"], ["Ctrl+K", "Find a parameter"], ["?", "This help"]])
    + group("Selection", [["L", "Look at"], ["Z", "Zoom to selection"], ["N", "Name the selection"], ["F2", "Rename"], ["H", "Hide body"], ["I", "Isolate body"], ["A", "Show all bodies"], ["Esc", "Clear selection"]])
    + group("Work", [["Ctrl+Enter", "Run simulation"], ["Ctrl+S", "Save"], ["Ctrl+Z / Ctrl+Y", "Undo / redo settings and parameters"], ["Ctrl+O", "Import CAD"], ["F", "Zoom to fit"]])
    + group("Windows", [["Ctrl+B", "Show or hide the left panel"], ["Ctrl+Shift+B", "Show or hide the right panel"], ["Ctrl + = / − / 0", "Interface size"], ["Drag a divider", "Resize a panel (double-click collapses)"], ["Drag a window title", "Move it (double-click puts it back)"]]);
  $("#shortcutsDialog").showModal();
}

function initStudioControls() {
  layout = createLayout($(".workbench"));
  layout.float($("#morphDock"), $("#morphDock .dock-head"), "morph");
  layout.float($("#legend"), $("#legend .legend-title"), "legend");
  layout.float($("#selectionHud"), $("#selectionHud"), "hud");
  layout.float($("#failureCard"), $("#failureCard .failure-head"), "failure");
  viewport.setMouseScheme?.(prefs.mouse, prefs.invertWheel);
  if (prefs.scale !== 1) document.documentElement.style.zoom = String(prefs.scale);
  $("#failureClose").addEventListener("click", hideFailure);
  $("#failureCopy").addEventListener("click", async () => {
    const text = `${$("#failureTitle").textContent}\n${$("#failureFix").textContent}\n\n${$("#failureDetails").textContent}`;
    try { await navigator.clipboard.writeText(text); notify("Details copied"); } catch { $("#failureMore").open = true; notify("Could not reach the clipboard: select the text instead"); }
  });
}

function initEntityTools() {
  $("#nameForm").addEventListener("submit", submitName);
  $("#viewportCanvas").closest(".viewport, main, section")?.addEventListener("contextmenu", event => { if (!event.target.closest("input, textarea, select")) event.preventDefault(); });
}

initEvents();
initEntityTools();
initViewport();
initStudioControls();
initViewportTools();
initComplianceTools();
initGuide({ onSkillChange: applySkillDefaults });
renderAll();
bootstrap().catch(error => notify(`Could not connect to Paramfabric: ${error.message}`, true));
