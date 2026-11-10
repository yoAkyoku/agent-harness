import {observeEvents} from "/assets/run-stream.js?v=7.2";
import {memoryPanel} from "/assets/memory.js?v=6.2";
import {lang, saveLang, t, translatePage} from "/assets/i18n.js?v=1.1";
const $ = (id) => document.getElementById(id);
translatePage();
const locale = lang === "en" ? "en-US" : "zh-TW";
const state = {config: null, workspace: null, session: null, run: null, approval: null, sources: [], selected: new Set(), busy: false, socket: null, generation: 0, approvalPending: false, cancelPending: false, runs: [], base: null, previewArtifact: null, pendingSubmission: null, initializing: true, planSequence: 0, correctionSequence: 0, mcpSelected: new Set(), skillSelected: new Set()};
const toolNames = {workspace_list: t("列出允許的來源"), source_read: t("讀取來源"), base_artifact_read: t("讀取修改基底"), artifact_write: t("保存成果"), artifact_draft: t("保存待核對草稿（尚未發布）"), plan_update: t("更新計畫"), table_inspect: t("檢查表格"), table_aggregate: t("計算欄位"), number_calculate: t("計算比例"), task_finish: t("提交完成回覆")};
const statusNames = {queued: t("等待執行"), running: t("執行中"), waiting_approval: t("等待你的確認"), cancel_requested: t("正在取消"), cancelled: t("已取消"), completed: t("已完成"), failed: t("未完成"), interrupted: t("執行已中斷")};
const effortNames = {low: t("低"), medium: t("中"), high: t("高"), xhigh: t("很高"), max: t("最高")};
const activeStatuses = new Set(["queued", "running", "waiting_approval", "cancel_requested"]);

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) {
    const detail = value.detail;
    const message = typeof detail === "string" ? detail : detail?.message || (Array.isArray(detail) ? detail.map((item) => item.msg).join(t("；")) : t("請重新確認輸入。"));
    const error = new Error(message); error.status = response.status; throw error;
  }
  return value;
}
function showError(error) { $("error").textContent = error.message || String(error); $("error").hidden = false; }
const memories = memoryPanel(api, showError);
function clearError() { $("error").hidden = true; }
function bytes(value) { return value < 1024 ? `${value} bytes` : `${(value / 1024).toFixed(1)} KiB`; }
function updateControls() {
  $("resume-run").disabled = state.initializing || state.busy || Boolean(state.pendingSubmission) || !state.checkpoint || Boolean(state.checkpoint.blockers.length);
  const locked = state.initializing || state.busy || Boolean(state.pendingSubmission);
  memories.setBusy(locked);
  $("start-task").disabled = state.initializing || state.busy || (!state.pendingSubmission && (!state.workspace || !state.selected.size));
  $("start-task").firstChild.textContent = (state.busy ? t("執行中") : state.pendingSubmission ? t("重試這次提交") : state.base ? t("建立修改版本") : t("開始任務")) + " ";
  for (const id of ["new-task", "lang-select", "upload-files", "use-demo", "workspace-path", "task-input", "require-plan-approval", "require-publish-approval", "publication-workflow", "completion-workflow", "run-history", "base-artifact", "revise-artifact"]) $(id).disabled = locked;
  $("workspace-form").querySelector("button").disabled = locked;
  document.querySelectorAll(".session-button").forEach((button) => { button.disabled = locked; });
  document.querySelectorAll(".source-item input").forEach((input) => { input.disabled = locked || (state.selected.size >= 8 && !input.checked); });
  document.querySelectorAll(".mcp-tool input, .skill-item input").forEach((input) => { input.disabled = locked; });
  $("use-instructions").disabled = locked || !state.instructions?.instructions;
  for (const id of ["model-select", "effort-select"]) $(id).disabled = locked;
  $("source-count").textContent = `${state.selected.size} / 8`;
  $("selection-hint").textContent = state.pendingSubmission ? t("提交結果尚未確認；重試會查回同一次執行") : state.selected.size ? t("本次允許讀取 {count} 份來源", {count: state.selected.size}) : t("先從左側選擇資料來源");
}
function closeStream() { state.generation += 1; if (state.socket) { state.socket.close(); state.socket = null; } }
function renderPlanContext() {
  const laterCorrection = state.correctionSequence > state.planSequence;
  $("plan-title").textContent = laterCorrection ? t("較早提出的計畫") : t("模型提出的計畫");
  $("plan-context").textContent = laterCorrection
    ? t("此計畫提出後曾有錯誤回饋或修正；請對照後續事件與成果。")
    : t("計畫是預計動作；工具是否成功、引用是否有效，以實際結果為準。");
}
function resetPlan() {
  state.planSequence = 0; state.correctionSequence = 0;
  $("plan").hidden = true; $("plan-steps").replaceChildren(); renderPlanContext();
}
function hideDiff() { $("preview-diff").hidden = true; $("preview-text").hidden = false; $("compare-artifact").textContent = t("與上一版比較"); }
function resetPreview() { state.previewArtifact = null; $("revise-artifact").hidden = true; $("compare-artifact").hidden = true; hideDiff(); $("preview-empty").hidden = false; $("preview-content").hidden = true; $("preview-heading").textContent = t("成果預覽"); $("preview-kind").textContent = t("等待成果"); }
function newTask() {
  if (state.initializing || state.busy || state.pendingSubmission) return;
  closeStream(); state.session = null; state.run = null; state.approval = null; state.runs = []; state.base = null;
  $("session-tools").hidden = true; $("session-usage").hidden = true; $("publication-workflow").value = "direct";
  $("completion-workflow").value = "model_summary";
  state.approvalPending = false; state.cancelPending = false;
  $("approval-card").hidden = true; $("require-publish-approval").checked = true; $("require-plan-approval").checked = false;
  $("use-instructions").checked = Boolean(state.instructions?.instructions);
  $("execution").hidden = true; resetPlan(); $("timeline").replaceChildren(); resetPreview(); clearError();
  $("task-input").value = t("讀取已選來源，保存一份可檢查的來源快照。");
  selectSkills();
  document.querySelectorAll(".session-button").forEach((button) => button.classList.remove("active"));
  updateControls(); $("task-input").focus();
}
async function loadWorkspace(path) {
  clearError();
  const workspace = await api("/api/workspaces", {path});
  const catalog = await api(`/api/workspaces/${workspace.id}/sources`);
  state.instructions = await api(`/api/workspaces/${workspace.id}/instructions`);
  newTask(); state.workspace = workspace; state.sources = catalog.sources;
  state.selected = new Set(catalog.sources.slice(0, 8).map((source) => source.path));
  $("workspace-path").value = workspace.path; $("workspace-label").textContent = workspace.name;
  $("workspace-breadcrumb").textContent = t("{workspace} / 新的工作", {workspace: workspace.name});
  $("upload-area").hidden = false;
  renderInstructions(); renderSources(); updateControls(); await memories.load(workspace);
}
// Saves each chosen file into the workspace's uploads/ folder (never overwriting), then selects it as a source.
async function uploadFiles(files) {
  const workspace = state.workspace;
  if (!workspace || !files.length || state.busy) return;
  clearError(); const added = [];
  try {
    for (const file of files) {
      const response = await fetch(`/api/workspaces/${workspace.id}/uploads?name=${encodeURIComponent(file.name)}`,
        {method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: file});
      const value = await response.json();
      if (!response.ok) throw new Error(t("{name} 無法上傳：{reason}", {name: file.name, reason: value.detail?.message || value.detail || response.status}));
      added.push(value.path);
    }
  } catch (error) { showError(error); }
  $("upload-input").value = "";
  if (state.workspace?.id !== workspace.id) return;
  state.sources = (await api(`/api/workspaces/${workspace.id}/sources`)).sources;
  for (const path of added) if (state.selected.size < 8) state.selected.add(path);
  renderSources(); updateControls();
}
// The workspace's AGENTS.md: applied only when ticked, pinned into the Run when it starts.
function renderInstructions() {
  const offered = state.instructions?.instructions, problem = state.instructions?.error;
  $("project-instructions").hidden = !offered && !problem;
  $("instructions-details").hidden = !offered;
  $("instructions-text").textContent = offered?.text || "";
  $("instructions-size").textContent = offered ? t("（{bytes} bytes）", {bytes: offered.bytes}) : "";
  $("instructions-problem").hidden = !problem;
  $("instructions-problem").textContent = problem ? t("無法套用：{message}", {message: problem.message}) : "";
  $("use-instructions").checked = Boolean(offered);
}
function renderMcpTools() {
  const servers = Object.entries(state.config?.mcp?.servers || {});
  $("mcp-tools").hidden = !servers.length;
  const list = $("mcp-tool-list"); list.replaceChildren();
  for (const [name, server] of servers) {
    if (server.status === "needs_sign_in" || server.status === "connecting") {
      // OAuth: the user signs in on the provider's page; this list refreshes until the server connects.
      const note = document.createElement("p"); note.className = "mcp-unavailable";
      note.textContent = server.status === "connecting" ? t("MCP 伺服器 {name} 連線中…", {name}) : t("MCP 伺服器 {name} 需要登入。", {name});
      if (server.authorization_url) {
        const link = document.createElement("a"); link.href = server.authorization_url; link.target = "_blank"; link.rel = "noopener noreferrer";
        link.textContent = t("登入 {name}", {name}); note.append(" ", link);
      }
      list.append(note); watchMcpSignIn(); continue;
    }
    if (server.status !== "connected") {
      const note = document.createElement("p"); note.className = "mcp-unavailable";
      note.textContent = t("MCP 伺服器 {name} 目前無法使用", {name}) + (server.error ? t("：{error}", {error: server.error}) : ""); list.append(note); continue;
    }
    for (const tool of server.tools) {
      const label = document.createElement("label"); label.className = "mcp-tool";
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.value = tool.name;
      checkbox.checked = state.mcpSelected.has(tool.name);
      checkbox.addEventListener("change", () => { if (checkbox.checked) state.mcpSelected.add(tool.name); else state.mcpSelected.delete(tool.name); });
      const title = document.createElement("strong"); title.textContent = tool.name;
      const access = document.createElement("span"); access.className = tool.approval === "required" ? "mcp-access ask" : "mcp-access";
      access.textContent = tool.approval === "required" ? t("每次需核准") : t("唯讀");
      const detail = document.createElement("small"); detail.textContent = tool.description || t("（伺服器未提供說明）");
      label.append(checkbox, title, access, detail); list.append(label);
    }
  }
  updateControls();
}
let mcpWatch = null;
function watchMcpSignIn() {
  if (mcpWatch) return;
  mcpWatch = setTimeout(async () => {
    mcpWatch = null;
    try { state.config = await api("/api/config"); renderMcpTools(); } catch { watchMcpSignIn(); }
  }, 3000);
}
function selectMcpTools(names) { state.mcpSelected = new Set(names || []); renderMcpTools(); }
function renderModelChoices() {
  const model = state.config?.model || {}, choices = model.choices || {models: [], efforts: []};
  const visible = choices.models.length > 1 || choices.efforts.length > 0;
  $("model-choice").hidden = !visible;
  $("model-select").replaceChildren(...choices.models.map((name) => new Option(name, name)));
  $("effort-select").replaceChildren(...choices.efforts.map((name) => new Option(effortNames[name] || name, name)));
  $("effort-select").hidden = $("effort-label").hidden = !choices.efforts.length;
  selectModel(model.name, model.effort);
}
// Show the model and effort a Run used, or the workbench defaults for a new task.
function selectModel(name, effort) {
  if (name && [...$("model-select").options].some((option) => option.value === name)) $("model-select").value = name;
  if (effort && [...$("effort-select").options].some((option) => option.value === effort)) $("effort-select").value = effort;
}
function availableSkills() { return state.config?.skills?.available || []; }
function renderSkills() {
  const skills = availableSkills();
  $("skills").hidden = !skills.length;
  const list = $("skill-list"); list.replaceChildren();
  for (const skill of skills) {
    const label = document.createElement("label"); label.className = "mcp-tool skill-item";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.value = skill.name;
    checkbox.checked = state.skillSelected.has(skill.name);
    checkbox.addEventListener("change", () => { if (checkbox.checked) state.skillSelected.add(skill.name); else state.skillSelected.delete(skill.name); });
    const title = document.createElement("strong"); title.textContent = `/${skill.name}`;
    const detail = document.createElement("small"); detail.textContent = skill.description;
    label.append(checkbox, title, document.createElement("span"), detail); list.append(label);
  }
  updateControls();
}
// A new task offers every installed skill; earlier Runs restore their own selection.
function selectSkills(names) { state.skillSelected = new Set(names ?? availableSkills().map((skill) => skill.name)); renderSkills(); }
function renderSources() {
  $("sources").replaceChildren();
  if (!state.sources.length) {
    const message = document.createElement("p"); message.className = "sidebar-note";
    message.textContent = t("找不到可用來源。請加入 UTF-8 的 .md、.txt、.json（每份不超過 256 KiB）、CSV 或 Excel 表格（每份不超過 20 MiB），或 PDF、Word、PowerPoint（每份不超過 10 MiB）；圖片需以 --tesseract 啟用 OCR。");
    $("sources").append(message); return;
  }
  for (const [index, source] of state.sources.entries()) {
    const row = document.createElement("div"); row.className = "source-item";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.id = `source-${index}`; checkbox.checked = state.selected.has(source.path);
    checkbox.addEventListener("change", () => { if (checkbox.checked) state.selected.add(source.path); else state.selected.delete(source.path); updateControls(); });
    const label = document.createElement("label"); label.htmlFor = checkbox.id; label.textContent = source.path;
    const size = document.createElement("small"); size.textContent = bytes(source.bytes); label.append(size);
    const preview = document.createElement("button"); preview.type = "button"; preview.className = "source-preview"; preview.textContent = t("查看"); preview.setAttribute("aria-label", t("查看來源 {path}", {path: source.path}));
    preview.addEventListener("click", () => previewSource(source).catch(showError));
    row.append(checkbox, label, preview); $("sources").append(row);
  }
}
async function previewSource(source) {
  const value = await api(`/api/workspaces/${state.workspace.id}/source?path=${encodeURIComponent(source.path)}`);
  state.previewArtifact = null; $("revise-artifact").hidden = true; $("compare-artifact").hidden = true; hideDiff();
  $("preview-heading").textContent = t("來源預覽"); $("preview-kind").textContent = t("任務輸入");
  $("preview-empty").hidden = true; $("preview-content").hidden = false;
  $("preview-name").textContent = source.path; $("preview-meta").textContent = `${bytes(value.bytes)} / SHA-256 ${value.sha256.slice(0, 12)}`;
  $("preview-text").textContent = value.text; $("download").hidden = true; $("artifact-list").replaceChildren();
}
async function refreshSessions() {
  const sessions = await api("/api/sessions"); $("sessions").replaceChildren();
  if (!sessions.length) { const note = document.createElement("p"); note.className = "sidebar-note"; note.textContent = t("完成的工作會保留在這裡。"); $("sessions").append(note); }
  for (const session of sessions) {
    const button = document.createElement("button"); button.type = "button"; button.className = `session-button${state.session?.id === session.id ? " active" : ""}`;
    button.textContent = session.title; button.title = session.title; button.disabled = state.initializing || state.busy || Boolean(state.pendingSubmission);
    button.addEventListener("click", () => openSession(session).catch(showError)); $("sessions").append(button);
  }
}
function renderSessionControls() {
  refreshSessionUsage().catch(showError);
  const supported = Boolean(state.config?.model?.context_capacity_tokens);
  $("session-tools").hidden = !supported || !state.session || !state.runs.length;
  $("run-history").replaceChildren();
  $("base-artifact").replaceChildren(new Option(t("不使用基底，建立新的任務"), ""));
  for (const [index, run] of state.runs.entries()) {
    const option = new Option(`${index + 1} · ${statusNames[run.status] || run.status} · ${run.task.slice(0, 35)}`, run.id);
    option.selected = state.run?.id === run.id; $("run-history").append(option);
    if (run.status === "completed") for (const artifact of run.artifacts) {
      $("base-artifact").append(new Option(`v${artifact.version || 1} · ${artifact.name} · ${artifact.id.slice(0, 8)}`, artifact.id));
    }
  }
  $("base-artifact").value = state.base?.artifact_id || "";
  $("base-hint").textContent = state.base ? t("固定基底 {id}；修改會建立新版本，原稿保留。", {id: state.base.artifact_id.slice(0, 8)}) : t("修改會建立新版本，原稿仍可下載。");
  updateControls();
}
async function refreshSessionRuns() {
  if (!state.session) return;
  const identity = state.session.id, generation = state.generation;
  const runs = await api(`/api/sessions/${identity}/runs`);
  if (identity !== state.session?.id || generation !== state.generation) return;
  state.runs = runs; renderSessionControls();
}
async function openHistoricalRun(identity) {
  if (state.busy) return;
  state.base = null; resetPreview();
  await observeRun(identity);
  $("task-input").value = state.run.task;
  state.selected = new Set(state.run.sources.map((source) => source.path)); renderSources();
  selectMcpTools(state.run.policy?.mcp_tools); selectSkills(state.run.policy?.skills || []); selectModel(state.run.model?.name, state.run.model?.effort);
  $("require-publish-approval").checked = Boolean(state.run.policy?.require_publish_approval);
  $("require-plan-approval").checked = Boolean(state.run.policy?.require_plan_approval);
  $("use-instructions").checked = Boolean(state.run.policy?.project_instructions);
  $("publication-workflow").value = state.run.policy?.publication_workflow || "direct";
  $("completion-workflow").value = state.run.policy?.completion_workflow || "model_summary";
  await refreshSessionRuns();
}
function chooseBase() {
  const identity = $("base-artifact").value;
  const owner = state.runs.find((run) => run.status === "completed" && run.artifacts.some((artifact) => artifact.id === identity));
  state.base = owner ? {run_id: owner.id, artifact_id: identity} : null;
  renderSessionControls();
}
function revisePreview() {
  if (state.busy || !state.previewArtifact) return;
  const artifact = state.previewArtifact;
  const owner = state.run?.id === artifact.run_id ? state.run : state.runs.find((run) => run.id === artifact.run_id);
  if (owner?.status !== "completed") return;
  state.runs = state.runs.some((run) => run.id === owner.id) ? state.runs.map((run) => run.id === owner.id ? owner : run) : [...state.runs, owner];
  state.base = {run_id: owner.id, artifact_id: artifact.id};
  renderSessionControls();
  $("task-input").value = t("請修改 {name}：", {name: state.previewArtifact.name});
  $("task-input").focus();
}
async function openSession(session) {
  if (state.busy) return;
  closeStream(); clearError();
  const workspaces = await api("/api/workspaces");
  const workspace = workspaces.find((item) => item.id === session.workspace_id);
  if (!workspace) throw new Error(t("這筆紀錄的工作區目前無法取得。"));
  await loadWorkspace(workspace.path); state.session = session;
  const runs = await api(`/api/sessions/${session.id}/runs`); state.runs = runs;
  if (runs.length) {
    const run = runs.at(-1); state.selected = new Set(run.sources.map((source) => source.path)); renderSources(); selectMcpTools(run.policy?.mcp_tools); selectSkills(run.policy?.skills || []); selectModel(run.model?.name, run.model?.effort);
    $("task-input").value = run.task; $("require-publish-approval").checked = Boolean(run.policy?.require_publish_approval); $("require-plan-approval").checked = Boolean(run.policy?.require_plan_approval); $("publication-workflow").value = run.policy?.publication_workflow || "direct"; $("completion-workflow").value = run.policy?.completion_workflow || "model_summary"; await observeRun(run.id);
  }
  await refreshSessions(); renderSessionControls(); updateControls();
}
const pendingStorageKey = "agent-harness.pending-submission.v1";
function clearPendingSubmission() {
  localStorage.removeItem(pendingStorageKey); state.pendingSubmission = null;
}
async function startTask(event) {
  event.preventDefault(); if (state.initializing || state.busy || (!state.workspace && !state.pendingSubmission)) return;
  clearError(); state.busy = true; updateControls();
  let accepted = false;
  try {
    if (!state.pendingSubmission) {
      if (state.config.mode === "fixture" && $("publication-workflow").value === "draft_review") {
        throw new Error(t("固定示範流程僅支援直接保存。請將成果流程改為「直接提出保存」後再開始。"));
      }
      if (state.config.mode === "fixture" && $("completion-workflow").value === "answer") {
        throw new Error(t("直接回答須由真實模型引用證據作答；固定示範流程請使用模型摘要回覆。"));
      }
      if ($("completion-workflow").value === "answer" && $("publication-workflow").value === "draft_review") {
        throw new Error(t("直接回答不會發布檔案，不能搭配草稿核對。"));
      }
      if (state.config.mode === "fixture" && $("completion-workflow").value === "artifact_receipt") {
        throw new Error(t("檔案確認須由真實模型選擇成果；固定示範流程請使用模型摘要回覆。"));
      }
      if (!state.session) state.session = await api("/api/sessions", {workspace_id: state.workspace.id, title: $("task-input").value.trim().slice(0, 50) || t("新任務")});
      const body = {task: $("task-input").value, sources: [...state.selected], require_publish_approval: $("require-publish-approval").checked, require_plan_approval: $("require-plan-approval").checked, publication_workflow: $("publication-workflow").value, completion_workflow: $("completion-workflow").value, submission_key: crypto.randomUUID()};
      if (state.mcpSelected.size) body.mcp_tools = [...state.mcpSelected];
      if (state.instructions?.instructions && $("use-instructions").checked) body.project_instructions = true;
      const slash = body.task.trim().match(/^\/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s|$)/);
      if (slash && availableSkills().some((skill) => skill.name === slash[1])) { state.skillSelected.add(slash[1]); renderSkills(); }
      if (state.skillSelected.size) body.skills = [...state.skillSelected];
      if (!$("model-choice").hidden) {
        body.model = $("model-select").value;
        if (!$("effort-select").hidden) body.effort = $("effort-select").value;
      }
      if (state.base) body.base_artifact = state.base;
      const pending = {session_id: state.session.id, workspace_path: state.workspace.path, body};
      localStorage.setItem(pendingStorageKey, JSON.stringify(pending));
      state.pendingSubmission = pending;
    }
    const pending = state.pendingSubmission;
    const run = pending.resume_from
      ? await api(`/api/runs/${pending.resume_from}/resume`, {checkpoint_sha256: pending.checkpoint_sha256, submission_key: pending.body.submission_key})
      : await api(`/api/sessions/${pending.session_id}/runs`, pending.body);
    accepted = true;
    if (!state.workspace || !state.session) await restorePendingSubmission();
    resetPreview(); await observeRun(run.id);
    clearPendingSubmission(); updateControls();
    await refreshSessions(); await refreshSessionRuns();
  } catch (error) {
    if (!accepted && error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) clearPendingSubmission();
    state.busy = false; updateControls(); showError(error);
  }
}
function readPendingSubmission() {
  const saved = localStorage.getItem(pendingStorageKey);
  if (!saved) return null;
  let pending;
  try { pending = JSON.parse(saved); } catch { localStorage.removeItem(pendingStorageKey); throw new Error(t("上次提交紀錄無法讀取，請從工作紀錄檢查。")); }
  if (typeof pending?.session_id !== "string" || typeof pending?.body?.submission_key !== "string" || typeof pending?.body?.task !== "string" || !Array.isArray(pending?.body?.sources)) {
    clearPendingSubmission(); throw new Error(t("上次提交紀錄不完整，請從工作紀錄檢查。"));
  }
  if (pending.resume_from !== undefined && (!/^[a-f0-9]{32}$/.test(pending.resume_from) || !/^[a-f0-9]{64}$/.test(pending.checkpoint_sha256))) {
    clearPendingSubmission(); throw new Error(t("續做紀錄不完整，請重新檢查中斷的執行。"));
  }
  return pending;
}
async function restorePendingSubmission() {
  const pending = state.pendingSubmission;
  if (!pending) return;
  const sessions = await api("/api/sessions");
  const session = sessions.find((item) => item.id === pending.session_id);
  if (!session || typeof pending?.workspace_path !== "string" || typeof pending?.body?.submission_key !== "string") {
    clearPendingSubmission();
    throw new Error(t("目前 Store 找不到上次提交的對話，請檢查工作紀錄。"));
  }
  await loadWorkspace(pending.workspace_path);
  if (state.workspace.id !== session.workspace_id) throw new Error(t("上次提交的工作區不符，請檢查工作紀錄。"));
  state.session = session; state.pendingSubmission = pending;
  state.selected = new Set(pending.body.sources); renderSources(); selectMcpTools(pending.body.mcp_tools); selectSkills(pending.body.skills || []);
  $("task-input").value = pending.body.task;
  $("require-publish-approval").checked = pending.body.require_publish_approval;
  $("require-plan-approval").checked = Boolean(pending.body.require_plan_approval);
  $("use-instructions").checked = Boolean(pending.body.project_instructions);
  $("publication-workflow").value = pending.body.publication_workflow || "direct";
  $("completion-workflow").value = pending.body.completion_workflow || "model_summary";
  state.base = pending.body.base_artifact || null;
  updateControls();
}

