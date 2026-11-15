# 一次 Run 的逐步導覽

[English](walkthrough.md)

這份導覽依照 [hard-02 批次](evaluation/records/qwen3.5-9b-hard-02-2026-11-16/README.md)保存的事件，走過一次真實的 Run（`delegated-vendors-1.ndjson`，Run `8af289e1…`，本機 Ollama 上的 Qwen3.5 9B）。內容沒有任何擺拍：每個數字都在那個檔案裡。整個過程約五分鐘可以講完，文末的指令可以重現同一個 Run。

**任務。** 選定兩份供應商報價作為來源。任務要求模型把兩份報價各交給一個子代理，列出兩家的單價與交期，並指出哪一家較便宜、哪一家較快。

## 1. 受理：模型被呼叫之前，Run 能碰什麼已經固定

`run.accepted` 記錄了模型、限額與政策（`subagents: true`）。兩個檔案在這時計算雜湊；之後內容若改變，讀取會被拒絕，而不是默默讀到新版本。工作台以 `--allow-subagents` 啟動，Run 也要求了子代理：兩者缺一不可。

## 2. 第 1 步：只提供規劃工具

```
5  model.requested   step 1   offered_tools: [plan_update]            上限估計 3,978 / 8,192 token
9  plan.updated      steps: [檢查輸入。, 依證據作答。, 核對成果。]
```

計畫保存之前，請求中只有 `plan_update` 這個工具；呼叫其他工具會因「未提供」被拒絕。每個請求都先量測；這裡顯示的是保守的上限估計，必須低於上下文大小。

## 3. 第 2–3 步：工具目錄中出現委派

```
11 model.requested   step 2   offered_tools: [plan_update, source_read, subagent_run, workspace_list]
18 tool.requested    subagent_run   {task: "請查詢 vendor-a.md 中的單價與交期。", sources: [vendor-a.md]}
19 tool.dispatched   subagent_run
```

`tool.dispatched` 在任何東西執行之前就已寫入。如果行程在這裡當掉，Run 會被回報為中斷、這次呼叫的效果未知；要繼續必須明確建立新的 Run。

## 4. 子代理內部：全新且較小的上下文、較少的工具

```
20 subagent.started          sources: [vendor-a.md]
21 subagent.model_requested  step 1   offered_tools: [source_read]                上限估計 2,308
23 subagent.tool_completed   source_read vendor-a.md
24 subagent.model_requested  step 2   offered_tools: [source_read, task_finish]   上限估計 3,114
26 subagent.completed        steps 2   驗證時對照：vendor-a.md
```

子代理第一個請求的上限估計是 2,308 token，而父 Run 這時已超過 5,500：子代理只看到被委派的那一句話，看不到父 Run 的對話。它只被提供 `source_read`，沒有任何能發布或委派的工具；`task_finish` 要等它有證據可引用之後才出現。它的兩次模型呼叫和一次工具呼叫都計入父 Run 的用量。

## 5. 報告通過驗證後才能跨越邊界

子代理的回答是 `…單價為每台 1850 元 [來源 E1]…`（它照抄了指示中的範例標籤，沒有寫檔名）。這個回答先用和直接回答相同的驗證器，對照子代理自己的證據檢查。之後父 Run 收到一筆證據：

```
27 tool.completed   subagent_run   evidence_id: E1
     text:    根據 vendor-a.md，供應商 A 的單價為每台 1850 元 (vendor-a.md)，交期為下單後 21 天 (vendor-a.md)。
     sources: [{path: vendor-a.md, sha256: eb24c6fd…}]
```

文字裡已經沒有子代理的 `E1`：父 Run 有自己的 `E1`，兩者不能混淆。改寫時用的是子代理證據記錄中的實際路徑，而不是它自己寫的標籤。這筆記錄保留了報告驗證時對照的檔案與雜湊。第二份報價以同樣方式處理（事件 30–39），成為父 Run 的 `E2`。

## 6. 答案先檢查，再連同依據一起記錄

```
42 tool.requested    task_finish
45 answer.verified   cited: E1 → 子代理報告，驗證時對照 vendor-a.md（eb24c6fd…）
                            E2 → 子代理報告，驗證時對照 vendor-b.md（0be18763…）
46 run.completed
```

答案在每個數字旁引用 `[subagent E1]` 與 `[subagent E2]`。`answer.verified` 記錄了這兩筆都是模型撰寫的報告，並列出它們背後的來源版本；讀者從答案中的數字到檔案雜湊，只需要兩步。全程 9 次模型呼叫、9 次工具呼叫，輸入 7,749 token、輸出 627 token，37 秒。

## 7. 被退回時是什麼樣子

同一批中（`refund-rate-1.ndjson`），模型第一次回答時引用了百分比，卻沒有為兩個欄位總和引用產生它們的計算。驗證器退回了它：

```
tool.rejected         task_finish   code: missing_calculation_citation
correction.available  correctable_fields: [summary]   remaining_corrections: 1
```

錯誤訊息引述了那一行，指出缺少的引用（`328517：[orders.csv E2]`、`45381：[orders.csv E3]`）、模型可以修改的欄位，以及剩餘的修正次數。下一次決策補上引用後被接受。被退回的那次嘗試仍留在事件中。

## 8. 停下來時是什麼樣子

`report-artifact-1.ndjson` 沒有完成。模型在彙總兩個欄位之後，又讀了整份 4 KB 的表格；請求再也放不進去：

```
context.pruned   source_excerpt_budget … acknowledged_evidence … tool_descriptions … artifact_name_descriptions
context.limited  upper_bound_tokens: 10,440   capacity_tokens: 8,192
run.failed       context_limit_exceeded
```

嘗試過的每個步驟都依序留在記錄中。Run 沒有截斷表格，也沒有發布不完整的報告。同一個任務若加上 `--compact`，就會被允許改讀表格文字的摘要。

## 重現

```bash
uv run python evaluation/hard-02/make_sources.py
```

```bash
uv run python -m agent_harness --model qwen3.5:9b-q4_K_M --decision-protocol json_tools --allow-subagents
```

在第二個終端機，把 `AGENT_HARNESS_TOKEN` 設為工作台印出的權杖：

```bash
uv run python -m agent_harness.headless --workspace evaluation/hard-02/workspace --source vendor-a.md --source vendor-b.md --answer --subagents --task "請把兩份報價各交給一個子代理查詢單價與交期。回答時列出兩家各自的單價與交期，並指出哪一家單價較低、哪一家交期較短，附來源。"
```

同一個 Run 的 OpenTelemetry trace（Run 是 `invoke_agent` span，每個子代理是巢狀的 `invoke_agent` span，掛在它的 `execute_tool subagent_run` span 之下）：

```bash
curl -s -H "Authorization: Bearer $AGENT_HARNESS_TOKEN" http://127.0.0.1:8787/api/runs/RUN_ID/trace
```

想在瀏覽器裡看的話，打開工作台印出的網址，選這兩個檔案，勾選「允許把子問題交給子代理」與直接回答，再貼上任務。時間軸顯示的就是這些事件被保存時的樣子。
