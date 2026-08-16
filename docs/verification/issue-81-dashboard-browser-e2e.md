# Issue #81 — authenticated dashboard browser review

The Playwright suite uses an in-browser, synthetic authenticated session and
intercepts only the synthetic PIA API origin. It does not connect to Supabase,
load a broker export, or use hosted credentials.

Run it from the repository root:

```sh
pnpm --filter @pia/web test:e2e
```

## Recorded responsive review matrix

| Viewport | State coverage | Browser checks |
| --- | --- | --- |
| Desktop: 1440 × 900 | fresh, stale/incomplete non-EUR, empty, error, explicit refresh | authenticated read/refresh path, progress semantics, live status, tab selection, keyboard focus |
| Mobile: 390 × 844 | fresh, stale/incomplete non-EUR, empty, error, explicit refresh | authenticated read/refresh path, progress semantics, live status, tab selection, keyboard focus |

The suite asserts exact API-provided Decimal strings and never calculates a
financial value in the browser. It records no screenshots, account identifiers,
or personal financial values.