function renderRun(run) {
  if (state.run?.id === run.id && (run.last_sequence || 0) < (state.run.last_sequence || 0)) return false;
  state.run = run; state.busy = activeStatuses.has(run.status);
  $("execution").hidden = false; $("run-status").textContent = statusNames[run.status] || run.status;
  $("run-status").className = `run-status ${run.status}`;
  $("run-caption").textContent = `${run.validation?.fault_once_source_read ? `${t("受控讀取故障驗證")} / ` : ""}${run.validation?.hold_first_tool ? `${t("受控同步點驗證")} / ` : ""}${run.model?.name || run.mode} / ${t("執行 {id}", {id: run.id.slice(0, 10)})} / ${new Date(run.created_at).toLocaleString(locale)}`;
  $("usage").textContent = usageText(run);
  $("usage").title = run.model?.price_per_mtok ? t("估計依 --price 設定、在這次執行開始時固定的單價：每百萬 tokens 輸入 US${input}、輸出 US${output}", {input: run.model.price_per_mtok.input, output: run.model.price_per_mtok.output}) : "";
  $("run-result").textContent = run.error ? `${run.error.message} (${run.error.code})` : run.result;
  if (run.status === "cancel_requested") $("run-result").textContent = t("已提出取消，等待執行器確認停止。");
  if (run.status === "cancelled") {
    $("run-result").textContent = run.cancellation?.outcome === "unknown_effects" ? t("執行器已停止；有已派發操作的結果尚待確認，已保存產物仍可檢查。") : t("執行器已停止。");
    if (run.cancellation?.background_work_may_finish) $("run-result").textContent += ` ${t("已派發的背景工作可能仍在結束。")}`;
  }
  $("cancel-run").hidden = !state.busy;
  $("add-message").hidden = !state.busy || run.status === "cancel_requested";
  $("cancel-run").disabled = state.cancelPending || run.status === "cancel_requested";
  $("cancel-run").textContent = run.status === "cancel_requested" ? t("正在停止…") : t("取消執行");
  renderApproval(run);
  $("recovery-card").hidden = !run.recovery;
  if (state.checkpoint?.source_run_id !== run.id) state.checkpoint = null;
  if (!state.busy) $("reload-run").hidden = true;
  $("workspace-breadcrumb").textContent = `${state.workspace?.name || t("工作區")} / ${state.session?.title || t("任務")}`;
  updateControls();
  return true;
}
// Counts against the Run's limits, then the tokens the provider reported and, with a configured price, an estimate.
function tokenParts(tokens, cost, partialNote) {
  const number = (value) => value.toLocaleString(locale), parts = [];
  if (tokens?.reported_responses) parts.push(t("Token 輸入 {input}／輸出 {output}", {input: number(tokens.input_tokens), output: number(tokens.output_tokens)}));
  if (tokens?.unreported_responses) parts.push(t("{count} 次回覆未回報用量", {count: tokens.unreported_responses}));
  if (cost) parts.push(t("估計 US${cost}", {cost: cost.usd.toFixed(4)}) + (cost.partial ? partialNote : ""));
  return parts;
}
function usageText(run) {
  return [t("模型請求 {count} 次", {count: run.usage.model_calls}), t("工具嘗試 {count} 次", {count: run.usage.tool_calls}),
    ...tokenParts(run.token_usage, run.estimated_cost, t("（不含未回報部分）"))].join(t("　"));
}
// Totals over the open Session (from the server), shown once it has more than one Run.
async function refreshSessionUsage() {
  const session = state.session;
  if (!session) { $("session-usage").hidden = true; return; }
  const usage = await api(`/api/sessions/${session.id}/usage`);
  if (state.session?.id !== session.id) return;
  $("session-usage").hidden = usage.runs < 2;
  $("session-usage").textContent = [t("這個對話共 {count} 次執行", {count: usage.runs}),
    ...tokenParts(usage.token_usage, usage.estimated_cost, t("（部分執行未計價或未回報用量）"))].join(t("　"));
}
function renderApproval(run) {
  const approval = run?.approvals?.find((item) => ["pending", "approved"].includes(item.status));
  state.approval = approval || null; $("approval-card").hidden = !approval;
  if (!approval) return;
  const publication = approval.tool === "artifact_write", plan = approval.tool === "plan_update";
  $("approval-heading").textContent = approval.status === "approved" ? t("已批准，等待派發") : plan ? t("核准這份執行計畫") : publication ? t("確認這次成果保存") : t("確認這次外部工具操作");
  $("approval-target").textContent = plan ? t("模型提出 {count} 個步驟；核准後才會依計畫讀取來源與執行。若要修改，寫下意見後按「要求修改」。", {count: approval.arguments.steps.length})
    : publication ? t("{tool}：{name}", {tool: toolNames[approval.tool] || approval.tool, name: approval.arguments.name})
    : t("外部工具 {tool}（MCP 伺服器 {server}），可能變更資料", {tool: approval.tool, server: approval.scope?.mcp?.server || t("未知")});
  $("approval-expiry").textContent = t("這次操作有效至 {time}", {time: new Date(approval.expires_at).toLocaleString(locale)});
  $("approval-content").textContent = plan ? approval.arguments.steps.map((step, index) => `${index + 1}. ${step}`).join("\n")
    : publication ? approval.arguments.content : JSON.stringify(approval.arguments, null, 2);
  $("approval-content").setAttribute("aria-label", plan ? t("待核准的計畫步驟") : publication ? t("待批准的完整成果內容") : t("待批准的完整工具參數"));
  $("approve-operation").textContent = plan ? t("核准計畫") : publication ? t("批准這次保存") : t("批准這次操作");
  $("deny-operation").textContent = plan ? t("要求修改") : t("拒絕");
  $("approval-note-field").hidden = !plan || approval.status !== "pending";
  $("approval-parameters").textContent = JSON.stringify({tool: approval.tool, arguments: approval.arguments, scope: approval.scope}, null, 2);
  $("approval-version").textContent = t("參數版本 SHA-256 {sha}", {sha: approval.parameters_sha256});
  for (const id of ["approve-operation", "deny-operation", "approval-note"]) $(id).disabled = approval.status !== "pending" || run.status !== "waiting_approval" || state.approvalPending || state.cancelPending;
}
async function refreshRun(identity, generation) {
  const run = await api(`/api/runs/${identity}`);
  if (generation !== state.generation) return;
  if (renderRun(run)) {
    if (!activeStatuses.has(run.status)) refreshSessionUsage().catch(showError);
    await renderRecovery(run, generation);
    await renderArtifacts(run, generation);
  }
}
async function renderArtifacts(run, generation) {
  const artifacts = [...(run.inherited_artifacts || []), ...run.artifacts];
  if (artifacts.length) {
    try { await showArtifacts(artifacts, generation); }
    catch (error) { if (generation === state.generation) showError(error); }
  }
}
async function renderRecovery(run, generation) {
  if (!run.recovery) return;
  $("recovery-summary").textContent = t("正在檢查保存紀錄…");
  $("resume-run").disabled = true;
  try {
    const checkpoint = await api(`/api/runs/${run.id}/checkpoint`);
    if (generation !== state.generation || state.run?.id !== run.id) return;
    const blockers = checkpoint.resume_blockers || checkpoint.blockers;
    state.checkpoint = {...checkpoint, blockers};
    $("recovery-summary").textContent = t("{confirmed} 個操作已確認，{pending} 個待釐清，{artifacts} 份成果紀錄。", {confirmed: checkpoint.confirmed.length, pending: checkpoint.pending.length, artifacts: checkpoint.artifacts.length}) + (blockers.length ? t("有未確認效果，請先人工釐清；目前無法續做。") : t("續做會建立新執行、沿用已確認結果；需要批准的成果操作會重新確認。"));
    $("recovery-attempts").replaceChildren(...checkpoint.pending.map((attempt) => {
      const item = document.createElement("li"); item.textContent = t("{tool}：{status}", {tool: toolNames[attempt.tool] || attempt.tool, status: ({unknown: t("結果待確認"), not_dispatched: t("尚未派發"), failed: t("待修正"), rejected: t("待修正")})[attempt.status] || attempt.status}) + (attempt.error ? ` / ${attempt.error.message}` : ""); return item;
    }));
    $("recovery-details").textContent = JSON.stringify(checkpoint, null, 2);
    updateControls();
  } catch (error) {
    if (generation === state.generation) { state.checkpoint = null; $("recovery-summary").textContent = t("無法驗證恢復紀錄，請重新載入。"); showError(error); }
  }
}
async function resumeRun() {
  const run = state.run, checkpoint = state.checkpoint;
  if (!run || !checkpoint || checkpoint.blockers.length || state.busy || state.pendingSubmission) return;
  const pending = {session_id: run.session_id, workspace_path: state.workspace.path,
    resume_from: run.id, checkpoint_sha256: checkpoint.sha256,
    body: {task: run.task, sources: run.sources.map((source) => source.path), require_publish_approval: Boolean(run.policy?.require_publish_approval), require_plan_approval: Boolean(run.policy?.require_plan_approval), publication_workflow: run.policy?.publication_workflow || "direct", completion_workflow: run.policy?.completion_workflow || "model_summary", submission_key: crypto.randomUUID(),
      ...(run.policy?.mcp_tools?.length ? {mcp_tools: run.policy.mcp_tools} : {}),
      ...(run.policy?.skills?.length ? {skills: run.policy.skills} : {}),
      ...(run.policy?.project_instructions ? {project_instructions: true} : {})}};
  try {
    localStorage.setItem(pendingStorageKey, JSON.stringify(pending)); state.pendingSubmission = pending;
    await startTask({preventDefault() {}});
  } catch (error) { showError(error); }
}
async function decideApproval(decision) {
  const approval = state.approval; const identity = state.run?.id; const generation = state.generation;
  if (!approval || !identity || state.approvalPending) return;
  clearError(); state.approvalPending = true; renderApproval(state.run);
  try {
    const note = $("approval-note").value.trim();
    if (decision === "deny" && approval.tool === "plan_update" && note) {
      await api(`/api/runs/${identity}/messages`, {message_id: `plan-${approval.id}`, text: note});
    }
    await api(`/api/runs/${identity}/approvals/${approval.id}`, {decision, parameters_sha256: approval.parameters_sha256});
    $("approval-note").value = "";
    await refreshRun(identity, generation);
  } catch (error) { if (generation === state.generation) showError(error); }
  finally { if (generation === state.generation) { state.approvalPending = false; renderApproval(state.run); } }
}
async function addMessage(event) {
  event.preventDefault();
  const identity = state.run?.id, text = $("add-message-text").value.trim();
  if (!identity || !text) return;
  clearError(); $("add-message-send").disabled = true;
  try {
    await api(`/api/runs/${identity}/messages`, {message_id: crypto.randomUUID(), text});
    $("add-message-text").value = "";
  } catch (error) { showError(error); }
  finally { $("add-message-send").disabled = false; }
}

