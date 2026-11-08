# Metrics Analysis Report

## Data Inspection
- **Source**: `metrics.csv` (E1)
- **Columns Found**: `week`, `orders`, `exceptions`
- **Rows Processed**: 2

## Calculations
- **Total Orders**: 240 (Sum of `orders` column) [E2]
- **Total Exceptions**: 15 (Sum of `exceptions` column) [E3]
- **Overall Exception Rate**: 6.25% (Calculated as Total Exceptions / Total Orders × 100) [E4]

## Conclusion
The dataset contains valid data for all required calculations. The overall exception rate is 6.25%.


## Verified tool evidence
- [E1] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698
- [E2] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698; sum(orders) = 240
- [E3] metrics.csv:L2-L3 SHA-256 c6def025fd7ec5096bc4aebfc7981026fd384f619748f724a0f5d0531f4b6698; sum(exceptions) = 15
- [E4] ratio_percent(E3, E2) = 6.250000
