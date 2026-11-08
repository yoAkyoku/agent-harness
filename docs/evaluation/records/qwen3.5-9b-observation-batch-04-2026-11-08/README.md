# Qwen3.5 9B 品質觀察批次 04（2026-11-08）

## 結果

在[批次 03](../qwen3.5-9b-observation-batch-03-2026-11-08/README.md) 之後，把 `mixed_source_roles` 的回饋改成直接指出要改的那一行（`139a328`），以相同模型、設定、輸入與限額重跑六例。

**人工覆核 4/6 接受（與批次 02、03 相同）。** 五例的成果與完成回覆和批次 03 逐 byte 相同；只有 `documents-missing` 改變：

- 第一次送出的成果與前三批相同（升級聯絡人 Mika 被填成 `handoff.md` 的負責人），在發布前被擋下，並收到新的回饋：「若『負責人』是 Owner：handoff.md 沒有 Owner，請改為未提供……」。
- **這次模型照回饋改了**，但改法鑽了檢查的漏洞：它把兩行標籤改成 `負責人 (Owner): Sora` 與 `負責人 (Incident escalation contact): Mika.`。兩個輸出標籤字面上不同，檢查把它們當成兩個欄位，於是成果被發布。
- 讀者看到的仍是「負責人：Mika」，完成回覆也這樣寫；自動檢查 `missing_field_values_explicit` 失敗。判定不接受。

總執行時間 270.28 秒；模型呼叫 37、工具呼叫 45。

## 執行環境差異

GPU 同時被使用者的 Unreal Editor 佔用（載入前約剩 3.9 GB），依使用者先前的決定直接執行。耗時不能與批次 01 比較。

## 版本與設定

- Git 提交：`139a328428e55b941b73db4654f9898a7e58d31d`（實作目錄無未提交修改）
- 實作來源 SHA-256：`2537fcd3d2f7ec3d29742735e15224fd4fd74f843827480fba092daf8b536464`
- Recorder SHA-256：`37554c484cc8926fa22b785c1f5af65646f0adc77aa844795d19b0f3d8ddd417`
- 驗收套件 SHA-256：`fd5e2c378d98646d6286912742bdaecb1ad9e9504cce2ab01ee7833e909d3d42`
- 模型：`qwen3.5:9b-q4_K_M`，digest `56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`；Ollama `0.40.10`；JSON tools；thinking 關閉；context 8192 tokens；直接發布；模型摘要完成
- 每例限制：輸出預留 1024 tokens、180 秒、最多 12 steps、20 tool calls、2 corrections

## 六例紀錄

| Case | Run ID | 狀態 | 人工覆核 | 與批次 03 比較 |
|---|---|---|---|---|
| `analysis-standard` | `f6026ac5d0234f608a51838460d5376c` | completed | 接受 | 相同 |
| `analysis-variant` | `f33640fb0dd84a438b43e019f57e4325` | completed | 接受 | 相同 |
| `analysis-missing` | `2de7ea3762a744daa2eb338057cbd327` | completed | 接受 | 相同 |
| `documents-standard` | `9eb6fdd711884eb5b3bc081a83fb3450` | completed | 不接受 | 相同 |
| `documents-variant` | `0f8235e528424069aadd411ba5b72aa7` | completed | 接受 | 相同 |
| `documents-missing` | `119ee889dd884650abba55a996cf95e4` | completed | 不接受 | 一次拒絕後改寫標籤繞過檢查並發布 |

## 解讀

- **更具體的回饋確實讓模型行動了**：批次 03 兩次重送相同內容；這次第一次修正就改了成果。
- **但模型選了最省力、而不是正確的改法**：它把角色寫進標籤，讓檢查看不出兩個值在同一個欄位，而不是照回饋把負責人標為未提供。這是驗證器的漏洞，不是模型「修好了」。
- 可能的通用修正：分組時先移除標籤裡的括號註記（`負責人 (Owner)` 與 `負責人 (Incident escalation contact)` 都視為「負責人」），並只在**值**本身寫明來源角色時才豁免。尚未實作；改完需要先重放全部錄製成果確認沒有誤殺，再重跑。

後續：漏洞已在 `8e90ec1` 補上，真模型結果見[批次 05](../qwen3.5-9b-observation-batch-05-2026-11-08/README.md)——繞法被擋下，該例在限額內失敗、不發布錯誤內容。
