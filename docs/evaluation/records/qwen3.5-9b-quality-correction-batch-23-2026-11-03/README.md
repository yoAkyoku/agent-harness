# Qwen3.5 9B 品質修正與六例驗收

## 結果

六例均以實際本機 Qwen3.5 9B 執行，未使用 transport fixture。每例的自動檢查全數通過，且已逐例檢查來源真值、產物、引用、計畫與完成回覆；六份 `manual-review.json` 均為 `accepted: true`。五例出現可修正的工具拒絕，模型在各案例的修正額度內完成修正並成功發布，沒有重送任何已送出的 case。

- 總執行時間：292.23 秒（六例驗收器記錄值）
- 模型呼叫：40；工具呼叫：57
- 範圍：`evaluation/slice-09` 六個固定案例。本結果只代表此候選版本在這六例通過，不代表未測任務或正式環境品質保證。

## 候選版本與模型設定

- Git 基線：`8f0ae64f64a23c8d0ca9d5b3120d94ae3778736e`，以當時 dirty working tree 建立候選指紋
- 實作來源 SHA-256：`93c32f29efda4627afd2501f70241f5d62f81d4dec5692b18a103723a6618600`
- Recorder SHA-256：`152551b5b51c8a04aca0427a4eba435f7980a3523aaf5f5b6f6d5fd38748c604`
- 驗收套件 SHA-256：`fd5e2c378d98646d6286912742bdaecb1ad9e9504cce2ab01ee7833e909d3d42`
- 模型：`qwen3.5:9b-q4_K_M`，digest `56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`
- Ollama：`0.40.10`；JSON tools；thinking 關閉；context 8192 tokens
- 每例限制：輸出預留 1024 tokens、180 秒、最多 12 steps、20 tool calls、2 corrections

## 品質修正

1. 將計算引用錯誤的回饋縮短，按正文行提供精簡修正方向，避免對每個數值重複塞入長結論而耗盡 context。
2. 在已保存計畫且 artifact 正處於修正／剛發布的決策中，允許空 `plan_update` 作為 no-op，讓模型能接著完成任務，同時仍拒絕不符合條件的空計畫更新。
3. CSV 缺少必要欄位時，回饋要求明確指出 canonical 欄名、以來源 EID 引用缺漏與缺少數值；同時遇到猜測欄名時合併成短回饋，讓模型能在修正額度內完成。

相關 focused public HTTP regression tests 曾以 TDD 驗證，結果為 **9 passed, 1 warning**。這是修正點的針對性測試，不代表全套測試或一般模型能力驗收。

## 六例紀錄

每個 case 目錄保存 `request.json`、`admission.json`、`run.json`、`events.json`、`assessment.json`、`manual-review.json`、來源 hash、loaded model 資訊及下載產物。完整產物 SHA-256 位於各自的 `artifact-manifest.json`。

| Case | Run ID | 自動檢查 | 人工覆核 | 修正拒絕碼 | 產物 |
|---|---|---|---|---|---|
| `analysis-standard` | `4cd4039f1b434c7a9a469bbb6fc50317` | 全通過 | 接受 | 無 | `report.md` |
| `analysis-variant` | `be8c0ab3ac4e4622bd9d49e2a318e70e` | 全通過 | 接受 | `missing_calculation_citation` | `report.md` |
| `analysis-missing` | `afe4637be90e42f09ade810031e5bf35` | 全通過 | 接受 | `missing_calculation_citation`, `unsupported_field_name` | `report.md` |
| `documents-standard` | `89c580f7a7b34e82a9a5831570998939` | 全通過 | 接受 | `missing_body_citation` | `index.md` |
| `documents-variant` | `36f3c745a69041768e048538c2e65e4e` | 全通過 | 接受 | `missing_body_citation` | `index.md` |
| `documents-missing` | `e3c7a904fc4740feb136707d838e0aa9` | 全通過 | 接受 | `output_language_mismatch` | `index.md` |

## 手動覆核重點

- 三個分析案例：訂單／異常總數與比率符合表格及計算工具真值；缺少 `exceptions` 的案例明確指出缺欄，不補零、不捏造異常率，缺漏說明有來源引用。
- 三個文件案例：主題、負責人、發布條件／事件聯絡人逐項對應來源；未提供的升級時段及缺少的 owner 均明確標示，沒有捏造人名或日期。
- 六例均檢查來源 SHA-256、正文引用與 evidence appendix、可見計畫、artifact 發布及完成回覆。被拒的初次工具呼叫保留在事件紀錄中，成功修正後的最終產物才列為接受結果。
