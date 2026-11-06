// 1D beam cross-sections and 2D shell thickness: properties and to-scale SVG previews
// formulas mirror paramfabric.solvers.structural.Section (SI units throughout)

export const SECTION_TYPES = [
  ["rectangular", "Rectangular"], ["circular", "Circular"], ["i_beam", "I-beam"], ["tube", "Tube"],
];

// [key, label, default in mm]
export const SECTION_FIELDS = {
  rectangular: [["width", "Width b (local y)", 20], ["height", "Height h (local z)", 40]],
  circular: [["diameter", "Diameter d", 30]],
  tube: [["diameter", "Outer diameter D", 40], ["wall", "Wall thickness t", 3]],
  i_beam: [["height", "Height h", 100], ["flange_width", "Flange width b", 55], ["web", "Web t_w", 4.1], ["flange", "Flange t_f", 5.7]],
};

// Returns `{ area, iy, iz, j }` or `{ error }` for a spec in metres
export function sectionProperties(spec) {
  const v = key => Number(spec[key]);
  const positive = keys => keys.every(key => Number.isFinite(v(key)) && v(key) > 0);
  if (!SECTION_FIELDS[spec.type]) return { error: `unknown section type ${spec.type}` };
  if (!positive(SECTION_FIELDS[spec.type].map(([key]) => key))) return { error: "All dimensions must be positive" };
  if (spec.type === "rectangular") {
    const b=v("width"), h = v("height");
    const big = Math.max(b, h), small = Math.min(b, h);
    const j = big * small ** 3 * (1 / 3 - 0.21 * small / big * (1 - small ** 4 / (12 * big ** 4)));
    return { area: b * h, iy: b * h ** 3 / 12, iz: h * b ** 3 / 12, j };
  }
  if (spec.type === "circular") {
    const d = v("diameter"), i = Math.PI * d ** 4 / 64;
    return { area: Math.PI * d ** 2 / 4, iy: i, iz: i, j: 2 * i };
  }
  if (spec.type === "tube") {
    const d = v("diameter"), inner = d - 2 * v("wall");
    if (inner <= 0) return { error: "Wall thickness must be less than half the outer diameter" };
    const i=Math.PI * (d ** 4 - inner ** 4) / 64;
    return { area: Math.PI * (d ** 2 - inner ** 2) / 4, iy: i, iz: i, j: 2 * i };
  }
  const h = v("height"), b = v("flange_width"), tw = v("web"), tf = v("flange");
  if (!(2 * tf < h) || !(tw <= b)) return { error: "I-beam needs 2·t_f < h and t_w ≤ b" };
  const clear = h - 2 * tf;
  return {
    area: 2 * b * tf + clear * tw,
    iy: (b * h ** 3 - (b - tw) * clear ** 3) / 12,
    iz: (2 * tf * b ** 3 + clear * tw ** 3) / 12,
    j: (2 * b * tf ** 3 + (h - tf) * tw ** 3) / 3,
  };
}

const mm = value => `${+(value * 1e3).toPrecision(4)}`;

function dimension(x1, y1, x2, y2, text, offset) {
  // A dimension line offset perpendicular to the measured span, with ticks and a centred label
  const dx = x2 - x1, dy = y2 - y1, length = Math.hypot(dx, dy) || 1;
  const ux = -dy / length, uy = dx / length;
  const [ax, ay, bx, by] = [x1 + ux * offset, y1 + uy * offset, x2 + ux * offset, y2 + uy * offset];
  const out = offset + (Math.sign(offset) || 1) * 9; // label sits just outside the dimension line
  const tx = (x1 + x2) / 2 + ux * out, ty = (y1 + y2) / 2 + uy * out;
  return `<g class="dim"><line x1="${x1}" y1="${y1}" x2="${ax}" y2="${ay}"/><line x1="${x2}" y1="${y2}" x2="${bx}" y2="${by}"/>`
    + `<line x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}" marker-start="url(#dimArrow)" marker-end="url(#dimArrow)"/>`
    + `<text x="${tx}" y="${ty}" text-anchor="middle" dominant-baseline="middle">${text}</text></g>`;
}

const DEFS = '<defs><marker id="dimArrow" viewBox="0 0 8 8" refX="4" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 1 L7 4 L0 7 z"/></marker></defs>';

