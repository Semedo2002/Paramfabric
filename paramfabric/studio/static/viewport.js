import * as THREE from "/studio-static/three.module.min.js";
import { mouseAction } from "/studio-static/controls.js";
import { cellGeometry, cellsCrossing, patchRowId, qualityColor } from "/studio-static/meshview.js";

// Sequential (one hue, dark -> light on the dark surface) and diverging
// (blue ↔ red with a neutral gray midpoint) ramps from the validated palette
const SEQUENTIAL = ["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"];
const DIVERGING = ["#9ec5f4", "#3987e5", "#1c5cab", "#4a4a46", "#b23a3a", "#e34948", "#f3a6a6"];
// glyphs use a warm single-hue ramp so they stand out on the blue contour ramps
const GLYPH = ["#7a2a0c", "#b5451b", "#e0632e", "#f2905e", "#fbc3a1"];
const BODY_COLOR = 0x8fa3b8;
const SPLIT_COLOR = 0xa9b9a1;
const EXTRUDE_COLOR = 0x6da7ec;
const SELECT_COLOR = 0xfab219;

function rampColor(stops, t) {
  const x = Math.min(1, Math.max(0, t)) * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(x));
  return new THREE.Color(stops[i]).lerp(new THREE.Color(stops[i + 1]), x - i);
}

export function colorFor(kind, value, min, max) {
  if (kind === "diverging") {
    const bound = Math.max(Math.abs(min), Math.abs(max)) || 1;
    return rampColor(DIVERGING, 0.5 + value / (2 * bound));
  }
  return rampColor(SEQUENTIAL, max > min ? (value - min) / (max - min) : 0.5);
}

export function paintLegend(canvas, kind) {
  const context = canvas.getContext("2d");
  const stops = kind === "diverging" ? DIVERGING : SEQUENTIAL;
  const gradient = context.createLinearGradient(0, 0, canvas.width, 0);
  stops.forEach((stop, index) => gradient.addColorStop(index / (stops.length - 1), stop));
  context.fillStyle = gradient;
  context.fillRect(0, 0, canvas.width, canvas.height);
}

function disposeTree(object) {
  object.traverse(child => {
    child.geometry?.dispose?.();
    const materials = Array.isArray(child.material) ? child.material : [child.material];
    materials.forEach(material => material?.dispose?.());
  });
}

function edgeSegments(polylines) {
  const positions=[];
  for (const line of polylines) {
    for (let i = 0; i + 5 < line.length; i += 3) {
      positions.push(line[i], line[i + 1], line[i + 2], line[i + 3], line[i + 4], line[i + 5]);
    }
  }
  return positions;
}

function boxEdges(low, high) {
  const [x0, y0, z0] = low;
  const [x1, y1, z1] = high;
  const c = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]];
  const pairs = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
  return pairs.flatMap(([a, b]) => [...c[a], ...c[b]]);
}

function arrowGeometry() {
  // unit arrow along +Y from the origin: shaft (0 -> 0.72) and head (0.72 -> 1)
  const shaft = new THREE.CylinderGeometry(0.05, 0.05, 0.68, 6, 1, true).translate(0, 0.34, 0).toNonIndexed();
  const head = new THREE.ConeGeometry(0.15, 0.32, 8, 1).translate(0, 0.84, 0).toNonIndexed();
  const merged = new THREE.BufferGeometry();
  for (const name of ["position", "normal"]) {
    const a = shaft.getAttribute(name).array; const b = head.getAttribute(name).array;
    const data = new Float32Array(a.length + b.length);
    data.set(a); data.set(b, a.length);
    merged.setAttribute(name, new THREE.BufferAttribute(data, 3));
  }
  shaft.dispose(); head.dispose();
  return merged;
}

function prepareVolume(volume) {
  if (!volume || !volume.positions?.length) return null;
  const scalars = {}; const vectors = {};
  for (const [name, values] of Object.entries(volume.scalars || {})) scalars[name] = Float32Array.from(values);
  for (const [name, values] of Object.entries(volume.vectors || {})) vectors[name] = Float32Array.from(values);
  const positions = Float32Array.from(volume.positions);
  const low=[Infinity, Infinity, Infinity]; const high = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < positions.length; i += 3) {
    for (let k = 0; k < 3; k += 1) {
      if (positions[i + k] < low[k]) low[k] = positions[i + k];
      if (positions[i + k] > high[k]) high[k] = positions[i + k];
    }
  }
  return { positions, tets: Uint32Array.from(volume.tets), cells: volume.cells, scalars, vectors, low, high };
}

// Edges of a tetrahedron, and the cut polygon for each sign pattern (marching tetrahedra)
const TET_EDGES = [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]];

// marching tetrahedra on a signed distance per node: one triangle for a 1|3
// sign split, a quad for a 2|2 split. For 2|2 the four cut edges (a,c) (a,d)
// (b,d) (b,c), with {a,b} above and {c,d} below, are already in cyclic order
function cutTetrahedra(volume, distance, field, vectorField) {
  const { positions, tets, scalars, vectors } = volume;
  const values = field ? scalars[field] : null;
  const vec = vectorField ? vectors[vectorField] : null;
  const outPositions = []; const outValues = []; const outVectors = []; const outlines = [];
  const corner = [0, 0, 0, 0];
  const point=(na, nb) => {
    const da = distance[na]; const w = da / (da - distance[nb]);
    return {
      x: positions[3 * na] + w * (positions[3 * nb] - positions[3 * na]),
      y: positions[3 * na + 1] + w * (positions[3 * nb + 1] - positions[3 * na + 1]),
      z: positions[3 * na + 2] + w * (positions[3 * nb + 2] - positions[3 * na + 2]),
      v: values ? values[na] + w * (values[nb] - values[na]) : 0,
      u: vec ? [0, 1, 2].map(k => vec[3 * na + k] + w * (vec[3 * nb + k] - vec[3 * na + k])) : null,
    };
  };
  for (let t = 0; t < tets.length; t += 4) {
    const above=[]; const below = [];
    for (let k = 0; k < 4; k += 1) { corner[k] = tets[t + k]; (distance[corner[k]] > 0 ? above : below).push(corner[k]); }
    if (!above.length || !below.length) continue;
    let points;
    if (above.length === 1) points = below.map(n => point(above[0], n));
    else if (below.length === 1) points = above.map(n => point(n, below[0]));
    else {
      const [a, b] = above; const [c, d] = below;
      points = [point(a, c), point(a, d), point(b, d), point(b, c)];
    }
    const triangles = points.length === 3 ? [[0, 1, 2]] : [[0, 1, 2], [0, 2, 3]];
    for (const triangle of triangles) {
      for (const k of triangle) {
        const p = points[k];
        outPositions.push(p.x, p.y, p.z);
        outValues.push(p.v);
        if (p.u) outVectors.push(...p.u);
      }
    }
    for (let k = 0; k < points.length; k += 1) {
      const p = points[k]; const q = points[(k + 1) % points.length];
      outlines.push(p.x, p.y, p.z, q.x, q.y, q.z);
    }
  }
  return { positions: outPositions, values: outValues, vectors: outVectors, outlines };
}

export function sliceVolume(volume, axis, position, field, vectorField) {
  const { positions } = volume;
  const distance = new Float32Array(positions.length / 3);
  for (let i = 0; i < distance.length; i += 1) distance[i] = positions[3 * i + axis] - position;
  return cutTetrahedra(volume, distance, field, vectorField);
}

// Iso-surface `isoField = level` (marching tetrahedra), colored by `colorField`
export function isoSurface(volume, isoField, level, colorField = null) {
  const values = volume.scalars[isoField];
  const distance = new Float32Array(values.length);
  for (let i = 0; i < values.length; i += 1) distance[i] = values[i] - level;
  return cutTetrahedra(volume, distance, colorField || isoField, null);
}

// point location and particle tracing
function buildLocator(volume) {
  const { positions, tets, low, high } = volume;
  const count = tets.length / 4;
  const cells = Math.max(4, Math.round(Math.cbrt(count / 2)));
  const size = [0, 1, 2].map(k => (high[k] - low[k]) / cells || 1);
  const grid = new Map();
  for (let t = 0; t < count; t += 1) {
    const lo = [Infinity, Infinity, Infinity]; const hi = [-Infinity, -Infinity, -Infinity];
    for (let c = 0; c < 4; c += 1) {
      const n = tets[4 * t + c];
      for (let k = 0; k < 3; k += 1) { lo[k] = Math.min(lo[k], positions[3 * n + k]); hi[k] = Math.max(hi[k], positions[3 * n + k]); }
    }
    const a = lo.map((v, k) => Math.max(0, Math.floor((v - low[k]) / size[k])));
    const b = hi.map((v, k) => Math.min(cells - 1, Math.floor((v - low[k]) / size[k])));
    for (let i = a[0]; i <= b[0]; i += 1) for (let j = a[1]; j <= b[1]; j += 1) for (let l = a[2]; l <= b[2]; l += 1) {
      const key = (i * cells + j) * cells + l;
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push(t);
    }
  }
  return { grid, cells, size, low };
}

