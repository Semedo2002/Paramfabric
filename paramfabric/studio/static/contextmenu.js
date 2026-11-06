// Generic right-click menu: nested submenus, viewport clamping, full keyboard control
// items come from menus.js; onCommand(item) runs when one is chosen

let closeActive = null;
let dismissing = false;

// true once if this click only closed a menu: the click should not also change the selection
export function consumeDismissal() {
  const value = dismissing;
  dismissing = false;
  return value;
}

export function closeContextMenu() {
  closeActive?.();
}

export function contextMenuOpen() {
  return Boolean(closeActive);
}

function place(menu, x, y, anchor = null) {
  const pad = 6;
  const { width, height } = menu.getBoundingClientRect();
  let left = x; let top = y;
  if (anchor) {
    left = anchor.right - 2;
    top = anchor.top - 5;
    if (left + width > window.innerWidth - pad) left = anchor.left - width + 2;
  } else if (left + width > window.innerWidth - pad) {
    left = x - width;
  }
  if (top + height > window.innerHeight - pad) top = window.innerHeight - height - pad;
  menu.style.left = `${Math.max(pad, left)}px`;
  menu.style.top = `${Math.max(pad, top)}px`;
}

export function openContextMenu(items, x, y, onCommand) {
  closeContextMenu();
  const restoreFocus = document.activeElement;
  const levels = [];

  const closeFrom = depth => {
    while (levels.length > depth) levels.pop().remove();
    levels.at(-1)?.querySelector(".ctx-item.open")?.classList.remove("open");
  };
  const close = () => {
    closeFrom(0);
    document.removeEventListener("pointerdown", outside, true);
    document.removeEventListener("keydown", keys, true);
    window.removeEventListener("blur", close);
    window.removeEventListener("resize", close);
    closeActive = null;
    if (restoreFocus?.isConnected) restoreFocus.focus?.({ preventScroll: true });
  };
  const outside = event => {
    if (levels.some(menu => menu.contains(event.target))) return;
    dismissing = true;
    window.addEventListener("pointerup", () => setTimeout(() => { dismissing = false; }, 0), { once: true });
    close();
  };
  const run = item => { close(); onCommand(item); };

  const enabled=menu => [...menu.querySelectorAll(":scope > .ctx-item:not([aria-disabled='true'])")];
  const openSub = (button, item, depth, focusFirst) => {
    closeFrom(depth + 1);
    button.classList.add("open");
    button.setAttribute("aria-expanded", "true");
    const sub = build(item.items, depth + 1);
    place(sub, 0, 0, button.getBoundingClientRect());
    if (focusFirst) enabled(sub)[0]?.focus();
  };

  function build(list, depth) {
    const menu=document.createElement("div");
    menu.className = "ctx-menu";
    menu.setAttribute("role", "menu");
    let hoverTimer;
    for (const item of list) {
      if (item.separator) {
        menu.append(Object.assign(document.createElement("div"), { className: "ctx-sep", role: "separator" }));
      } else if (item.header !== undefined) {
        const head = document.createElement("div");
        head.className = "ctx-head";
        head.append(Object.assign(document.createElement("strong"), { textContent: item.header }));
        if (item.sub) head.append(Object.assign(document.createElement("span"), { textContent: item.sub }));
        menu.append(head);
      } else {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `ctx-item${item.danger ? " danger" : ""}`;
        const hasSub = Boolean(item.items?.length);
        const checkable = item.checked !== undefined;
        button.setAttribute("role", checkable ? "menuitemcheckbox" : "menuitem");
        if (checkable) button.setAttribute("aria-checked", String(Boolean(item.checked)));
        if (hasSub) { button.setAttribute("aria-haspopup", "menu"); button.setAttribute("aria-expanded", "false"); }
        if (item.disabled) { button.setAttribute("aria-disabled", "true"); button.tabIndex = -1; }
        const tick = Object.assign(document.createElement("span"), { className: "ctx-tick", textContent: item.checked ? "✓" : "" });
        const label = Object.assign(document.createElement("span"), { className: "ctx-label", textContent: item.label });
        const tail = Object.assign(document.createElement("span"), { className: "ctx-tail", textContent: hasSub ? "▸" : item.shortcut || "" });
        button.append(tick, label, tail);
        button.addEventListener("pointerenter", () => {
          clearTimeout(hoverTimer);
          if (item.disabled) { closeFrom(depth + 1); return; }
          button.focus({ preventScroll: true });
          hoverTimer = setTimeout(() => (hasSub ? openSub(button, item, depth, false) : closeFrom(depth + 1)), 110);
        });
        button.addEventListener("click", event => {
          event.stopPropagation();
          if (item.disabled) return;
          if (hasSub) openSub(button, item, depth, false);
          else run(item);
        });
        button.addEventListener("keydown", event => {
          if ((event.key === "ArrowRight" || event.key === "Enter" || event.key === " ") && hasSub && !item.disabled) {
            event.preventDefault();
            event.stopPropagation();
            openSub(button, item, depth, true);
          }
        });
        menu.append(button);
      }
    }
    document.body.append(menu);
    levels[depth] = menu;
    return menu;
  }

  function keys(event) {
    const menu = levels.at(-1);
    const list = enabled(menu);
    const index = list.indexOf(document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation();
      if (levels.length > 1) {
        const parent=levels.at(-2).querySelector(".ctx-item.open");
        closeFrom(levels.length - 1);
        parent?.focus();
      } else close();
    } else if (event.key === "ArrowDown") {
      event.preventDefault(); event.stopPropagation();
      list[(index + 1) % list.length]?.focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault(); event.stopPropagation();
      list[(index - 1 + list.length) % list.length]?.focus();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault(); event.stopPropagation();
      list[event.key === "Home" ? 0 : list.length - 1]?.focus();
    } else if (event.key === "ArrowLeft" && levels.length > 1) {
      event.preventDefault(); event.stopPropagation();
      const parent=levels.at(-2).querySelector(".ctx-item.open");
      closeFrom(levels.length - 1);
      parent?.focus();
    } else if (event.key === "Tab") {
      event.preventDefault(); close();
    } else if (!["ArrowRight", "Enter", " "].includes(event.key)) {
      event.stopPropagation(); // the Studio's single-key shortcuts stay quiet while a menu is open
    }
  }

  const root = build(items, 0);
  place(root, x, y);
  enabled(root)[0]?.focus({ preventScroll: true });
  document.addEventListener("pointerdown", outside, true);
  document.addEventListener("keydown", keys, true);
  window.addEventListener("blur", close);
  window.addEventListener("resize", close);
  closeActive = close;
  return root;
}
