# 訂單分析報告

## 計算結果
- **訂單總數**: 240 (來源: E2)
- **異常總數**: 15 (來源: E3)
- **整體異常率**: 6.25% (來源: E4)

## 資料來源
- [metrics.csv](metrics.csv) (E1)


## Verified tool evidence
- [E1] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698
- [E2] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698; sum(orders) = 240
- [E3] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698; sum(exceptions) = 15
- [E4] ratio_percent(E3, E2) = 6.250000
