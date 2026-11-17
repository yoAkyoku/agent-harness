// The app shell: which sidebar section is showing, and which main tab (set up the task, or watch the Run).
// It only shows and hides parts of the page; the task logic in app.js does not know about it.
const sideButtons = [...document.querySelectorAll(".activity-button")];
const sideSections = [...document.querySelectorAll("[data-side-section]")];
const tabs = [...document.querySelectorAll(".main-tab")];
const panes = [...document.querySelectorAll("[data-main-pane]")];
const execution = document.getElementById("execution");

function selectSide(name) {
  for (const button of sideButtons) button.setAttribute("aria-pressed", String(button.dataset.side === name));
  for (const section of sideSections) {
    const shown = section.dataset.sideSection === name;
    section.classList.toggle("current", shown);
    if (section.tagName === "DETAILS") section.open = shown;  // the panels are <details>; the rail opens them
  }
}

function selectMain(name) {
  for (const tab of tabs) tab.setAttribute("aria-selected", String(tab.dataset.main === name));
  for (const pane of panes) pane.classList.toggle("current", pane.dataset.mainPane === name);
}

for (const button of sideButtons) button.addEventListener("click", () => selectSide(button.dataset.side));
for (const tab of tabs) tab.addEventListener("click", () => { if (!tab.disabled) selectMain(tab.dataset.main); });
// A panel's <summary> is its heading here, not a toggle.
for (const section of sideSections) {
  if (section.tagName === "DETAILS") section.querySelector("summary").addEventListener("click", (event) => event.preventDefault());
}

// Other code opens a panel to point at it (a routine was just saved); bring that panel forward.
for (const section of sideSections) {
  if (section.tagName === "DETAILS") section.addEventListener("toggle", () => {
    if (section.open && !section.classList.contains("current")) selectSide(section.dataset.sideSection);
  });
}

// app.js shows the Run section when there is a Run to show and hides it for a new task; the tabs follow.
function followRun() {
  const running = !execution.hidden;
  document.getElementById("tab-run").disabled = !running;
  selectMain(running ? "run" : "compose");
}
new MutationObserver(followRun).observe(execution, {attributes: true, attributeFilter: ["hidden"]});

// The two splitters: drag, or focus one and use the arrow keys; a double click (or Enter) goes back to the default width.
// A width is a custom property on the shell, so the stylesheet still decides the layout (and ignores it when stacked).
const app = document.querySelector(".app");
const sizeKey = "agent-harness.pane-widths.v1";
const RAIL = 76, MAIN_MIN = 420;
const SPLITS = {
  side: {panel: document.querySelector(".sidebar"), property: "--side-width", min: 200, max: 520, sign: 1},
  inspect: {panel: document.querySelector(".preview-panel"), property: "--inspect-width", min: 280, max: 900, sign: -1},
};
let widths = {};
try { widths = JSON.parse(localStorage.getItem(sizeKey) || "{}") || {}; } catch { widths = {}; }
const widthOf = (name) => SPLITS[name].panel.getBoundingClientRect().width;
const stacked = () => matchMedia("(max-width:1180px)").matches;  // the preview sits under the task area

function limit(name, width) {
  const split = SPLITS[name];
  const other = stacked() ? 0 : widthOf(name === "side" ? "inspect" : "side");
  const room = innerWidth - RAIL - MAIN_MIN - other;  // the task area keeps its minimum width
  return Math.round(Math.max(split.min, Math.min(width, split.max, room)));
}

function setWidth(name, width) {
  const split = SPLITS[name];
  if (width === null || !Number.isFinite(width)) { app.style.removeProperty(split.property); delete widths[name]; }
  else { widths[name] = limit(name, width); app.style.setProperty(split.property, `${widths[name]}px`); }
  try { localStorage.setItem(sizeKey, JSON.stringify(widths)); } catch { /* the widths are only a convenience */ }
  const handle = document.querySelector(`[data-split="${name}"]`);
  handle.setAttribute("aria-valuemin", String(split.min));
  handle.setAttribute("aria-valuemax", String(split.max));
  handle.setAttribute("aria-valuenow", String(Math.round(widthOf(name))));
}

for (const handle of document.querySelectorAll(".splitter")) {
  const name = handle.dataset.split, sign = SPLITS[name].sign;
  let start = null;
  const stop = () => { start = null; handle.classList.remove("dragging"); document.body.classList.remove("resizing"); };
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    start = {x: event.clientX, width: widthOf(name)};
    handle.classList.add("dragging"); document.body.classList.add("resizing");
  });
  handle.addEventListener("pointermove", (event) => { if (start) setWidth(name, start.width + sign * (event.clientX - start.x)); });
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
  handle.addEventListener("dblclick", () => setWidth(name, null));
  handle.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      setWidth(name, widthOf(name) + sign * (event.key === "ArrowRight" ? 16 : -16));
    } else if (event.key === "Enter") setWidth(name, null);
  });
}
const applyWidths = () => { for (const name of Object.keys(SPLITS)) setWidth(name, name in widths ? Number(widths[name]) : null); };
addEventListener("resize", applyWidths);  // a smaller window takes width back from the panels, not from the task area
applyWidths();

selectSide("workspace");  // every visit starts where a task starts: the folder and its sources
followRun();
