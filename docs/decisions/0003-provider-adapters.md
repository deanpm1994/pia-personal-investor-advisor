# ADR 0003 — Use provider adapters and preserve provenance

## Context

No free provider reliably covers every required market, fundamentals, corporate action, research, and news use case. Provider terms, quotas, symbols, and coverage change.

## Decision

Define interfaces for broker import, market data, fundamentals, news, and AI. Store raw provider metadata, normalized data, source URLs, timestamps, provider identifiers, and ingestion-run identifiers.

## Alternatives considered

- Hardcode one provider throughout the application: rejected because migration cost and provider risk would be high.
- Scrape consumer financial websites: rejected because licensing, reliability, and maintenance are unsuitable.
- Buy an enterprise data provider for version one: deferred because it violates the zero-required-cost goal.

## Consequences

The first adapter can be narrow and limited to the user’s watchlist. Changing providers later should affect adapter and contract tests, not accounting, alert, or UI domain code.
