# 訂單追蹤改版摘要

## 目標
讓客服在同一頁查看訂單進度、配送節點與異常紀錄 [brief.md E1]。

## 已知風險
- 舊系統的配送狀態命名不一致 [brief.md E1]。
- 尚未核對所有歷史欄位 [brief.md E1]。

## 三週訂單數變化
- 第一週：120 筆
- 第二週：150 筆
- 第三週：180 筆
- **總訂單數**：450 筆 [metrics.csv E4]
- **總異常數**：30 筆 [metrics.csv E5]

## 討論重點
- 優先處理客服每日查詢的訂單追蹤頁 [notes.md E3]。
- 本週先核對配送狀態欄位，不直接修改正式系統 [notes.md E3]。
- 待確認：退貨與補寄是否需要獨立狀態 [notes.md E3]。

## Verified tool evidence
- [E1] brief.md:L1-L7 SHA-256 052b349403397ce33e71bb4eb861f51777312cceba2be98730712c4ae2d0fe57
- [E2] metrics.csv:L1-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b
- [E3] notes.md:L1-L6 SHA-256 8c6ad0691bb7dafcacbd7765c6870234c1fbb03d17ff5892cbf8bbccf2817876
- [E4] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b; sum(orders) = 450
- [E5] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b; sum(exceptions) = 30
