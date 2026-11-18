# 訂單追蹤改版摘要

## 目標
讓客服在同一頁查看訂單進度、配送節點與異常紀錄 [brief.md E1]。

## 已知風險
- 舊系統的配送狀態命名不一致 [brief.md E1]。
- 尚未核對所有歷史欄位 [brief.md E1]。

## 三週訂單數變化
根據 `metrics.csv` [E2]：
- 第1週：120 筆
- 第2週：150 筆 [metrics.csv E4]
- 第3週：180 筆

**總計**：450 筆 [E3]
**平均**：150 筆/週 [E4]

## Verified tool evidence
- [E1] brief.md:L1-L7 SHA-256 052b349403397ce33e71bb4eb861f51777312cceba2be98730712c4ae2d0fe57
- [E2] metrics.csv:L1-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b
- [E3] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b; sum(orders) = 450
- [E4] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b; mean(orders) = 150
