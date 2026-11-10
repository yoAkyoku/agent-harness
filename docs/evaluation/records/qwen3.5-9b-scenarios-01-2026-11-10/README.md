# Qwen3.5 9B 場景批次 01（2026-11-10）

## 目的

2026-11-10 新增的使用場景（直接回答、Excel、PowerPoint、大型表格、`web_fetch`、Python 沙箱、無人值守）先前只用照劇本回覆的模擬模型測過。這批以真實的本機模型對每個場景各跑**一次**，確認模型能實際選對工具、得到正確答案並正確引用。

[`evaluation/scenarios-01`](../../../../evaluation/scenarios-01/cases.json) 定義七個任務。來源檔由 `make_sources.py` 決定性產生，真值由同一支程式從資料計算，在任何模型執行前寫入 `cases.json`：Q2 總和 6563（Q1 為 7194，用來檢查有沒有讀錯工作表）、500 筆訂單的平均 1020.61、中位數 1031。

## 結果

**7/7 與真值相符。** 每個 case 只跑一次，沒有重跑；全部在限額內結束。

| Case | 場景 | Run ID | 結果 | 用到的工具 | 備註 |
|---|---|---|---|---|---|
| `answer-policy` | 直接回答 | `f596e9b7d4594a2ca7eed689675d247d` | 30 天、林怡君，皆引用 `[policy.md E1]` | source_read → task_finish | 51 秒（含模型載入） |
| `excel-sheet-sum` | Excel | `61791ac82b69411e84e5e855e4a61e91` | 6563 `[sales.xlsx E2]` | table_inspect → table_aggregate | 正確指定 Q2，沒有用到 Q1 |
| `pptx-risks` | PowerPoint | `7eba4489e29244ce9cdad9f5262ea3ed` | 三項風險全對 | source_read | |
| `large-csv-mean` | 大型表格 | `8bded621776b476d9b909c111dec948c` | 1020.61 `[orders.csv E2]` | table_inspect → table_aggregate | 模型看到的是前 20 列預覽，平均由伺服器以全部 500 列計算 |
| `web-example` | 網頁 | `4fd6a1905d4a4e45af1048673f6c5022` | 正確說明 example.com 用於文件範例、不宜用於測試或監控 | source_read → web_fetch | 第一次回答沒有引用，被 `missing_answer_citation` 退回一次後修正 |
| `python-median` | Python 沙箱 | `6491b94459ac4676a839cd881c1731a1` | 1031.0 `[python_run E2]` | table_inspect → python_run | 真實 Docker 容器（無網路、來源唯讀） |
| `unattended-approval` | 無人值守 | `7f42d10fe79b438e953a9e4ad6f6cf5a` | 退出碼 5，Run 已取消，沒有成果 | source_read → 要求核准 `artifact_write` | 符合預期：需要人確認的發布沒有在無人時執行 |

## 觀察與限制

1. **網頁答案的引用格式**：修正後的答案寫成 `[E2]`，沒有附網址。`verify_answer` 只要求括號內有存在的證據 ID，所以接受了；`answer.verified` 事件仍記錄了 E2 對應的網址與雜湊，可追溯，但答案本身對讀者不如 `[https://example.com/ E2]` 清楚。這是驗證器刻意寬鬆的地方，不是錯誤。
2. **題目簡單、樣本很小**：每個場景一題、一次執行，題目刻意直接。這批證明的是「各場景的工具鏈能在真實模型下走通」，不代表模型在困難題目上的品質；先前批次顯示 9B 模型在語意較複雜的任務會出錯。
3. **沒有涵蓋的場景**：圖片與掃描 PDF 的 OCR（這台電腦沒有安裝 Tesseract；OCR 已在 CI 與 Docker 中用真實 Tesseract 驗證）；Claude 與 OpenAI 相容供應商（沒有付費 API 金鑰）。
4. **機器環境**：這台電腦的安全軟體會攔截 HTTPS。`web_fetch` 在 `e5eef0b` 改用系統信任憑證後才能讀到真實網頁；這批在修正之後執行。

## 版本與設定

- Git 提交：`e5eef0b`（實作目錄無未提交修改）
- 模型：`qwen3.5:9b-q4_K_M`，Ollama 0.40.10，JSON tools 協定，thinking 關閉
- 工作台：`--allow-web --python-sandbox python:3.12-slim`；沙箱映像為本機已有的 `python:3.12-slim`，Docker Engine 29.7.2
- 限額：每個 Run `max_steps` 12、`max_tool_calls` 20、`max_corrections` 2
- 執行方式：`uv run python evaluation/scenarios-01/run_cases.py <輸出資料夾>`，每個 case 透過無頭模式送出
- 每個 case 的完整事件在同目錄的 `<case>.ndjson`，摘要在 `results.json`
