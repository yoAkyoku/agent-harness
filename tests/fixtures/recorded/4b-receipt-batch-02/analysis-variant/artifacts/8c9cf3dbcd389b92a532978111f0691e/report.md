# Metrics Analysis Report

## Data Inspection
- **Source**: `metrics.csv` (E1)
- **Columns Found**: `week`, `orders`, `exceptions`
- **Rows Processed**: 2

## Calculated Values
- **Total Orders**: 280 (E2)
- **Total Exceptions**: 21 (E3)
- **Overall Exception Rate**: 7.5% (E4)

## References
- E1: `metrics.csv` - Full column inspection and sample data.
- E2: Sum of `orders` column from `metrics.csv`.
- E3: Sum of `exceptions` column from `metrics.csv`.
- E4: Ratio calculation (E3 / E2 * 100).


## Verified tool evidence
- [E1] metrics.csv:L2-L3 SHA-256 1405c39960d3bd5eb343cdfc167ccb589ffb99689548b497709bd6e8db516d9b
- [E2] metrics.csv:L2-L3 SHA-256 1405c39960d3bd5eb343cdfc167ccb589ffb99689548b497709bd6e8db516d9b; sum(orders) = 280
- [E3] metrics.csv:L2-L3 SHA-256 1405c39960d3bd5eb343cdfc167ccb589ffb99689548b497709bd6e8db516d9b; sum(exceptions) = 21
- [E4] ratio_percent(E3, E2) = 7.500000
