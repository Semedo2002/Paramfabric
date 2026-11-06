// mesh analysis for the viewer. No DOM or WebGL here: it works on the volume payload
// ({ positions: [x,y,z,...], tets: [a,b,c,d,...], cells?: [{ kind, cells, tets_per_cell }] })

export const POOR_QUALITY = 0.1;
export const FAIR_QUALITY = 0.3;

/**
 * Shape quality of each tetrahedron: 6√2·V / l_rms³, 1 for a regular tetrahedron and 0 for a flat one.
 * The same measure as VolumeMesh.quality() on the server. The `scored` mask is false for tets cut from
 * prisms and hexes: boundary-layer prisms are stretched on purpose, so this measure would wrongly flag them.
 */
export function tetQualities(volume) {
  const { positions: p, tets } = volume;
  const count = tets.length / 4;
  const quality = new Float32Array(count);
  const scored = new Uint8Array(count);
  let tet = 0;
  for (const block of volume.cells || [{ kind: "tet4", cells: count, tets_per_cell: 1 }]) {
    const total = block.cells * block.tets_per_cell;
    if (block.tets_per_cell === 1) scored.fill(1, tet, tet + total);
    tet += total;
  }
  for (let t = 0; t < count; t += 1) {
    const a = 3 * tets[4 * t]; const b = 3 * tets[4 * t + 1]; const c = 3 * tets[4 * t + 2]; const d = 3 * tets[4 * t + 3];
    const ux = p[b] - p[a], uy = p[b + 1] - p[a + 1], uz = p[b + 2] - p[a + 2];
    const vx = p[c] - p[a], vy = p[c + 1] - p[a + 1], vz = p[c + 2] - p[a + 2];
    const wx = p[d] - p[a], wy = p[d + 1] - p[a + 1], wz = p[d + 2] - p[a + 2];
    const volume6=Math.abs(ux * (vy * wz - vz * wy) - uy * (vx * wz - vz * wx) + uz * (vx * wy - vy * wx));
    const ex = vx - ux, ey = vy - uy, ez = vz - uz;
    const fx = wx - ux, fy = wy - uy, fz = wz - uz;
    const gx = wx - vx, gy = wy - vy, gz = wz - vz;
    const squares = (ux * ux + uy * uy + uz * uz) + (vx * vx + vy * vy + vz * vz) + (wx * wx + wy * wy + wz * wz)
      + (ex * ex + ey * ey + ez * ez) + (fx * fx + fy * fy + fz * fz) + (gx * gx + gy * gy + gz * gz);
    const rms = Math.sqrt(squares / 6);
    quality[t] = rms > 0 ? (Math.SQRT2 * volume6) / (rms * rms * rms) : 0;
  }
  return { quality, scored };
}

export function qualityStats({ quality, scored }, bins = 10) {
  const counts = new Array(bins).fill(0);
  let total = 0; let sum = 0; let min = Infinity; let worst = -1; let poor = 0; let fair = 0;
  for (let t = 0; t < quality.length; t += 1) {
    if (!scored[t]) continue;
    const q = quality[t];
    total += 1; sum += q;
    if (q < min) { min = q; worst = t; }
    if (q < POOR_QUALITY) poor += 1;
    else if (q < FAIR_QUALITY) fair += 1;
    counts[Math.min(bins - 1, Math.floor(q * bins))] += 1;
  }
  return { bins: counts, total, unscored: quality.length - total, min: total ? min : null, mean: total ? sum / total : null, worst, poor, fair };
}

// What the numbers mean, in words. `level` is good, warn, or bad.
export function qualityVerdict(stats) {
  if (!stats.total) return { level: "good", text: "All cells are prisms or hexes, which this tetrahedron measure does not score. Run checkMesh for those." };
  const share = stats.poor / stats.total;
  if (stats.min < 0.01) return { level: "bad", text: `${stats.poor.toLocaleString()} cells are nearly flat (worst ${stats.min.toFixed(3)}). Expect the solver to struggle: refine there or simplify the geometry first.` };
  if (stats.poor && share > 0.01) return { level: "bad", text: `${stats.poor.toLocaleString()} poor cells (${(share * 100).toFixed(1)}%). Refine near them before trusting results.` };
  if (stats.poor) return { level: "warn", text: `${stats.poor.toLocaleString()} poor cells out of ${stats.total.toLocaleString()}. Usually acceptable away from the region you care about: check where they are.` };
  if (stats.min < FAIR_QUALITY) return { level: "good", text: `No poor cells. The worst is ${stats.min.toFixed(3)}, which is normal for a tetrahedral mesh.` };
  return { level: "good", text: `Well-shaped mesh. The worst cell is ${stats.min.toFixed(3)}.` };
}

