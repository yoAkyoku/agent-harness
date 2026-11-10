// Workbench UI language. Every key is the zh-TW text exactly as the UI shows it; EN maps each key to English.
// Keep EN a JSON-compatible object literal: tests/test_workbench_i18n.py parses it to check coverage.
const STORAGE_KEY = "agent-harness-lang";
const LANGUAGES = ["en", "zh-TW"];

function savedLang() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return LANGUAGES.includes(value) ? value : null;
  } catch { return null; }
}
function browserLang() {
  const preferred = navigator.language || navigator.languages?.[0] || "";
  return preferred.toLowerCase().startsWith("zh") ? "zh-TW" : "en";
}
export const lang = savedLang() || browserLang();

export function saveLang(value) {
  if (!LANGUAGES.includes(value)) return false;
  try { localStorage.setItem(STORAGE_KEY, value); return true; } catch { return false; }
}

const EN = {
  "工作區與紀錄": "Workspace and history",
  "Agent Harness 首頁": "Agent Harness home",
  "本機任務工作台": "Local task workbench",
  "介面語言": "Language",
  "上傳檔案": "Upload files",
  "直接回答（不產生檔案，需引用證據）": "Answer directly (no file; facts must cite evidence)",
  "直接回答須由真實模型引用證據作答；固定示範流程請使用模型摘要回覆。": "A direct answer needs a real model that cites evidence; with the scripted fixture, use the model summary.",
  "直接回答不會發布檔案，不能搭配草稿核對。": "A direct answer publishes no file, so it cannot be combined with draft review.",
  "MCP 伺服器 {name} 連線中…": "MCP server {name} is connecting…",
  "MCP 伺服器 {name} 需要登入。": "MCP server {name} needs you to sign in.",
  "登入 {name}": "Sign in to {name}",
  "Hook 允許：{tool}": "Hook allowed: {tool}",
  "Hook 拒絕：{tool}（{reason}）": "Hook denied: {tool} ({reason})",
  "未說明原因": "no reason given",
  "已通知 Hook：{tool}": "Hook notified: {tool}",
  "Hook 通知失敗：{tool}": "Hook notification failed: {tool}",
  "這個對話共 {count} 次執行": "This Session: {count} Runs",
  "（部分執行未計價或未回報用量）": " (some Runs are unpriced or reported no usage)",
  "檔案會存進工作資料夾的 uploads/，不會覆寫既有檔案。": "Files are saved in the workspace's uploads/ folder and never overwrite existing files.",
  "{name} 無法上傳：{reason}": "{name} could not be uploaded: {reason}",
  "＋": "+",
  "新任務": "New task",
  "工作資料夾": "Workspace folder",
  "資料夾完整路徑": "Full folder path",
  "載入資料夾": "Load folder",
  "使用範例資料": "Use sample data",
  "尚未選擇工作區": "No workspace selected",
  "任務來源": "Task sources",
  "載入資料夾後，選擇這次任務可以讀取的檔案。": "After loading a folder, choose the files this task may read.",
  "工作區記憶": "Workspace memory",
  "只保存你在此輸入的內容。適用於這個資料夾，不授予檔案或工具權限。": "Stores only what you enter here. Applies to this folder and grants no file or tool access.",
  "新增記憶": "New memory",
  "名稱": "Name",
  "類型": "Type",
  "偏好": "Preference",
  "待來源核實的事實": "Fact to verify against sources",
  "記憶內容": "Memory content",
  "適用關鍵字（以逗號分隔）": "Keywords (comma-separated)",
  "例如：摘要, 報告": "e.g. summary, report",
  "任務或已選檔名包含任一關鍵字時使用；留空適用所有新任務。超過任務預算會省略。": "Used when the task or a selected file name contains any keyword; leave empty to apply to every new task. Omitted when it exceeds the task budget.",
  "來源註記（由你填寫）": "Source note (written by you)",
  "明確保存記憶": "Save memory",
  "取消編輯": "Cancel editing",
  "工作紀錄": "History",
  "完成的工作會保留在這裡。": "Finished work stays here.",
  "檔案與紀錄保存在本機": "Files and history stay on this computer",
  "新的工作": "New task",
  "載入模型設定": "Loading model settings",
  "把資料交給任務。": "Hand your data to a task.",
  "選好來源、交代工作，在同一個地方查看執行與成果。": "Pick sources, describe the work, and follow the Run and its artifacts in one place.",
  "模型模式": "Model mode",
  "正在讀取這個工作台的模型設定。": "Reading this workbench's model settings.",
  "查看這個對話的執行紀錄": "Runs in this Session",
  "查看執行版本": "View Run",
  "這次修改以哪份成果為基底？": "Which artifact should this revision build on?",
  "不使用基底，建立新的任務": "No base; start a new task",
  "修改會建立新版本，原稿仍可下載。": "A revision creates a new version; the original stays downloadable.",
  "這次要完成什麼？": "What should this task do?",
  "例如：讀取已選來源，保存一份可檢查的來源快照。": "e.g. Read the selected sources and save a source snapshot you can inspect.",
  "讀取已選來源，保存一份可檢查的來源快照。": "Read the selected sources and save a source snapshot you can inspect.",
  "成果流程": "Artifact workflow",
  "直接提出保存": "Propose saving directly",
  "先草稿，再由模型核對後提出保存": "Draft first, let the model check it, then propose saving",
  "草稿流程會多用一次以上模型決策。草稿尚非可下載成果，內容仍需檢查。": "The draft workflow uses at least one extra model decision. A draft is not yet a downloadable artifact, and its content still needs checking.",
  "完成時如何回覆？": "How should the task finish?",
  "由模型回覆摘要": "Model replies with a summary",
  "只確認已發布的檔案": "Only confirm published files",
  "檔案確認僅列出模型選定、已核對的成果；正文仍需檢查。": "The file confirmation lists only verified artifacts the model chose; their content still needs checking.",
  "模型": "Model",
  "思考強度": "Reasoning effort",
  "這次可用的 Skills": "Skills for this task",
  "模型只先看到名稱與說明，需要時才載入完整指示。任務開頭輸入 /名稱 也會勾選該 skill。": "The model first sees only each skill's name and description, and loads the full instructions when needed. Starting the task with /name also selects that skill.",
  "這次可用的 MCP 工具": "MCP tools for this task",
  "只會提供你勾選的工具。標示「每次需核准」的工具可能變更資料，每次呼叫都會先等你確認。": "Only the tools you tick are offered. Tools marked “Approval each time” may change data and wait for your approval on every call.",
  "專案指令": "Project instructions",
  "套用工作區的 AGENTS.md": "Apply the workspace's AGENTS.md",
  "查看指令內容": "View instructions",
  "指令只調整做法與格式，不能增加來源、工具或批准。開始時會記錄這份內容與 SHA-256，續做時沿用同一份。": "Instructions only adjust approach and format; they cannot add sources, tools or approvals. The content and its SHA-256 are recorded when the Run starts, and a resumed Run reuses the same copy.",
  "先讓我核准計畫，再開始讀取來源與執行": "Let me approve the plan before sources are read and the Run proceeds",
  "保存成果前，由我確認這次內容": "Let me approve the content before an artifact is saved",
  "先從左側選擇資料來源": "Select sources on the left first",
  "開始任務": "Start task",
  "執行過程": "Run progress",
  "等待執行": "Queued",
  "取消執行": "Cancel Run",
  "執行中補充指示（會在模型的下一步送出）": "Add an instruction while running (sent with the model's next step)",
  "例如：也請列出營收數字": "e.g. Also list the revenue figures",
  "送出補充": "Send",
  "檢查中斷後的工作": "Review interrupted work",
  "查看保存的恢復紀錄": "View saved recovery record",
  "建立新執行並續做": "Resume in a new Run",
  "需要你的決定": "Your decision is needed",
  "確認這次成果保存": "Approve saving this artifact",
  "待批准的完整成果內容": "Full artifact content awaiting approval",
  "查看完整有效參數與範圍": "View full effective parameters and scope",
  "修改意見（選填，會在要求修改時一併交給模型）": "Change notes (optional; sent to the model when you request changes)",
  "例如：也請核對營收總額": "e.g. Also check the revenue total",
  "批准這次保存": "Approve save",
  "拒絕": "Deny",
  "模型提出的計畫": "Plan proposed by the model",
  "計畫是預計動作；工具是否成功、引用是否有效，以實際結果為準。": "A plan lists intended actions; whether tools succeed and citations hold is decided by the actual results.",
  "已保存的執行事件": "Saved Run events",
  "重新載入進度": "Reload progress",
  "每筆工具結果都先保存，再顯示於工作台。": "Every tool result is saved before it appears in the workbench.",
  "來源與成果預覽": "Source and artifact preview",
  "成果預覽": "Artifact preview",
  "等待成果": "No artifact yet",
  "工作完成後，成果在這裡。": "Artifacts appear here when the work is done.",
  "內容、檔案大小與下載連結，直接對照實際保存的檔案。": "Content, file size and download link come straight from the saved file.",
  "下載": "Download",
  "以這個版本修改": "Revise this version",
  "與上一版比較": "Compare with previous version",
  "與上一版的差異": "Changes from the previous version",
  "來源與模型輸出以純文字呈現。": "Sources and model output are shown as plain text.",
  "列出允許的來源": "List allowed sources",
  "讀取來源": "Read source",
  "讀取修改基底": "Read revision base",
  "保存成果": "Save artifact",
  "保存待核對草稿（尚未發布）": "Save draft for checking (not published)",
  "更新計畫": "Update plan",
  "檢查表格": "Inspect table",
  "計算欄位": "Aggregate column",
  "計算比例": "Calculate ratio",
  "提交完成回覆": "Submit final reply",
  "執行中": "Running",
  "等待你的確認": "Waiting for your approval",
  "正在取消": "Cancelling",
  "已取消": "Cancelled",
  "已完成": "Completed",
  "未完成": "Failed",
  "執行已中斷": "Interrupted",
  "低": "Low",
  "中": "Medium",
  "高": "High",
  "很高": "Very high",
  "最高": "Max",
  "；": "; ",
  "請重新確認輸入。": "Please check your input.",
  "重試這次提交": "Retry this submission",
  "建立修改版本": "Create revision",
  "提交結果尚未確認；重試會查回同一次執行": "Submission not confirmed yet; retrying looks up the same Run",
  "本次允許讀取 {count} 份來源": "Sources this task may read: {count}",
  "較早提出的計畫": "Earlier plan",
  "此計畫提出後曾有錯誤回饋或修正；請對照後續事件與成果。": "Errors were fed back or corrected after this plan was proposed; compare it with later events and artifacts.",
  "{workspace} / 新的工作": "{workspace} / New task",
  "（{bytes} bytes）": "({bytes} bytes)",
  "無法套用：{message}": "Cannot apply: {message}",
  "MCP 伺服器 {name} 目前無法使用": "MCP server {name} is unavailable",
  "：{error}": ": {error}",
  "每次需核准": "Approval each time",
  "唯讀": "Read-only",
  "（伺服器未提供說明）": "(no description from the server)",
  "找不到可用來源。請加入 UTF-8 的 .md、.txt、.json（每份不超過 256 KiB）、CSV 或 Excel 表格（每份不超過 20 MiB），或 PDF、Word、PowerPoint（每份不超過 10 MiB）；圖片需以 --tesseract 啟用 OCR。": "No usable sources found. Add UTF-8 .md, .txt or .json files (up to 256 KiB each), CSV or Excel tables (up to 20 MiB each), or PDF, Word or PowerPoint files (up to 10 MiB each); images need OCR enabled with --tesseract.",
  "查看": "View",
  "查看來源 {path}": "View source {path}",
  "來源預覽": "Source preview",
  "任務輸入": "Task input",
  "固定基底 {id}；修改會建立新版本，原稿保留。": "Base fixed to {id}; the revision creates a new version and keeps the original.",
  "請修改 {name}：": "Revise {name}: ",
  "這筆紀錄的工作區目前無法取得。": "This Session's workspace is not available right now.",
  "固定示範流程僅支援直接保存。請將成果流程改為「直接提出保存」後再開始。": "The fixed demo flow only supports saving directly. Set Artifact workflow to “Propose saving directly” and start again.",
  "檔案確認須由真實模型選擇成果；固定示範流程請使用模型摘要回覆。": "File confirmation needs a real model to choose the artifacts; use the model summary reply with the fixed demo flow.",
  "上次提交紀錄無法讀取，請從工作紀錄檢查。": "The last submission record cannot be read; check History.",
  "上次提交紀錄不完整，請從工作紀錄檢查。": "The last submission record is incomplete; check History.",
  "續做紀錄不完整，請重新檢查中斷的執行。": "The resume record is incomplete; review the interrupted Run again.",
  "目前 Store 找不到上次提交的對話，請檢查工作紀錄。": "The current Store has no Session for the last submission; check History.",
  "上次提交的工作區不符，請檢查工作紀錄。": "The last submission's workspace does not match; check History.",
  "受控讀取故障驗證": "Controlled read-fault validation",
  "受控同步點驗證": "Controlled sync-point validation",
  "執行 {id}": "Run {id}",
  "估計依 --price 設定、在這次執行開始時固定的單價：每百萬 tokens 輸入 US${input}、輸出 US${output}": "Estimate uses the --price unit prices fixed when this Run started: US${input} input and US${output} output per million tokens",
  "已提出取消，等待執行器確認停止。": "Cancellation requested; waiting for the runner to confirm it stopped.",
  "執行器已停止；有已派發操作的結果尚待確認，已保存產物仍可檢查。": "The runner stopped. Some dispatched operations have unconfirmed results; saved artifacts can still be inspected.",
  "執行器已停止。": "The runner stopped.",
  "已派發的背景工作可能仍在結束。": "Dispatched background work may still be finishing.",
  "正在停止…": "Stopping…",
  "工作區": "Workspace",
  "任務": "Task",
  "模型請求 {count} 次": "Model requests: {count}",
  "工具嘗試 {count} 次": "Tool attempts: {count}",
  "Token 輸入 {input}／輸出 {output}": "Tokens in: {input} / out: {output}",
  "{count} 次回覆未回報用量": "Responses without reported usage: {count}",
  "估計 US${cost}": "Estimated US${cost}",
  "（不含未回報部分）": " (excluding unreported usage)",
  "　": " · ",
  "已批准，等待派發": "Approved, waiting to dispatch",
  "核准這份執行計畫": "Approve this plan",
  "確認這次外部工具操作": "Approve this external tool call",
  "模型提出 {count} 個步驟；核准後才會依計畫讀取來源與執行。若要修改，寫下意見後按「要求修改」。": "The model proposed a plan of {count} steps. Nothing is read or run until you approve it. To change it, write a note and choose “Request changes”.",
  "{tool}：{name}": "{tool}: {name}",
  "外部工具 {tool}（MCP 伺服器 {server}），可能變更資料": "External tool {tool} (MCP server {server}) may change data",
  "未知": "unknown",
  "這次操作有效至 {time}": "This approval is valid until {time}",
  "待核准的計畫步驟": "Plan steps awaiting approval",
  "待批准的完整工具參數": "Full tool arguments awaiting approval",
  "核准計畫": "Approve plan",
  "批准這次操作": "Approve this call",
  "要求修改": "Request changes",
  "參數版本 SHA-256 {sha}": "Parameters SHA-256 {sha}",
  "正在檢查保存紀錄…": "Checking saved records…",
  "{confirmed} 個操作已確認，{pending} 個待釐清，{artifacts} 份成果紀錄。": "Operations confirmed: {confirmed}; unresolved: {pending}; artifact records: {artifacts}. ",
  "有未確認效果，請先人工釐清；目前無法續做。": "Some effects are unconfirmed; resolve them manually first. Resuming is not possible yet.",
  "續做會建立新執行、沿用已確認結果；需要批准的成果操作會重新確認。": "Resuming creates a new Run that reuses confirmed results; artifact operations that need approval are confirmed again.",
  "{tool}：{status}": "{tool}: {status}",
  "結果待確認": "result unconfirmed",
  "尚未派發": "not dispatched",
  "待修正": "needs correction",
  "無法驗證恢復紀錄，請重新載入。": "Cannot verify the recovery record; reload the page.",
  "已接收 {count} 筆保存事件": "Saved events received: {count}",
  "已連線": "Connected",
  "保存事件已同步": "Saved events in sync",
  "任務已接收": "Task accepted",
  "開始執行": "Run started",
  "請求模型": "Model requested",
  "模型提出 {count} 個工具操作": "Model proposed tool calls: {count}",
  "計畫已更新": "Plan updated",
  "準備：{tool}": "Preparing: {tool}",
  "執行：{tool}": "Running: {tool}",
  "完成：{tool}": "Completed: {tool}",
  "你要求修改計畫": "You requested plan changes",
  "操作被拒絕：{tool}": "Rejected: {tool}",
  "工具未完成：{tool}": "Tool did not complete: {tool}",
  "已請模型提出新計畫": "Asked the model for a new plan",
  "錯誤已回饋模型，等待修正": "Error sent back to the model; waiting for a correction",
  "已執行後續修正操作": "Follow-up correction ran",
  "受控驗證：刻意注入一次讀取失敗": "Controlled validation: one read failure injected on purpose",
  "成果已保存：{name}": "Artifact saved: {name}",
  "任務完成": "Task completed",
  "任務未完成": "Task failed",
  "已選取目前有效記憶": "Selected current memories",
  "已選取對話背景與專案指令 {path}": "Selected Session context and project instructions {path}",
  "已選取對話背景": "Selected Session context",
  "已依預算省略已保存背景": "Omitted saved context to fit the budget",
  "完整請求超出 Context 上限": "Full request exceeds the context limit",
  "等待你核准計畫": "Waiting for you to approve the plan",
  "等待批准外部操作：{tool}": "Waiting for approval of external call: {tool}",
  "等待批准這次保存": "Waiting for approval to save",
  "這次操作已批准": "Approved",
  "這次操作已拒絕": "Denied",
  "單次批准已用於派發": "One-time approval used for dispatch",
  "批准已失效": "Approval expired",
  "批准因取消而失效": "Approval voided by cancellation",
  "取消請求已保存": "Cancellation request saved",
  "執行器已確認停止": "Runner confirmed it stopped",
  "已派發操作的結果待確認": "Dispatched operation result unconfirmed",
  "受控驗證：工具在同步點暫停": "Controlled validation: tool paused at the sync point",
  "已收到補充指示：{text}": "Instruction received: {text}",
  "補充指示已送給模型": "Instruction sent to the model",
  "補充指示未及送給模型，執行已結束": "Instruction not delivered; the Run had already ended",
  "中斷恢復紀錄已保存": "Recovery checkpoint saved",
  "已載入前次確認結果": "Loaded previously confirmed results",
  "重啟後已檢查中斷狀態": "Checked interrupted state after restart",
  "成果對帳確認：{tool}": "Reconciled with saved artifact: {tool}",
  "沿用已確認結果：{tool}": "Reused confirmed result: {tool}",
  "保留成果已核對": "Kept artifact verified",
  "完成回覆空白，僅允許補交完成摘要": "Final reply was empty; only a completion summary may be submitted",
  "成果目前無法下載，請確認檔案仍然完整。": "The artifact cannot be downloaded right now; check that the file is intact.",
  "沿用前次成果": "Carried over from an earlier Run",
  "已保存": "Saved",
  "基底 {id}": "base {id}",
  "v{from} 與 v{to} 內容相同": "v{from} and v{to} are identical",
  "v{from} → v{to}：新增 {added} 行、刪除 {removed} 行": "v{from} → v{to}: {added} lines added, {removed} removed",
  "顯示這一版全文": "Show full text of this version",
  "固定示範流程僅支援直接保存來源快照。草稿核對須使用本機模型模式。": "The fixed demo flow only saves source snapshots directly. Draft review needs a real model.",
  "模擬模型": "Simulated model",
  "這是一條可檢查的示範流程": "This is an inspectable demo flow",
  "由本機模型選擇工具與產生成果": "A local model chooses tools and produces artifacts",
  "由遠端模型選擇工具與產生成果": "A remote model chooses tools and produces artifacts",
  "先選來源，再交代資料分析或文件整理任務。計畫、工具與成果可逐筆檢查；模型輸出仍需對照來源。": "Pick sources, then describe a data analysis or document task. Plans, tool calls and artifacts can be inspected one by one; model output still needs checking against the sources.",
  "依固定步驟讀取已選來源、保存來源快照；此模式不會理解任意任務。": "Reads the selected sources in fixed steps and saves a source snapshot; this mode does not understand arbitrary tasks.",
  "受控驗證模式：每筆任務首次來源讀取會刻意失敗一次。": "Controlled validation mode: the first source read of each task fails once on purpose.",
  "受控同步點驗證：本服務首個指定工具會暫停，供取消／併發觀察。": "Controlled sync-point validation: the first designated tool in this service pauses so cancellation and concurrency can be observed.",
  "手動保存 · {kind} · {keywords}\n來源註記：{note}": "Saved manually · {kind} · {keywords}\nSource note: {note}",
  "待核實事實": "Fact to verify",
  "、": ", ",
  "所有新任務": "all new tasks",
  "未填寫": "none",
  "編輯": "Edit",
  "編輯 v{version}": "Edit v{version}",
  "保存新版本": "Save new version",
  "刪除": "Delete",
  "已刪除；下一次模型請求不再選入，舊 Run 快照仍保留。": "Deleted. The next model request will not include it; snapshots in earlier Runs are kept.",
  "版本與來源紀錄": "Versions and source record",
  "已明確保存；下一次模型請求依相關性與預算選取。": "Saved. The next model request selects it by relevance and budget.",
  "正在補讀保存事件": "Catching up on saved events",
  "正在連線": "Connecting",
  "事件格式無法辨識，請重新載入紀錄。": "Unrecognized event format; reload the Run.",
  "更新已停止": "Updates stopped",
  "請重新載入執行紀錄": "Reload the Run",
  "連線中斷，可手動重新連線": "Disconnected; you can reconnect manually",
  "連線中斷，正在重連": "Disconnected; reconnecting",
  "連線暫時不可用": "Connection temporarily unavailable"
};

