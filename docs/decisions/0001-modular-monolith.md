# ADR 0001 — Start with a modular monolith

## Context

PIA is a single-user personal project with a zero-recurring-cost goal. It needs reliable accounting, import workflows, data ingestion, charts, alerts, and optional AI explanations.

## Decision

Use a modular monolith: a Next.js frontend, a FastAPI backend, Supabase services, and scheduled Python jobs. Keep financial, provider, AI, and notification modules separated through interfaces.

## Alternatives considered

- Microservices and a message queue: rejected for early complexity and operational cost.
- Frontend-only Supabase application: rejected because financial logic needs a tested Python domain layer.
- A desktop-only application: deferred; a private responsive web/PWA is the primary surface.

## Consequences

The first implementation favors clear modules and tests over independent deployment of every component. Service extraction is allowed only when an actual scalability or operational need appears.
