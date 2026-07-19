# Trade Republic CSV v1 mapping

`trade-republic-csv-v1` is PIA's strict, source-faithful description of one
observed Trade Republic transaction-export dialect. It is a contract for a
future provider adapter, not a parser, upload API, or ledger-write interface.
The observed export used to establish the shape is not included in this
repository. All committed examples are synthetic.

## Version detection and file format

The file is UTF-8 CSV; a leading UTF-8 BOM is tolerated. It uses a comma
delimiter, RFC 4180-style quoting, and either LF or CRLF line endings. The
header must be present exactly once, must have no unknown columns, and must be
in this exact order:

```text
datetime,date,account_type,category,type,asset_class,name,symbol,shares,price,amount,fee,tax,currency,original_amount,original_currency,fx_rate,description,transaction_id,counterparty_name,counterparty_iban,payment_reference,mcc_code
```

Missing, reordered, duplicate, or unknown headers are rejected; this version
does not attempt name-based header recovery. Every row must contain exactly 23
CSV fields. `datetime` is an ISO-8601 UTC timestamp ending in `Z`, and `date`
is an ISO `YYYY-MM-DD` date equal to `datetime`'s UTC calendar date. Decimal
fields use an optional leading minus and a dot decimal separator only. No
thousands separators, locale decimal commas, float conversion, rounding, or
value inference is permitted.

## Source fields and identity

`transaction_id` is opaque. It is neither a UUID requirement nor a semantic
identifier. A nonblank ID establishes the canonical source identity as:

```text
provider = "trade-republic"
event_reference = "{transaction_id}:base"
```

Rows with nonzero `fee` or `tax` establish additional independent cash facts:

```text
{transaction_id}:fee
{transaction_id}:withholding-tax
```

These references are deterministic and must not be replaced with a hash,
generated identifier, or a value derived from a name, description, account, or
counterparty field.

`symbol` maps verbatim to `instrument_id`. It is not asserted to be an ISIN,
and this mapping does not resolve or validate it against an external provider.
For `BUY` and `SELL`, both a nonblank `symbol` and nonblank positive `shares`
are required. `name`, `asset_class`, `account_type`, `category`, `price`,
`description`, counterparty fields, payment reference, and MCC are retained
only as source context when a future staging implementation supports them; they
do not change the event mapping below.

## Strict observed-type mapping

The strict production dialect supports only these observed source `type`
values. In the table, a signed source cash amount is represented as a positive
canonical `Money` magnitude plus the stated cash direction. The digits after
the sign are preserved as source Decimal precision.

| Source `type` | Canonical event | Required `amount` sign | Cash direction | Instrument direction |
| --- | --- | --- | --- | --- |
| `CUSTOMER_INBOUND` | `deposit` | positive | in | — |
| `TRANSFER_INBOUND` | `deposit` | positive | in | — |
| `TRANSFER_INSTANT_INBOUND` | `deposit` | positive | in | — |
| `TRANSFER_OUTBOUND` | `withdrawal` | negative | out | — |
| `TRANSFER_INSTANT_OUTBOUND` | `withdrawal` | negative | out | — |
| `BUY` | `buy` | negative | out | in (`symbol`, `shares`) |
| `SELL` | `sell` | positive | in | out (`symbol`, `shares`) |
| `DIVIDEND` | `dividend` | positive | in | — |
| `INTEREST_PAYMENT` | `interest` | positive | in | — |

The base event always uses `{transaction_id}:base`. A sign mismatch is
rejected, never flipped or normalized. A zero base `amount` is invalid for all
supported types.

`fee` and `tax` are optional decimal columns. An empty value or `0` creates no
additional fact. A nonzero value must be negative and produces, respectively,
a `fee` or `withholding_tax` event with an outbound cash leg and the component
identity above. A positive fee or tax is a diagnostic; it is never interpreted
as a tax refund, rebate, or adjustment to the base trade amount. Fees and taxes
remain separate facts and are never folded into a buy, sell, dividend, or
interest amount.

