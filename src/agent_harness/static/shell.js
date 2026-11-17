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

selectSide("workspace");  // every visit starts where a task starts: the folder and its sources
followRun();
