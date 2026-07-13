# PIA — Project Bible

PIA is a private, mobile-first personal financial cockpit. It helps one user understand savings and investments, import Trade Republic history, monitor risks, investigate opportunities, learn, and make more disciplined decisions.

This file is the constitution of the project. GitHub Issues and the GitHub Project are the live execution roadmap.

## Product contract

- The product is decision support, never a promise of returns.
- The user is a balanced investor with an active Discovery Radar.
- Savings, emergency reserves, cash, and investments must be understood together.
- Trade Republic CSV import is the first broker integration; broker credentials and scraping are out of scope.
- Every trade remains a manual user decision. Automatic trading is forbidden.
- The first release is private and single-user. Public repositories use synthetic data only.

## Technical baseline

- Frontend: Next.js, TypeScript, Tailwind CSS, custom design tokens.
- Charts: TradingView Lightweight Charts with custom data and required attribution.
- Backend: Python, FastAPI, Pydantic, SQLAlchemy, Alembic, and Decimal arithmetic.
- Persistence: Supabase PostgreSQL, Auth, and private Storage.
- Architecture: modular monolith with provider adapters; no premature microservices.
- Jobs: deterministic Python ingestion and alert jobs, scheduled locally or with GitHub Actions.
- AI: optional explanation layer. Local Ollama is the default; the application must work with AI disabled.
- Provider interfaces isolate broker, market-data, news, fundamentals, and AI vendors.

## Non-negotiable guardrails

### Financial correctness

- Calculate monetary amounts with Decimal, never binary floating point.
- Do not silently change cost-basis, fee, tax, split, or currency-conversion policy.
- Every portfolio number must be traceable to transactions, prices, formulas, and timestamps.
- Never hide stale, missing, or contradictory data.
- A recommendation must include evidence, risk, confidence, and invalidation conditions.

### Privacy and security

- Never commit credentials, broker exports, user identifiers, or real financial values.
- Never expose a Supabase service-role key to the browser.
- Every user-owned record must have an ownership boundary and row-level security.
- Raw imports are private and auditable.
- Demo mode uses only synthetic data.

### AI and cost

- AI explains supplied evidence; it does not calculate facts or receive unrestricted database access.
- AI responses require a strict schema and evidence IDs; unsupported claims are rejected.
- No paid provider is part of the core path without an approved issue.
- Never call AI on page load; cache and batch analysis.
- The product must remain useful with market providers, hosted AI, and push notifications disabled.

## Work governance

- GitHub Issues contain executable task scope, dependencies, acceptance criteria, tests, and verification evidence.
- GitHub Project controls status, phase, priority, risk, approval, and roadmap views.
- [PIA Roadmap](https://github.com/users/deanpm1994/projects/1) is the live execution tracker.
- Architecture Decision Records capture durable choices and trade-offs.
- A task may be implemented only when its GitHub Project Approval field is Approved.
- A task is Done only when tests, relevant reviews, documentation, and issue evidence are complete.

## Ralph loop

For one approved issue only:

1. Read this file, AGENTS.md, the issue, linked ADRs, and dependencies.
2. Inspect the existing code; identify the smallest testable slice.
3. Write or update the failing test for behavior changes.
4. Implement only the approved scope.
5. Run focused checks, then the relevant full checks.
6. Review the diff for financial, security, privacy, provider, and cost regressions.
7. Post verification evidence on the issue and move it to Review.
8. Stop. Do not begin the next issue without approval.

Stop immediately if the work needs a new product decision, paid provider, secret, unclear license, risky migration, failing financial fixture, or unsupported AI claim.

## Review loops

- Financial: hand-worked fixtures; fees, taxes, dividends, splits, FX, duplicates, rounding, and freshness.
- Security: authentication, cross-user access denial, RLS, storage access, uploads, secrets, logs, and deletion.
- Providers: licensing, rate limits, outages, timestamps, source attribution, and identifier mapping.
- AI: schema validity, evidence grounding, abstention, prohibited certainty language, and cost quotas.
- UX: mobile and desktop layout, keyboard behavior, loading, error, empty, and stale-data states.
- Recruiter quality: clear architecture, documented trade-offs, reproducible demo, focused tests, and readable README.

## Roadmap phases

1. Governance and repository foundation
2. Security, identity, and database baseline
3. Financial domain model and fixtures
4. Trade Republic import
5. Portfolio accounting and dashboard
6. Market data and TradingView-style analysis
7. Alert and threshold engine
8. News intelligence
9. Deterministic advisor and Discovery Radar
10. Local AI explanation layer
11. Notifications, learning coach, and decision journal
12. Hardening, demo mode, deployment, and recruiter documentation

The detailed backlog, field setup, milestones, and issue templates are in [docs/project-management.md](docs/project-management.md).
