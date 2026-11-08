# 訂單異常率分析報告

## 資料來源檢查
- **檔案**: metrics.csv
- **欄位**: `week`, `orders`
- **缺失欄位**: `abnormal_orders` (異常數)
- **結論**: 缺少「異常數」欄位，無法計算異常總數及整體異常率。

## 可計算項目
- **訂單總數**: 150 (來源: E2)

## 缺項說明
- **異常總數**: 資料中無 `abnormal_orders` 欄位，無法取得。需補充相關數據才能計算。
- **整體異常率**: 因缺少異常總數，無法計算 (公式：異常數 / 訂單數 × 100)。

---
*報告生成時間*: 2023-10-27
*資料來源引用*: E1, E2

## Verified tool evidence
- [E1] metrics.csv:L2-L3 SHA-256 c0c04b429bc6bcfeac5c5f0eac8d4ee604b151bd2ad872680afc5a59d3d920e8
- [E2] metrics.csv:L2-L3 SHA-256 c0c04b429bc6bcfeac5c5f0eac8d4ee604b151bd2ad872680afc5a59d3d920e8; sum(orders) = 150