async function cancelRun() {
  const identity = state.run?.id; const generation = state.generation;
  if (!identity || state.cancelPending) return;
  state.cancelPending = true; clearError(); $("cancel-run").disabled = true;
  try { await api(`/api/runs/${identity}/cancel`, {}); await refreshRun(identity, generation); }
  catch (error) { if (generation === state.generation) showError(error); }
  finally { if (generation === state.generation && state.run) { state.cancelPending = false; renderRun(state.run); } }
}
async function observeRun(identity) {
  closeStream(); const generation = state.generation;
  const run = await api(`/api/runs/${identity}`); if (generation !== state.generation) return;
  renderRun(run); $("timeline").replaceChildren(); $("reload-run").hidden = true; resetPlan();
  await renderRecovery(run, generation);
  await renderArtifacts(run, generation);
  state.socket = observeEvents(identity, {
    onEvent(event) {
      if (generation !== state.generation) return;
      appendEvent(event);
      if (["run.started", "run.completed", "run.failed", "run.interrupted", "run.cancelled", "run.cancel_requested", "artifact.published"].includes(event.kind) || event.kind.startsWith("approval.")) {
        refreshRun(identity, generation).then(async () => {
          if (generation === state.generation && !state.busy) { await refreshSessions(); await refreshSessionRuns(); }
        }).catch((error) => { if (generation === state.generation) showError(error); });
      }
    },
    onState(message, cursor) {
      if (generation !== state.generation) return;
      $("connection-state").textContent = `${message} · ${t("已接收 {count} 筆保存事件", {count: cursor})}`;
      $("reload-run").hidden = message === t("已連線");
      if (message === t("保存事件已同步")) {
        refreshRun(identity, generation).then(async () => {
          if (generation === state.generation && !state.busy) { await refreshSessions(); await refreshSessionRuns(); }
        }).catch((error) => {
          if (generation === state.generation) { $("reload-run").hidden = false; showError(error); }
        });
      }
    },
    onError(error) { if (generation === state.generation) showError(error); },
  });
}
function appendEvent(event) {
  if (event.kind === "plan.updated") {
    state.planSequence = event.sequence;
    $("plan-steps").replaceChildren(...event.data.steps.map((step) => { const item = document.createElement("li"); item.textContent = step; return item; }));
    $("plan").hidden = false; renderPlanContext();
  }
  if (["correction.available", "correction.observed"].includes(event.kind)) {
    state.correctionSequence = Math.max(state.correctionSequence, event.sequence);
    renderPlanContext();
  }
  if (event.kind === "model.text_delta") {
    const id = `stream-step-${event.data.step}`;
    let row = document.getElementById(id);
    if (!row) { row = document.createElement("li"); row.id = id; row.className = "event"; $("timeline").append(row); }
    row.textContent += event.data.text;
    return;
  }
  const tool = toolNames[event.data.tool] || event.data.tool || "";
  const titles = {"run.accepted": t("任務已接收"), "run.started": t("開始執行"), "model.requested": t("請求模型"), "model.responded": event.data.text || t("模型提出 {count} 個工具操作", {count: event.data.tool_count}), "plan.updated": t("計畫已更新"), "tool.requested": t("準備：{tool}", {tool}), "tool.dispatched": t("執行：{tool}", {tool}), "tool.completed": t("完成：{tool}", {tool}), "tool.rejected": event.data.code === "plan_changes_requested" ? t("你要求修改計畫") : t("操作被拒絕：{tool}", {tool}), "tool.failed": t("工具未完成：{tool}", {tool}), "correction.available": event.data.error?.code === "plan_changes_requested" ? t("已請模型提出新計畫") : t("錯誤已回饋模型，等待修正"), "correction.observed": t("已執行後續修正操作"), "tool.fault_injected": t("受控驗證：刻意注入一次讀取失敗"), "artifact.published": t("成果已保存：{name}", {name: event.data.name}), "run.completed": t("任務完成"), "run.failed": t("任務未完成"), "run.interrupted": t("執行已中斷")};
  Object.assign(titles, {"context.memory_refreshed": t("已選取目前有效記憶"), "context.selected": event.data.project_instructions ? t("已選取對話背景與專案指令 {path}", {path: event.data.project_instructions.path}) : t("已選取對話背景"), "context.pruned": t("已依預算省略已保存背景"), "context.limited": t("完整請求超出 Context 上限"),"approval.requested": event.data.tool === "plan_update" ? t("等待你核准計畫") : event.data.tool && event.data.tool !== "artifact_write" ? t("等待批准外部操作：{tool}", {tool: event.data.tool}) : t("等待批准這次保存"), "approval.approved": t("這次操作已批准"), "approval.denied": t("這次操作已拒絕"), "approval.consumed": t("單次批准已用於派發"), "approval.expired": t("批准已失效"), "approval.cancelled": t("批准因取消而失效"), "run.cancel_requested": t("取消請求已保存"), "run.cancelled": t("執行器已確認停止"), "tool.unknown": t("已派發操作的結果待確認"), "validation.tool_held": t("受控驗證：工具在同步點暫停"), "message.queued": t("已收到補充指示：{text}", {text: event.data.text || ""}), "message.delivered": t("補充指示已送給模型"), "message.undelivered": t("補充指示未及送給模型，執行已結束"), "hook.decided": event.data.decision === "allow" ? t("Hook 允許：{tool}", {tool: event.data.tool}) : t("Hook 拒絕：{tool}（{reason}）", {tool: event.data.tool, reason: event.data.reason || t("未說明原因")}), "hook.notified": event.data.status === "delivered" ? t("已通知 Hook：{tool}", {tool: event.data.tool}) : t("Hook 通知失敗：{tool}", {tool: event.data.tool})});
  const row = document.createElement("li"); row.className = `event${event.kind.startsWith("run.") || event.kind === "artifact.published" ? " important" : ""}${event.kind.endsWith("failed") || event.kind.endsWith("rejected") ? " failure" : ""}`;
  const details = document.createElement("details"); const summary = document.createElement("summary");
  const number = document.createElement("span"); number.className = "event-number"; number.textContent = String(event.sequence).padStart(2, "0");
  Object.assign(titles, {"checkpoint.saved": t("中斷恢復紀錄已保存"), "checkpoint.loaded": t("已載入前次確認結果"), "recovery.detected": t("重啟後已檢查中斷狀態"), "tool.reconciled": t("成果對帳確認：{tool}", {tool}), "tool.reused": t("沿用已確認結果：{tool}", {tool}), "artifact.reconciled": t("保留成果已核對")});
  Object.assign(titles, {"completion.recovery_requested": t("完成回覆空白，僅允許補交完成摘要")});
  const title = document.createElement("span"); title.className = "event-title"; title.textContent = titles[event.kind] || event.kind;
  const time = document.createElement("time"); time.className = "event-time"; time.dateTime = event.timestamp; time.textContent = new Date(event.timestamp).toLocaleTimeString(locale, {hour12: false});
  summary.append(number, title, time); const raw = document.createElement("pre"); raw.textContent = JSON.stringify(event.data, null, 2);
  details.append(summary, raw); row.append(details); $("timeline").append(row);
}
async function showArtifacts(artifacts, generation) {
  if (generation !== state.generation) return;
  $("artifact-list").replaceChildren();
  for (const artifact of artifacts) {
    if (artifacts.length > 1) { const button = document.createElement("button"); button.type = "button"; button.textContent = artifact.name; button.addEventListener("click", () => showArtifact(artifact, generation).catch(showError)); $("artifact-list").append(button); }
  }
  await showArtifact(artifacts.at(-1), generation);
}
async function showArtifact(artifact, generation) {
  const response = await fetch(artifact.download_url); if (!response.ok) throw new Error(t("成果目前無法下載，請確認檔案仍然完整。"));
  const text = await response.text(); if (generation !== state.generation) return;
  $("preview-heading").textContent = t("成果預覽"); $("preview-kind").textContent = artifact.run_id !== state.run?.id ? t("沿用前次成果") : t("已保存");
  $("preview-empty").hidden = true; $("preview-content").hidden = false;
  $("preview-name").textContent = artifact.name; $("preview-meta").textContent = `${bytes(artifact.bytes)} / SHA-256 ${artifact.sha256.slice(0, 12)}`;
  state.previewArtifact = artifact;
  const owner = state.run?.id === artifact.run_id ? state.run : state.runs.find((run) => run.id === artifact.run_id);
  $("revise-artifact").hidden = !state.config?.model?.context_capacity_tokens || owner?.status !== "completed";
  $("revise-artifact").disabled = state.busy;
  $("preview-meta").textContent += ` / v${artifact.version || 1}${artifact.parent_artifact_id ? ` / ${t("基底 {id}", {id: artifact.parent_artifact_id.slice(0, 8)})}` : ""}`;
  $("preview-text").textContent = text; $("download").href = artifact.download_url; $("download").hidden = false;
  $("compare-artifact").hidden = !artifact.parent_artifact_id; hideDiff();
}
async function toggleDiff() {
  const artifact = state.previewArtifact;
  if (!artifact?.parent_artifact_id) return;
  if (!$("preview-diff").hidden) { hideDiff(); return; }
  const diff = await api(`/api/runs/${artifact.run_id}/artifacts/${artifact.id}/diff`);
  if (state.previewArtifact !== artifact) return;
  const view = $("preview-diff"); view.replaceChildren();
  const summary = document.createElement("span"); summary.className = "diff-summary";
  summary.textContent = diff.identical ? t("v{from} 與 v{to} 內容相同", {from: diff.from.version, to: diff.to.version})
    : t("v{from} → v{to}：新增 {added} 行、刪除 {removed} 行", {from: diff.from.version, to: diff.to.version, added: diff.added, removed: diff.removed});
  view.append(summary);
  const lines = diff.diff.replace(/\n$/, "").split("\n");
  lines.forEach((line, index) => {
    const row = document.createElement("span");
    // Only the first two lines are file headers; a removed "---" rule appears as "----".
    row.className = index < 2 ? "diff-file" : line.startsWith("+") ? "diff-add"
      : line.startsWith("-") ? "diff-del" : line.startsWith("@@") ? "diff-hunk" : "";
    row.textContent = line || " "; view.append(row);
  });
  view.hidden = false; $("preview-text").hidden = true; $("compare-artifact").textContent = t("顯示這一版全文");
}
$("workspace-form").addEventListener("submit", (event) => { event.preventDefault(); loadWorkspace($("workspace-path").value).catch(showError); });
$("use-demo").addEventListener("click", () => loadWorkspace(state.config.demo_path).catch(showError));
$("new-task").addEventListener("click", newTask);
$("compare-artifact").addEventListener("click", () => toggleDiff().catch(showError));
$("task-form").addEventListener("submit", startTask);
$("reload-run").addEventListener("click", async () => {
  const identity = state.run?.id, generation = state.generation;
  if (!identity) return;
  try {
    clearError(); await refreshRun(identity, generation);
    if (generation === state.generation) {
      if (state.socket) state.socket.reconnect(); else await observeRun(identity);
    }
  } catch (error) { if (generation === state.generation) { $("reload-run").hidden = false; showError(error); } }
});
$("approve-operation").addEventListener("click", () => decideApproval("approve"));
$("deny-operation").addEventListener("click", () => decideApproval("deny"));
$("run-history").addEventListener("change", () => openHistoricalRun($("run-history").value).catch(showError));
$("base-artifact").addEventListener("change", chooseBase);
$("revise-artifact").addEventListener("click", revisePreview);
$("cancel-run").addEventListener("click", cancelRun);
$("add-message").addEventListener("submit", addMessage);
$("resume-run").addEventListener("click", resumeRun);
// Switching language saves the choice and reloads, so every view renders in one language.
$("lang-select").value = lang;
$("upload-files").addEventListener("click", () => $("upload-input").click());
$("upload-input").addEventListener("change", () => uploadFiles([...$("upload-input").files]).catch(showError));
$("lang-select").addEventListener("change", () => { saveLang($("lang-select").value); location.reload(); });
updateControls();
try {
  state.pendingSubmission = readPendingSubmission();
  if (state.pendingSubmission) {
    $("task-input").value = state.pendingSubmission.body.task;
    state.selected = new Set(state.pendingSubmission.body.sources); updateControls();
  }
  state.config = await api("/api/config");
  const local = state.config.mode !== "fixture";  // a real model: local Ollama, OpenAI-compatible or Claude
  $("publication-workflow").querySelector('option[value="draft_review"]').disabled = !local;
  $("completion-workflow").querySelector('option[value="artifact_receipt"]').disabled = !local;
  $("completion-workflow").querySelector('option[value="answer"]').disabled = !local;
  if (!local) $("publication-workflow-hint").textContent = t("固定示範流程僅支援直接保存來源快照。草稿核對須使用本機模型模式。");
  $("mode-badge").textContent = local ? state.config.model.name : t("模擬模型");
  $("mode-title").textContent = !local ? t("這是一條可檢查的示範流程") : state.config.mode === "local" ? t("由本機模型選擇工具與產生成果") : t("由遠端模型選擇工具與產生成果");
  $("mode-description").textContent = local ? t("先選來源，再交代資料分析或文件整理任務。計畫、工具與成果可逐筆檢查；模型輸出仍需對照來源。") : t("依固定步驟讀取已選來源、保存來源快照；此模式不會理解任意任務。");
  if (state.config.validation?.fault_once_source_read) $("mode-description").textContent += ` ${t("受控驗證模式：每筆任務首次來源讀取會刻意失敗一次。")}`;
  if (state.config.validation?.hold_first_tool) $("mode-description").textContent += ` ${t("受控同步點驗證：本服務首個指定工具會暫停，供取消／併發觀察。")}`;
  renderMcpTools(); selectSkills(); renderModelChoices();
  await refreshSessions(); await restorePendingSubmission(); updateControls();
} catch (error) { showError(error); }
finally { state.initializing = false; updateControls(); }