function barycentric(positions, tets, t, x, y, z) {
  const p = k => tets[4 * t + k] * 3;
  const [a, b, c, d] = [p(0), p(1), p(2), p(3)];
  const v=(i, k) => positions[i + k];
  const m = [[v(b, 0) - v(a, 0), v(c, 0) - v(a, 0), v(d, 0) - v(a, 0)], [v(b, 1) - v(a, 1), v(c, 1) - v(a, 1), v(d, 1) - v(a, 1)], [v(b, 2) - v(a, 2), v(c, 2) - v(a, 2), v(d, 2) - v(a, 2)]];
  const r = [x - v(a, 0), y - v(a, 1), z - v(a, 2)];
  const det = m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
  if (Math.abs(det) < 1e-300) return null;
  const solve = col => {
    const mm = m.map((row, i) => row.map((value, j) => (j === col ? r[i] : value)));
    return (mm[0][0] * (mm[1][1] * mm[2][2] - mm[1][2] * mm[2][1]) - mm[0][1] * (mm[1][0] * mm[2][2] - mm[1][2] * mm[2][0]) + mm[0][2] * (mm[1][0] * mm[2][1] - mm[1][1] * mm[2][0])) / det;
  };
  const l1 = solve(0); const l2 = solve(1); const l3 = solve(2);
  const w = [1 - l1 - l2 - l3, l1, l2, l3];
  return w.every(value => value >= -1e-6) ? w : null;
}

export function sampleVector(volume, field, x, y, z, hint = -1) {
  const { positions, tets } = volume;
  const locator = volume.locator || (volume.locator = buildLocator(volume));
  const vec = volume.vectors[field];
  const tryTet = t => {
    const w = barycentric(positions, tets, t, x, y, z);
    if (!w) return null;
    const out = [0, 0, 0];
    for (let c = 0; c < 4; c += 1) for (let k = 0; k < 3; k += 1) out[k] += w[c] * vec[3 * tets[4 * t + c] + k];
    return { value: out, tet: t };
  };
  if (hint >= 0) { const hit = tryTet(hint); if (hit) return hit; }
  const index = [x, y, z].map((v, k) => Math.floor((v - locator.low[k]) / locator.size[k]));
  if (index.some(i => i < 0 || i >= locator.cells)) return null;
  for (const t of locator.grid.get((index[0] * locator.cells + index[1]) * locator.cells + index[2]) || []) {
    const hit = tryTet(t);
    if (hit) return hit;
  }
  return null;
}

// RK2 streamlines through the tetrahedral velocity field; each line is [x, y, z, |U|, ...]
export function traceStreamlines(volume, field, seeds, { step, maxSteps = 600 } = {}) {
  const lines = [];
  for (let s = 0; s < seeds.length; s += 3) {
    let x = seeds[s], y = seeds[s + 1], z = seeds[s + 2];
    let hint = -1;
    const line = [];
    for (let i = 0; i < maxSteps; i += 1) {
      const first = sampleVector(volume, field, x, y, z, hint);
      if (!first) break;
      hint = first.tet;
      const speed = Math.hypot(...first.value);
      line.push(x, y, z, speed);
      if (speed < 1e-9) break;
      const dt = step / speed;
      const mid=sampleVector(volume, field, x + 0.5 * dt * first.value[0], y + 0.5 * dt * first.value[1], z + 0.5 * dt * first.value[2], hint);
      if (!mid) break;
      x += dt * mid.value[0]; y += dt * mid.value[1]; z += dt * mid.value[2];
    }
    if (line.length >= 8) lines.push(line);
  }
  return lines;
}

// Camera angles (theta, phi of the orbit) that look at a surface with outward normal `n` head on
export function viewAngles(n) {
  const length=Math.hypot(n[0], n[1], n[2]) || 1;
  const z = Math.max(-1, Math.min(1, n[2] / length));
  const phi=Math.min(Math.PI - 1e-3, Math.max(1e-3, Math.acos(z)));
  const flat = Math.abs(n[0]) + Math.abs(n[1]) < 1e-9 * length;
  return { theta: flat ? null : Math.atan2(n[1], n[0]), phi };
}

