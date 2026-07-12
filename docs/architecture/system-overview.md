# System overview

```mermaid
flowchart TB
    USER["Private user"] --> WEB["Next.js web and PWA"]
    WEB --> API["FastAPI application API"]
    API --> AUTH["Supabase Auth"]
    API --> DB[("Supabase PostgreSQL")]
    API --> STORAGE["Private import storage"]

    CSV["Trade Republic CSV"] --> IMPORT["Staged import and review"]
    IMPORT --> ACCOUNTING["Accounting engine"]
    ACCOUNTING --> DB

    JOBS["Scheduled Python jobs"] --> PROVIDERS["Market, news, and fundamentals adapters"]
    PROVIDERS --> FEATURES["Normalized data and feature engine"]
    FEATURES --> DB
    FEATURES --> ALERTS["Deterministic alerts and advisor evidence"]
    ALERTS --> DB

    ALERTS --> AI["Optional local AI explanation"]
    AI --> VALIDATE["Schema and evidence validator"]
    VALIDATE --> DB
```

## Boundary rules

- The browser never calculates financial totals and never receives server-only credentials.
- Domain calculations are pure Python modules independent of HTTP and provider clients.
- Providers return normalized data through adapters; no UI component calls external market services directly.
- AI receives validated evidence packs and cannot mutate financial facts.
- Every displayed recommendation links to evidence, source, and freshness metadata.
