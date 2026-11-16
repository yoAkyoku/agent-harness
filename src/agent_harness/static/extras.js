// Workbench parts added around the Run view: the compact activity list, the actions on a finished Run
// (save as routine, draft a skill, download the trace) and the routines panel.
import {t} from "/assets/i18n.js?v=1.7";

const $ = (id) => document.getElementById(id);
const showAllKey = "agent-harness.show-all-events.v1";
// Events that stay visible in the compact view; tool calls are shown as one step row each instead.
const KEPT = new Set(["correction.available", "context.compacted", "context.compaction_failed", "context.limited",
  "message.delivered", "completion.recovery_requested", "publication.recovery_requested", "checkpoint.loaded",
  "recovery.detected", "answer.verified", "approval.requested", "approval.approved", "approval.denied", "approval.expired"]);
const FINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function initActivity() {
  const box = $("show-all-events");
  let showAll = false;
  try { showAll = localStorage.getItem(showAllKey) === "1"; } catch { showAll = false; }
  box.checked = showAll;
  $("timeline").classList.toggle("compact", !showAll);
  box.addEventListener("change", () => {
    $("timeline").classList.toggle("compact", !box.checked);
    try { localStorage.setItem(showAllKey, box.checked ? "1" : "0"); } catch { /* the preference is optional */ }
  });
}

function hint(argumentsOf) {
  const value = argumentsOf?.path || argumentsOf?.name || argumentsOf?.query || argumentsOf?.url;
  if (typeof value === "string") return value.length > 60 ? `${value.slice(0, 57)}…` : value;
  if (Array.isArray(argumentsOf?.steps)) return t("{count} 個步驟", {count: argumentsOf.steps.length});
  if (Array.isArray(argumentsOf?.sources)) return argumentsOf.sources.join(", ");
  return "";
}

function setStatus(row, text, kind) {
  const status = row.querySelector(".step-status");
  status.textContent = text;
  row.classList.remove("running", "done", "failure");
  if (kind) row.classList.add(kind);
}

function subStep(parent, text, failed) {
  let list = parent.querySelector(".substeps");
  if (!list) { list = element("ul", "substeps"); parent.append(list); }
  list.append(element("li", failed ? "failure" : "", text));
}

// One row per tool call, updated as its events arrive; a subagent's steps are listed under the call that started it.
export function noteEvent(event, toolNames, rawRow) {
  rawRow.classList.add("raw");
  if (KEPT.has(event.kind)) rawRow.classList.add("keep");
  const data = event.data || {};
  const label = (name) => toolNames[name] || name || "";
  if (event.kind === "tool.requested") {
    const row = element("li", "event step running");
    row.id = `step-${data.call_id}`;
    const line = element("div", "step-line");
    const detail = hint(data.arguments);
    line.append(element("span", "step-status", t("準備中")), element("span", "event-title", detail ? `${label(data.tool)} · ${detail}` : label(data.tool)),
      element("time", "event-time", new Date(event.timestamp).toLocaleTimeString(undefined, {hour12: false})));
    row.append(line);
    $("timeline").append(row);
    return;
  }
  const row = document.getElementById(`step-${data.call_id}`);
  if (!row) return;
  if (event.kind === "tool.dispatched") setStatus(row, t("執行中"), "running");
  else if (["tool.completed", "tool.reused", "tool.reconciled"].includes(event.kind)) {
    const evidence = data.result?.evidence_id;
    setStatus(row, evidence ? t("完成 · {id}", {id: evidence}) : t("完成"), "done");
  } else if (["tool.failed", "tool.rejected"].includes(event.kind)) setStatus(row, t("被退回 · {code}", {code: data.code}), "failure");
  else if (event.kind === "subagent.started") subStep(row, t("子代理：{task}", {task: String(data.task || "").slice(0, 100)}));
  else if (event.kind === "subagent.tool_completed") subStep(row, t("子代理完成：{tool}", {tool: label(data.tool)}));
  else if (event.kind === "subagent.tool_refused") subStep(row, t("子代理被退回：{tool} · {code}", {tool: label(data.tool), code: data.code}), true);
  else if (event.kind === "subagent.completed") subStep(row, t("子代理的報告已通過引用驗證"));
  else if (event.kind === "subagent.failed") subStep(row, t("子代理未產生可驗證的報告：{reason}", {reason: data.reason}), true);
}

// What a Run request looked like, rebuilt from the saved Run, for saving it as a routine.
function requestFromRun(run) {
  const policy = run.policy || {};
  const body = {task: run.task, sources: run.sources.map((source) => source.path), limits: run.limits,
    require_publish_approval: Boolean(policy.require_publish_approval),
    publication_workflow: policy.publication_workflow || "direct", completion_workflow: policy.completion_workflow || "model_summary"};
  for (const flag of ["require_plan_approval", "web_access", "python_sandbox", "subagents", "context_compaction", "source_search"]) {
    if (policy[flag]) body[flag] = true;
  }
  if (policy.project_instructions) body.project_instructions = true;
  if (policy.mcp_tools?.length) body.mcp_tools = policy.mcp_tools;
  if (policy.skills?.length) body.skills = policy.skills;
  return body;
}

let actionRun = null;
let host = null;

