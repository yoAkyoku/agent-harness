# Qwen3.5 9B：browser-01 與 search-01 的重複執行（2026-11-18）

## 目的

[browser-01](../qwen3.5-9b-browser-01-2026-11-18/README.md) 的 C 與 [search-01](../qwen3.5-9b-search-01-2026-11-16/README.md) 的 H 都只通過一次。一次通過不是成功率。這一批在 harness 不變的情況下（提交 `0aa86d7` 的程式碼，另加錄影腳本的 `--temperature` 選項）重複執行，看這兩個結果有多穩定。

## 事先寫定的計畫（執行前寫入並提交）

溫度 0 時重複執行會重現同一個 Run（[hard-02 變異批次](../qwen3.5-9b-hard-02-variance-2026-11-16/README.md)已觀察到），所以重複主要在溫度 0.7 下進行。

- **browser-01**（[`scripts/record_real_model.py`](../../../../scripts/record_real_model.py)，時間上限 600 秒，與 C 相同）：
  - D：溫度 0，一次，看 C 能否重現。
  - E、F、G、H：溫度 0.7，各一次。
  - 通過條件與先前相同，寫在腳本說明裡：Run 以 `completed` 結束並保存一份成果。內容是否正確由人閱讀後記錄，不計分。
- **search-01**（`evaluation/run_suite.py evaluation/search-01 <輸出> --repeat 5 --temperature 0.7`）：三題各 5 次，共 15 次；評分條件是 `cases.json` 裡從第一批起就沒有改過的那一份。
- 所有執行都保留並計入，不重跑、不挑選；執行之後不修改評分條件。
- Ollama 在第一次執行前啟動，之後連續執行，所以只有第一次是冷啟動；時間只作參考。

## 結果

（執行後填寫。）
