# Qwen3.5 9B 較難題組 hard-01（2026-11-15）

## 目的

先前的場景批次（scenarios-01）題目刻意直接。這批改用較難的題目，並第一次以真實模型執行子代理與上下文壓縮：兩個版本的政策、相近的角色、名稱相似的欄位、跨工作表的比率、來源中沒有的欄位、比上下文還大的日誌，以及要求委派的比較題。

[`evaluation/hard-01`](../../../../evaluation/hard-01/cases.json) 定義八個任務。來源由 `make_sources.py` 決定性產生，真值與評分條件在任何模型執行前寫入 `cases.json`；評分由 `run_cases.py` 的 `grade` 依這些條件自動判定，不看措辭。每個 case 只跑**一次**，沒有重跑。

## 結果

**依執行前寫好的評分條件：7/8 通過。**

| Case | Run ID | 評分 | 答案（節錄） | 用到的工具 | 秒數 |
|---|---|---|---|---|---|
| `policy-in-force` | `b2c9c0da33304cff9f5e69ecbc6df516` | 通過 | 14 天內，營運主管張雅婷 `[refund-policy-2026.md E1]` | source_read ×1 | 27（含模型載入） |
| `deputy-approver` | `ed0bd39de7cc41688e1109142a157acb` | 通過 | 由資深工程師 黃詩涵代理核准 `[escalation.md E1]` | source_read | 13 |
| `refund-rate` | `ed813cdf62834a518808bba8f4ab0e0e` | 通過 | 13.82% `[E4]`（44,278 / 320,479） | table_inspect → table_aggregate ×2 → number_calculate | 22 |
| `sheet-ratio` | `dcb52d2683f140ca80b6052ee59907df` | 通過 | 85.88% `[E5]` | table_inspect ×2 → table_aggregate ×2 → number_calculate | 22 |
| `missing-field` | `a05489af20ea49c0b70f8dd64c34d3f3` | 通過 | 保固期限：未提供；建議售價 2490 元 | source_read | 12 |
| `long-log-compaction` | `7154084a4faa437588bc698c52901e3d` | 通過 | 中斷 47 分鐘，平台組蔡宗翰 `[ops-log.md E1]` | source_read，壓縮 1 次（4 段摘要） | 34 |
| `delegated-vendors` | `6e6e7c2eb06548ca8c533a11d016f9b5` | **未通過** | B 單價較低（1790 元）；A 交期較短（21 天） | subagent_run ×2 | 26 |
| `report-artifact` | `8ad27bbf319f4cebb4b86ca53de02167` | 通過 | `refund-summary.md`：320479、44278、14 天內，各附引用 | table_aggregate ×2、source_read、artifact_write | 21 |

## 觀察

1. **`delegated-vendors` 未通過的原因是評分條件，不是結論**。兩個結論（B 較便宜、A 較快）都正確，子代理也各被呼叫一次，兩份報告都通過引用驗證。但執行前寫下的條件要求答案同時包含四個數字（1790、1850、21、35），模型只寫了勝出的兩個。題目只問「哪一家」，這個條件比題意嚴格；依規則仍記為未通過，條件沒有在事後修改。
2. **子代理的報告把引用標籤寫成字面的「來源」**。子代理的系統提示以 `[來源 EID]` 說明格式，模型照抄了「來源」兩個字，因此父 Run 收到的報告文字是「單價為每台 1850 元 (來源)」。報告的 `sources` 欄位仍正確記錄了 `vendor-a.md` 與其 SHA-256，所以可追溯，但報告文字本身沒有說出檔名。這是 harness 的缺陷，已在這批**之後**修正（改寫引用時改用證據記錄中的實際來源路徑）；這批沒有因此重跑。
3. **壓縮如設計運作**。`ops-log.md` 有 15,901 bytes，超過 8,192 token 的上下文上限；未開啟壓縮時這個 Run 會以 `context_limit_exceeded` 停止。開啟後分 4 段摘要成 859 bytes，請求降到 5,548 token 以內。含答案的那一行落在第 2 段，摘要保留了「47 分鐘」與「蔡宗翰」。`tool.completed` 事件仍保有 15,901 bytes 的全文。這題的答案只在一行、摘要時又帶著題目，屬於對壓縮有利的情況；答案分散在多段、或題目需要逐行細節時，摘要可能漏掉。
4. **兩題被驗證器退回一次後修正**：`refund-rate` 第一次回答缺少計算引用（`missing_calculation_citation`），`long-log-compaction` 第一次回答沒有引用（`missing_answer_citation`）。兩者都在修正額度內補上。
5. **`policy-in-force` 只讀了 2026 年版**。答案正確，但模型沒有讀 2025 年版來確認它已失效；題目若把生效中的版本放在名稱不明顯的檔案裡，這個做法可能出錯。
6. **部分答案使用裸引用**（`[E4]`、`[E1]`），驗證器接受；這是先前已記錄的寬鬆之處。
7. **樣本仍然很小**：八題、各一次、單一 9B 模型。這批顯示各機制在真實模型下走得通，並不足以估計正確率。

## 版本與設定

- Git 提交：`33691e3` 加上未提交的無頭模式 `--timeout-seconds` 參數（與本批在同一個提交中加入）
- 模型：`qwen3.5:9b-q4_K_M`，Ollama 0.40.10，JSON tools 協定，thinking 關閉，上下文上限 8,192 token
- 工作台：`--allow-subagents`；各 case 的旗標見 `cases.json`（`--answer`、`--compact`、`--subagents`）
- 限額：每個 Run `max_steps` 14、`max_tool_calls` 24、`max_corrections` 2、`timeout_seconds` 420
- 執行方式：`uv run python evaluation/hard-01/run_cases.py <輸出資料夾>`
- 每個 case 的完整事件在同目錄的 `<case>.ndjson`，摘要與評分在 `results.json`
- 沒有涵蓋：Claude 與 OpenAI 相容供應商（沒有付費 API 金鑰）
