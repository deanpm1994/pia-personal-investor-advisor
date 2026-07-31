# ADR 0006 — Keep staged-import writes behind the trusted API boundary

## Context

The browser has an authenticated Supabase JWT and may upload a raw CSV to its
private Storage prefix. It must not be able to create normalized candidates,
validation diagnostics, or lifecycle events: those records are the evidence
that allows a staged import to become an immutable financial ledger entry.
Owner-only RLS alone is insufficient because it lets an owner manufacture
arbitrary review-ready data through the REST API.

## Decision

Trade Republic CSV parsing, validation, and persistence run in the Python API.
The API uploads the raw file with the owner's bearer token, then uses its
server-only `PIA_DATABASE_URL` to atomically persist the parsed rows,
diagnostics, lifecycle history, and `trusted_staged_at` provenance marker.

The authenticated Supabase role retains owner-scoped `SELECT` access and the
anonymous role retains no access. Neither role has `INSERT`, `UPDATE`, or
`DELETE` grants on any staged-import table. `confirm_staged_import` requires both the normal
`review_ready` state and a non-null `trusted_staged_at` marker. Pre-existing or
otherwise untrusted review-ready batches therefore cannot write ledger facts.

## Consequences

`PIA_DATABASE_URL` is required by the API deployment that enables import
staging; it is a server-side database credential, never browser configuration
or a Supabase service-role key. Local Supabase uses its documented local-only
database URL, so authorization tests remain credential-free with respect to
hosted projects and providers.

The API is now the trusted parser boundary. Its database transaction makes the
database-side staging records all-or-nothing; if it fails after Storage upload,
the API deletes that private object. Raw-file ownership and review RLS remain
unchanged. Any future parser or broker format must use this same boundary (or a
new approved decision with equivalent provenance guarantees).
