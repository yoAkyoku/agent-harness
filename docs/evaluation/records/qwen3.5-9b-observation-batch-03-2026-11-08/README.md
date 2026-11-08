# Qwen3.5 9B 品質觀察批次 03（2026-11-08）

## 結果

在[批次 02](../qwen3.5-9b-observation-batch-02-2026-11-08/README.md) 之後加入角色一致性檢查（`consistent_source_roles`，`8402eeb`），以相同模型、設定、輸入與限額重跑六例。

**人工覆核 4/6 接受（與批次 02 相同）。** 五例的成果與完成回覆和批次 02 逐 byte 相同；只有目標案例 `documents-missing` 改變：

- **Harness 行為正確**：模型第一次送出的成果與前兩批發布的完全相同（把升級聯絡人 Mika 填成 `handoff.md` 的負責人），這次在發布前被 `mixed_source_roles` 擋下，錯誤的負責人沒有進入任何成果。
- **模型沒有利用回饋**：兩次修正機會中，模型兩次都送出一字不差的內容，中間只重存計畫。用完修正額度後 Run 以 `correction_limit_exceeded` 失敗，沒有成果。
- 所以這例從「完成但內容錯誤」變成「明確失敗、不發布錯誤內容」。人工覆核結果不變（不接受），但失敗的方式更安全。

總執行時間 195.49 秒；模型呼叫 37、工具呼叫 45；修正：`documents-variant` 一次自然修正（成功），`documents-missing` 兩次（未成功）。

## 執行環境差異

GPU 同時被使用者的 Unreal Editor 佔用（載入前約剩 3 GB），依使用者先前的決定直接執行。模型仍回報 5.45 GB 在 VRAM。耗時不能與批次 01 比較。

## 版本與設定

- Git 提交：`50bcdc8c94b7962136be0371cef9e3f6c2efaafa`（實作目錄無未提交修改）
- 實作來源 SHA-256：`01630ac8685937ef2a13657fba61951530667d260557bbb47e8054797b2f02d4`
- Recorder SHA-256：`37554c484cc8926fa22b785c1f5af65646f0adc77aa844795d19b0f3d8ddd417`（與批次 01、02 相同）
- 驗收套件 SHA-256：`fd5e2c378d98646d6286912742bdaecb1ad9e9504cce2ab01ee7833e909d3d42`
- 模型：`qwen3.5:9b-q4_K_M`，digest `56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`；Ollama `0.40.10`；JSON tools；thinking 關閉；context 8192 tokens；直接發布；模型摘要完成
- 每例限制：輸出預留 1024 tokens、180 秒、最多 12 steps、20 tool calls、2 corrections

## 六例紀錄

| Case | Run ID | 狀態 | 人工覆核 | 與批次 02 比較 |
|---|---|---|---|---|
| `analysis-standard` | `f903b00b77144843bb28d53c147fc8f0` | completed | 接受 | 成果與回覆相同 |
| `analysis-variant` | `e5067eb9ec9e4f3d96c36177f8acec6c` | completed | 接受 | 成果與回覆相同 |
| `analysis-missing` | `7e20e3422ee24d798c70d298faeccc4e` | completed | 接受 | 成果與回覆相同 |
| `documents-standard` | `d8177acc5de54f879570abda8c447097` | completed | 不接受 | 成果與回覆相同（誤讀任務結構仍在） |
| `documents-variant` | `fcf1a1e9adfc4e0497890cc68fe76fd6` | completed | 接受 | 成果與回覆相同，同樣一次自然修正 |
| `documents-missing` | `bc150dc1201043d4aba02e01f0bd7ee3` | **failed** | 不接受 | 角色替換被擋下三次；模型重送相同內容，修正額度用完，未發布 |

## 解讀

- 新檢查在真模型上**沒有誤殺**：其餘五例完全不受影響。
- 新檢查**準確擋下**了目標錯誤，harness 也在限額內停止，沒有把錯誤內容當成功發布。
- 瓶頸變成**模型能不能照回饋修改**。目前的回饋只說明哪個欄位混了哪些值；可能的通用改進是讓回饋直接指出要改的那一行與可行做法（例如「`handoff.md` 的負責人沒有來源支持，請改為未提供；Mika 屬於事件升級聯絡人」）。這仍然只用證據產生，不讀任務文字，但尚未實作與驗證。

後續：回饋改成直接指出要改的那一行後，見[批次 04](../qwen3.5-9b-observation-batch-04-2026-11-08/README.md)——模型照回饋行動了，但以改寫標籤的方式繞過檢查。