export function renderRunActions(run) {
  const finished = Boolean(run) && FINAL.has(run.status);
  $("run-actions").hidden = !finished;
  if (!finished) { $("skill-draft-card").hidden = true; return; }
  if (actionRun?.id !== run.id) {
    $("skill-draft-card").hidden = true; $("run-action-feedback").textContent = "";
    $("routine-name").value = String(run.task || "").replace(/\s+/g, " ").slice(0, 60);
    $("skill-name").value = "";
  }
  actionRun = run;
  const completed = run.status === "completed";
  for (const id of ["routine-name", "save-routine", "skill-name", "draft-skill"]) $(id).disabled = !completed;
  $("download-trace").href = `/api/runs/${run.id}/trace`;
  $("download-trace").download = `trace-${run.id}.json`;
}

async function saveRoutine() {
  const run = actionRun, workspace = host.getWorkspace();
  const name = $("routine-name").value.trim();
  if (!run || !workspace || !name) { $("run-action-feedback").textContent = t("請先輸入 routine 的名稱。"); return; }
  try {
    await host.api("/api/routines", {workspace_id: workspace.id, name, run: requestFromRun(run)});
    $("run-action-feedback").textContent = t("已存成 routine「{name}」；可在左側的 Routines 觸發或暫停。", {name});
    await refreshRoutines();
    $("routines-panel").open = true;
  } catch (error) { host.showError(error); }
}

async function draftSkill() {
  const run = actionRun, name = $("skill-name").value.trim();
  if (!run) return;
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name)) { $("run-action-feedback").textContent = t("Skill 名稱請用小寫英數與連字號，例如 weekly-report。"); return; }
  try {
    const draft = await host.api(`/api/runs/${run.id}/skill-draft?name=${encodeURIComponent(name)}`);
    $("skill-draft-text").textContent = draft.content;
    $("skill-draft-path").textContent = t("草稿內容如下；確認並修改後，存成 skills 資料夾下的 {file}。工作台不會替你寫入。", {file: draft.file});
    const link = $("skill-draft-download");
    if (link.href.startsWith("blob:")) URL.revokeObjectURL(link.href);
    link.href = URL.createObjectURL(new Blob([draft.content], {type: "text/markdown"}));
    link.download = "SKILL.md";
    $("skill-draft-card").hidden = false; $("run-action-feedback").textContent = "";
  } catch (error) { host.showError(error); }
}

async function routineAction(routine, action) {
  try {
    if (action === "delete") {
      const response = await fetch(`/api/routines/${routine.id}`, {method: "DELETE"});
      if (!response.ok) throw new Error(t("無法刪除這個 routine。"));
    } else {
      await host.api(`/api/routines/${routine.id}/${action}`, {});
    }
    $("routine-feedback").textContent = action === "trigger" ? t("已啟動「{name}」；按「查看紀錄」可以看它的執行過程。", {name: routine.name}) : "";
  } catch (error) { $("routine-feedback").textContent = error.message || String(error); }
  await refreshRoutines();
}

export async function refreshRoutines() {
  if (!host) return;
  let routines = [];
  try { routines = await host.api("/api/routines"); } catch (error) { host.showError(error); return; }
  const workspace = host.getWorkspace();
  const shown = workspace ? routines.filter((routine) => routine.workspace_id === workspace.id) : routines;
  $("routine-count").textContent = String(shown.length);
  $("routine-empty").hidden = shown.length > 0;
  $("routine-list").replaceChildren(...shown.map((routine) => {
    const item = element("li", "routine");
    const last = routine.last_run;
    const state = routine.paused ? t("已暫停") : last ? `${host.statusNames[last.status] || last.status} · ${new Date(last.created_at).toLocaleString()}` : t("尚未執行");
    item.append(element("strong", "", routine.name), element("span", "sidebar-note", state));
    const buttons = element("div", "routine-actions");
    const add = (text, action, disabled) => {
      const button = element("button", "subtle-button", text);
      button.type = "button"; button.disabled = Boolean(disabled);
      button.addEventListener("click", () => action());
      buttons.append(button);
    };
    add(t("立即執行"), () => routineAction(routine, "trigger"), routine.paused);
    add(routine.paused ? t("恢復") : t("暫停"), () => routineAction(routine, routine.paused ? "resume" : "pause"));
    add(t("查看紀錄"), async () => {
      try {
        const sessions = await host.api("/api/sessions");
        const session = sessions.find((entry) => entry.id === routine.session_id);
        if (session) await host.openSession(session);
      } catch (error) { host.showError(error); }
    }, !last);
    let armed = false;
    const remove = element("button", "subtle-button", t("刪除"));
    remove.type = "button";
    remove.addEventListener("click", () => {  // two clicks: the first only arms it
      if (armed) { routineAction(routine, "delete"); return; }
      armed = true; remove.textContent = t("再按一次以刪除");
    });
    buttons.append(remove);
    item.append(buttons);
    return item;
  }));
}

export function initExtras(dependencies) {
  host = dependencies;
  initActivity();
  $("save-routine").addEventListener("click", saveRoutine);
  $("draft-skill").addEventListener("click", draftSkill);
  $("routines-panel").addEventListener("toggle", () => { if ($("routines-panel").open) refreshRoutines(); });
}
