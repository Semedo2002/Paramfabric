// Guided engineering assistant: knowledge registry, help badges, hover cards,
// the slide-over reference, side-by-side comparisons, and the skill level
// 
// any element with data-help-id="<topic>" gets help: a "?" badge (Beginner),
// a glass hover card (Beginner and Intermediate), and the full reference on
// double-click or "Learn more". Expert hides all of it.

const $ = (selector, root = document) => root.querySelector(selector);

const SKILLS = ["beginner", "intermediate", "expert"];
const STORAGE_KEY = "paramfabric.skill-level";
const guide = { registry: null, skill: "intermediate", hoverTimer: null, hideTimer: null, onSkill: () => {} };

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}

export function topic(id) {
  return guide.registry?.topics?.[id] || null;
}

export function skillLevel() {
  return guide.skill;
}

export function comparisonFor(id) {
  if (!guide.registry) return null;
  return Object.entries(guide.registry.comparisons).find(([, comparison]) => comparison.options.includes(id))?.[0] || null;
}

// badges
const BADGE_TARGETS = "label[data-help-id], .label-line[data-help-id], .section-head[data-help-id], .bc-row[data-help-id]";

function decorate(root = document) {
  if (!guide.registry) return;
  root.querySelectorAll?.(BADGE_TARGETS).forEach(element => {
    const id = element.dataset.helpId;
    const existing=element.querySelector(":scope > .help-badge, :scope > span > .help-badge");
    if (existing) {
      existing.dataset.help = id;
      return;
    }
    if (!topic(id)) return;
    const badge = document.createElement("button");
    badge.type = "button";
    badge.className = "help-badge";
    badge.dataset.help = id;
    badge.textContent = "?";
    badge.setAttribute("aria-label", `Help: ${topic(id).title}`);
    // put the badge right after the label text (before the input inside a label.field)
    const textNode = [...element.childNodes].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    const firstSpan = element.querySelector(":scope > span");
    if (textNode && element.matches("label.field")) {
      // .field is a grid: keep the label text and its badge on one line
      const line = document.createElement("span");
      line.className = "label-line";
      textNode.replaceWith(line);
      line.append(textNode, badge);
    } else if (textNode) textNode.after(badge);
    else if (firstSpan) firstSpan.append(badge);
    else element.append(badge);
  });
}

// hover card
function tipHtml(id) {
  const entry = topic(id);
  if (!entry) return "";
  return `<span class="tip-cat">${escapeHtml(entry.category)}</span><strong>${escapeHtml(entry.title)}</strong>
    <p>${escapeHtml(entry.summary)}</p>
    ${entry.yplus ? `<div class="tip-yplus"><svg><use href="#i-ruler"/></svg><span>${escapeHtml(entry.yplus)}</span></div>` : ""}
    <div class="tip-foot"><span>Double-click for equations &amp; references</span><button type="button" class="link-btn learn" data-learn="${escapeHtml(id)}">Learn more →</button></div>`;
}

function showTip(id, anchor) {
  const tip = $("#helpTip");
  if (!tip || !topic(id)) return;
  tip.innerHTML = tipHtml(id);
  tip.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = tip.offsetWidth; const height = tip.offsetHeight;
  let left = Math.min(window.innerWidth - width - 12, Math.max(12, rect.left));
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - 12) top = Math.max(12, rect.top - height - 8);
  if (left + width > window.innerWidth) left = window.innerWidth - width - 12;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
  tip.dataset.for = id;
}

function hideTip(immediate = false) {
  clearTimeout(guide.hoverTimer);
  clearTimeout(guide.hideTimer);
  const close = () => { const tip = $("#helpTip"); if (tip) tip.hidden = true; };
  if (immediate) close(); else guide.hideTimer = setTimeout(close, 180);
}

function helpTarget(element) {
  if (guide.skill === "expert") return null;
  const badge = element.closest?.(".help-badge");
  if (badge) return badge;
  return element.closest?.("[data-help-id]") || null;
}

