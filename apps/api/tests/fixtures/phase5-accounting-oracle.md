# Phase 5 accounting fixture reconciliation

All values are synthetic and are Decimal source strings. This is an expected-result
oracle for P5.2, not a production accounting implementation.

## Brokerage account

The Phase 3 events share `occurred_at` and fixture `created_at`; their UUIDs sort
from `...301` to `...315`. The correction is `+0.0100 EUR`; its reversal is
`-0.0100 EUR`, so their combined cash effect is zero.

| Result | Hand-worked reconciliation | Expected |
| --- | --- | --- |
| EUR cash | `1000.0000 - 20.005 - 100.000 - 1.000 - 50.000 - 0.500 + 150.005 - 0.995 + 10.005 - 1.505 + 0.0100 + 9.2000 + 0.0100 - 0.0100` | `995.2150 EUR` |
| USD cash | `-10.0000` from the source-reported FX conversion | `-10.0000 USD` |
| Buy 1 basis | `100.000 + 1.000` | `101.000` |
| Buy 2 basis | `50.000 + 0.500` | `50.500` |
| Sale allocation | `1.250` from Buy 1 plus `0.750` from Buy 2 | `101.000 + 15.150 = 116.150` basis |
| Sale proceeds | `150.005 - 0.995` | `149.010` |
| Realized gain | `149.010 - 116.150` | `32.860` |
| Open lot before split | Buy 2: `2.500 - 0.750` | `1.750`, basis `35.350` |
| 2:1 split | `1.750 × 2`; total basis unchanged | `3.500`, basis `35.350` |

Purchase fees share `trade-buy-1` / `trade-buy-2` with their respective buys.
The sale and its fee share `trade-sell-1`. The withholding tax remains a separate
cash fact and is not included in the sale basis, proceeds, or realized gain.

## Manual accounts and aggregation

| Account | Hand-worked reconciliation | Expected |
| --- | --- | --- |
| Cash | `100.0000 + 25.000 - 10.000 - 40.000` | `75.0000 EUR` |
| Savings | `500.0000 + 40.000` | `540.0000 EUR` |
| Targeted EUR reserve | `300.0000 + 50.000` against target `500.0000` | `350.0000 EUR`, `0.7000` progress |
| Untargeted EUR reserve | opening balance | `200.0000 EUR`; excluded from target progress |
| Targeted USD reserve | opening balance | `80.0000 USD`; makes target aggregation unavailable without source-reported EUR evidence |

The paired cash-to-savings transfer uses `transfer-cash-savings-1`: `-40.000 EUR`
and `+40.000 EUR`, so it has no aggregate owner-cash effect. Native balances are
`2160.2150 EUR` and `70.0000 USD`. A whole-owner EUR aggregate is unavailable:
the fixture does not infer FX for USD cash.

## Expected incomplete histories

- `ACCOUNTING_NON_EUR_AGGREGATION_UNAVAILABLE`: targeted USD reserve lacks
  source-reported EUR evidence.
- `ACCOUNTING_OVERSELL`: sell `4.000` units after the valid history leaves only
  `3.500` units.
- `ACCOUNTING_NON_NEGATING_REVERSAL`: a `-1.0000 EUR` reversal linked to the
  original `+1000.0000 EUR` deposit does not negate its linked economic legs.
