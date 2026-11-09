// Explicit user-owned memory editor. Conversation text is never copied automatically.
import {t} from "/assets/i18n.js?v=1.1";
export function memoryPanel(api, showError) {
  const $ = (id) => document.getElementById(id);
  let workspace = null, editing = null, busy = false, saving = false, generation = 0;
  function controls() {
    $("memory-panel").querySelectorAll("button,input,select,textarea").forEach((element) => {
      element.disabled = busy || saving || !workspace;
    });
  }
  function reset() {
    editing = null; $("memory-form").reset();
    $("memory-editor-heading").textContent = t("新增記憶");
    $("memory-save").textContent = t("明確保存記憶");
  }
  async function load(value) {
    const token = ++generation;
    workspace = value; reset(); $("memory-list").replaceChildren(); controls(); $("memory-feedback").textContent = "";
    const identity = value.id;
    const memories = await api(`/api/workspaces/${identity}/memories`);
    if (generation !== token) return false;
    $("memory-count").textContent = memories.length;
    $("memory-list").replaceChildren();
    for (const memory of memories) {
      const card = document.createElement("article"); card.className = "memory-item";
      const title = document.createElement("strong"); title.textContent = `${memory.title} · v${memory.version}`;
      const text = document.createElement("p"); text.textContent = memory.content;
      const note = document.createElement("small");
      note.textContent = t("手動保存 · {kind} · {keywords}\n來源註記：{note}", {kind: memory.kind === "preference" ? t("偏好") : t("待核實事實"), keywords: memory.keywords.join(t("、")) || t("所有新任務"), note: memory.origin.note || t("未填寫")});
      const edit = document.createElement("button"); edit.type = "button"; edit.className = "subtle-button"; edit.textContent = t("編輯");
      edit.addEventListener("click", () => {
        editing = memory;
        $("memory-title").value = memory.title; $("memory-kind").value = memory.kind;
        $("memory-content").value = memory.content; $("memory-keywords").value = memory.keywords.join(", ");
        $("memory-note").value = memory.origin.note;
        $("memory-editor-heading").textContent = t("編輯 v{version}", {version: memory.version});
        $("memory-save").textContent = t("保存新版本"); $("memory-content").focus();
      });
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "subtle-button"; remove.textContent = t("刪除");
      remove.addEventListener("click", () => mutate(async () => {
        const mutationGeneration = generation;
        await api(`/api/workspaces/${identity}/memories/${memory.id}/delete`, {expected_version: memory.version});
        if (generation !== mutationGeneration) return;
        if (await load(value)) $("memory-feedback").textContent = t("已刪除；下一次模型請求不再選入，舊 Run 快照仍保留。");
      }));
      const history = document.createElement("details"), summary = document.createElement("summary"), versions = document.createElement("pre");
      summary.textContent = t("版本與來源紀錄");
      history.append(summary, versions);
      history.addEventListener("toggle", async () => {
        if (!history.open) return;
        try { versions.textContent = JSON.stringify(await api(`/api/workspaces/${identity}/memories/${memory.id}/versions`), null, 2); }
        catch (error) { showError(error); }
      });
      card.append(title, text, note, edit, remove, history); $("memory-list").append(card);
    }
    controls();
    return true;
  }
  async function mutate(operation) {
    if (busy || saving || !workspace) return;
    const mutationWorkspace = workspace;
    saving = true; controls();
    try { await operation(); } catch (error) { if (workspace === mutationWorkspace) showError(error); }
    finally { saving = false; controls(); }
  }
  $("memory-reset").addEventListener("click", reset);
  $("memory-form").addEventListener("submit", (event) => {
    event.preventDefault();
    mutate(async () => {
      const value = workspace, mutationGeneration = generation;
      const body = {title: $("memory-title").value, content: $("memory-content").value, kind: $("memory-kind").value,
        keywords: $("memory-keywords").value.split(/[,\uFF0C]/).map((item) => item.trim()).filter(Boolean), source_note: $("memory-note").value};
      const url = `/api/workspaces/${value.id}/memories`;
      await api(editing ? `${url}/${editing.id}/versions` : url, editing ? {...body, expected_version: editing.version} : body);
      if (generation !== mutationGeneration) return;
      if (await load(value)) $("memory-feedback").textContent = t("已明確保存；下一次模型請求依相關性與預算選取。");
    });
  });
  return {load, setBusy(value) { busy = value; controls(); }};
}
