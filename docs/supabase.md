# Supabase local and hosted configuration

PIA uses hosted Supabase in deployed environments and the Supabase CLI locally.
The local configuration in `supabase/config.toml` is credential-free and provides
the Supabase boundaries PIA uses now (PostgreSQL, Auth, Storage, and API) for
development and integration testing. Optional Studio, Realtime, Edge Functions,
vector Storage, and analytics services are disabled until an approved issue
needs them. P2.1 does not create application tables, authentication flows, RLS
policies, buckets, or financial data. P2.2 adds only the local Auth signing-key
setup and browser-safe public configuration described below.

## Local setup

Prerequisites: Docker Desktop, the Supabase CLI, Node.js/pnpm, Python/uv, and
`jq` as documented in the repository root.

```sh
./scripts/prepare-local-supabase-auth.sh
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

## Local Auth for P2.2

`./scripts/prepare-local-supabase-auth.sh` creates an ES256 signing-key set at
`supabase/signing_keys.json` if it does not already exist. This private,
machine-local file is ignored by Git. Run the script before the first
`supabase start` and whenever the file has been intentionally removed.

After startup, use `supabase status` to obtain the local API URL and anon key.
Copy only those two public values into an untracked `apps/web/.env.local`:

```dotenv
NEXT_PUBLIC_SUPABASE_URL=http://localhost:54321
NEXT_PUBLIC_SUPABASE_ANON_KEY=local-anon-key-from-supabase-status
```

The browser uses these public values exclusively. Never add the service-role
key, database password, signing key, or hosted credentials to a browser
environment file.

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
is local-only. Only values with a `NEXT_PUBLIC_` prefix may reach the browser.
P2.2 permits only `NEXT_PUBLIC_SUPABASE_URL` and
`NEXT_PUBLIC_SUPABASE_ANON_KEY`; both are public Supabase client configuration,
never server credentials.

For hosted Supabase, create an untracked `apps/api/.env` (or inject the value
through the deployment's server-side secret manager) with the hosted
`PIA_DATABASE_URL`. Do not commit the hosted database password, Supabase
service-role key, anon key, project URL, SMTP credentials, or a `supabase link`
configuration. Browser configuration uses only the public URL and anon key; it
must never expose a service-role key.

Hosted project creation, credentials, and magic-link email configuration are
founder-provided implementation inputs and are not required for local setup.
