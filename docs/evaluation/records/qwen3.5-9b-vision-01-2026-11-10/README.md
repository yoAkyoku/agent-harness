# Qwen3.5 9B 圖片批次 vision-01（2026-11-10）

## 目的

`--vision-model` 讓本機視覺模型轉錄圖片來源，轉錄文字標示為模型輸出。這批以真實模型對兩張圖片各跑**一次**，確認轉錄、引用與標示在實際模型下都能運作。

[`evaluation/vision-01`](../../../../evaluation/vision-01/cases.json) 定義兩個任務。圖片由 `make_sources.py` 從固定數值以 Pillow 繪製，真值在任何模型執行前寫入 `cases.json`：表格圖中 east 為 508；長條圖中最多的是 Thu，62 張。

## 結果

**2/2 與真值相符。** 每個 case 只跑一次，沒有重跑。

| Case | Run ID | 答案 | 用到的工具 | 秒數 |
|---|---|---|---|---|
| `table-image-value` | `25438c9d6b9e486d9a02de6661a3becb` | east 地區的 Units 是 508 `[sales-table.png E1]` | plan_update → source_read → task_finish | 34（含模型載入） |
| `chart-image-max` | `2d08b690a36f4358adfc921de7add422` | 星期四（Thu），共 62 張 `[tickets-chart.png E1]` | plan_update → workspace_list → source_read → task_finish | 15 |

兩題的 `source_read` 結果都以 `[image, model transcription by qwen3.5:9b-q4_K_M; not verbatim source text]` 開頭，`answer.verified` 事件也都記錄了 `transcription`（模型名稱與 digest `56671c2a…`）。

## 觀察與限制

1. **表格轉錄完全正確**：五列都以 `cell | cell` 呈現，數字無誤。
2. **長條圖的對應關係沒有寫出來**：轉錄是先列出五個數字（34、51、47、62、29），再列出五個星期名稱，沒有寫成「Thu | 62」。模型依兩組資料的順序推得 Thu 對 62，答案正確；但這個對應是推論出來的，證據文字本身沒有寫明。版面更複雜的圖（例如數字標籤與長條錯開）可能因此配錯，驗證器也查不出來。
3. **沒有和 OCR 交叉比對**：這台電腦沒有安裝 Tesseract，所以 `unconfirmed_numbers` 沒有實際執行過（測試中以替代程式驗證）。
4. **圖片簡單、樣本很小**：兩張圖都是乾淨的電腦繪圖，字體清楚。照片、掃描、手寫與中文圖片都沒有測。
5. 轉錄與作答用的是同一個模型（`qwen3.5:9b-q4_K_M`），Ollama 只載入一次。

## 版本與設定

- Git 提交：`f3cd197` 加上未提交的 `--vision-model` 實作（本批與該實作在同一個提交中加入）
- 模型：`qwen3.5:9b-q4_K_M`，Ollama 0.40.10，JSON tools 協定，thinking 關閉；`/api/show` 回報能力為 completion、vision、tools、thinking
- 工作台：`--vision-model qwen3.5:9b-q4_K_M`，沒有 `--tesseract`
- 限額：每個 Run `max_steps` 12、`max_tool_calls` 20、`max_corrections` 2
- 執行方式：`uv run python evaluation/vision-01/run_cases.py <輸出資料夾>`
- 每個 case 的完整事件在同目錄的 `<case>.ndjson`，摘要在 `results.json`
