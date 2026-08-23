# ADR 0009 — Use OpenFIGI and Marketstack for private daily EOD analysis

## Status

Accepted on 2026-08-23 for the private, single-user PIA deployment described by
`PROJECT_BIBLE.md`.

This decision does not approve commercial use, public display, redistribution,
real-time data, a paid plan, or a mandatory provider credential. Any of those
changes requires a new approved issue and a fresh terms review.

## Context

Phase 6 needs enough daily end-of-day (EOD) history to calculate SMA-20,
SMA-50, SMA-200, and RSI-14 for a small set of listed instruments identified by
ISIN. The data must be usable in a private chart, persistable with provenance,
available without a required payment, and replaceable without changing
accounting or user-facing domain contracts.

ADR 0003 requires provider adapters and preserved provenance. It does not
select a provider or define the licensing, coverage, quota, retention,
correction, freshness, or outage rules needed by Phase 6.

The provider review was performed on 2026-08-23 using the official sources in
the [evidence register](#evidence-register). Provider plans, terms, coverage,
and quotas can change, so this ADR deliberately treats them as runtime policy
rather than permanent facts.

## Decision

Use two replaceable adapters:

- **OpenFIGI** resolves a validated ISIN to open FIGI metadata and candidate
  listing identities. The unauthenticated API is the default, so no OpenFIGI
  secret is required.
- **Marketstack** supplies raw daily EOD OHLCV bars for an explicitly resolved
  listing. The free plan is the only approved Marketstack tier. Its access key
  is optional runtime configuration supplied by the owner and must never be
  committed, logged, sent to the browser, or embedded in a stored source URL.

The Marketstack adapter is disabled by default. PIA remains useful when it is
disabled: financial accounting, imports, savings, reserves, and existing
snapshots continue to work; market analysis is visibly unavailable.

The approved use is personal, private, non-commercial use by the single owner.
The owner may view the data and derived charts only after accepting the current
provider terms and configuring their own free key. Public hosting, access by a
second user, client work, business use, redistribution, or monetization is not
covered by this decision.

## Licensing and coverage disposition

### OpenFIGI

OpenFIGI is approved for identifier resolution because:

- the API accepts `ID_ISIN` mapping jobs and returns FIGI, ticker, exchange, and
  descriptive metadata;
- unauthenticated mapping is free and limited to 25 requests per minute with
  up to 10 jobs per request;
- FIGI identifiers and associated open symbology metadata may be stored, used,
  and redistributed without licensing or reuse fees; and
- an API key is optional and only raises limits.

OpenFIGI does not establish that Marketstack covers a listing and does not
provide its EOD prices. A successful OpenFIGI result is therefore a candidate,
not an approved price mapping.

### Marketstack

Marketstack is approved only for the private EOD use described above because
its current free plan provides:

- 100 requests per month;
- EOD data and up to one year of history;
- ticker, exchange, currency, and timezone information;
- HTTPS access; and
- global listed-equity coverage described as more than 170,000 tickers across
  70 exchanges.

One year is sufficient to bootstrap at least 200 ordinary trading sessions for
most continuously listed instruments. PIA must still report insufficient
history when the actual response contains fewer than an indicator requires.

The free plan does not grant commercial-use rights. The official service
agreement reviewed for this decision does not add an attribution requirement
or a provider-data deletion deadline. PIA nevertheless shows source
attribution, limits retention, and rechecks the terms because upstream data
licenses and plans can change.

No reviewed Marketstack source promises an exact EOD publication time or a
free-plan service-level agreement. The 06:00 UTC schedule below is a
conservative PIA retrieval policy, not a provider guarantee; data remains
pending until a valid bar actually arrives.

Coverage claims never override observed results. Marketstack data is treated
as indicative analysis data, not an exchange official close, accounting fact,
tax value, execution price, or promise of accuracy.

## Supported-instrument boundary

Phase 6 supports an instrument only when all of the following are true:

1. Its ISIN passes ISO 6166 format and check-digit validation.
2. OpenFIGI returns a listed equity or ETF candidate.
3. Exactly one candidate can be matched to a Marketstack ticker and ISO 10383
   MIC without guessing.
4. Marketstack reports an explicit three-letter quote currency.
5. Marketstack returns structurally valid daily OHLCV data for that exact
   provider symbol.
6. The instrument fits within the 20-active-instrument policy cap.

Common stocks and exchange-traded funds are in scope. Mutual funds, bonds,
indices, options, futures, CFDs, cryptocurrencies, forex, OTC-only securities,
delisted instruments, and instruments without a unique listing/currency match
are unsupported by this decision.

The same ISIN can have several listings or trading currencies. PIA must not
choose a primary listing, infer a venue from the ISIN country prefix, or merge
bars across venues. Multiple valid candidates produce an `ambiguous` result
until a later approved workflow records an explicit listing choice.

## Canonical identity and provider mapping

The canonical instrument identity is the validated ISIN plus its OpenFIGI
share-class FIGI when available. A priced listing has a separate immutable
listing identity:

```text
(canonical_instrument_id, mic, quote_currency)
```

Provider mappings are versioned records, not fields on the canonical
instrument:

```text
(provider, provider_symbol, provider_exchange_code, mic, quote_currency,
 valid_from, valid_to, resolved_at, resolution_source_url, resolution_status)
```

The Marketstack symbol is never a canonical identifier. Symbol changes,
provider corrections, and provider replacement create a new mapping version.
No stored history is silently reassigned to a new symbol or venue.

Resolution proceeds as follows:

1. validate the ISIN locally;
2. map it through OpenFIGI `POST /v3/mapping` with `ID_ISIN`;
3. retain only in-scope listed equity/ETF candidates;
4. translate the OpenFIGI exchange code through a versioned OpenFIGI
   exchange-code-to-MIC reference and preserve that evidence;
5. match ticker plus MIC against Marketstack reference data;
6. verify the returned quote currency; and
7. return exactly one of `supported`, `invalid`, `unsupported`, `ambiguous`,
   `temporarily_unavailable`, or `provider_disabled`.

Empty and ambiguous results are never cached as permanent unsupported facts.
They may be retried after the configured backoff.

## Provider-neutral contract

Domain and application code depend on narrow interfaces, not provider response
shapes:

```text
InstrumentResolver.resolve_isin(isin) -> ResolutionOutcome
DailyBarProvider.fetch(mapping, start_date, end_date) -> FetchOutcome
```

`ResolutionOutcome` carries the canonical identifier, candidate listing
identities, provider mapping version, resolution status, evidence source, and
retrieval time.

`FetchOutcome` carries normalized bars plus provider identity, provider symbol,
MIC, quote currency, requested interval, provider as-of value, retrieval time,
source URL, completeness status, quota state, and diagnostics.

No downstream contract exposes an OpenFIGI or Marketstack payload. A future
provider replaces one or both adapters and passes the same contract tests;
accounting, indicators, API schemas, and charts do not import provider SDKs or
provider-specific names.

## EOD bar and provenance policy

A normalized bar contains:

```text
listing_id, market_date, open, high, low, close, volume,
provider, provider_symbol, quote_currency, provider_as_of,
retrieved_at, ingestion_run_id, source_url, mapping_version,
completeness_status, revision
```

Prices use decimal strings and fixed-precision decimal storage, never binary
floating point. Volume is an integer when supplied. Raw, as-traded OHLCV is the
Phase 6 source. Adjusted values, splits, dividends, and total-return series are
out of scope and must not be substituted silently.

The canonical source URL is the provider endpoint and non-secret query shape.
It excludes access keys and other credentials. The request parameters and a
hash of the response may be retained as ingestion evidence, but raw provider
payloads are not stored in the repository, issue comments, logs, or browser
responses.

Every displayed price or indicator includes:

- `Market data: Marketstack`;
- quote currency and listing MIC;
- market-date/as-of value and retrieval time;
- freshness and completeness state; and
- a link to the Marketstack product or documentation page.

Instrument evidence identifies OpenFIGI separately. TradingView Lightweight
Charts attribution is an independent UI requirement and does not replace data
source attribution.

## Schedule and quota policy

Controlled server-side ingestion runs once per day at 06:00 UTC, after the
previous US trading day and most global trading sessions have closed. Page
loads, browser requests, and user navigation never call either provider.
Sunday and Monday runs make no routine EOD request; they may perform only a
budgeted retry or correction check.

The free Marketstack budget is allocated conservatively:

- at most 31 scheduled multi-symbol EOD requests per calendar month;
- at most 20 one-time history bootstrap requests for active instruments; and
- at least 49 requests reserved for retry, correction checks, onboarding, and
  month-length variation.

The scheduler maintains its own monthly request ledger because absence of a
provider warning is not permission to exceed the plan. It stops before the
100-request limit. It never purchases overages or changes plan automatically.
The active-instrument cap cannot be raised without a quota and product review.

OpenFIGI requests use the unauthenticated limit: no more than 10 jobs in a
request and no more than 25 requests per minute. A `429` response always honors
`Retry-After` or the rate-limit reset header when present.

## Freshness, partial coverage, and outage behavior

PIA stores both the market date and retrieval time. Retrieval time is never
presented as the price time.

For the Tuesday-through-Saturday scheduled run, the target is the previous UTC
weekday. Because this policy does not approve a separate exchange-holiday
provider, absence on a possible holiday is `not_yet_available`, not fabricated
as a zero-volume bar. A listing is:

- `fresh` when the newest valid bar is for the target date;
- `pending` when the newest bar is one target weekday behind;
- `stale` when it is two or more target weekdays behind;
- `incomplete` when only part of the requested date range or instrument batch
  is returned; or
- `unavailable` when there is no valid bar.

Weekends are excluded from target-weekday age. Known exchange holidays may be
recorded from an approved static calendar later, but are never inferred from a
missing response. The UI always shows the actual market date so a holiday or
provider delay cannot look current accidentally.

HTTP timeouts, `429`, `5xx`, malformed JSON, schema changes, currency mismatch,
invalid OHLC relationships, negative price/volume, duplicate contradictory
bars, and missing instruments fail closed. Valid instruments from a partial
batch may be committed, but the run and omitted instruments remain
`partial`/`failed`; a partial response never advances their freshness.

Retries use bounded exponential backoff with jitter, honor provider headers,
and stop after three attempts or when the reserved monthly budget would be
crossed. Quota exhaustion is not retried until the next provider reset.

## Corrections and idempotency

The unique observation identity is:

```text
(provider, provider_symbol, market_date, revision)
```

An identical replay is idempotent. If a provider later changes a previously
accepted bar, PIA appends a correction revision and preserves the prior value,
response hash, ingestion run, and retrieval time. It never silently overwrites
evidence. Indicators are recomputed deterministically from the earliest
corrected date, and affected output is marked corrected.

Contradictory duplicates within one response are rejected. A correction that
changes currency, MIC, or canonical mapping is not a bar correction; it blocks
the listing pending a new mapping decision.

## Retention and provider disablement

Normalized Marketstack bars and their audit revisions are retained for 400
calendar days while the owner's provider account and terms remain active. This
supports the one-year free-plan boundary and SMA-200 without building an
indefinite licensed-data archive. Ingestion-run metadata, hashes, diagnostics,
and non-price mapping provenance may be retained longer for auditability.

Raw provider payloads are processed transiently and discarded after
normalization and hashing. Backups follow the same retention deadline.

Terms, plan, or license checks run before implementation and at least every 90
days thereafter. If rights become unclear, the account ends, the key is
removed, the quota changes incompatibly, or the provider is disabled:

1. stop all provider calls;
2. mark analysis `provider_disabled` or `license_review_required`;
3. preserve accounting and non-market features;
4. hide expired provider data from charts rather than imply freshness; and
5. delete or retain stored data according to the newly applicable terms, using
   a separately reviewed deletion operation.

No provider may be enabled merely because an environment variable exists. The
application requires an explicit provider-enabled setting plus a valid
server-side key.

## Security and privacy

- Provider secrets live only in server-side secret configuration.
- URLs, errors, telemetry, fixtures, screenshots, and issue evidence redact
  credentials and do not identify the owner's holdings.
- Resolution and ingestion operate only on eligible persisted instruments.
- Tests use synthetic ISINs, symbols, bars, responses, and credentials.
- Provider adapters receive instrument identifiers, never user profiles,
  transactions, quantities, cost basis, account balances, or portfolio totals.
- Browser clients receive normalized authenticated API data and never provider
  credentials or raw payloads.

## Alternatives considered

- **EODHD:** rejected for Phase 6 display. Its free plan offers 20 calls per day,
  one year of EOD history, and ISIN mapping, but its current personal-use terms
  explicitly prohibit displaying the information. Storage also ends with the
  subscription and requires deletion within one month. These restrictions do
  not safely cover the approved chart UI.
- **Alpha Vantage plus OpenFIGI:** rejected for bootstrap history. The free raw
  daily endpoint is limited to the latest 100 observations; full 20-year output
  requires premium access, so a new instrument cannot calculate SMA-200.
- **Twelve Data:** rejected for zero-required-cost global EOD use. The current
  free Basic plan provides internal non-display use and global trial symbols,
  while global EOD equities/ETFs and internal display are paid-plan features.
- **Stooq or Yahoo Finance downloads:** rejected because Phase 6 forbids
  scraping and no reviewed official API license established the required
  automated retrieval, storage, and display rights.
- **One hard-coded provider model:** rejected by ADR 0003 because it would leak
  provider identifiers and failure semantics into accounting, API, and UI code.

## Consequences

PIA gets a zero-required-cost path for a deliberately small private universe,
with enough initial history for SMA-200 and an open ISIN mapping layer. The
trade-off is a strict quota, a provider credential supplied by the owner, a
400-day retention window, no commercial/public use, and explicit unavailable
states whenever coverage or licensing is uncertain.

Future replacement is localized to adapters, mapping records, ingestion
configuration, and contract tests. A replacement still needs a new approved
licensing decision; interface compatibility alone is insufficient.

## Evidence register

Official sources reviewed on 2026-08-23:

- [Marketstack pricing](https://marketstack.com/pricing) — free-plan EOD,
  one-year history, request allowance, reference data, and commercial-use
  boundary.
- [Marketstack product page](https://apilayer.com/products/marketstack/) — free
  plan, global exchange/ticker coverage, currency/timezone coverage, and paid
  commercial-use rights.
- [Marketstack service agreement](https://marketstack.com/agreement) — service,
  subscription, customer, availability, and termination terms.
- [Marketstack EOD example](https://marketstack.com/find-ticker-symbol) — EOD
  request shape, multi-symbol parameter, OHLCV, adjusted fields, exchange MIC,
  and timestamp response fields.
- [OpenFIGI API documentation](https://www.openfigi.com/api/documentation) —
  `ID_ISIN` mapping, response metadata, unauthenticated limits, status codes,
  and retry headers.
- [OpenFIGI terms](https://www.openfigi.com/docs/terms-of-service) — public-domain
  dedication and unrestricted FIGI identifier use.
- [OpenFIGI FAQ](https://www.openfigi.com/docs/faqs) — storage, reuse, and
  redistribution disposition for FIGI symbology and metadata.
- [EODHD terms](https://eodhd.com/financial-apis/terms-conditions) — personal
  storage, display restriction, deletion, availability, and accuracy terms.
- [Alpha Vantage daily API](https://www.alphavantage.co/documentation/) — raw
  daily coverage and free 100-observation versus premium full-history limit.
- [Twelve Data pricing](https://twelvedata.com/pricing) and
  [usage policy](https://support.twelvedata.com/en/articles/5332349-commercial-and-personal-usage)
  — free-plan features and individual-plan display/use limits.

This review is an engineering licensing disposition, not legal advice. Any
conflict between this summary and current provider terms resolves in favor of
the provider terms and disables ingestion pending review.
