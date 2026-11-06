// which right-click commands apply to what was clicked. No DOM here: the items are plain data
// ({ id, label, args, shortcut, checked, disabled, danger, items } | { header } | { separator })

const SEP = { separator: true };

function join(...groups) {
  const out = [];
  for (const group of groups) {
    const items = group.filter(Boolean);
    if (!items.length) continue;
    if (out.length) out.push(SEP);
    out.push(...items);
  }
  return out;
}

// boundary conditions that make sense for the current physics, on faces
export function conditionItems(ctx, command = "bc", args = {}) {
  const types=ctx.analysis === "fea"
    ? [["fixed", "Fixed support"], ["force", "Force"], ["pressure", "Pressure"]]
    : [...(ctx.internal ? [["inlet", "Velocity inlet"], ["outlet", "Pressure outlet"]] : []), ...(ctx.heat ? [["heat", "Heat source"]] : [])];
  return types.map(([type, label]) => ({ id: command, label, args: { ...args, type } }));
}

function visibilityItems(target, ctx) {
  const hidden = ctx.hidden?.has(target.key);
  return [
    hidden ? { id: "show", label: "Show body", args: { key: target.key } } : { id: "hide", label: "Hide body", shortcut: "H", args: { key: target.key } },
    { id: "isolate", label: "Isolate body", shortcut: "I", args: { key: target.key } },
    ctx.hidden?.size ? { id: "showAll", label: "Show all bodies", shortcut: "A" } : null,
  ];
}

function probeItems(ctx) {
  if (!ctx.probe) return ctx.probeCount ? [{ id: "clearProbes", label: "Clear probes" }] : [];
  return [
    { id: "probe", label: `Probe value here (${ctx.probe.text})` },
    { id: "isoHere", label: "Iso-surface at this value", disabled: !ctx.hasVolume },
    { label: "Cut plane here", disabled: !ctx.hasCut, items: ["X", "Y", "Z"].map((axis, index) => ({ id: "cutHere", label: `Normal to ${axis}`, args: { axis: index } })) },
    ctx.probeCount ? { id: "clearProbes", label: "Clear probes" } : null,
  ];
}

function namedItems(kind, ctx, count) {
  const same = (ctx.named || []).filter(selection => selection.kind === kind);
  return [
    { id: "nameSelection", label: count > 1 ? `Create named selection from ${count} ${kind}s…` : "Create named selection…", shortcut: "N" },
    same.length ? { label: "Add to named selection", items: same.map(selection => ({ id: "addToNamed", label: selection.name, args: { selection: selection.id } })) } : null,
  ];
}

function faceMenu(target, ctx) {
  const count = target.count || 1;
  const conditions = conditionItems(ctx);
  return join(
    [{ header: target.title, sub: count > 1 ? `${count} faces selected` : target.subtitle }],
    [{ id: "lookAt", label: "Look at", shortcut: "L" }, { id: "zoomTo", label: "Zoom to selection", shortcut: "Z" }],
    [{ label: "Select", items: [
      { id: "selectBody", label: "Parent body" },
      { id: "selectFaces", label: "Coplanar faces", args: { mode: "coplanar" }, disabled: !target.normal },
      { id: "selectFaces", label: "Parallel faces", args: { mode: "parallel" }, disabled: !target.normal },
      { id: "selectFaces", label: `All ${target.surface || "matching"} faces`, args: { mode: "surface" } },
      { id: "selectFaces", label: "All faces of this body", args: { mode: "all" } },
    ] }, ...namedItems("face", ctx, count)],
    [
      conditions.length ? { label: "Insert boundary condition", items: conditions } : null,
      ctx.analysis === "fea" ? { id: "shell", label: "Assign 2D shell…" } : null,
      { id: "refine", label: "Refine mesh at this face" },
      ctx.exact && target.surface === "plane" ? { id: "pushPull", label: "Push / pull face" } : null,
    ],
    probeItems(ctx),
    visibilityItems(target, ctx),
    [{ id: "copyPoint", label: "Copy coordinates" }, { id: "properties", label: "Properties" }],
  );
}

function edgeMenu(target, ctx) {
  const count = target.count || 1;
  return join(
    [{ header: target.title, sub: count > 1 ? `${count} edges selected` : target.subtitle }],
    [{ id: "lookAt", label: "Look at", shortcut: "L" }, { id: "zoomTo", label: "Zoom to selection", shortcut: "Z" }],
    [{ id: "selectBody", label: "Select parent body" }, ...namedItems("edge", ctx, count)],
    [
      ctx.analysis === "fea" ? { id: "beam", label: "Assign 1D beam…" } : null,
      ctx.exact ? { id: "edgeFeature", label: "Fillet", args: { type: "fillet" } } : null,
      ctx.exact ? { id: "edgeFeature", label: "Chamfer", args: { type: "chamfer" } } : null,
    ],
    probeItems(ctx),
    visibilityItems(target, ctx),
    [{ id: "copyPoint", label: "Copy coordinates" }, { id: "properties", label: "Properties" }],
  );
}

function bodyMenu(target, ctx) {
  return join(
    [{ header: target.title, sub: target.subtitle }],
    [{ id: "zoomTo", label: "Zoom to body", shortcut: "Z" }],
    [
      { id: "selectFaces", label: "Select all faces", args: { mode: "all" } },
      ...namedItems("body", ctx, target.count || 1),
      { id: "rename", label: "Rename…", shortcut: "F2" },
    ],
    [{ label: "Insert feature", items: [
      { id: "bodyFeature", label: "Extrude…", args: { type: "extrude" } },
      { id: "bodyFeature", label: "Split…", args: { type: "split" } },
      { id: "bodyFeature", label: "Scale…", args: { type: "scale" } },
    ] }],
    visibilityItems(target, ctx),
    [{ id: "properties", label: "Properties" }],
  );
}

