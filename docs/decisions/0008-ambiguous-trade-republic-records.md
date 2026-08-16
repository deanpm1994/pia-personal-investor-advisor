# ADR 0008 — Preserve incomplete Trade Republic movements automatically

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

The importer records source-faithful observed movements automatically. They are
immutable ledger facts with exact broker-reported cash or quantity and source
identity, but are not priced trades, FIFO lots, or complete corporate actions.
They may contribute to holdings and native-currency cash flow. Their affected
FIFO basis and return results remain visibly unavailable.

They are not mapped to a buy, sell, stock split, correction, reversal, or a
zero-cost lot. The import remains atomic; no row-review workflow is required.

| Source type | Disposition | Why it cannot be mapped now | Minimum evidence before a future decision |
| --- | --- | --- | --- |
| `MIGRATION` | Observed position movement. | A quantity alone does not establish lots or basis. | Complete per-lot acquisition evidence before FIFO is available. |
| `BONUS_ISSUE` | Observed position movement. | Basis allocation and tax treatment are unknown. | Corporate-action terms and reported basis allocation. |
| `BONUS_ISSUE_CANCELLED` | Observed position movement. | Cancellation target and all economic legs are unknown. | Durable original-record link and complete negating evidence. |
| `IPO_SUBSCRIPTION` | Observed cash movement. | Allocation and settlement are unknown. | Source settlement tying cash, quantity, fees, and currency evidence together. |

### Accounting and completeness consequences

No migration record may establish an opening position or a FIFO lot. No bonus
record may create a zero-basis position, be treated as a split, or change an
existing lot's basis. No cancelled bonus may be accepted as a reversal, and no
IPO subscription may be treated as a buy before its execution and settlement
are proven by source evidence.

Observed movements contribute only their explicit quantities or cash flow to
accounting, snapshots, and the financial-picture API. The related FIFO basis,
realized return, allocation, and settlement claims remain incomplete. Unaffected
confirmed ledger history remains accountable under ADR 0007.

### Future implementation boundary

A future implementation requires a separate approved issue and a new ADR
before reclassifying these observed movements as complete canonical events or
changing their accounting behavior. The ADR must specify the canonical events
and legs, durable source grouping,
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

No amendment to ADR 0007 is needed because observed movements preserve its
incomplete-result rule: the accounting replay includes explicit quantity or
cash while suppressing unsupported FIFO and return claims. This decision
records an importer and financial-correctness boundary alongside ADR 0005 and
ADR 0006. Any future supported bonus action will require a new ADR because it
would reclassify an observed movement and change its lot-basis meaning; a
future settled IPO may use `buy` only after its source evidence proves the
existing buy contract exactly.