// To-scale cross-section drawing (local y -> right, local z -> up) with dimensions
export function sectionPreviewSvg(spec, size = 220) {
  const props=sectionProperties(spec);
  const v = key => Number(spec[key]) || 0;
  const extent = spec.type === "rectangular" ? [v("width"), v("height")]
    : spec.type === "i_beam" ? [v("flange_width"), v("height")] : [v("diameter"), v("diameter")];
  const pad = 46, box = size - 2 * pad;
  const scale = box / Math.max(...extent, 1e-9);
  const cx = size / 2, cy = size / 2;
  const w = extent[0] * scale, h = extent[1] * scale;
  const left=cx - w / 2, right = cx + w / 2, top = cy - h / 2, bottom = cy + h / 2;
  let shape = ""; let dims = "";
  if (props.error) {
    shape = `<text x="${cx}" y="${cy}" text-anchor="middle" class="err">${props.error}</text>`;
  } else if (spec.type === "rectangular") {
    shape = `<rect x="${left}" y="${top}" width="${w}" height="${h}"/>`;
    dims = dimension(left, bottom, right, bottom, `b ${mm(v("width"))}`, 16) + dimension(right, bottom, right, top, `h ${mm(v("height"))}`, 16);
  } else if (spec.type === "circular") {
    shape = `<circle cx="${cx}" cy="${cy}" r="${w / 2}"/>`;
    dims = dimension(left, cy + w / 2, right, cy + w / 2, `d ${mm(v("diameter"))}`, 16);
  } else if (spec.type === "tube") {
    const inner = (v("diameter") - 2 * v("wall")) * scale / 2;
    shape = `<path fill-rule="evenodd" d="M${cx - w / 2} ${cy} a${w / 2} ${w / 2} 0 1 0 ${w} 0 a${w / 2} ${w / 2} 0 1 0 ${-w} 0 Z M${cx - inner} ${cy} a${inner} ${inner} 0 1 0 ${2 * inner} 0 a${inner} ${inner} 0 1 0 ${-2 * inner} 0 Z"/>`;
    dims = dimension(left, cy + w / 2, right, cy + w / 2, `D ${mm(v("diameter"))}`, 16)
      + dimension(right, cy, cx + inner, cy, `t ${mm(v("wall"))}`, -14);
  } else {
    const tf = v("flange") * scale, tw = v("web") * scale;
    shape = `<path d="M${left} ${top} H${right} V${top + tf} H${cx + tw / 2} V${bottom - tf} H${right} V${bottom} H${left} V${bottom - tf} H${cx - tw / 2} V${top + tf} H${left} Z"/>`;
    dims = dimension(left, top, right, top, `b ${mm(v("flange_width"))}`, -16)
      + dimension(right, bottom, right, top, `h ${mm(v("height"))}`, 16)
      + dimension(left, bottom - tf, left, bottom, `t_f ${mm(v("flange"))}`, 14)
      + dimension(cx - tw / 2, cy, cx + tw / 2, cy, `t_w ${mm(v("web"))}`, 0.001);
  }
  const axes = `<g class="axes"><line x1="${cx}" y1="${cy}" x2="${cx + 26}" y2="${cy}"/><line x1="${cx}" y1="${cy}" x2="${cx}" y2="${cy - 26}"/><text x="${cx + 30}" y="${cy + 4}">y</text><text x="${cx - 4}" y="${cy - 30}">z</text></g>`;
  return `<svg class="section-preview" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="${spec.type} cross-section, to scale">${DEFS}<g class="shape">${shape}</g>${props.error ? "" : axes}${dims}</svg>`;
}

// side view of a shell: the selected face (dashed), the offset mid-surface and the thickness slab
export function shellPreviewSvg(thickness, offset, width = 260, height = 150) {
  const t = Math.max(Number(thickness) || 0, 0), e = Number(offset) || 0;
  const span = Math.max(t, Math.abs(e) * 2 + t, 1e-9);
  const scale = (height - 50) / span * 0.85; // the slab and the offset fill the drawing whatever their size
  const face = height / 2;
  const mid = face - e * scale;
  const half=t * scale / 2;
  const x0 = 20, x1 = width - 96; // room on the right for the labels
  const slab = `<rect class="slab" x="${x0}" y="${mid - half}" width="${x1 - x0}" height="${2 * half}"/>`;
  const lines = `<line class="face" x1="${x0 - 10}" y1="${face}" x2="${x1 + 10}" y2="${face}"/><line class="mid" x1="${x0}" y1="${mid}" x2="${x1}" y2="${mid}"/>`;
  // labels sit right of the slab, kept apart when the face and mid-surface lines are close
  const offsetShown = Math.abs(e) > 1e-12;
  const faceLabel=offsetShown && Math.abs(mid - face) < 14 ? face + (mid < face ? 12 : -6) : face + 4;
  const midLabel = offsetShown && Math.abs(mid - face) < 14 ? mid + (mid < face ? -6 : 12) : mid + 4;
  const labels = `<text x="${x1 + 16}" y="${faceLabel}" class="muted">CAD face</text>`
    + (offsetShown ? `<text x="${x1 + 16}" y="${midLabel}">mid-surface</text>` : "");
  const normal = `<g class="axes"><line x1="${x0 + 10}" y1="${face}" x2="${x0 + 10}" y2="${face - 24}" marker-end="url(#dimArrow)"/><text x="${x0 + 14}" y="${face - 20}">n</text></g>`;
  const dims = dimension((x0 + x1) / 2, mid + half, (x0 + x1) / 2, mid - half, `t ${mm(t)}`, 0.001)
    + (offsetShown ? dimension(x0 + 44, face, x0 + 44, mid, `e ${mm(e)}`, 0.001) : "");
  return `<svg class="section-preview" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="Shell thickness and offset">${DEFS}${slab}${lines}${normal}${dims}${labels}</svg>`;
}