// Scored cells with low <= quality < high, worst first, at most `limit`
export function cellsInRange({ quality, scored }, low, high, limit = 20000) {
  const found = [];
  for (let t = 0; t < quality.length; t += 1) if (scored[t] && quality[t] >= low && quality[t] < high) found.push(t);
  found.sort((a, b) => quality[a] - quality[b]);
  return found.slice(0, limit);
}

// tets the plane passes through: drawn whole, this shows the real cells instead of a sliced outline
export function cellsCrossing(volume, axis, position, limit = 200000) {
  const { positions: p, tets } = volume;
  const found = [];
  for (let t = 0; t < tets.length / 4 && found.length < limit; t += 1) {
    let below=false; let above = false;
    for (let k = 0; k < 4; k += 1) {
      if (p[3 * tets[4 * t + k] + axis] < position) below = true; else above = true;
    }
    if (below && above) found.push(t);
  }
  return found;
}

// Non-indexed triangles and de-duplicated edges of the given tets
export function cellGeometry(volume, cells, quality = null) {
  const { positions: p, tets } = volume;
  const triangles = new Float32Array(cells.length * 36);
  const values = quality ? new Float32Array(cells.length * 12) : null;
  const seen = new Set();
  const edges = [];
  const nodeCount = p.length / 3;
  const FACES = [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]];
  const PAIRS = [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]];
  let out = 0;
  cells.forEach((t, index) => {
    const nodes = [tets[4 * t], tets[4 * t + 1], tets[4 * t + 2], tets[4 * t + 3]];
    for (const face of FACES) {
      for (const k of face) {
        const n = 3 * nodes[k];
        triangles[out] = p[n]; triangles[out + 1] = p[n + 1]; triangles[out + 2] = p[n + 2];
        out += 3;
      }
    }
    if (values) values.fill(quality[t], index * 12, (index + 1) * 12);
    for (const [i, j] of PAIRS) {
      const a = Math.min(nodes[i], nodes[j]); const b = Math.max(nodes[i], nodes[j]);
      const key = a * nodeCount + b;
      if (seen.has(key)) continue;
      seen.add(key);
      edges.push(p[3 * a], p[3 * a + 1], p[3 * a + 2], p[3 * b], p[3 * b + 1], p[3 * b + 2]);
    }
  });
  return { triangles, edges, values };
}

export function cellCorners(volume, t) {
  const out = [];
  for (let k = 0; k < 4; k += 1) {
    const n = 3 * volume.tets[4 * t + k];
    out.push([volume.positions[n], volume.positions[n + 1], volume.positions[n + 2]]);
  }
  return out;
}

// Red for bad cells, through amber, to a calm blue-grey for good ones: [r, g, b] in 0..1
export function qualityColor(q) {
  const stops = [[0, [0.86, 0.16, 0.14]], [POOR_QUALITY, [0.93, 0.36, 0.13]], [FAIR_QUALITY, [0.96, 0.72, 0.2]], [0.6, [0.44, 0.66, 0.74]], [1, [0.3, 0.46, 0.66]]];
  const value = Math.max(0, Math.min(1, q));
  for (let i = 1; i < stops.length; i += 1) {
    if (value <= stops[i][0]) {
      const [a, ca] = stops[i - 1]; const [b, cb] = stops[i];
      const f = (value - a) / (b - a);
      return ca.map((c, k) => c + f * (cb[k] - c));
    }
  }
  return stops.at(-1)[1];
}

const PATCH_COLORS = { inlet: "#4fa3e8", outlet: "#e8894f", slip: "#8a97a6", interface: "#c58be0", heatflux: "#e85f5f", adiabatic: "#7f8c9b" };
const WALL_COLORS = ["#a9b8c8", "#b9a98c", "#8fb59a", "#b38fb0", "#8fa9c9", "#c2a07a"];

/**
 * The boundary patches as the viewer lists them. Each CAD face of a part is its own patch, so faces are
 * merged into one "Part surface" row; flow-box sides start hidden so the part is visible.
 */
export function patchRows(patches) {
  const rows=new Map();
  let walls = 0;
  for (const patch of patches || []) {
    const merged = patch.kind === "face" || patch.kind === "named";
    const id = merged ? "part-surface" : patch.name;
    if (!rows.has(id)) {
      const color = PATCH_COLORS[patch.kind] || WALL_COLORS[walls++ % WALL_COLORS.length];
      rows.set(id, { id, label: merged ? "Part surface" : patch.name.replaceAll("_", " "), kind: patch.kind, color, hiddenByDefault: patch.kind === "slip", triangles: 0 });
    }
    rows.get(id).triangles += patch.indices.length / 3;
  }
  return [...rows.values()];
}

export function patchRowId(patch) {
  return patch.kind === "face" || patch.kind === "named" ? "part-surface" : patch.name;
}
