# 訂單異常分析報告

## 計算結果

| 項目 | 數值 |
| :--- | :--- |
| 總訂單數 | 280 |
| 異常總數 | 21 |
| 整體異常率 | 7.5% |

## 來源引用

- [metrics.csv E1]: 原始資料表結構與樣本。
- [metrics.csv E2]: 訂單總數 (orders) 加總結果。
- [metrics.csv E3]: 異常總數 (exceptions) 加總結果。
- [metrics.csv E4]: 基於 E3/E2 計算之異常率百分比。


## Verified tool evidence
- [E1] metrics.csv:L2-L3 SHA-256 1405c39960d3bd5eb343cdfc167ccb589ffb99689548b497709bd6e8db516d9b
- [E2] metrics.csv:L2-L3 SHA-256 1405c39960d3bd5eb343cdfc167ccb589ffb99689548b497709bd6e8db516d9b; sum(orders) = 280
- [E3] metrics.csv:L2-L3 SHA-256 1405c39960d3bd5eb343cdfc167ccb589ffb99689548b497709bd6e8db516d9b; sum(exceptions) = 21
- [E4] ratio_percent(E3, E2) = 7.500000
