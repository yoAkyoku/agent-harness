# Qwen3.5 9B 品質觀察批次 01（2026-11-08）

## 結果

六例以實際本機 Qwen3.5 9B 執行，未使用 transport fixture。這是第 09 片修訂後的**品質觀察批次**（`--observe-all`）：每例跑完都先寫人工覆核，但不接受的案例不會中止批次。

**人工覆核 3/6 接受；自動檢查 5/6 通過。** 六個 Run 全部在限額內完成，沒有逾時或 Context 超限。

- 總執行時間：136.22 秒（六例相加，單例 16.6–31.8 秒）
- 模型呼叫：36；工具呼叫：43；修正：1 次（自然發生，非注入）
- 模型完整放在 GPU（`size_vram` 5,451,095,735 bytes），第 27 批的 130 MB 配置問題沒有出現
- 範圍：`evaluation/slice-09` 六個固定案例，只代表這個版本在這六例的表現，不代表未測任務或正式環境品質

## 版本與設定

- Git 提交：`c98f4f68cc61c70064292dec9696934d1b487526`（實作目錄無未提交修改）
- 實作來源 SHA-256：`efdd734e5628fd1b3f9ebea211d7dbe97e7b36ac49e15c9dbea68ab9d4e9b4bd`
- Recorder SHA-256：`37554c484cc8926fa22b785c1f5af65646f0adc77aa844795d19b0f3d8ddd417`
- 驗收套件 SHA-256：`fd5e2c378d98646d6286912742bdaecb1ad9e9504cce2ab01ee7833e909d3d42`（與第 23 批相同）
- 模型：`qwen3.5:9b-q4_K_M`，digest `56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`
- Ollama `0.40.10`；JSON tools；thinking 關閉；context 8192 tokens；直接發布；模型摘要完成
- 每例限制：輸出預留 1024 tokens、180 秒、最多 12 steps、20 tool calls、2 corrections

與第 23 批相比，模型、設定、輸入與限額都相同；差別只在程式：runtime 已移除針對這六例的規則、系統提示詞由 1684 精簡為 1040 bytes，且不再覆寫模型的完成回覆。

## 六例紀錄

| Case | Run ID | 自動檢查 | 人工覆核 | 修正 | 主要發現 |
|---|---|---|---|---|---|
| `analysis-standard` | `395e31df7b5c49e6945c66e34d2ceced` | 全通過 | 接受 | 無 | 240／15／6.25% 正確且引用正確；完成回覆有一句多餘的缺欄條件句 |
| `analysis-variant` | `070ec1193ea74912a9f6f4d4d894d06f` | 全通過 | 接受 | 無 | 280／21／7.5% 正確；同樣的多餘條件句 |
| `analysis-missing` | `54f345e3500f4e74a1a81c85364da60f` | 全通過 | **不接受** | 無 | 成果正確（150、異常未提供、比率無法計算），但完成回覆宣稱包含異常總數與異常率；另多算一次未要求的平均值 |
| `documents-standard` | `56095dfb38ce471c9769df7089649327` | 全通過 | **不接受** | 無 | 把任務括號說明當成另一欄，`release.md` 已有發布條件卻又標「發布條件或事件升級窗口：未提供」 |
| `documents-variant` | `5a82d979e887435ebbd58fcd97f76241` | 全通過 | 接受 | `missing_body_citation` | 第一次發布未逐份連結來源被擋，模型依回饋補引用後成功；聯絡人放在「事件升級窗口」標籤下（值本身寫明是聯絡人） |
| `documents-missing` | `b2bcf7c14a214273837b8f65d5f280f4` | `missing_field_values_explicit` 失敗 | **不接受** | 無 | `handoff.md` 沒有 Owner，模型把升級聯絡人 Mika 填成負責人；註記宣稱已標未提供但沒有 |

每個 case 目錄保存 `request.json`、`admission.json`、`run.json`、`events.json`、`assessment.json`、`manual-review.json`、來源 hash、loaded model 資訊及下載的成果。

## 解讀

**Harness 行為符合預期。** 六例都在限額內完成，沒有 Context 超限；`documents-variant` 有一次自然發生的「拒絕 → 回饋 → 模型新決策 → 修正成功」。

**三個不接受的案例都是模型語意錯誤，沒有一個是 harness 故障：**

1. **完成回覆與成果不一致**（`analysis-missing`，另兩例的條件句也有同樣傾向）：三個分析案例的完成回覆一字不差，模型像是在套用固定句子，而不是描述這份成果。目前沒有任何檢查比對完成回覆與成果。重構前 runtime 用固定句「已發布 X」直接覆寫回覆，這等於把問題藏起來，而不是偵測出來。
2. **誤讀任務結構**（`documents-standard`）：模型把「關鍵事項（發布條件或事件升級窗口）」拆成兩個欄位。
3. **角色替換**（`documents-missing`）：用另一個角色的人名填補缺少的負責人。

第 2、3 類正是先前被移除的案例專屬規則（`_document_escalation_detail_issues`、`_document_owner_issues`）在擋的問題。第 23 批的 6/6 有一部分來自這些規則，而不是模型本身的能力；這批 3/6 才是這個模型在通用 harness 下的實際表現。

## 後續改進

- **完成回覆（已實作，`c097c86`）**：根本原因是收尾請求只有系統提示與任務文字，模型看不到自己發布的成果，只能重述任務。現在收尾時附上已發布正文，並以 `completion_summary` 比對回覆與成果。拿這批六個真實回覆重放：三個人工接受的全部通過；`analysis-missing` 與 `documents-standard` 的回覆被拒絕（兩者都宣稱包含成果標為缺項的內容）；`documents-missing` 的角色替換不在此檢查範圍。真模型驗證見[批次 02](../qwen3.5-9b-observation-batch-02-2026-11-08/README.md)：成果不變，回覆改為描述實際成果，結果 4/6。
- **角色替換**：可以做成不讀任務文字的證據檢查，例如正文某個欄位填入的值，若在來源中只出現在另一個標籤之後，就回饋給模型。這需要先確認不會誤殺正確內容。
- 任何改進都要再跑一個新批次，並加上規格要求的兩個未見任務，不能只看這六例。
