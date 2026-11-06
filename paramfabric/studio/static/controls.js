// pure rules for how the Studio is driven: mouse schemes, command search, pane sizes, solve timing

// left click always selects and a right click always opens the menu; these set what dragging does
// Wheel zoom works in every scheme
export const MOUSE_SCHEMES = {
  paramfabric: {
    label: "Paramfabric",
    summary: "Left-drag rotates, right-drag or Shift+left-drag pans.",
    drag: [{ button: 0, shift: true, action: "pan" }, { button: 0, action: "orbit" }, { button: 2, action: "pan" }, { button: 1, shift: true, action: "zoom" }, { button: 1, action: "pan" }],
  },
  solidworks: {
    label: "SolidWorks",
    summary: "Middle-drag rotates, Ctrl+middle pans, Shift+middle zooms.",
    drag: [{ button: 1, ctrl: true, action: "pan" }, { button: 1, shift: true, action: "zoom" }, { button: 1, action: "orbit" }],
  },
  ansys: {
    label: "Ansys Mechanical",
    summary: "Middle-drag rotates, Ctrl+middle pans, Shift+middle zooms.",
    drag: [{ button: 1, ctrl: true, action: "pan" }, { button: 1, shift: true, action: "zoom" }, { button: 1, action: "orbit" }],
  },
  nx: {
    label: "Siemens NX",
    summary: "Middle-drag rotates, Shift+middle pans, Ctrl+middle zooms.",
    drag: [{ button: 1, shift: true, action: "pan" }, { button: 1, ctrl: true, action: "zoom" }, { button: 1, action: "orbit" }],
  },
  fusion: {
    label: "Autodesk Fusion",
    summary: "Middle-drag pans, Shift+middle rotates.",
    drag: [{ button: 1, shift: true, action: "orbit" }, { button: 1, action: "pan" }],
  },
};

export function mouseAction(scheme, { button, shift = false, ctrl = false }) {
  const rules=(MOUSE_SCHEMES[scheme] || MOUSE_SCHEMES.paramfabric).drag;
  for (const rule of rules) {
    if (rule.button !== button) continue;
    if (rule.shift && !shift) continue;
    if (rule.ctrl && !ctrl) continue;
    return rule.action;
  }
  return null;
}

// commands matching `query`, best first. All query words must match: whole words beat fragments.
export function rankCommands(commands, query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return commands;
  const scored = [];
  commands.forEach((command, index) => {
    const label = command.label.toLowerCase();
    const haystack = `${label} ${(command.keywords || "").toLowerCase()} ${(command.group || "").toLowerCase()}`;
    let score = 0;
    for (const word of words) {
      const at = haystack.indexOf(word);
      if (at >= 0) {
        score += label.startsWith(word) ? 50 : (at === 0 || haystack[at - 1] === " ") ? 30 : 10;
        if (at < label.length) score += 15;
      } else if (subsequence(word, haystack)) {
        score += 2;
      } else {
        return;
      }
    }
    scored.push({ command, score, index });
  });
  return scored.sort((a, b) => b.score - a.score || a.index - b.index).map(item => item.command);
}

function subsequence(needle, text) {
  let at = 0;
  for (const ch of needle) {
    at = text.indexOf(ch, at) + 1;
    if (!at) return false;
  }
  return needle.length >= 3;
}

export const PANE_MIN = 200;
export const VIEWPORT_MIN = 360;

// width a side pane may take given the window width and the other pane
export function clampPane(width, windowWidth, otherPane) {
  const most = Math.max(PANE_MIN, windowWidth - otherPane - VIEWPORT_MIN);
  return Math.round(Math.max(PANE_MIN, Math.min(most, width)));
}

// Keep a floating window fully reachable inside its parent
export function clampFloat(left, top, width, height, parentWidth, parentHeight) {
  return { left: Math.round(Math.max(0, Math.min(parentWidth - width, left))), top: Math.round(Math.max(0, Math.min(parentHeight - height, top))) };
}

function duration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  const hours = Math.floor(seconds / 3600);
  return `${hours} h ${Math.round((seconds - hours * 3600) / 60)} min`;
}

/**
 * Speed and time left, from when iterations were seen. Solves can stop early when they converge,
 * so the remaining time is an upper limit, and the text says so.
 */
export function solveProgress(samples, maxIterations) {
  if (samples.length < 2) return null;
  const recent = samples.slice(-40);
  const first = recent[0]; const last = recent.at(-1);
  const iterations = last.iteration - first.iteration;
  const seconds = last.time - first.time;
  if (iterations <= 0 || seconds <= 0) return null;
  const perIteration = seconds / iterations;
  const speed = perIteration >= 1 ? `${perIteration.toFixed(1)} s/it` : `${(1 / perIteration).toFixed(1)} it/s`;
  if (!maxIterations || last.iteration >= maxIterations) return { perIteration, text: `${last.iteration} it · ${speed}` };
  return { perIteration, remaining: (maxIterations - last.iteration) * perIteration, text: `${last.iteration} / ${maxIterations} it · ${speed} · at most ${duration((maxIterations - last.iteration) * perIteration)} left` };
}

export const UI_SCALES = [0.8, 0.9, 1, 1.1, 1.25, 1.5];
export function nextScale(current, direction) {
  const index = UI_SCALES.findIndex(value => value >= current - 1e-6);
  const at = index < 0 ? UI_SCALES.length - 1 : index;
  return UI_SCALES[Math.max(0, Math.min(UI_SCALES.length - 1, at + direction))];
}