function namedMenu(target, ctx) {
  const conditions = target.selectionKind === "face" ? conditionItems(ctx, "namedBc", { selection: target.id }) : [];
  return join(
    [{ header: target.title, sub: target.subtitle }],
    [
      { id: "namedSelect", label: "Select", args: { selection: target.id } },
      { id: "namedLook", label: target.selectionKind === "body" ? "Zoom to" : "Look at", args: { selection: target.id } },
    ],
    [conditions.length ? { label: "Insert boundary condition", items: conditions } : null],
    [{ id: "namedRename", label: "Rename…", shortcut: "F2", args: { selection: target.id } }, { id: "namedDelete", label: "Delete", danger: true, args: { selection: target.id } }],
  );
}

function emptyMenu(ctx) {
  const views = [["iso", "Isometric"], ["front", "Front"], ["back", "Back"], ["top", "Top"], ["bottom", "Bottom"], ["right", "Right"], ["left", "Left"]];
  return join(
    [{ label: "View", items: views.map(([preset, label]) => ({ id: "view", label, args: { preset } })) },
      { id: "fit", label: "Zoom to fit", shortcut: "F" },
      { id: "previousView", label: "Previous view", disabled: !ctx.canGoBack }],
    [{ label: "Selection mode", items: [["auto", "Auto"], ["face", "Faces only"], ["edge", "Edges only"]].map(([mode, label]) => ({ id: "pickMode", label, args: { mode }, checked: ctx.pickMode === mode })) },
      { id: "selectBodies", label: "Select all bodies", disabled: !ctx.bodies },
      { id: "clearSelection", label: "Clear selection", disabled: !ctx.hasSelection }],
    [{ label: "Display", items: [["shaded", "Shaded"], ["edges", "Edges"], ["xray", "X-ray"]].map(([mode, label]) => ({ id: "display", label, args: { mode }, checked: ctx.display === mode })) },
      { id: "layer", label: "Mesh", args: { layer: "mesh" }, checked: ctx.layers?.mesh },
      ctx.hasResult ? { id: "layer", label: "Contour", args: { layer: "contour" }, checked: ctx.layers?.contour } : null,
      ctx.hasResult && ctx.analysis === "cfd" ? { id: "layer", label: "Streamlines", args: { layer: "streamlines" }, checked: ctx.layers?.streamlines } : null,
      ctx.hasResult && ctx.fields?.length > 1 ? { label: "Result field", items: ctx.fields.map(field => ({ id: "field", label: field.label, args: { field: field.id }, checked: field.id === ctx.activeField })) } : null,
      { id: "cut", label: "Cutting plane", checked: ctx.cutOn },
      ctx.hidden?.size ? { id: "showAll", label: `Show all bodies (${ctx.hidden.size} hidden)`, shortcut: "A" } : null],
    probeItems(ctx),
    [{ label: "Mouse controls", items: (ctx.mouseSchemes || []).map(([scheme, label]) => ({ id: "mouseScheme", label, args: { scheme }, checked: ctx.mouseScheme === scheme })) },
      { id: "palette", label: "Search commands…", shortcut: "Ctrl+P" },
      { id: "resetLayout", label: "Reset window layout" }],
    [{ id: "saveImage", label: "Save viewport image" }],
    [{ id: "importCad", label: "Import CAD…" }, ctx.bodies ? null : { id: "loadDemo", label: "Load demo part" }],
  );
}

export function menuFor(target, ctx) {
  switch (target.kind) {
    case "face": return faceMenu(target, ctx);
    case "edge": return edgeMenu(target, ctx);
    case "body": return bodyMenu(target, ctx);
    case "named": return namedMenu(target, ctx);
    case "model":
      return join([{ header: target.title, sub: target.subtitle }],
        [{ id: "zoomModel", label: "Zoom to model" }, { id: "rename", label: "Rename…", shortcut: "F2" }, { id: "properties", label: "Properties" }],
        [{ id: "removeModel", label: "Remove from workspace", danger: true }]);
    case "feature":
      return join([{ header: target.title, sub: target.subtitle }],
        [{ id: "properties", label: "Properties" }, { id: "rename", label: "Rename…", shortcut: "F2" }],
        [{ id: "deleteFeature", label: "Delete feature", danger: true }]);
    case "parameter":
      return join([{ header: target.title, sub: target.subtitle }],
        [{ id: "properties", label: "Edit" }, { id: "copyName", label: "Copy name" }, { id: "copyValue", label: "Copy value with unit" }]);
    case "mesh":
      return join([{ header: "Volume mesh", sub: target.subtitle }],
        [{ id: "layer", label: "Show mesh", args: { layer: "mesh" }, checked: ctx.layers?.mesh }, { id: "meshWorst", label: "Go to worst cell" }, { id: "goStep", label: "Open Mesh & Solve", args: { step: 3 } }]);
    case "results":
      return join([{ header: "Results", sub: target.subtitle }],
        [{ id: "goStep", label: "Open results", args: { step: 4 } }, { id: "layer", label: "Contour", args: { layer: "contour" }, checked: ctx.layers?.contour }],
        [ctx.probeCount ? { id: "clearProbes", label: "Clear probes" } : null]);
    default: return emptyMenu(ctx);
  }
}