## Native currency and source-reported EUR evidence

`currency` is required for every mapped cash fact. If `original_amount`,
`original_currency`, and `fx_rate` are all empty, the base cash fact uses
`amount` and `currency` directly. If all three are present, `amount` must be
EUR and is retained as `SourceReportedEurEvidence` with its source lexical
precision, the source `fx_rate`, and `datetime`. The canonical base cash fact
instead uses `original_amount` and `original_currency` as its native money.
Its direction follows the strict mapping table; the observed sign is validated
before constructing the positive money magnitude required by the canonical
contract.

All three FX fields must be present together or absent together. PIA never
calculates a replacement amount or rate, backfills a currency, or turns a
reported EUR amount into an inferred conversion. The accompanying amount, rate,
and timestamp are evidence, not an `source_reported_fx_conversion` event.

## Unsupported observed types

The observed source values `DELIVERY`, `MIGRATION`, `BONUS_ISSUE`,
`BONUS_ISSUE_CANCELLED`, and `IPO_SUBSCRIPTION` are deliberately not mapped by
this version. They produce diagnostics and no canonical event. In particular,
they must not be coerced into a trade, stock split, correction, reversal, or
cash event.

## Test-only Phase 3 extension profile

`trade-republic-csv-v1-phase3-test-only-extension` is a fixture-only contract,
not part of `trade-republic-csv-v1` and not a claim about current Trade Republic
exports. Its CSV fixtures intentionally retain the same 23-column header so a
future adapter can distinguish source dialect detection from event coverage.
It uses synthetic source `type` values `FX_CONVERSION`, `STOCK_SPLIT`,
`CORRECTION`, and `REVERSAL` solely to cover the Phase 3 vocabulary.

Correction and reversal targets, plus details not expressible in the observed
header, live in the synthetic sidecar manifest. A production implementation
must not accept these extension types unless a future, approved source mapping
adds evidence for them.

## Row diagnostics

Diagnostics are row-level unless they concern the file header. They are stable
fixture-contract codes for P4.3; they are not a public HTTP response schema.

| Code | Condition |
| --- | --- |
| `TRCSV001_MISSING_HEADER` | The file has no header row. |
| `TRCSV002_REORDERED_HEADER` | The header has the required names but not the required order. |
| `TRCSV003_UNKNOWN_HEADER` | A header is missing, duplicated, or unknown. |
| `TRCSV004_BLANK_TRANSACTION_ID` | `transaction_id` is empty or whitespace-only. |
| `TRCSV005_INVALID_DATETIME` | `datetime` is not a UTC ISO-8601 timestamp ending in `Z`. |
| `TRCSV006_DATE_MISMATCH` | `date` is invalid or does not equal the UTC calendar date of `datetime`. |
| `TRCSV007_INVALID_DECIMAL` | A populated numeric field is not a finite dot-decimal value. |
| `TRCSV008_INVALID_SIGN` | A supported base amount is zero or has the wrong sign for its source type. |
| `TRCSV009_INCOMPLETE_FX_TRIPLE` | Only some of `original_amount`, `original_currency`, and `fx_rate` are populated. |
| `TRCSV010_MISSING_SECURITY_IDENTIFIER` | A `BUY` or `SELL` has a blank `symbol`. |
| `TRCSV011_MISSING_SHARES` | A `BUY` or `SELL` has blank, zero, negative, or invalid `shares`. |
| `TRCSV012_FEE_OR_TAX_SIGN` | A nonzero `fee` or `tax` is positive. |
| `TRCSV013_UNSUPPORTED_SOURCE_TYPE` | The source `type` is outside the strict observed mapping. |

The synthetic corpus is under
`apps/api/tests/fixtures/trade_republic_csv_v1/`. Its `expected-events.json`
sidecar is the expected event and diagnostic manifest. It contains only
invented IDs, values, instruments, and correction/reversal links.
