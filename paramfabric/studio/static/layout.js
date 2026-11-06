// Resizable side panes and movable floating windows. The arrangement is remembered per browser profile.
import { clampFloat, clampPane } from "/studio-static/controls.js";

const KEY = "paramfabric.layout";
const DEFAULTS = { left: 300, right: 352 };
const COLLAPSED = 0;

function load() {
  try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; }
}

export function createLayout(workbench) {
  const saved = load();
  const layout={ left: saved.left ?? DEFAULTS.left, right: saved.right ?? DEFAULTS.right, restore: saved.restore || {}, floats: saved.floats || {} };
  const floats = new Map();

  const save = () => {
    try { localStorage.setItem(KEY, JSON.stringify(layout)); } catch { /* private mode: the layout just lasts this session */ }
  };

  const apply = () => {
    workbench.style.setProperty("--left-w", `${layout.left}px`);
    workbench.style.setProperty("--right-w", `${layout.right}px`);
    for (const side of ["left", "right"]) {
      const grip = workbench.querySelector(`.splitter[data-side="${side}"]`);
      if (!grip) continue;
      grip.classList.toggle("collapsed", layout[side] === COLLAPSED);
      grip.setAttribute("aria-valuenow", String(layout[side]));
      grip.title = layout[side] === COLLAPSED ? `Drag or double-click to show the ${side} panel` : "Drag to resize · double-click to collapse";
    }
    workbench.querySelector(".left-panel").inert = layout.left === COLLAPSED;
    workbench.querySelector(".right-panel").inert = layout.right === COLLAPSED;
  };

  const setPane = (side, width) => {
    const other = side === "left" ? layout.right : layout.left;
    layout[side] = clampPane(width, workbench.clientWidth, other);
    apply();
  };

  const toggle = side => {
    if (layout[side] === COLLAPSED) {
      setPane(side, layout.restore[side] || DEFAULTS[side]);
    } else {
      layout.restore[side] = layout[side];
      layout[side] = COLLAPSED;
      apply();
    }
    save();
  };

  for (const side of ["left", "right"]) {
    const grip = document.createElement("div");
    grip.className = "splitter";
    grip.dataset.side = side;
    grip.tabIndex = 0;
    grip.setAttribute("role", "separator");
    grip.setAttribute("aria-orientation", "vertical");
    grip.setAttribute("aria-label", `Resize the ${side} panel`);
    workbench.append(grip);
    let drag=null;
    grip.addEventListener("pointerdown", event => {
      if (event.button !== 0) return;
      event.preventDefault();
      grip.setPointerCapture(event.pointerId);
      drag = { x: event.clientX, width: layout[side] };
      grip.classList.add("dragging");
      document.body.classList.add("resizing");
    });
    grip.addEventListener("pointermove", event => {
      if (!drag) return;
      const delta = (event.clientX - drag.x) * (side === "left" ? 1 : -1);
      setPane(side, drag.width + delta);
    });
    const end = () => {
      if (!drag) return;
      drag = null;
      grip.classList.remove("dragging");
      document.body.classList.remove("resizing");
      save();
    };
    grip.addEventListener("pointerup", end);
    grip.addEventListener("pointercancel", end);
    grip.addEventListener("dblclick", () => toggle(side));
    grip.addEventListener("keydown", event => {
      const step = event.shiftKey ? 48 : 16;
      const growKey = side === "left" ? "ArrowRight" : "ArrowLeft";
      const shrinkKey = side === "left" ? "ArrowLeft" : "ArrowRight";
      if (event.key === growKey || event.key === shrinkKey) {
        event.preventDefault();
        setPane(side, layout[side] + (event.key === growKey ? step : -step));
        save();
      } else if (event.key === "Enter") {
        toggle(side);
      }
    });
  }

  // floating windows
  const placeFloat = (element, left, top) => {
    const parent = element.parentElement;
    const place = clampFloat(left, top, element.offsetWidth, element.offsetHeight, parent.clientWidth, parent.clientHeight);
    Object.assign(element.style, { left: `${place.left}px`, top: `${place.top}px`, right: "auto", bottom: "auto" });
    return place;
  };

  const clampFloats = () => {
    for (const [id, element] of floats) {
      const spot=layout.floats[id];
      if (spot && element.offsetWidth) placeFloat(element, spot.left, spot.top);
    }
  };

  const float = (element, handle, id) => {
    floats.set(id, element);
    handle.classList.add("float-handle");
    handle.title = handle.title || "Drag to move · double-click to put back";
    let drag=null;
    handle.addEventListener("pointerdown", event => {
      if (event.button !== 0 || event.target.closest("button, select, input, a, summary")) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      drag = { x: event.clientX, y: event.clientY, left: element.offsetLeft, top: element.offsetTop };
      element.classList.add("floating-drag");
    });
    handle.addEventListener("pointermove", event => {
      if (!drag) return;
      layout.floats[id] = placeFloat(element, drag.left + event.clientX - drag.x, drag.top + event.clientY - drag.y);
    });
    const end = () => {
      if (!drag) return;
      drag = null;
      element.classList.remove("floating-drag");
      save();
    };
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
    handle.addEventListener("dblclick", event => {
      if (event.target.closest("button, select, input")) return;
      delete layout.floats[id];
      Object.assign(element.style, { left: "", top: "", right: "", bottom: "" });
      save();
    });
    // A window that was hidden when the layout loaded takes its place the first time it appears
    new MutationObserver(() => {
      const spot=layout.floats[id];
      if (spot && !element.hidden && element.offsetWidth) placeFloat(element, spot.left, spot.top);
    }).observe(element, { attributes: true, attributeFilter: ["hidden"] });
    const spot=layout.floats[id];
    if (spot && element.offsetWidth) placeFloat(element, spot.left, spot.top);
  };

  const reset = () => {
    layout.left = DEFAULTS.left;
    layout.right = DEFAULTS.right;
    layout.restore = {};
    layout.floats = {};
    for (const element of floats.values()) Object.assign(element.style, { left: "", top: "", right: "", bottom: "" });
    apply();
    save();
  };

  new ResizeObserver(() => {
    // the window shrank: panes and windows stay reachable, and the stored sizes are left alone only if they still fit
    for (const side of ["left", "right"]) if (layout[side] !== COLLAPSED) setPane(side, layout[side]);
    clampFloats();
  }).observe(workbench);

  apply();
  return { toggle, reset, float, state: () => ({ left: layout.left, right: layout.right, floats: { ...layout.floats } }) };
}
