# 受控故障案例 controlled-current-12（2026-11-08）

以目前版本（`72a4bf8`，Qwen3.5 9B、JSON-tools）執行 `scripts/validate_correction.py`。工作台以 `--fault-once-source-read` 啟動：每個 Run 的第一次 `source_read` 在讀檔前被刻意失敗，事件中標示為 `tool.fault_injected`。

## 結果：自動檢查全過、人工覆核接受

1. 第一次 `source_read` 被注入故障，回傳可修正錯誤 `source_unavailable`。
2. 模型做出新的決策，以**新的 call ID** 重讀同一個檔案，成功（`correction.observed`）。
3. 發布 `handoff_summary.md`：負責人 Mei、發布條件 passing regression tests、事件升級聯絡人 Bo，角色標示正確並引用 E1。
4. executor 沒有自動重試或改用其他路徑；修正完全來自模型的新決策。

小瑕疵：任務要求事件升級**窗口**，來源只有聯絡人，成果沒有明說窗口未提供。

Run `c4ec3780b5af495b80a97aa822d99659`，用量 {'model_calls': 5, 'tool_calls': 5}。GPU 與使用者的 Unreal Editor 共用。
