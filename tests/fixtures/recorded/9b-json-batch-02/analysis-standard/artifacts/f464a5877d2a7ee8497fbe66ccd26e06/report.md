# 訂單異常率分析報告

## 資料來源
- `metrics.csv` (E1)

## 計算結果
| 指標 | 數值 | 來源引用 |
|------|------|----------|
| 訂單總數 | 240 | E2 |
| 異常總數 | 15 | E3 |
| 整體異常率 | 6.25% | E4 |

## 缺項說明
無。


## Verified tool evidence
- [E1] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698
- [E2] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698; sum(orders) = 240
- [E3] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698; sum(exceptions) = 15
- [E4] ratio_percent(E3, E2) = 6.250000
