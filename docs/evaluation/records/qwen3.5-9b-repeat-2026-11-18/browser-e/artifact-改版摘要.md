# 訂單追蹤改版摘要

## 目標
讓客服在同一頁查看訂單進度、配送節點與異常紀錄。

## 已知風險
- 舊系統的配送狀態命名不一致；
- 尚未核對所有歷史欄位。

## 三週訂單數變化
本週（第 1 至 3 週）總訂單數為 450 單 [metrics.csv E4]。

### 來源證據
- **改版目標與風險**：來自 `brief.md` [E1]。
- **三週訂單總計**：來自 `metrics.csv` 中 `orders` 欄位的加總結果 [E4]。


## Verified tool evidence
- [E1] brief.md:L1-L7 SHA-256 052b349403397ce33e71bb4eb861f51777312cceba2be98730712c4ae2d0fe57
- [E4] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b; sum(orders) = 450
