# 訂單異常率報告

## 計算結果

| 項目 | 數值 |
| :--- | :--- |
| 總訂單數 | 240 |
| 異常總數 | 15 |
| 整體異常率 | 6.25% |

## 來源引用

- [metrics.csv E1]: 原始資料表結構與樣本。
- [metrics.csv E2]: 訂單總數 (orders) 加總結果。
- [metrics.csv E3]: 異常總數 (exceptions) 加總結果。
- [metrics.csv E4]: 基於 E3/E2 計算之異常率百分比。


## Verified tool evidence
- [E1] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698
- [E2] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698; sum(orders) = 240
- [E3] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698; sum(exceptions) = 15
- [E4] ratio_percent(E3, E2) = 6.250000