// slide-over reference
export function openTopic(id) {
  const entry = topic(id);
  if (!entry) return;
  hideTip(true);
  const panel = $("#helpPanel");
  $("#helpKicker").textContent = entry.category.toUpperCase();
  $("#helpTitle").textContent = entry.title;
  const comparison = comparisonFor(id);
  const list = items => `<ul>${items.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
  $("#helpBody").innerHTML = `
    <p>${escapeHtml(entry.summary)}</p>
    ${entry.available ? `<div class="availability"><svg><use href="#i-info"/></svg><span>${escapeHtml(entry.available)}</span></div>` : ""}
    ${entry.yplus ? `<section><h3>y⁺ guidance</h3><p>${escapeHtml(entry.yplus)}</p></section>` : ""}
    ${entry.formulation?.length ? `<section><h3>Formulation</h3>${entry.formulation.map(eq => `<div class="equation"><span class="eq-label">${escapeHtml(eq.label)}</span><span class="eq">${eq.html}</span></div>`).join("")}</section>` : ""}
    ${entry.use_cases?.length ? `<section><h3>Use cases</h3>${list(entry.use_cases)}</section>` : ""}
    ${entry.pitfalls?.length ? `<section><h3>Common pitfalls</h3>${list(entry.pitfalls)}</section>` : ""}
    ${entry.references?.length ? `<section class="refs"><h3>References</h3>${list(entry.references)}</section>` : ""}
    ${comparison ? `<div class="btn-row"><button type="button" class="tbtn" data-compare="${comparison}" data-chosen="${escapeHtml(id)}"><svg><use href="#i-compare"/></svg><span>Compare: ${escapeHtml(guide.registry.comparisons[comparison].title)}</span></button></div>` : ""}`;
  panel.hidden = false;
  $("#helpClose").focus();
}

function closeTopic() {
  $("#helpPanel").hidden = true;
}

// side-by-side comparison
export function openCompare(key, chosen = null) {
  const comparison = guide.registry?.comparisons?.[key];
  if (!comparison) return;
  hideTip(true);
  $("#compareTitle").textContent = comparison.title;
  const heads = comparison.options.map(id => `<th class="${id === chosen ? "chosen" : ""}" scope="col"><span class="label-line">${escapeHtml(topic(id)?.title || id)}<button type="button" class="help-badge" data-help="${escapeHtml(id)}" data-learn="${escapeHtml(id)}" aria-label="Open reference">?</button></span></th>`).join("");
  const rows = Object.entries(comparison.rows).map(([label, cells]) => `<tr><th scope="row">${escapeHtml(label)}</th>${cells.map((cell, index) => `<td class="${comparison.options[index] === chosen ? "chosen" : ""}">${escapeHtml(cell)}</td>`).join("")}</tr>`).join("");
  $("#compareBody").innerHTML = `<table class="compare-table"><thead><tr><th></th>${heads}</tr></thead><tbody>${rows}</tbody></table>`;
  const dialog = $("#compareDialog");
  if (!dialog.open) dialog.showModal();
}

export function compareButton(key, chosen = "") {
  return `<button type="button" class="mini-btn compare-btn" data-compare="${key}" data-chosen="${escapeHtml(chosen)}" title="Side-by-side comparison"><svg><use href="#i-compare"/></svg>Compare</button>`;
}

// skill level
export function setSkill(level, { silent = false } = {}) {
  if (!SKILLS.includes(level)) return;
  const previous = guide.skill;
  guide.skill = level;
  document.body.dataset.skill = level;
  document.querySelectorAll("[data-skill]").forEach(button => {
    if (button.tagName === "BUTTON") button.setAttribute("aria-checked", String(button.dataset.skill === level));
  });
  try { localStorage.setItem(STORAGE_KEY, level); } catch { /* storage unavailable: keep it for this session */ }
  if (level === "expert") hideTip(true);
  if (!silent && previous !== level) guide.onSkill(level, previous);
}

// wiring
export async function initGuide({ onSkillChange } = {}) {
  guide.onSkill = onSkillChange || guide.onSkill;
  let stored = "intermediate";
  try { stored = localStorage.getItem(STORAGE_KEY) || stored; } catch { /* default */ }
  setSkill(SKILLS.includes(stored) ? stored : "intermediate", { silent: true });
  document.querySelectorAll(".skill-toggle [data-skill]").forEach(button => button.addEventListener("click", () => setSkill(button.dataset.skill)));
  try {
    const response = await fetch("/studio-static/knowledge.json", { cache: "no-cache" });
    guide.registry = await response.json();
  } catch {
    guide.registry = { topics: {}, comparisons: {} };
  }
  decorate();
  let pending=false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; decorate(); });
  }).observe(document.body, { childList: true, subtree: true });

  document.addEventListener("mouseover", event => {
    const target = helpTarget(event.target);
    const tip = $("#helpTip");
    if (tip && !tip.hidden && event.target.closest("#helpTip")) { clearTimeout(guide.hideTimer); return; }
    if (!target) { if (tip && !tip.hidden) hideTip(); return; }
    // Intermediate: no badges, so the labelled control itself shows the card
    const id = target.dataset.help || target.dataset.helpId;
    clearTimeout(guide.hideTimer);
    clearTimeout(guide.hoverTimer);
    guide.hoverTimer = setTimeout(() => showTip(id, target), target.classList.contains("help-badge") ? 120 : 450);
  });
  document.addEventListener("focusin", event => {
    const badge = event.target.closest?.(".help-badge");
    if (badge && guide.skill !== "expert") showTip(badge.dataset.help, badge);
  });
  document.addEventListener("dblclick", event => {
    if (guide.skill === "expert") return;
    if (event.target.closest("input, select, textarea")) return;
    const target = event.target.closest(".help-badge, [data-help-id]");
    if (!target) return;
    event.preventDefault();
    openTopic(target.dataset.help || target.dataset.helpId);
  });
  document.addEventListener("click", event => {
    const learn = event.target.closest("[data-learn]");
    if (learn) { event.preventDefault(); $("#compareDialog").open && $("#compareDialog").close(); openTopic(learn.dataset.learn); return; }
    const compare = event.target.closest("[data-compare]");
    if (compare) { event.preventDefault(); openCompare(compare.dataset.compare, compare.dataset.chosen || null); return; }
    const badge = event.target.closest(".help-badge");
    if (badge) { event.preventDefault(); showTip(badge.dataset.help, badge); }
  });
  $("#helpClose").addEventListener("click", closeTopic);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") { hideTip(true); if (!$("#helpPanel").hidden) closeTopic(); }
  });
  return guide.registry;
}
