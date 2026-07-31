# ADR 0007 — Define portfolio-accounting, manual-account, and snapshot policy

## Context

ADR 0005 preserves immutable, Decimal-only financial facts, but it deliberately
defers the rules for replaying those facts into a financial picture. Phase 5
needs one deterministic policy for event ordering, corrections, FIFO lots,
manual cash accounts, incomplete multi-currency results, and reproducible
snapshots. These rules must retain source provenance and must not introduce
market prices, inferred FX, tax calculations, or destructive history edits.

## Decision

### Deterministic ledger replay

Accounting consumes only immutable event and leg facts. It orders events by the
following total order, ascending:

1. `occurred_at`;
2. `created_at`; then
3. event UUID in its canonical textual form.

Legs within an event are ordered by their persisted positive `position`.
Timestamps are instants, not display-local dates. Database query order, HTTP
arrival order, UI order, source-file row order, and provider-specific reference
formatting must never affect the result. The same ordered ledger facts and
account metadata must therefore produce byte-equivalent serialized accounting
results.

Every explicit leg is applied exactly once. A correction is a new, additive
delta event linked by `correction_of_event_id`; it does not replace, hide, or
reconstruct the linked event. A reversal is a new event linked by
`reversal_of_event_id` whose explicit legs negate the linked event's economic
legs; both events remain in the replay. Implementations must validate that the
link is owner and account scoped, is not self-referential or cyclic, and is
compatible with the linked event. A malformed link, non-negating reversal,
unsupported correction shape, or history whose replay becomes impossible is an
incomplete result with diagnostics, not an opportunity to infer missing legs or
silently omit a fact.

### FIFO lots, grouped fees, taxes, and splits

Supported buy events create acquisition lots in deterministic replay order.
Each lot keeps its exact fractional quantity, native-currency total basis, buy
event ID, and all fee-evidence event IDs. A sell allocates quantity from the
oldest remaining compatible lots first (FIFO); partial allocations retain their
remaining exact quantity and proportional total basis without binary floating
point or silent rounding.

An explicitly grouped purchase fee increases the basis of its one compatible
buy. An explicitly grouped sale fee reduces proceeds of its one compatible
sell. A grouped fee is usable only when its owner, account, provider,
currency, and durable source-group reference identify exactly one compatible
base trade. Withholding tax and every other tax event remain separate cash
facts: they never change lot basis, sale proceeds, realized gain, or a tax
liability calculation.

A stock split transforms each open lot of the identified instrument in the
account: its quantity changes by the reported ratio and its total basis remains
unchanged. Per-unit basis changes only as a consequence of that exact ratio.
Cash-in-lieu, fractional-share disposal, reverse-split cash adjustments, and
other corporate actions are unsupported until separately decided. A split with
no compatible open quantity, a non-positive or non-reconciling ratio, an
oversell, mixed-currency lot calculation, ambiguous fee group, or unsupported
correction creates a visible incomplete diagnostic; it must not produce a
misleading realized result.

### Durable source groups

An event may carry an opaque, nonblank `source_group_reference`. It is a
durable association key scoped by owner, account, and source provider; it is
not derived by the accounting engine from `source_event_reference` or any other
provider-specific identity. Importers and manual workflows create and persist
the group when they have trustworthy source or workflow evidence. A base trade,
its fee, and its tax fact from one source transaction share that reference.
Manual paired-transfer legs likewise share a distinct transfer group.

Accounting uses only equality of the persisted group reference and the
scoping facts above. Events without a trustworthy applicable group remain
unattributed; calculations that require attribution report incompleteness
rather than guessing. Existing source identities remain immutable and unique.

### Accounts and manual history

Accounts have one of these roles:

- `brokerage` for broker-held cash and securities;
- `cash` for manually tracked cash;
- `savings` for manually tracked savings; and
- `emergency_reserve` for manually tracked emergency funds.

An emergency-reserve account may have one optional, positive Decimal EUR
target. No target means that account is excluded from target progress. Aggregate
target progress is the exact sum of eligible EUR emergency-reserve balances
against the exact sum of configured targets. Non-EUR balances never contribute
to that progress without source-reported EUR evidence; unavailable evidence
makes the affected aggregate incomplete rather than converted. No configured
targets produces an unavailable progress result, not `0%`.

Manual history is append-only and uses stable server-generated source
identities. An opening balance is the account's first manual economic fact and
may be recorded once; later funding uses deposits. Deposits and withdrawals are
new factual events, never mutable balance overrides. A transfer is exactly two
same-owner events—one withdrawal and one deposit—with equal currency and exact
amount, a shared transfer group, and one idempotency identity; both legs commit
atomically. It must leave aggregate owner cash unchanged.