export function createViewport(canvas, { onSelect = () => {}, onPointer = () => {}, onContext = null } = {}) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, preserveDrawingBuffer: false });
  } catch (error) {
    return { available: false, error };
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.localClippingEnabled = true;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.001, 1000);
  camera.up.set(0, 0, 1);
  scene.add(new THREE.HemisphereLight(0xdfe9f5, 0x1a2230, 1.25));
  const key = new THREE.DirectionalLight(0xffffff, 1.7);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x9ec5f4, 0.5);
  scene.add(fill);

  const layers={
    grid: new THREE.Group(), bodies: new THREE.Group(), domain: new THREE.Group(),
    mesh: new THREE.Group(), results: new THREE.Group(), highlight: new THREE.Group(),
    section: new THREE.Group(), glyphs: new THREE.Group(), iso: new THREE.Group(), traces: new THREE.Group(),
  };
  Object.values(layers).forEach(group => scene.add(group));

  const state = {
    target: new THREE.Vector3(), radius: 1, theta: -Math.PI / 3.2, phi: Math.PI / 2.9, distance: 3,
    models: new Map(), sceneData: null, bodyObjects: new Map(), display: "shaded",
    selection: null, needsRender: true, visible: { mesh: false, streamlines: true, contour: true },
    resultRanges: null, field: null, deformation: 1, resultData: null,
    pickMode: "auto", meshVolume: null, resultVolume: null, glyphs: true, sectionPending: false,
    clip: { enabled: false, axis: 0, fraction: 0.5, flip: false },
    hidden: new Set(), extras: [], history: [], probes: [], mouse: "paramfabric", invertWheel: false, meshDisplay: null,
    meshView: { style: "shaded", hidden: new Set(), colors: {}, highlight: null, quality: null },
    iso: { enabled: false, field: null, fraction: 0.5, color: null },
    traces: { enabled: true, seeds: 40, animate: true, lines: [], particles: null, phase: 0 },
  };
  const clipPlane=new THREE.Plane(new THREE.Vector3(-1, 0, 0), 0);
  const arrow = arrowGeometry();
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();

  function requestRender() { state.needsRender = true; }

  function updateCamera() {
    const sinPhi = Math.sin(state.phi);
    const offset = new THREE.Vector3(
      state.distance * sinPhi * Math.cos(state.theta),
      state.distance * sinPhi * Math.sin(state.theta),
      state.distance * Math.cos(state.phi),
    );
    camera.position.copy(state.target).add(offset);
    camera.near = Math.max(state.distance / 1000, 1e-5);
    camera.far = state.distance * 100 + state.radius * 20;
    camera.lookAt(state.target);
    camera.updateProjectionMatrix();
    key.position.copy(camera.position).add(new THREE.Vector3(state.radius, -state.radius, state.radius * 2));
    fill.position.copy(state.target).sub(offset);
    updateProbes();
    updateTriad();
    requestRender();
  }

  function contentBox() {
    const box = new THREE.Box3();
    for (const group of [layers.bodies, layers.domain, layers.results]) {
      if (group.children.length) box.expandByObject(group);
    }
    return box;
  }

  function fit() {
    const box = contentBox();
    if (box.isEmpty()) {
      state.target.set(0, 0, 0);
      state.radius = 1;
    } else {
      const sphere = box.getBoundingSphere(new THREE.Sphere());
      state.target.copy(sphere.center);
      state.radius = Math.max(sphere.radius, 1e-4);
    }
    state.distance = state.radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.08;
    rebuildGrid();
    updateCamera();
  }

  function view(preset) {
    const presets={
      iso: [-Math.PI / 3.2, Math.PI / 2.9], front: [-Math.PI / 2, Math.PI / 2 - 1e-3],
      top: [-Math.PI / 2, 1e-3], right: [0, Math.PI / 2 - 1e-3],
      back: [Math.PI / 2, Math.PI / 2 - 1e-3], bottom: [-Math.PI / 2, Math.PI - 1e-3], left: [Math.PI, Math.PI / 2 - 1e-3],
    };
    rememberView();
    if (preset === "fit") return fit();
    [state.theta, state.phi] = presets[preset] || presets.iso;
    fit();
  }

  // look at, zoom to, previous view
  function currentView() {
    return { target: state.target.clone(), distance: state.distance, theta: state.theta, phi: state.phi };
  }

  function rememberView() {
    state.history.push(currentView());
    if (state.history.length > 20) state.history.shift();
  }

  let cameraAnimation = 0;
  function moveCamera(to) {
    const from = currentView();
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const turn = Math.atan2(Math.sin(to.theta - from.theta), Math.cos(to.theta - from.theta)); // the short way round
    const apply=t => {
      state.target.lerpVectors(from.target, to.target, t);
      state.distance = from.distance + (to.distance - from.distance) * t;
      state.theta = from.theta + turn * t;
      state.phi = from.phi + (to.phi - from.phi) * t;
      updateCamera();
    };
    cancelAnimationFrame(cameraAnimation);
    if (reduce) { apply(1); return; }
    const start = performance.now();
    const step = now => {
      const k = Math.min(1, (now - start) / 280);
      apply(k * k * (3 - 2 * k));
      if (k < 1) cameraAnimation = requestAnimationFrame(step);
    };
    cameraAnimation = requestAnimationFrame(step);
  }

  function bodyMeshes(key) {
    const object = state.bodyObjects.get(key);
    if (!object) return {};
    object.updateMatrixWorld(true);
    let faces = null;
    object.traverse(child => { if (child.userData.kind === "faces") faces = child; });
    return { object, faces };
  }

  // World-space points of an entity: { key } (whole body), { key, faceId }, or { key, points } (edge polyline)
  function entityPoints({ key, faceId, points }) {
    const { object, faces } = bodyMeshes(key);
    if (!object) return [];
    const out = [];
    const v = new THREE.Vector3();
    if (points) {
      for (let i = 0; i < points.length; i += 3) out.push(v.set(points[i], points[i + 1], points[i + 2]).applyMatrix4(object.matrixWorld).clone());
    } else if (faces && faceId !== undefined) {
      const source = faces.geometry.attributes.position;
      faces.userData.faceIds.forEach((id, triangle) => {
        if (id !== faceId) return;
        for (let k = 0; k < 3; k += 1) out.push(v.fromBufferAttribute(source, 3 * triangle + k).applyMatrix4(faces.matrixWorld).clone());
      });
    } else {
      const box = new THREE.Box3().setFromObject(object);
      if (!box.isEmpty()) out.push(box.min.clone(), box.max.clone());
    }
    return out;
  }

  function sphereOf(entities) {
    const box = new THREE.Box3();
    for (const entity of entities) entity.world ? box.expandByPoint(new THREE.Vector3(...entity.world)) : entityPoints(entity).forEach(p => box.expandByPoint(p));
    return box.isEmpty() ? null : box.getBoundingSphere(new THREE.Sphere());
  }

  function fitDistance(radius) {
    return Math.max(radius, state.radius * 0.03) / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.25;
  }

  function zoomTo(entities) {
    const sphere = sphereOf(entities);
    if (!sphere) return false;
    rememberView();
    moveCamera({ target: sphere.center, distance: fitDistance(sphere.radius), theta: state.theta, phi: state.phi });
    return true;
  }

  // Head-on view of a face (normal) or square-on view of an edge (from where the camera already is)
  function lookAt(entities, normal = null) {
    const sphere = sphereOf(entities);
    if (!sphere) return false;
    let direction = normal;
    if (!direction) {
      const points = entities[0].points ? entityPoints(entities[0]) : [];
      const eye = camera.position.clone().sub(sphere.center).normalize();
      if (points.length >= 2) {
        const tangent = points.at(-1).clone().sub(points[0]).normalize();
        eye.addScaledVector(tangent, -eye.dot(tangent));
        if (eye.lengthSq() < 1e-6) eye.set(0, 0, 1);
      }
      direction = eye.normalize().toArray();
    }
    const angles = viewAngles(direction);
    rememberView();
    // straight along Z the orbit angle is free: square the part up with the screen
    const square = Math.round(state.theta / (Math.PI / 2)) * (Math.PI / 2);
    moveCamera({ target: sphere.center, distance: fitDistance(sphere.radius), theta: angles.theta ?? square, phi: angles.phi });
    return true;
  }

  function previousView() {
    const view=state.history.pop();
    if (view) moveCamera(view);
    return Boolean(view);
  }

  // visibility
  function setHidden(keys) {
    state.hidden = new Set(keys);
    for (const [key, object] of state.bodyObjects) object.visible = !state.hidden.has(key);
    requestRender();
  }

  // probes and image
  function resultAt(event) {
    const legend = state.resultRanges;
    if (!legend) return null;
    setRay(event);
    const targets = [];
    layers.results.children.forEach(child => { if (child.isMesh && child.visible && child.userData.values) targets.push(child); });
    layers.section.children.forEach(child => { if (child.isMesh && child.userData.values) targets.push(child); });
    const hits = raycaster.intersectObjects(targets, false).filter(hit => {
      const planes = hit.object.material.clippingPlanes;
      return !planes || planes.every(plane => plane.distanceToPoint(hit.point) >= 0);
    });
    const hit = hits[0];
    if (!hit) return null;
    const { a, b, c } = hit.face;
    const position = hit.object.geometry.attributes.position;
    const weights = new THREE.Triangle(
      new THREE.Vector3().fromBufferAttribute(position, a), new THREE.Vector3().fromBufferAttribute(position, b), new THREE.Vector3().fromBufferAttribute(position, c),
    ).getBarycoord(hit.object.worldToLocal(hit.point.clone()), new THREE.Vector3());
    const values = hit.object.userData.values;
    return { point: hit.point.toArray(), value: weights.x * values[a] + weights.y * values[b] + weights.z * values[c], field: hit.object.userData.field || legend.field, unit: legend.unit };
  }

  const probeHost = document.createElement("div");
  probeHost.className = "vp-probes";
  canvas.parentElement?.append(probeHost);

  function updateProbes() {
    const { clientWidth: w, clientHeight: h } = canvas;
    const v = new THREE.Vector3();
    for (const probe of state.probes) {
      v.set(...probe.point).project(camera);
      const visible = v.z < 1 && Math.abs(v.x) < 1 && Math.abs(v.y) < 1;
      probe.node.hidden = !visible;
      if (visible) probe.node.style.transform = `translate(${(v.x + 1) / 2 * w}px, ${(1 - v.y) / 2 * h}px)`;
    }
  }

  function addProbe(point, text) {
    const node = document.createElement("div");
    node.className = "vp-probe";
    node.textContent = text;
    probeHost.append(node);
    state.probes.push({ point, node });
    updateProbes();
    return state.probes.length;
  }

  function clearProbes() {
    state.probes.forEach(probe => probe.node.remove());
    state.probes = [];
  }

  function snapshot() {
    renderer.render(scene, camera); // the drawing buffer is only valid straight after a render
    const out = document.createElement("canvas");
    out.width = canvas.width; out.height = canvas.height;
    const context = out.getContext("2d");
    context.fillStyle = "#0b1119";
    context.fillRect(0, 0, out.width, out.height);
    context.drawImage(canvas, 0, 0);
    return out.toDataURL("image/png");
  }

  function edgeAt(key, point) {
    const { object } = bodyMeshes(key);
    if (!object) return null;
    let edges = null;
    object.traverse(child => { if (child.userData.kind === "edges") edges = child; });
    if (!edges) return null;
    const target = new THREE.Vector3(...point);
    const a = new THREE.Vector3(); const b = new THREE.Vector3(); const closest = new THREE.Vector3();
    let best = null; let bestDistance = Infinity;
    for (const polyline of edges.userData.polylines) {
      for (let i = 0; i + 5 < polyline.length; i += 3) {
        a.set(polyline[i], polyline[i + 1], polyline[i + 2]).applyMatrix4(object.matrixWorld);
        b.set(polyline[i + 3], polyline[i + 4], polyline[i + 5]).applyMatrix4(object.matrixWorld);
        new THREE.Line3(a, b).closestPointToPoint(target, true, closest);
        const distance=closest.distanceTo(target);
        if (distance < bestDistance) { bestDistance = distance; best = polyline; }
      }
    }
    return best;
  }

  function rebuildGrid() {
    disposeTree(layers.grid);
    layers.grid.clear();
    const box = contentBox();
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const span=Math.max(size.x, size.y) * 1.6 || 1;
    const step = Math.pow(10, Math.floor(Math.log10(span / 10)));
    const divisions = Math.min(200, Math.ceil(span / step));
    const grid = new THREE.GridHelper(divisions * step, divisions, 0x2b3644, 0x1a222d);
    grid.rotation.x = Math.PI / 2;
    const center = box.getCenter(new THREE.Vector3());
    grid.position.set(center.x, center.y, box.min.z - size.z * 0.002);
    grid.material.transparent = true;
    grid.material.opacity = 0.7;
    layers.grid.add(grid);
  }

  // bodies
  function bodyMaterial(color, entry) {
    const xray = state.display === "xray";
    return new THREE.MeshStandardMaterial({
      color, metalness: 0.15, roughness: 0.55, side: THREE.DoubleSide,
      transparent: xray || entry.role === "tool", opacity: entry.role === "tool" ? 0.25 : (xray ? 0.28 : 1),
      depthWrite: !(xray || entry.role === "tool"),
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
    });
  }

  function buildBody(entry) {
    const [modelId, bodyId] = entry.key.split("/");
    const model = state.models.get(modelId);
    const stored = model?.bodies.find(item => item.id === bodyId);
    const group = new THREE.Group();
    group.userData = { key: entry.key, kind: "body" };
    const pivot = new THREE.Group();
    group.add(pivot);
    if (!stored) return group;
    // Exact (OpenCASCADE-regenerated) bodies carry their own tessellation
    const body = entry.tessellation ? { ...stored, ...entry.tessellation } : stored;
    const anchor=entry.anchor;
    const baseColor = stored.color ? new THREE.Color(...stored.color).getHex() : BODY_COLOR;
    const showFaces = state.display !== "edges" || !body.edges.length;
    if (body.triangles.length && showFaces) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(body.triangles, 3));
      geometry.translate(-anchor[0], -anchor[1], -anchor[2]);
      geometry.computeVertexNormals();
      const planes = [];
      const copies = entry.split ? 2 : 1;
      for (let copy = 0; copy < copies; copy += 1) {
        const material = bodyMaterial(copy === 1 ? SPLIT_COLOR : baseColor, entry);
        if (entry.split) {
          const normal = new THREE.Vector3();
          normal.setComponent(entry.split.axis, copy === 0 ? -1 : 1);
          const plane=new THREE.Plane(normal, copy === 0 ? entry.split.position : -entry.split.position);
          material.clippingPlanes = [plane];
          planes.push(plane);
        }
        const mesh = new THREE.Mesh(geometry, material);
        mesh.userData = { key: entry.key, kind: "faces", faceIds: body.face_ids };
        pivot.add(mesh);
      }
    }
    if (body.edges.length) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(edgeSegments(body.edges), 3));
      geometry.translate(-anchor[0], -anchor[1], -anchor[2]);
      const lines = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({
        color: body.kind === "wireframe" ? 0x9ec5f4 : 0x1b2330, transparent: true, opacity: body.kind === "wireframe" ? 1 : 0.9,
      }));
      lines.userData = { key: entry.key, kind: "edges", polylines: body.edges };
      pivot.add(lines);
    }
    if (!body.triangles.length) {
      const box = new THREE.LineSegments(
        new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(boxEdges(body.bbox_min, body.bbox_max), 3)).translate(-anchor[0], -anchor[1], -anchor[2]),
        new THREE.LineBasicMaterial({ color: 0x6da7ec }),
      );
      pivot.add(box);
    }
    group.position.set(...anchor);
    group.scale.set(...entry.scale);
    return group;
  }

  function buildDerived(entry) {
    const size = entry.bbox_max.map((v, i) => v - entry.bbox_min[i]);
    const geometry = new THREE.BoxGeometry(...size);
    const mesh = new THREE.Mesh(geometry, bodyMaterial(EXTRUDE_COLOR, entry));
    mesh.position.set(...entry.bbox_min.map((v, i) => v + size[i] / 2));
    mesh.userData = { key: entry.key, kind: "derived" };
    const edges=new THREE.LineSegments(new THREE.EdgesGeometry(geometry), new THREE.LineBasicMaterial({ color: 0x1b2330 }));
    mesh.add(edges);
    return mesh;
  }

  function setScene(sceneData) {
    state.sceneData = sceneData;
    if (!sceneData) {
      disposeTree(layers.bodies); layers.bodies.clear();
      disposeTree(layers.domain); layers.domain.clear();
      state.bodyObjects.clear();
      requestRender();
      return;
    }
    const wanted = new Set(sceneData.bodies.map(entry => entry.key));
    for (const [key, object] of state.bodyObjects) {
      if (!wanted.has(key) || object.userData.signature !== signature(sceneData.bodies.find(e => e.key === key))) {
        disposeTree(object);
        layers.bodies.remove(object);
        state.bodyObjects.delete(key);
      }
    }
    for (const entry of sceneData.bodies) {
      let object = state.bodyObjects.get(entry.key);
      if (!object) {
        object = buildBody(entry);
        object.userData.signature = signature(entry);
        object.visible = !state.hidden.has(entry.key);
        layers.bodies.add(object);
        state.bodyObjects.set(entry.key, object);
      } else {
        object.position.set(...entry.anchor);
        object.scale.set(...entry.scale);
      }
    }
    layers.bodies.children.filter(child => child.userData.kind === "derived").forEach(child => { disposeTree(child); layers.bodies.remove(child); });
    sceneData.derived.forEach(entry => layers.bodies.add(buildDerived(entry)));
    disposeTree(layers.domain);
    layers.domain.clear();
    if (sceneData.flow_domain) {
      const { bbox_min: low, bbox_max: high } = sceneData.flow_domain;
      const lines = new THREE.LineSegments(
        new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(boxEdges(low, high), 3)),
        new THREE.LineBasicMaterial({ color: 0x3987e5, transparent: true, opacity: 0.75 }),
      );
      lines.userData = { kind: "domain" };
      layers.domain.add(lines);
      const length=(high[0] - low[0]) * 0.18;
      const arrow = new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0),
        new THREE.Vector3(low[0], (low[1] + high[1]) / 2, (low[2] + high[2]) / 2), length, 0x6da7ec, length * 0.25, length * 0.12);
      layers.domain.add(arrow);
    }
    refreshHighlight();
    applyClip();
    requestRender();
  }

  function signature(entry) {
    return entry ? `${state.display}|${entry.role}|${JSON.stringify(entry.split)}|${entry.anchor.join(",")}|${entry.revision || ""}` : "";
  }

  function setModels(models) {
    state.models = models;
    for (const object of state.bodyObjects.values()) { disposeTree(object); layers.bodies.remove(object); }
    state.bodyObjects.clear();
    if (state.sceneData) setScene(state.sceneData);
  }

  function setDisplay(mode) {
    state.display = mode;
    for (const object of state.bodyObjects.values()) { disposeTree(object); layers.bodies.remove(object); }
    state.bodyObjects.clear();
    if (state.sceneData) setScene(state.sceneData);
  }

  // mesh
  // mesh viewer
  function buildMeshSurface() {
    disposeTree(layers.mesh);
    layers.mesh.clear();
    const display = state.meshDisplay;
    if (!display || display.kind !== "surface") return;
    const { style, hidden, colors, highlight } = state.meshView;
    // over a result contour the mesh is drawn as lines only, so the colours stay visible
    const overResult = Boolean(state.resultRanges && state.visible.contour);
    const patches = display.patches?.length ? display.patches : [{ name: "mesh", kind: "wall", positions: display.positions, indices: display.indices }];
    for (const patch of patches) {
      const id = patchRowId(patch);
      if (hidden.has(id)) continue;
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(patch.positions, 3));
      geometry.setIndex(patch.indices);
      geometry.computeVertexNormals();
      const color = new THREE.Color(colors[id] || "#a9b8c8");
      if (!overResult && style !== "wire") {
        const xray = style === "xray";
        layers.mesh.add(new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
          color, flatShading: true, roughness: 0.85, side: THREE.DoubleSide, transparent: xray, opacity: xray ? 0.12 : 1, depthWrite: !xray,
          polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
        })));
      }
      const hiddenLine=!overResult && style === "shaded";
      layers.mesh.add(new THREE.LineSegments(new THREE.WireframeGeometry(geometry), new THREE.LineBasicMaterial({
        color: hiddenLine ? 0x10161f : overResult ? 0x0a0e14 : color, transparent: true, opacity: hiddenLine ? 0.75 : overResult ? 0.35 : 0.8,
      })));
    }
    if (highlight?.length && state.meshVolume) {
      const { triangles, edges } = cellGeometry(state.meshVolume, highlight);
      const fill = new THREE.Mesh(
        new THREE.BufferGeometry().setAttribute("position", new THREE.BufferAttribute(triangles, 3)),
        new THREE.MeshBasicMaterial({ color: 0xff3b30, transparent: true, opacity: 0.55, side: THREE.DoubleSide, depthTest: false }),
      );
      fill.renderOrder = 8;
      fill.userData.kind = "mesh-highlight";
      const lines = new THREE.LineSegments(
        new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(edges, 3)),
        new THREE.LineBasicMaterial({ color: 0xffd0cc, depthTest: false }),
      );
      lines.renderOrder = 9;
      layers.mesh.add(fill, lines);
    }
    layers.mesh.visible = state.visible.mesh;
  }

  function syncBodies() {
    const resultCovers = Boolean(state.resultRanges && state.visible.contour && !state.resultData?.lines?.length);
    const meshCovers = state.visible.mesh && state.meshDisplay?.kind === "surface" && state.meshView.style === "shaded";
    layers.bodies.visible = !resultCovers && !meshCovers;
  }

  function setMeshView(options) {
    state.meshView = { ...state.meshView, ...options };
    buildMeshSurface();
    syncBodies();
    applyClip();
    requestRender();
    return meshViewInfo();
  }

  function meshViewInfo() {
    let filled = 0; let lines = 0; let highlighted = 0;
    layers.mesh.children.forEach(child => {
      if (child.userData.kind === "mesh-highlight") highlighted = child.geometry.attributes.position.count / 12;
      else if (child.isLineSegments) lines += 1;
      else if (child.isMesh) filled += 1;
    });
    return { filled, lines, highlighted };
  }

  function buildCrinkle(position, axis) {
    const volume = state.meshVolume;
    const cells = cellsCrossing(volume, axis, position);
    if (!cells.length) return;
    const quality = state.meshView.quality;
    const { triangles, edges, values } = cellGeometry(volume, cells, quality);
    const geometry = new THREE.BufferGeometry().setAttribute("position", new THREE.BufferAttribute(triangles, 3));
    const colors = new Float32Array(triangles.length);
    for (let i = 0; i < colors.length / 3; i += 1) colors.set(values ? qualityColor(values[i]) : [0.55, 0.64, 0.74], 3 * i);
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    const face = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
    face.userData = { kind: "section", field: "mesh", crinkle: cells.length };
    layers.section.add(face);
    layers.section.add(new THREE.LineSegments(
      new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(edges, 3)),
      new THREE.LineBasicMaterial({ color: 0x0a0e14, transparent: true, opacity: 0.7 }),
    ));
  }

  function setMesh(display, volume = null) {
    disposeTree(layers.mesh);
    layers.mesh.clear();
    state.meshVolume = prepareVolume(volume);
    state.meshDisplay = display;
    state.meshView = { ...state.meshView, highlight: null, quality: null };
    if (!display) { syncBodies(); applyClip(); requestRender(); return; }
    if (display.kind === "surface") {
      buildMeshSurface();
      syncBodies();
      applyClip();
      requestRender();
      return;
    }
    const { origin, spacing, shape, solid_centres: centres } = display;
    const count = centres.length / 3;
    if (count) {
      const cube = new THREE.BoxGeometry(spacing[0] * 0.92, spacing[1] * 0.92, spacing[2] * 0.92);
      const instanced = new THREE.InstancedMesh(cube, new THREE.MeshStandardMaterial({ color: 0xec835a, roughness: 0.7, transparent: true, opacity: 0.85 }), count);
      const matrix = new THREE.Matrix4();
      for (let i = 0; i < count; i += 1) {
        matrix.makeTranslation(centres[3 * i], centres[3 * i + 1], centres[3 * i + 2]);
        instanced.setMatrixAt(i, matrix);
      }
      layers.mesh.add(instanced);
    }
    const lines = [];
    const z = origin[2];
    const x1 = origin[0] + shape[0] * spacing[0];
    const y1 = origin[1] + shape[1] * spacing[1];
    const stride = Math.max(1, Math.ceil(Math.max(shape[0], shape[1]) / 160));
    for (let i = 0; i <= shape[0]; i += stride) lines.push(origin[0] + i * spacing[0], origin[1], z, origin[0] + i * spacing[0], y1, z);
    for (let j = 0; j <= shape[1]; j += stride) lines.push(origin[0], origin[1] + j * spacing[1], z, x1, origin[1] + j * spacing[1], z);
    const grid=new THREE.LineSegments(new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(lines, 3)),
      new THREE.LineBasicMaterial({ color: 0xec835a, transparent: true, opacity: 0.35 }));
    layers.mesh.add(grid);
    layers.mesh.visible = state.visible.mesh;
    requestRender();
  }

  // results
  function range(values, mask) {
    let min=Infinity; let max = -Infinity;
    values.forEach((value, index) => {
      if (mask && mask[index]) return;
      if (value < min) min = value;
      if (value > max) max = value;
    });
    return Number.isFinite(min) ? [min, max] : [0, 0];
  }

  function setResult(result, field) {
    const fresh = result !== state.resultData;
    if (fresh) state.resultVolume = prepareVolume(result?.volume);
    state.resultData = result;
    if (fresh) { state.traces.lines = []; queueMicrotask(() => { rebuildTraces(); rebuildIso(); }); }
    disposeTree(layers.results);
    layers.results.clear();
    state.resultRanges = null;
    layers.bodies.visible = true;
    if (!result || !result.fields && !result.nodes && !result.surfaces && !result.lines) { applyClip(); requestRender(); return null; }
    let legend = null;
    if (result.surfaces || result.lines) {
      legend = buildSurfaceResult(result, field);
      state.resultRanges = legend;
      if (state.meshDisplay) { buildMeshSurface(); syncBodies(); }
      applyClip();
      requestRender();
      return legend;
    }
    if (result.analysis === "cfd" && result.fields) {
      const chosen = field || "pressure";
      const values = result.fields[chosen];
      const solid = result.fields.solid;
      const [min, max] = range(values, solid);
      const kind = chosen === "pressure" ? "diverging" : "sequential";
      const [nx, ny] = result.grid.shape;
      const [ox, oy, oz] = result.grid.origin;
      const [dx, dy] = result.grid.spacing;
      const positions=new Float32Array(nx * ny * 3);
      const colors = new Float32Array(nx * ny * 3);
      for (let j = 0; j < ny; j += 1) {
        for (let i = 0; i < nx; i += 1) {
          const index = j * nx + i;
          positions.set([ox + i * dx, oy + j * dy, oz], index * 3);
          const color = colorFor(kind, values[index], min, max);
          colors.set([color.r, color.g, color.b], index * 3);
        }
      }
      const indices = [];
      for (let j = 0; j < ny - 1; j += 1) {
        for (let i = 0; i < nx - 1; i += 1) {
          const a = j * nx + i; const b = a + 1; const c = a + nx; const d = c + 1;
          if (solid[a] || solid[b] || solid[c] || solid[d]) continue;
          indices.push(a, b, d, a, d, c);
        }
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      geometry.setIndex(indices);
      const contour = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide, transparent: true, opacity: 0.9, depthWrite: false }));
      contour.userData.layer = "contour";
      contour.visible = state.visible.contour;
      layers.results.add(contour);
      const [smin, smax] = range(result.streamlines.flatMap(line => line.filter((_, k) => k % 4 === 3)));
      for (const line of result.streamlines) {
        const points=[]; const lineColors = [];
        for (let k = 0; k < line.length; k += 4) {
          points.push(line[k], line[k + 1], line[k + 2] + dx * 0.05);
          const color = colorFor("sequential", line[k + 3], smin, smax);
          lineColors.push(color.r, color.g, color.b);
        }
        const geometryLine=new THREE.BufferGeometry();
        geometryLine.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
        geometryLine.setAttribute("color", new THREE.Float32BufferAttribute(lineColors, 3));
        const streamline = new THREE.Line(geometryLine, new THREE.LineBasicMaterial({ vertexColors: true }));
        streamline.userData.layer = "streamlines";
        streamline.visible = state.visible.streamlines;
        layers.results.add(streamline);
      }
      legend = { field: chosen, kind, min, max, unit: result.units?.[chosen] || "" };
    } else if (result.analysis === "fea" && result.nodes) {
      const values = result.von_mises;
      const [min, max] = range(values);
      const span=Math.max(...result.nodes.map(n => n[0])) - Math.min(...result.nodes.map(n => n[0]));
      const peak = Math.max(...result.displacement.map(([u, w]) => Math.hypot(u, w)), 1e-30);
      const scaleFactor = state.deformation * 0.08 * span / peak;
      const [y0, y1] = result.y_range;
      const positions=[]; const colors = [];
      const nodeCount = result.nodes.length;
      for (const y of [y0, y1]) {
        result.nodes.forEach(([x, z], n) => {
          const [u, w] = result.displacement[n];
          positions.push(x + u * scaleFactor, y, z + w * scaleFactor);
          const color = colorFor("sequential", values[n], min, max);
          colors.push(color.r, color.g, color.b);
        });
      }
      const indices = [];
      for (const [a, b, c, d] of result.elements) {
        for (const offset of [0, nodeCount]) indices.push(a + offset, b + offset, c + offset, a + offset, c + offset, d + offset);
        indices.push(a, b, b + nodeCount, a, b + nodeCount, a + nodeCount, d, c, c + nodeCount, d, c + nodeCount, d + nodeCount);
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
      geometry.setIndex(indices);
      geometry.computeVertexNormals();
      const slab = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide }));
      slab.userData.layer = "contour";
      slab.visible = state.visible.contour;
      const wire = new THREE.LineSegments(new THREE.WireframeGeometry(geometry), new THREE.LineBasicMaterial({ color: 0x0a0e14, transparent: true, opacity: 0.25 }));
      slab.add(wire);
      layers.results.add(slab);
      layers.bodies.visible = !state.visible.contour;
      legend = { field: "von_mises", kind: "sequential", min, max, unit: "Pa", deformationScale: scaleFactor };
    }
    state.resultRanges = legend;
    applyClip();
    requestRender();
    return legend;
  }

  function fieldRange(result, field) {
    const values = [];
    for (const surface of [...(result.surfaces || []), ...(result.lines || [])]) if (surface.values[field]) values.push(...surface.values[field]);
    if (result.slice?.values?.[field]) values.push(...result.slice.values[field]);
    return values.length ? range(values) : [0, 0];
  }

  function coloredMesh(positions, indices, values, kind, min, max, displacement, scaleFactor, opacity = 1) {
    const geometry = new THREE.BufferGeometry();
    const shifted=displacement && scaleFactor ? positions.map((v, i) => v + displacement[i] * scaleFactor) : positions;
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(shifted, 3));
    geometry.setIndex(indices);
    const colors=new Float32Array(positions.length);
    const neutral = new THREE.Color(0x8fa3b8);
    for (let i = 0; i < positions.length / 3; i += 1) {
      const color = values ? colorFor(kind, values[i], min, max) : neutral;
      colors.set([color.r, color.g, color.b], i * 3);
    }
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    geometry.computeVertexNormals();
    const coloredResult = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
      vertexColors: true, side: THREE.DoubleSide, transparent: opacity < 1, opacity,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
    }));
    coloredResult.userData.values = values; // the probe interpolates these
    return coloredResult;
  }

  function buildSurfaceResult(result, requested) {
    const fields = result.field_names || (result.analysis === "cfd" ? ["pressure", "speed", "wall_shear"] : ["von_mises", "displacement"]);
    const field = fields.includes(requested) ? requested : fields[0];
    const kind = field === "pressure" ? "diverging" : "sequential";
    const [min, max] = fieldRange(result, field);
    let scaleFactor = 0;
    const parts = [...(result.surfaces || []), ...(result.lines || [])];
    // modal results carry one shape per mode field; static results one displacement field
    const shapeOf=part => part.mode_displacements?.[field] || part.displacement;
    if (result.analysis === "fea" && parts.length) {
      const box = new THREE.Box3();
      parts.forEach(part => box.union(new THREE.Box3().setFromArray(part.positions)));
      const span = box.getSize(new THREE.Vector3()).length();
      const magnitude = result.physics === "modal" ? field : "displacement";
      const peak=Math.max(...parts.map(part => Math.max(...(part.values[magnitude] || [0]))), 1e-30);
      scaleFactor = state.deformation * 0.06 * span / peak;
    }
    for (const line of result.lines || []) {
      const geometry = new THREE.BufferGeometry();
      const shape = shapeOf(line);
      const shifted = shape && scaleFactor ? line.positions.map((v, i) => v + shape[i] * scaleFactor) : line.positions;
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(shifted, 3));
      const colors=new Float32Array(line.positions.length);
      line.values[field].forEach((value, i) => { const c = colorFor(kind, value, min, max); colors.set([c.r, c.g, c.b], 3 * i); });
      geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      const segments = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ vertexColors: true, linewidth: 2 }));
      segments.userData.layer = "contour";
      segments.visible = state.visible.contour;
      layers.results.add(segments);
    }
    for (const surface of result.surfaces || []) {
      const mesh = coloredMesh(surface.positions, surface.indices, surface.values[field], kind, min, max, shapeOf(surface), scaleFactor);
      mesh.userData.layer = "contour";
      mesh.visible = state.visible.contour;
      mesh.add(new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry, 25), new THREE.LineBasicMaterial({ color: 0x0a0e14, transparent: true, opacity: 0.35 })));
      layers.results.add(mesh);
    }
    if (result.slice && result.slice.positions && result.slice.positions.length) {
      const sliceField = result.slice.values[field] ? field : Object.keys(result.slice.values)[0];
      const sliceKind = sliceField === "pressure" ? "diverging" : "sequential";
      const [smin, smax] = sliceField === field ? [min, max] : range(result.slice.values[sliceField]);
      const plane = coloredMesh(result.slice.positions, result.slice.indices, result.slice.values[sliceField], sliceKind, smin, smax, null, 0, 0.85);
      plane.material.depthWrite = false;
      plane.userData.layer = "contour";
      plane.visible = state.visible.contour;
      layers.results.add(plane);
    }
    if (result.streamlines && result.streamlines.length) {
      const [smin, smax] = range(result.streamlines.flatMap(line => line.filter((_, k) => k % 4 === 3)));
      for (const line of result.streamlines) {
        const points=[]; const lineColors = [];
        for (let k = 0; k < line.length; k += 4) {
          points.push(line[k], line[k + 1], line[k + 2]);
          const color = colorFor("sequential", line[k + 3], smin, smax);
          lineColors.push(color.r, color.g, color.b);
        }
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
        geometry.setAttribute("color", new THREE.Float32BufferAttribute(lineColors, 3));
        const streamline = new THREE.Line(geometry, new THREE.LineBasicMaterial({ vertexColors: true }));
        streamline.userData.layer = "streamlines";
        streamline.visible = state.visible.streamlines;
        layers.results.add(streamline);
      }
    }
    layers.bodies.visible = !state.visible.contour || Boolean(result.lines?.length);
    if (result.lines?.length) {
      // Beam/shell idealizations: keep the solid visible as a translucent reference
      layers.bodies.traverse(child => { if (child.material && !Array.isArray(child.material)) { child.material.transparent = true; child.material.opacity = Math.min(child.material.opacity, 0.18); child.material.depthWrite = false; } });
    }
    return { field, kind, min, max, unit: (result.units && result.units[field]) || "", fields, labels: result.field_labels || null, deformationScale: scaleFactor };
  }

  function setLayer(name, visible) {
    state.visible[name] = visible;
    if (name === "mesh") { buildMeshSurface(); layers.mesh.visible = visible; scheduleSection(); }
    if (name === "streamlines") layers.traces.visible = visible;
    layers.results.children.forEach(child => { if (child.userData.layer === name) child.visible = visible; });
    if (name === "contour") buildMeshSurface();
    syncBodies();
    applyClip();
    requestRender();
  }

  function setDeformation(value) {
    state.deformation = value;
    if (state.resultData?.analysis === "fea") return setResult(state.resultData, state.resultRanges?.field);
    return state.resultRanges;
  }

  // cutting plane, section, and vector glyphs
  function clipBounds() {
    const volume = state.resultVolume || state.meshVolume;
    if (volume) return { low: volume.low, high: volume.high };
    const box = contentBox();
    if (box.isEmpty()) return null;
    return { low: box.min.toArray(), high: box.max.toArray() };
  }

  function clipPosition() {
    const bounds = clipBounds();
    if (!bounds) return 0;
    const { axis, fraction } = state.clip;
    return bounds.low[axis] + fraction * (bounds.high[axis] - bounds.low[axis]);
  }

  function applyClip() {
    const { enabled, axis, flip } = state.clip;
    const position = clipPosition();
    const normal = new THREE.Vector3();
    normal.setComponent(axis, flip ? 1 : -1);
    clipPlane.set(normal, flip ? -position : position);
    for (const group of [layers.bodies, layers.mesh, layers.results, layers.highlight]) {
      group.traverse(child => {
        const materials = Array.isArray(child.material) ? child.material : (child.material ? [child.material] : []);
        for (const material of materials) {
          if (!material.userData.baseClipping) material.userData.baseClipping = material.clippingPlanes ? [...material.clippingPlanes] : [];
          const base = material.userData.baseClipping;
          const next = enabled ? [...base, clipPlane] : base;
          if ((material.clippingPlanes?.length || 0) !== next.length) material.needsUpdate = true;
          material.clippingPlanes = next.length ? next : null;
        }
      });
    }
    scheduleSection();
  }

  function scheduleSection() {
    if (state.sectionPending) return;
    state.sectionPending = true;
    requestAnimationFrame(() => { state.sectionPending = false; rebuildSection(); });
  }

  function placeArrows(points, vectors, scale, lift = null) {
    const count = points.length / 3;
    if (!count) return null;
    let peak = 0;
    const magnitudes = new Float32Array(count);
    for (let i = 0; i < count; i += 1) {
      magnitudes[i] = Math.hypot(vectors[3 * i], vectors[3 * i + 1], vectors[3 * i + 2]);
      peak = Math.max(peak, magnitudes[i]);
    }
    if (peak <= 0) return null;
    const material = new THREE.MeshBasicMaterial({ vertexColors: false });
    const instanced = new THREE.InstancedMesh(arrow, material, count);
    const matrix = new THREE.Matrix4(); const quaternion = new THREE.Quaternion();
    const up = new THREE.Vector3(0, 1, 0); const direction = new THREE.Vector3();
    for (let i = 0; i < count; i += 1) {
      const length = Math.max(magnitudes[i] / peak, 0.08) * scale;
      direction.set(vectors[3 * i], vectors[3 * i + 1], vectors[3 * i + 2]);
      if (direction.lengthSq() > 0) quaternion.setFromUnitVectors(up, direction.normalize()); else quaternion.identity();
      const origin = new THREE.Vector3(points[3 * i], points[3 * i + 1], points[3 * i + 2]);
      if (lift) origin.add(lift);
      matrix.compose(origin, quaternion, new THREE.Vector3(length, length, length));
      instanced.setMatrixAt(i, matrix);
      instanced.setColorAt(i, rampColor(GLYPH, magnitudes[i] / peak));
    }
    instanced.instanceMatrix.needsUpdate = true;
    if (instanced.instanceColor) instanced.instanceColor.needsUpdate = true;
    instanced.userData = { peak, count };
    return instanced;
  }

  function thin(points, vectors, cell, axis) {
    // keep one sample per grid cell in the plane so glyphs are evenly spaced
    const [i1, i2] = [0, 1, 2].filter(k => k !== axis);
    const seen = new Set(); const keptPoints = []; const keptVectors = [];
    for (let i = 0; i < points.length / 3; i += 1) {
      const key = `${Math.floor(points[3 * i + i1] / cell)},${Math.floor(points[3 * i + i2] / cell)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      keptPoints.push(points[3 * i], points[3 * i + 1], points[3 * i + 2]);
      keptVectors.push(vectors[3 * i], vectors[3 * i + 1], vectors[3 * i + 2]);
    }
    return [keptPoints, keptVectors];
  }

  function rebuildSection() {
    disposeTree(layers.section); layers.section.clear();
    disposeTree(layers.glyphs); layers.glyphs.clear();
    const result = state.resultData;
    const span = (() => {
      const bounds = clipBounds();
      return bounds ? Math.max(...bounds.high.map((v, k) => v - bounds.low[k])) : 1;
    })();
    if (state.glyphs && result?.ports?.length) {
      for (const port of result.ports) {
        const glyphs=placeArrows(port.positions, port.vectors, span * 0.03);
        if (glyphs) { glyphs.userData.layer = "glyphs"; layers.glyphs.add(glyphs); }
      }
    }
    const volume = state.resultVolume || state.meshVolume;
    if (!state.clip.enabled || !volume) { requestRender(); return; }
    const { axis } = state.clip;
    const position = clipPosition();
    if (state.visible.mesh && state.meshVolume) { // show the real cells at the cut, not a sliced outline
      buildCrinkle(position, axis);
      requestRender();
      return;
    }
    const legend = state.resultRanges;
    const fieldName = legend && volume.scalars[legend.field] ? legend.field : Object.keys(volume.scalars)[0];
    const vectorName = volume.vectors.U ? "U" : null;
    const cut = sliceVolume(volume, axis, position, fieldName, vectorName);
    if (!cut.positions.length) { requestRender(); return; }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(cut.positions, 3));
    let material;
    if (fieldName) {
      const sameField = legend && legend.field === fieldName;
      let [min, max] = sameField ? [legend.min, legend.max] : range(cut.values);
      if (min === max) { min -= 1; max += 1; }
      const kind = fieldName === "pressure" ? "diverging" : "sequential";
      const colors=new Float32Array(cut.positions.length);
      cut.values.forEach((value, i) => { const c = colorFor(kind, value, min, max); colors.set([c.r, c.g, c.b], 3 * i); });
      geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      material = new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide });
    } else {
      material = new THREE.MeshBasicMaterial({ color: 0x3b4a5c, side: THREE.DoubleSide, transparent: true, opacity: 0.9 });
    }
    const face = new THREE.Mesh(geometry, material);
    face.userData = { kind: "section", field: fieldName, values: fieldName ? cut.values : null };
    layers.section.add(face);
    if (!fieldName || state.visible.mesh) {
      const outlines = new THREE.LineSegments(
        new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(cut.outlines, 3)),
        new THREE.LineBasicMaterial({ color: fieldName ? 0x0a0e14 : 0xec835a, transparent: true, opacity: fieldName ? 0.35 : 0.8 }),
      );
      layers.section.add(outlines);
    }
    if (state.glyphs && cut.vectors.length) {
      const centres = []; const vectors = [];
      for (let i = 0; i < cut.positions.length / 9; i += 1) {
        for (let k = 0; k < 3; k += 1) {
          centres.push((cut.positions[9 * i + k] + cut.positions[9 * i + 3 + k] + cut.positions[9 * i + 6 + k]) / 3);
          vectors.push((cut.vectors[9 * i + k] + cut.vectors[9 * i + 3 + k] + cut.vectors[9 * i + 6 + k]) / 3);
        }
      }
      const [points, kept] = thin(centres, vectors, span / 45, axis);
      // lift the arrows slightly toward the visible side so they do not z-fight with the section
      const glyphs = placeArrows(points, kept, span / 28, clipPlane.normal.clone().multiplyScalar(span * 0.004));
      if (glyphs) { glyphs.userData.layer = "glyphs"; layers.glyphs.add(glyphs); }
    }
    requestRender();
  }

  function setClip(options) {
    state.clip = { ...state.clip, ...options };
    applyClip();
    return { position: clipPosition(), bounds: clipBounds(), enabled: state.clip.enabled };
  }

  function setGlyphs(on) { state.glyphs = on; scheduleSection(); }

  // iso-surfaces
  function rebuildIso() {
    disposeTree(layers.iso); layers.iso.clear();
    const volume = state.resultVolume;
    const options=state.iso;
    if (!options.enabled || !volume) { requestRender(); return null; }
    const field = volume.scalars[options.field] ? options.field : Object.keys(volume.scalars)[0];
    if (!field) return null;
    const [low, high] = range(volume.scalars[field]);
    const level = low + options.fraction * (high - low);
    const legend = state.resultRanges;
    const colorField = options.color && volume.scalars[options.color] ? options.color
      : legend && volume.scalars[legend.field] ? legend.field : field;
    const surface = isoSurface(volume, field, level, colorField);
    state.isoInfo = { field, level, triangles: surface.positions.length / 9, colorField };
    if (!surface.positions.length) { requestRender(); return state.isoInfo; }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(surface.positions, 3));
    let [min, max] = legend && legend.field === colorField ? [legend.min, legend.max] : range(surface.values);
    if (min === max) { min -= 1; max += 1; }
    const kind = colorField === "pressure" ? "diverging" : "sequential";
    const colors = new Float32Array(surface.positions.length);
    surface.values.forEach((value, i) => { const c = colorFor(kind, value, min, max); colors.set([c.r, c.g, c.b], 3 * i); });
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    geometry.computeVertexNormals();
    const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide, roughness: 0.6, transparent: true, opacity: 0.8, depthWrite: false }));
    mesh.userData = { kind: "iso", field, level };
    layers.iso.add(mesh);
    requestRender();
    return state.isoInfo;
  }

  function setIso(options) {
    state.iso = { ...state.iso, ...options };
    const info = rebuildIso();
    if (!info) return null;
    const volume = state.resultVolume;
    const [low, high] = range(volume.scalars[info.field]);
    return { ...info, min: low, max: high, fields: Object.keys(volume.scalars) };
  }

  // particle-traced streamlines
  function traceSeeds(volume, count) {
    const inlets = (state.resultData?.ports || []).filter(port => port.kind === "inlet");
    const candidates=[];
    const span=Math.max(...volume.high.map((v, k) => v - volume.low[k]));
    for (const port of inlets) {
      for (let i = 0; i < port.positions.length; i += 3) {
        const u = port.vectors.slice(i, i + 3);
        const speed = Math.hypot(...u);
        if (speed < 1e-9) continue; // wall nodes on the inlet rim
        const nudge=span * 2e-3 / speed;
        candidates.push(port.positions[i] + u[0] * nudge, port.positions[i + 1] + u[1] * nudge, port.positions[i + 2] + u[2] * nudge);
      }
    }
    const stride = Math.max(1, Math.floor(candidates.length / 3 / count));
    const seeds = [];
    for (let i = 0; i < candidates.length / 3; i += stride) seeds.push(candidates[3 * i], candidates[3 * i + 1], candidates[3 * i + 2]);
    return { seeds, span };
  }

  function rebuildTraces() {
    disposeTree(layers.traces); layers.traces.clear();
    state.traces.particles = null;
    const result = state.resultData; const volume = state.resultVolume;
    const options = state.traces;
    // solver-provided streamlines win; trace in the browser only when there are none
    if (!options.enabled || !volume?.vectors.U || result?.streamlines?.length) { requestRender(); return 0; }
    if (!options.lines.length) {
      const { seeds, span } = traceSeeds(volume, options.seeds);
      options.lines = traceStreamlines(volume, "U", seeds, { step: span / 150, maxSteps: 900 });
    }
    if (!options.lines.length) { requestRender(); return 0; }
    const [smin, smax] = range(options.lines.flatMap(line => line.filter((_, k) => k % 4 === 3)));
    const heads = [];
    for (const line of options.lines) {
      const points=[]; const lineColors = [];
      for (let k = 0; k < line.length; k += 4) {
        points.push(line[k], line[k + 1], line[k + 2]);
        const color = colorFor("sequential", line[k + 3], smin, smax);
        lineColors.push(color.r, color.g, color.b);
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
      geometry.setAttribute("color", new THREE.Float32BufferAttribute(lineColors, 3));
      const streamline = new THREE.Line(geometry, new THREE.LineBasicMaterial({ vertexColors: true }));
      streamline.userData = { layer: "streamlines", kind: "trace" };
      layers.traces.add(streamline);
      heads.push(0, 0, 0);
    }
    if (options.animate) {
      options.times = options.lines.map(line => {
        const t = [0];
        for (let k = 4; k < line.length; k += 4) {
          const ds = Math.hypot(line[k] - line[k - 4], line[k + 1] - line[k - 3], line[k + 2] - line[k - 2]);
          t.push(t[t.length - 1] + 2 * ds / Math.max(line[k + 3] + line[k - 1], 1e-12));
        }
        return t;
      });
      const geometry = new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(heads, 3));
      const particles = new THREE.Points(geometry, new THREE.PointsMaterial({ color: 0xf4f7fb, size: 4, sizeAttenuation: false }));
      particles.userData = { layer: "streamlines", kind: "particles" };
      layers.traces.add(particles);
      options.particles = particles;
    }
    layers.traces.visible = state.visible.streamlines;
    requestRender();
    return options.lines.length;
  }

  function advanceParticles() {
    const particles = state.traces.particles;
    if (!particles || !layers.traces.visible) return;
    state.traces.phase = (state.traces.phase + 0.004) % 1;
    const attribute = particles.geometry.attributes.position;
    const times = state.traces.times;
    state.traces.lines.forEach((line, i) => {
      // Particles move with the local speed: time along a line is the integral of ds/|U|
      const t = times[i]; const target = state.traces.phase * t[t.length - 1];
      let k = 0;
      while (k < t.length - 1 && t[k + 1] < target) k += 1;
      attribute.setXYZ(i, line[4 * k], line[4 * k + 1], line[4 * k + 2]);
    });
    attribute.needsUpdate = true;
    state.needsRender = true;
  }

  function setStreamlines(options) {
    const reseed = options.seeds !== undefined && options.seeds !== state.traces.seeds;
    state.traces = { ...state.traces, ...options };
    if (reseed) state.traces.lines = [];
    return rebuildTraces();
  }

  function sectionInfo() {
    const face = layers.section.children.find(child => child.userData.kind === "section");
    const glyphs = layers.glyphs.children.reduce((sum, child) => sum + (child.userData.count || 0), 0);
    const iso = layers.iso.children.find(child => child.userData.kind === "iso");
    const traces = layers.traces.children.filter(child => child.userData.kind === "trace");
    return {
      crinkle: face?.userData.crinkle || 0, mesh: meshViewInfo(),
      triangles: face ? face.geometry.attributes.position.count / 3 : 0, field: face?.userData.field || null, glyphs,
      iso: iso ? { triangles: iso.geometry.attributes.position.count / 3, field: iso.userData.field, level: iso.userData.level } : null,
      traces: { lines: traces.length, points: traces.reduce((sum, line) => sum + line.geometry.attributes.position.count, 0), animated: Boolean(state.traces.particles) },
    };
  }

  function setPickMode(mode) { state.pickMode = mode; }

  // selection
  function refreshHighlight() {
    disposeTree(layers.highlight);
    layers.highlight.clear();
    for (const entity of [state.selection, ...state.extras]) if (entity?.key) highlightEntity(entity);
    requestRender();
  }

  function highlightEntity(selection) {
    const object=state.bodyObjects.get(selection.key);
    if (!object) return;
    const facesMesh = object.children[0]?.children.find(child => child.userData.kind === "faces");
    if (selection.kind === "face" && facesMesh) {
      const source = facesMesh.geometry.attributes.position.array;
      const faceIds = facesMesh.userData.faceIds;
      const positions=[];
      faceIds.forEach((faceId, triangle) => {
        if (faceId === selection.faceId) for (let k = 0; k < 9; k += 1) positions.push(source[triangle * 9 + k]);
      });
      const geometry = new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      // Keep result colours readable under the highlight
      const opacity = state.resultRanges && state.visible.contour ? 0.16 : 0.55;
      const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ color: SELECT_COLOR, transparent: true, opacity, side: THREE.DoubleSide, depthTest: false }));
      mesh.renderOrder = 10;
      const holder = new THREE.Group();
      holder.position.copy(object.position); holder.scale.copy(object.scale);
      holder.add(mesh);
      layers.highlight.add(holder);
    } else if (selection.kind === "edge" && selection.points) {
      const geometry = new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(edgeSegments([selection.points]), 3));
      const line = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: SELECT_COLOR, depthTest: false }));
      line.renderOrder = 11;
      const holder = new THREE.Group();
      holder.position.copy(object.position); holder.scale.copy(object.scale);
      holder.add(line);
      layers.highlight.add(holder);
    } else if (selection.kind === "body") {
      const box = new THREE.BoxHelper(object, SELECT_COLOR);
      box.material.depthTest = false;
      box.renderOrder = 11;
      layers.highlight.add(box);
    }
  }

  // `extras` are the rest of a multi-selection (Ctrl+click, "select coplanar faces")
  function select(selection, extras = []) {
    state.selection = selection;
    state.extras = extras;
    refreshHighlight();
  }

  function setRay(event) {
    const rect = canvas.getBoundingClientRect();
    pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    raycaster.params.Line.threshold = state.radius * 0.012;
  }

  function pick(event) {
    setRay(event);
    const faceTargets = []; const edgeTargets = [];
    layers.bodies.traverse(child => {
      if (state.hidden.has(child.userData.key)) return;
      if (child.userData.kind === "faces") faceTargets.push(child);
      if (child.userData.kind === "edges") edgeTargets.push(child);
    });
    const visibleHit = hit => {
      const planes = hit.object.material.clippingPlanes;
      return !planes || planes.every(plane => plane.distanceToPoint(hit.point) >= 0);
    };
    const faceHit = state.pickMode === "edge" ? null : raycaster.intersectObjects(faceTargets, false).find(visibleHit);
    const edgeHit=state.pickMode === "face" || (state.display === "xray" && state.pickMode !== "edge")
      ? null : raycaster.intersectObjects(edgeTargets, false).find(visibleHit);
    if (edgeHit && (state.pickMode === "edge" || !faceHit || edgeHit.distance <= faceHit.distance + state.radius * 0.01)) {
      const segment=Math.floor(edgeHit.index / 2);
      let counted = 0;
      for (const polyline of edgeHit.object.userData.polylines) {
        const segments = polyline.length / 3 - 1;
        if (segment < counted + segments) {
          return { kind: "edge", key: edgeHit.object.userData.key, points: polyline, point: edgeHit.point.toArray() };
        }
        counted += segments;
      }
    }
    if (faceHit) {
      const faceId = faceHit.object.userData.faceIds[faceHit.faceIndex];
      return { kind: "face", key: faceHit.object.userData.key, faceId, point: faceHit.point.toArray() };
    }
    const derived = raycaster.intersectObjects(layers.bodies.children.filter(c => c.userData.kind === "derived"), false)[0];
    if (derived) return { kind: "feature", key: derived.object.userData.key };
    return null;
  }

  // controls
  let drag = null;
  canvas.addEventListener("contextmenu", event => event.preventDefault());
  canvas.addEventListener("pointerdown", event => {
    try { canvas.setPointerCapture(event.pointerId); } catch { /* synthetic or released pointer */ }
    drag = {
      x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY, moved: false,
      action: mouseAction(state.mouse, { button: event.button, shift: event.shiftKey, ctrl: event.ctrlKey || event.metaKey }),
    };
  });
  canvas.addEventListener("mousedown", event => { if (event.button === 1) event.preventDefault(); }); // no browser autoscroll
  canvas.addEventListener("pointermove", event => {
    if (!drag) {
      onPointer(null);
      return;
    }
    const dx = event.clientX - drag.x; const dy = event.clientY - drag.y;
    if (Math.abs(event.clientX - drag.startX) + Math.abs(event.clientY - drag.startY) > 4) drag.moved = true;
    if (!drag.moved) return; // a right click that wobbles a pixel still opens the menu
    drag.x = event.clientX; drag.y = event.clientY;
    if (drag.action === "zoom") {
      state.distance = clampDistance(state.distance * Math.exp(dy * 0.006));
    } else if (drag.action === "pan") {
      const scale=state.distance * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * 2 / canvas.clientHeight;
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
      const up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
      state.target.addScaledVector(right, -dx * scale).addScaledVector(up, dy * scale);
    } else if (drag.action === "orbit") {
      state.theta -= dx * 0.008;
      state.phi = Math.min(Math.PI - 1e-3, Math.max(1e-3, state.phi - dy * 0.008));
    } else {
      return;
    }
    updateCamera();
  });
  canvas.addEventListener("pointerup", event => {
    if (drag && !drag.moved && event.button === 0) onSelect(pick(event), { add: event.ctrlKey || event.metaKey });
    if (drag && !drag.moved && event.button === 2) onContext?.(pick(event), resultAt(event), event);
    drag = null;
  });
  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    const before = state.distance;
    state.distance = clampDistance(before * Math.exp(event.deltaY * 0.0012 * (state.invertWheel ? -1 : 1)));
    // zoom toward the cursor: the point under it stays put instead of sliding to the screen centre
    const rect = canvas.getBoundingClientRect();
    const ndcX = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    const ndcY = 1 - ((event.clientY - rect.top) / rect.height) * 2;
    const half = before * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const shift = 1 - state.distance / before;
    state.target
      .addScaledVector(new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0), ndcX * half * camera.aspect * shift)
      .addScaledVector(new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1), ndcY * half * shift);
    updateCamera();
  }, { passive: false });

  function clampDistance(distance) {
    return Math.max(state.radius * 0.005, Math.min(state.radius * 60, distance));
  }

  // orientation triad
  const triad=document.createElement("div");
  triad.className = "vp-triad";
  triad.setAttribute("aria-label", "View orientation");
  canvas.parentElement?.append(triad);
  const TRIAD_AXES = [
    { label: "X", color: "#ef6461", vector: [1, 0, 0], preset: "right", name: "Right (+X)" }, { label: "", color: "#ef6461", vector: [-1, 0, 0], preset: "left", name: "Left (−X)" },
    { label: "Y", color: "#5fc27e", vector: [0, 1, 0], preset: "back", name: "Back (+Y)" }, { label: "", color: "#5fc27e", vector: [0, -1, 0], preset: "front", name: "Front (−Y)" },
    { label: "Z", color: "#5f9eea", vector: [0, 0, 1], preset: "top", name: "Top (+Z)" }, { label: "", color: "#5f9eea", vector: [0, 0, -1], preset: "bottom", name: "Bottom (−Z)" },
  ];
  triad.addEventListener("click", event => {
    const preset = event.target.closest("[data-preset]")?.dataset.preset;
    if (preset) view(preset);
  });

  function updateTriad() {
    camera.updateMatrixWorld();
    const size = 84; const centre = size / 2; const reach = 29;
    const v = new THREE.Vector3();
    const ends = TRIAD_AXES.map(axis => {
      v.set(...axis.vector).transformDirection(camera.matrixWorldInverse);
      return { ...axis, x: centre + v.x * reach, y: centre - v.y * reach, z: v.z };
    }).sort((a, b) => a.z - b.z);
    triad.innerHTML = `<svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">${ends.map(end => (
      `<g data-preset="${end.preset}"><title>${end.name}</title>`
      + (end.label ? `<line x1="${centre}" y1="${centre}" x2="${end.x.toFixed(1)}" y2="${end.y.toFixed(1)}" stroke="${end.color}" stroke-width="2.5"/>` : "")
      + `<circle cx="${end.x.toFixed(1)}" cy="${end.y.toFixed(1)}" r="${end.label ? 9 : 5.5}" fill="${end.label ? end.color : "rgba(10,14,20,.75)"}" stroke="${end.color}" stroke-width="1.5"/>`
      + (end.label ? `<text x="${end.x.toFixed(1)}" y="${(end.y + 3.6).toFixed(1)}" text-anchor="middle" fill="#0a0e14">${end.label}</text>` : "")
      + "</g>"
    )).join("")}</svg>`;
  }

  const resize = () => {
    const width = canvas.clientWidth; const height = canvas.clientHeight;
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    requestRender();
  };
  new ResizeObserver(resize).observe(canvas);
  resize();
  updateCamera();
  const loop = () => {
    advanceParticles();
    if (state.needsRender) {
      state.needsRender = false;
      renderer.render(scene, camera);
    }
    requestAnimationFrame(loop);
  };
  loop();

  return {
    available: true,
    renderer: renderer.capabilities.isWebGL2 ? "WebGL2" : "WebGL",
    setModels, setScene, setMesh, setResult, setLayer, setDisplay, setDeformation, select, fit, view,
    setClip, setGlyphs, setPickMode, sectionInfo, setIso, setStreamlines,
    lookAt, zoomTo, previousView, canGoBack: () => state.history.length > 0, setHidden, addProbe, clearProbes, probeCount: () => state.probes.length,
    snapshot, edgeAt, clipBounds, setMeshView,
    setMouseScheme: (scheme, invertWheel = false) => { state.mouse = scheme; state.invertWheel = invertWheel; },
    meshVolume: () => state.meshVolume,
    debugView: () => ({ theta: state.theta, phi: state.phi, distance: state.distance, target: state.target.toArray() }),
    hasContent: () => !contentBox().isEmpty(),
  };
}