const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const warned = new Set();
function warnMissing(key) {
  if (warned.has(key)) return;
  warned.add(key);
  console.warn(`[i18n] Missing English text for: ${key}`);
}
function fill(text, values) {
  return values ? text.replace(/\{(\w+)\}/g, (match, name) => has(values, name) ? String(values[name]) : match) : text;
}
// t("模型請求 {count} 次", {count: 3}): the zh-TW key, or its English entry, with placeholders filled.
export function t(text, values) {
  if (lang !== "en") return fill(text, values);
  if (has(EN, text)) return fill(EN[text], values);
  warnMissing(text);
  return fill(text, values);
}

const ATTRIBUTES = ["placeholder", "aria-label", "title"];
function looksChinese(text) {
  return [...text].some((char) => {
    const code = char.codePointAt(0);
    return (code >= 0x3000 && code <= 0x9fff) || (code >= 0xff00 && code <= 0xffef);
  });
}
function translated(value) {
  const key = value.trim();
  if (!key) return null;
  if (has(EN, key)) {
    const start = value.indexOf(key);
    return value.slice(0, start) + EN[key] + value.slice(start + key.length);
  }
  if (looksChinese(key)) warnMissing(key);
  return null;
}
// Static markup: text nodes and placeholder / aria-label / title values whose trimmed text is a key.
// Elements marked translate="no" (the language names) are left as written.
export function translatePage(root = document) {
  if (root === document) document.documentElement.lang = lang === "en" ? "en" : "zh-Hant";
  if (lang !== "en") return;
  const skipped = (element) => Boolean(element?.closest('[translate="no"]'));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (skipped(node.parentElement)) continue;
    const value = translated(node.nodeValue);
    if (value !== null) node.nodeValue = value;
  }
  for (const element of root.querySelectorAll("[placeholder],[aria-label],[title]")) {
    if (skipped(element)) continue;
    for (const name of ATTRIBUTES) {
      if (!element.hasAttribute(name)) continue;
      const value = translated(element.getAttribute(name));
      if (value !== null) element.setAttribute(name, value);
    }
  }
}
