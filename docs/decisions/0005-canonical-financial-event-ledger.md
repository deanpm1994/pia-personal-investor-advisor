# ADR 0005 — Define the canonical financial-event ledger policy

## Context

PIA needs a financial record that preserves what a broker or other source reported
before import, persistence, portfolio accounting, or user interfaces are added.
Financial facts must remain traceable without silently changing money, quantity,
currency, fee, tax, or correction history. EUR is PIA's reporting currency, but a
source transaction's native currency remains authoritative. Consistent with ADR
0001, this is a financial-domain boundary within the modular monolith.

## Decision

### Immutable source events

The canonical ledger records source events: facts reported by a broker or other
source. An economic fact is never edited in place. A correction or reversal is a
new immutable event with an explicit link to the event it corrects or reverses;
the original event remains in the history. Source events retain a stable source
identity or reference so later layers can detect duplicate source facts without
rewriting history.

### Initial vocabulary and asset boundary

The initial event vocabulary is fixed to:

- deposit
- withdrawal
- buy
- sell
- dividend
- interest
- fee
- withholding tax
- source-reported FX conversion
- stock split
- correction
- reversal

The ledger supports securities and cash only. Securities may have fractional
units. Cryptoassets, bonds, derivatives, savings-plan mechanics, and complex
corporate actions are deferred. No omitted case is to be represented by an
incorrect event type.

### Currency and FX evidence

Each monetary event preserves the native transaction currency reported by its
source. EUR is the reporting currency. An event may additionally retain EUR
amount, rate, and timestamp evidence only when those values were source-reported.
The ledger must not infer, calculate, backfill, or provider-fetch FX values.

### Exact numeric values

All monetary amounts, rates, and quantities use Decimal semantics only. Source
precision is retained, including fractional security units. Binary floating-point
values and silent rounding are prohibited at the ledger boundary.

### Future partial-sale accounting policy

When partial-sale accounting is implemented, it uses FIFO lot allocation.
Purchase fees increase acquisition basis and sale fees reduce proceeds. Taxes,
including withholding tax, remain separate recorded factual events rather than
being folded into trade basis or proceeds.

This records a future accounting policy; it does not introduce a calculator,
position, gain, valuation, or tax calculation in this phase.

### Taxes

Tax events record cash facts only. They do not constitute tax advice and must not
calculate tax liability, allowances, loss offsets, or tax-return outcomes.

## Deferred implementation boundary

This decision establishes policy only. It adds no contracts, tables, migrations,
imports, APIs, calculations, provider integrations, or UI. Persistence remains
owner-scoped and protected by the existing privacy and row-level-security
guardrails; its concrete schema and policies are deferred to P3.3. In accordance
with ADR 0004, any resulting PIA-owned application objects will be Alembic
managed, while Supabase-managed infrastructure remains under Supabase migrations.

## Downstream contract map

| Policy | P3.2 — contracts and validation | P3.3 — persistence and RLS | P3.4 — fixtures and invariants |
| --- | --- | --- | --- |
| Decimal-only values, preserved precision, and fractional units | ✓ Decimal types and validation | ✓ Decimal persistence | ✓ precision-sensitive fixtures |
| Fixed event vocabulary and supported asset boundary | ✓ event shape and legs | ✓ event and leg constraints | ✓ coverage of baseline events |
| Immutable facts, source identity, and correction/reversal links | ✓ source identities, linkage, and validation | ✓ immutable owner-scoped history and source-identity uniqueness | ✓ duplicate and reversal-history assertions |
| Native currency and source-reported EUR evidence only | ✓ EUR evidence contract and validation | ✓ native money and source-reported EUR evidence | ✓ FX traceability assertions |
| Future FIFO, fee, and separate-tax policy | ✓ factual event and leg representation only | ✓ factual event persistence only | ✓ hand-worked FIFO allocations and fee/tax-separation assertions |
| Cash-fact-only tax boundary | ✓ tax event validation | ✓ tax-event persistence | ✓ separate withholding-tax fixture and invariant |

## Consequences

Later implementations must preserve these source facts and boundaries. Any new
event type, asset class, FX treatment, rounding rule, cost-basis treatment, or
tax behavior requires an explicit decision before it changes ledger meaning.
