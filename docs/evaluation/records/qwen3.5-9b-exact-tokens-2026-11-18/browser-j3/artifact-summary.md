# 訂單追蹤改版摘要

## 目標
讓客服在同一頁查看訂單進度、配送節點與異常紀錄 [brief.md E1]。

## 已知風險
- 舊系統的配送狀態命名不一致；尚未核對所有歷史欄位 [brief.md E1]。

## 三週訂單數變化
- **總訂單數**：450 [metrics.csv E2, E4]
- **異常訂單總數**：30 [metrics.csv E2, E5]
- **每週數據**（[metrics.csv E2]）：
  - 第1週：120 訂單（12 異常）
  - 第2週：150 訂單（9 異常）
  - 第3週：180 訂單（9 異常）

> 備註：範例資料僅供本機 Demo，沒有客戶個人資料 [notes.md E3]。

## Verified tool evidence
- [E1] brief.md:L1-L7 SHA-256 052b349403397ce33e71bb4eb861f51777312cceba2be98730712c4ae2d0fe57
- [E2] metrics.csv:L1-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b
- [E4] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b; sum(orders) = 450
- [E5] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b; sum(exceptions) = 30
- [E3] notes.md:L1-L6 SHA-256 8c6ad0691bb7dafcacbd7765c6870234c1fbb03d17ff5892cbf8bbccf2817876
