# Supabase local and hosted configuration

PIA uses hosted Supabase in deployed environments and the Supabase CLI locally.
The local configuration in `supabase/config.toml` is credential-free and provides
the Supabase boundaries PIA uses now (PostgreSQL, Auth, Storage, and API) for
development and integration testing. Optional Studio, Realtime, Edge Functions,
vector Storage, and analytics services are disabled until an approved issue
needs them. P2.1 does not create application tables, authentication flows, RLS
policies, buckets, or financial data.

## Local setup

Prerequisites: Docker Desktop, the Supabase CLI, Node.js/pnpm, and Python/uv as
documented in the repository root.

```sh
supabase start
pnpm db:migrate
```

`supabase start` prints local-only API and database connection details. The
default `PIA_DATABASE_URL` in `.env.example` and `apps/api/.env.example` matches
the CLI database port and its documented local development credentials, so
`pnpm db:migrate` needs no manually supplied secret. Stop services with:

```sh
supabase stop
```

## Migration authority

Alembic is the sole migration authority for PIA application-owned schema and
RLS history. Its configuration and revisions live in `apps/api/migrations`.
Run the deterministic local upgrade command from the repository root:

```sh
pnpm db:migrate
```

Supabase-managed schemas, including `auth` and `storage`, are never altered by
Alembic. `supabase/config.toml` disables Supabase CLI application SQL migrations
and seeds to prevent a second application-schema history.

The P2.1 baseline contains no domain tables. The only database artifact after an
upgrade is Alembic's version bookkeeping; later approved migrations own their
application tables explicitly.

## Credential boundary

`PIA_DATABASE_URL` is server-only configuration. It is read by Python API and
Alembic processes; never put a hosted value in `apps/web/.env*`, source code,
browser build variables, logs, or committed files. The checked-in example value
is local-only. Only values with a `NEXT_PUBLIC_` prefix may reach the browser,
and P2.1 intentionally defines none.

For hosted Supabase, create an untracked `apps/api/.env` (or inject the value
through the deployment's server-side secret manager) with the hosted
`PIA_DATABASE_URL`. Do not commit the hosted database password, Supabase
service-role key, anon key, project URL, SMTP credentials, or a `supabase link`
configuration. A future approved identity task will define the browser-safe
public URL and anon-key variables; it must never expose a service-role key.

Hosted project creation, credentials, and magic-link email configuration are
founder-provided implementation inputs and are not required for local setup.
