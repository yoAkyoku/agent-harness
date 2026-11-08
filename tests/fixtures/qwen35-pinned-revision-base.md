# metrics.csv 分析報告

## 數據來源
- **metrics.csv** (E1)

## 計算結果
| 項目 | 數值 |
| :--- | :--- |
| 訂單總數 | 450 [E2] |
| 異常總數 | 30 [E3] |
| 整體異常率 | 6.67% [E4] |

## 來源引用
- E1: metrics.csv (原始數據)
- E2: orders 欄位總和
- E3: exceptions 欄位總和
- E4: 異常率計算結果

## Verified tool evidence
- [E1] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b
- [E2] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b; sum(orders) = 450
- [E3] metrics.csv:L2-L4 SHA-256 0b83926d68f86f8d019ecdede89333a607dd320351583d42f7d0e2731f02155b; sum(exceptions) = 30
- [E4] ratio_percent(E3, E2) = 6.666667