Corrections are append-only delta or reversal events under the replay policy
above. Accounts with history are archived, never deleted: archiving prevents
new ordinary activity while retaining their events and their contribution to
historical snapshots. No account metadata edit, archive operation, correction,
or import may overwrite or delete an economic event or its legs.

### Native currency, EUR evidence, and completeness

Cash balances and results retain their native currency. Aggregation of native
balances is allowed only within the same currency. An EUR aggregate may use
EUR cash facts directly and may include a non-EUR fact only when the source
persisted complete EUR amount, rate, and timestamp evidence for that same fact.
It must use that evidence as reported and never calculate, backfill, or fetch a
rate.

Results expose structured, stable completeness diagnostics with the relevant
event, account, and group evidence IDs. When a requested account or aggregate
depends on missing group attribution, unsupported currency evidence, impossible
history, or another unsupported input, the affected value is unavailable and
incomplete—not a partial value presented as complete. Unaffected native-currency
outputs may remain available with the same diagnostics. Diagnostics never
invent financial values or conceal the original source facts.

### Explicit immutable snapshots

A snapshot is created only by an authenticated, explicit refresh. It represents
the ledger and relevant account metadata read atomically for that refresh; it
does not imply a market-price refresh. `as_of` is the latest `occurred_at` in
the included ordered ledger (or unavailable when there are no ledger facts),
while `refreshed_at` records when the explicit refresh completed. Freshness is
determined by comparing the latest input fingerprint with the snapshot's
fingerprint, not by display time alone.

The fingerprint is SHA-256 of a canonical, versioned serialization containing:

- the accounting-policy and serialization versions;
- the owner ID;
- every included event in replay order, including IDs, timestamps, source
  identity, type, correction/reversal links, source group, source-reported EUR
  evidence, and legs in `position` order with Decimal source strings;
- all relevant account metadata in canonical account-ID order, including role,
  archive state, and emergency-reserve target; and
- the explicit inclusion boundary used by the refresh.

Serialization fixes field order, UTF-8 encoding, canonical UUID and UTC
timestamp rendering, and Decimal lexical representation. It must not depend on
database query order, JSON map order, a generated current timestamp, or a
client-supplied display value. The fingerprint, input counts, and evidence IDs
are persisted with the snapshot.

An unchanged fingerprint reuses the existing completed snapshot identity. A
changed fingerprint creates a new immutable snapshot and preserves older
snapshots as audit history. A completed snapshot may explicitly contain
auditable incomplete diagnostics. A refresh that cannot produce a valid
accounting result or persist all snapshot material fails atomically and exposes
no partial completed snapshot. Snapshots are never updated or deleted through
authenticated client access.

## Downstream contract map

| Child issue | Required policy implementation or verification |
| --- | --- |
| ✓ P5.2 — #58 fixtures | Hand-worked ordering, correction, FIFO, fee, tax, split, manual-account, reserve, and incomplete-history oracle. |
| ✓ P5.3 — #59 grouping | Owner/account/provider-scoped durable source groups; trustworthy migration only; no accounting identity parsing. |
| ✓ P5.4 — #60 manual accounts | Roles, optional EUR reserve targets, append-only opening/deposit/withdrawal/transfer/correction workflows, archive, RLS, and idempotency. |
| ✓ P5.5 — #61 accounting fold | Total replay ordering, once-only legs, cash/position aggregation, splits, evidence, and stable diagnostics. |
| ✓ P5.6 — #62 FIFO | Fractional FIFO lots, grouped fee basis/proceeds, separate taxes, split transformations, and impossible-history handling. |
| ✓ P5.7 — #63 snapshots | Canonical fingerprint, explicit-refresh atomicity, immutable idempotent persistence, as-of/freshness, RLS, and evidence. |
| ✓ P5.8 — #64 API | Decimal-safe snapshot read/refresh states, owner isolation, freshness, completeness, and evidence response contract. |
| ✓ P5.9 — #65 dashboard | Explicit refresh UX, native-currency and reserve progress display, unavailable/incomplete/stale states, and evidence access. |
| ✓ P5.10 — #66 end-to-end verification | Synthetic reconciliation, snapshot idempotency/atomicity, privacy adversarial checks, and visible diagnostics through the UI. |

## Consequences

Phase 5 implementations must preserve this policy and use Decimal arithmetic
throughout. They may add contracts, migrations, services, APIs, and UI only in
the approved child issue that owns that work. Any change to replay order,
correction semantics, lot method, fee or tax attribution, account role, FX
treatment, snapshot identity, or completeness behavior requires a new explicit
decision.
