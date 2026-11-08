# 訂單異常率報告

## 資料來源
- `metrics.csv` (SHA256: c0c04b429bc6bcfeac5c5f0eac8d4ee604b151bd2ad872680afc5a59d3d920e8)

## 欄位檢查
- **可用欄位**: `week`, `orders`
- **缺失欄位**: 資料集中無「異常數」相關欄位。

## 計算結果
- **總訂單數**: 150 (來源: E2)
- **異常總數**: 無法計算 (缺項：無異常數欄位)
- **整體異常率**: 無法計算 (缺項：無異常數欄位)

> 註：因資料集缺少「異常數」欄位，僅能提供總訂單數。異常率需待補齊異常數據後方可計算。


## Verified tool evidence
- [E1] metrics.csv:L2-L3 SHA-256 c0c04b429bc6bcfeac5c5f0eac8d4ee604b151bd2ad872680afc5a59d3d920e8
- [E2] metrics.csv:L2-L3 SHA-256 c0c04b429bc6bcfeac5c5f0eac8d4ee604b151bd2ad872680afc5a59d3d920e8; sum(orders) = 150
