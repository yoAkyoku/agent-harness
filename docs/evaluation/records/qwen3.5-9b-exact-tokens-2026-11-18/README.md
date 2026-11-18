# Qwen3.5 9B：以精確 token 計數重跑（2026-11-18）

## 目的

[重複執行批次](../qwen3.5-9b-repeat-2026-11-18/README.md)中，`search-absent` 有兩次因 `context_limit_exceeded` 失敗，而保守的上限把每個 byte 當一個 token。提交 `7c8dae4` 加入跨平台的精確計數（`--exact-tokenizer-model`）。這一批只改這一件事，其他設定和重複執行批次相同，看結果有沒有不同。

## 事先寫定的計畫（執行前寫入並提交）

- **唯一的差別**：工作台加上 `--exact-tokenizer-model <Qwen3.5 9B 的 GGUF blob>`。溫度 0.7，時間上限、輸出保留（1,024 tokens）、修正與查找額度都不變。
- **search-01**：`evaluation/run_suite.py evaluation/search-01 <輸出> --repeat 5 --temperature 0.7 --exact-tokenizer-model <blob>`，三題各 5 次；評分條件不變。
- **browser-01**：`scripts/record_real_model.py <輸出> --channel msedge --time-limit 600 --temperature 0.7 --exact-tokenizer-model <blob>`，4 次（J1 到 J4）；通過條件不變，內容由人閱讀後記錄。
- **事先的預期**：`context_limit_exceeded` 應該不再出現，因為同樣的請求只算到約四分之一。其他種類的失敗（工具誤用、輸出上限、評分用詞）是模型的行為，不預期會改變。
- 所有執行都保留並計入，不重跑、不挑選。溫度 0.7 下每次抽樣都不同，5 次與 4 次只能看出大的差異；這一批不能證明成功率提高或降低。

## 結果

（執行後填寫。）
