# Qwen3.5 9B 較難題組 hard-02（2026-11-16，每題 3 次）

## 目的

hard-01 每題只跑一次，並留下三個問題：委派題的評分條件比題意嚴格、壓縮只測過「答案在單一行」的情況、多個版本的文件只因檔名帶年份才答對。這批針對這三點修改題目，其餘題目保留作為對照，並且每題跑 **3 次**，看結果是否穩定。

[`evaluation/hard-02`](../../../../evaluation/hard-02/cases.json) 定義九個任務，由 [`evaluation/run_suite.py`](../../../../evaluation/run_suite.py) 執行並自動評分。真值與評分條件在任何模型執行前寫入 `cases.json`。27 次執行全部保留並計入，沒有重跑、也沒有挑選較好的結果。

## 結果

**27 次執行中 21 次通過：七題 3/3，兩題 0/3。**

| Case | 通過 | 觀察 |
|---|---|---|
| `policy-unnamed-versions` | **0/3** | 三次都讀了兩個檔案，卻回答已失效的版本（30 天、林怡君），引用 `[E2]` |
| `deputy-approver` | 3/3 | 黃詩涵 |
| `refund-rate` | 3/3 | 13.81%；第一次回答缺少計算引用，被退回後修正 |
| `sheet-ratio` | 3/3 | 102.13% |
| `missing-field` | 3/3 | 回答保固期限「未提供」，售價 2490 元 |
| `long-log-single` | 3/3 | 15,901 bytes 壓縮成 859 bytes（4 段） |
| `long-log-scattered` | 3/3 | 16,111 bytes 壓縮成 1,311 bytes（4 段），三次中斷事件全部列出 |
| `delegated-vendors` | 3/3 | 兩個子代理，列出兩家的單價與交期並比較 |
| `report-artifact` | **0/3** | 模型在計算後又用 `source_read` 讀了整份 `orders.csv`，請求超過上下文上限，Run 以 `context_limit_exceeded` 停止 |

## 觀察

1. **三次執行的結果完全相同**。每一題的三次答案與工具順序都逐字一致（本機模型溫度為 0）。所以「每題 3 次」這次量到的是可重現性，不是變異：兩個 0/3 是穩定的失敗，不是偶發。要量變異，需要改用非零溫度，或對題目做小幅改寫。
2. **`policy-unnamed-versions` 是模型的推理錯誤，harness 擋不住**。hard-01 中檔名帶有年份時答對；這次檔名改成 `policy-a.md`／`policy-b.md`，模型兩份都讀了（這點由 `observed_reads` 記錄，三次皆是），仍選了「失效日期 2026-06-30」的那一版來回答 2026-09-01 的問題。引用格式正確、被引用的證據也確實存在，所以所有驗證器都通過。這正是[設計決策](../../../design-decisions.zh-TW.md)第 2 點所說的代價：驗證器不讀任務，判斷不了「哪個版本適用於題目問的日期」。
3. **`report-artifact` 的失敗來自保守的上下文上限**。hard-01 的同類題目中，模型讀的是政策檔而通過；這次模型在兩次 `table_aggregate` 之後又讀了整份 4 KB 的 CSV。剛讀回來的結果不能被省略，請求的保守上限估計是 10,440 token，超過 8,192。Run 如設計停止，沒有默默截斷，也沒有發布任何內容。這題沒有開啟壓縮；上限是以「每個 byte 至多一個 token」估算，實際 token 數會少得多，但在沒有驗證過的 tokenizer 下，harness 選擇保守。
4. **壓縮在答案分散時仍然成立**。三次中斷事件分別落在第 1、2、3 段摘要，全部被保留。不過摘要本身有模型寫錯的地方：第一段寫了「維運日誌共 39 週」（實際上那一段只涵蓋前 39 週左右）。這個錯誤沒有影響答案，但說明摘要確實是有損、且可能不準確的模型輸出，標示是必要的。
5. **子代理報告現在會寫出實際檔名**。hard-01 發現子代理把引用標籤寫成字面的「來源」；修正後，這批的報告文字是「單價為每台 1850 元 (vendor-a.md)」。
6. **評分條件修正後委派題通過**。題目現在明確要求列出兩家各自的單價與交期，和評分條件一致。
7. **樣本仍然小，而且只有一個模型**：九題、單一 9B 模型。Claude 與 OpenAI 相容供應商仍未涵蓋（沒有付費 API 金鑰）。

## 版本與設定

- Git 提交：`54c0fb0`（題組檔案 `evaluation/hard-02` 與 `evaluation/run_suite.py` 當時尚未提交，與本批在同一個提交中加入）
- 模型：`qwen3.5:9b-q4_K_M`，Ollama 0.40.10，JSON tools 協定，thinking 關閉，上下文上限 8,192 token
- 工作台：`--allow-subagents`；各 case 的旗標見 `cases.json`
- 限額：每個 Run `max_steps` 14、`max_tool_calls` 24、`max_corrections` 2、`timeout_seconds` 420
- 執行方式：`uv run python evaluation/run_suite.py evaluation/hard-02 <輸出資料夾> --repeat 3`
- 每次執行的完整事件在同目錄的 `<case>-<次數>.ndjson`，逐次結果在 `results.json`，各題通過次數在 `summary.json`
