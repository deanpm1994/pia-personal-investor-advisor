# ADR 0004 — Separate Supabase infrastructure migrations

## Decision

Alembic is authoritative for PIA-owned application objects in `public`.
Supabase CLI SQL migrations are authoritative only for Supabase-managed
infrastructure, beginning with Storage buckets and `storage.objects` policies.

## Consequences

Neither migration system may manage the other's objects. Storage access policies
must be version controlled and covered by local-Supabase authorization tests;
manual dashboard configuration is not a source of truth.
