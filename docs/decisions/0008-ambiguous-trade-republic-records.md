# ADR 0008 — Defer ambiguous Trade Republic migration, bonus, and IPO records

## Context

The production Trade Republic CSV v1 parser observed `MIGRATION`,
`BONUS_ISSUE`, `BONUS_ISSUE_CANCELLED`, and `IPO_SUBSCRIPTION` source values
in a user-authorized, private export. The public repository contains only
synthetic fixtures. The v1 header does not establish enough economic facts to
map these values safely: in particular, it cannot establish transferred FIFO
lots and basis, corporate-action basis allocation, a cancellation target, or
whether an IPO subscription was executed and settled.

ADR 0005 prohibits representing an omitted case with an incorrect canonical
event. ADR 0007 requires incomplete histories to remain visibly incomplete
rather than infer missing legs, source groups, currency evidence, or values.

## Decision

### Current disposition

All four values are **deferred and rejected from confirmation** in the
`trade-republic-csv-v1` production profile. They remain auditable through the
private raw import and staged row diagnostic, but produce no canonical
candidate or ledger event. A batch containing any of them is not confirmation
eligible and reports `TRCSV013_UNSUPPORTED_SOURCE_TYPE`.

They are not mapped to a buy, sell, stock split, correction, reversal, cash
event, or a zero-cost lot. This decision preserves the existing strict
all-or-nothing confirmation boundary; it adds no partial ledger fact or
accounting exception.

| Source type | Disposition | Why it cannot be mapped now | Minimum evidence before a future decision |
| --- | --- | --- | --- |
| `MIGRATION` | Deferred; reject confirmation. | A quantity alone does not establish the acquired lots, acquisition dates, native-currency basis, fees, or source-reported EUR evidence needed for FIFO. | Broker transfer evidence and complete, durable per-lot acquisition history that reconciles exact instrument quantities, basis, currency, and any source-reported EUR evidence. |
| `BONUS_ISSUE` | Deferred; reject confirmation. | A credit is not necessarily a stock split and its basis allocation, fractional treatment, cash effects, and tax treatment are not established. | Source corporate-action evidence stating the affected instrument, exact credited quantity, terms, cash and tax effects, fractional treatment, and an explicit reported basis allocation if one applies. |
| `BONUS_ISSUE_CANCELLED` | Deferred; reject confirmation. | The CSV does not prove which earlier bonus record it cancels or that every economic leg negates that record. | A durable link to the original supported bonus record plus source evidence of every negating cash and instrument leg. |
| `IPO_SUBSCRIPTION` | Deferred; reject confirmation. | A subscription request, cash reservation, allocation, and settlement are distinct facts; the observed row does not safely distinguish them. | Source settlement evidence tying the actual cash debit, allocated instrument quantity, fees/taxes, currency evidence, and durable source group to one completed transaction. |

### Accounting and completeness consequences

No migration record may establish an opening position or a FIFO lot. No bonus
record may create a zero-basis position, be treated as a split, or change an
existing lot's basis. No cancelled bonus may be accepted as a reversal, and no
IPO subscription may be treated as a buy before its execution and settlement
are proven by source evidence.

Consequently, an affected import cannot contribute to accounting, snapshots,
or the financial-picture API. The history remains unconfirmed rather than
partially presented as complete. Unaffected confirmed ledger history remains
accountable under ADR 0007. The Phase 5 parent imported-history exit criterion
is explicitly blocked for a history containing these records until an approved
follow-on implements a supported mapping.

### Future implementation boundary

A future implementation requires a separate approved issue and a new ADR
before changing the v1 parser, ledger vocabulary, or accounting behavior. The
ADR must specify the canonical events and legs, durable source grouping,
source-currency and EUR-evidence handling, FIFO-basis consequences,
correction/cancellation links, and completeness diagnostics. It must not
derive any of those facts from descriptions, IDs, file order, market data, or
non-source FX.

Its synthetic regression corpus must cover at least:

- a migration with complete lot evidence and a migration with one missing lot
  field, proving the latter cannot create a position or basis;
- a bonus issue with explicit source terms, and a case without a reported basis
  allocation or with a cash-in-lieu amount;
- a bonus cancellation linked to its original record, and an unlinked or
  non-negating cancellation; and
- an IPO request without settlement and a completed IPO settlement, including
  fee, tax, non-EUR evidence, duplicate, and source-group boundary cases.

All fixtures must remain wholly synthetic: no raw export rows, names,
identifiers, account details, counterparties, IBANs, or real financial values.

## Consequences

No amendment to ADR 0007 is needed because this decision introduces no replay,
FIFO, or snapshot rule. It records an importer and financial-correctness
boundary alongside ADR 0005 and ADR 0006. Any future supported bonus action
will require a new ADR because it changes the canonical event vocabulary or
lot-basis meaning; a future settled IPO may use `buy` only after its source
evidence proves the existing buy contract exactly.
