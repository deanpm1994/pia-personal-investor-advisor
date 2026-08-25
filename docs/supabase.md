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

The local Auth issuer is explicitly `http://localhost:54321/auth/v1`, matching
the API verifier's default `PIA_SUPABASE_URL`. Keep these origins aligned; a
`localhost`/`127.0.0.1` mismatch makes otherwise valid browser sessions fail
API JWT issuer validation.

When starting the API with `pnpm dev:api`, the development script reads only the
local stack's public anon key from `supabase status`. This gives the API the
required Supabase gateway key while the browser's bearer token remains the
identity used for RLS; it does not expose or load a service-role key.

## Migration authority

Alembic is the sole migration authority for PIA application-owned `public`
schema and RLS history. Its configuration and revisions live in `apps/api/migrations`.
Run the deterministic local upgrade command from the repository root:

```sh
pnpm db:migrate
```

Supabase-managed schemas, including `auth` and `storage`, are never altered by
Alembic. Supabase CLI migrations in `supabase/migrations` are reserved strictly
for Supabase infrastructure such as Storage buckets and `storage.objects`
policies; they must not create application tables. See ADR 0004.

P2.3 introduces the first application-owned table: `public.profiles`. It is
keyed by `auth.users.id`, while the `auth` schema itself remains wholly
Supabase-managed. The migration backfills existing Auth users, and a
security-definer trigger creates a profile when Auth creates a user and
synchronizes its email when Auth changes it. Deleting an Auth user removes the
profile through the foreign key.

`profiles` has RLS enabled and no anonymous access. Authenticated users can
select only the row whose `id` equals `auth.uid()`; profile creation, updates,
and deletion are not client operations. Future user-owned tables must use the
same pattern: a non-null `user_id` foreign key to `public.profiles(id)`, RLS
enabled, and policies whose `USING` and `WITH CHECK` clauses compare `user_id`
with `auth.uid()`. Grant only the operations required by the policies.

P3.3 adds the owner-scoped canonical ledger tables: financial accounts,
instruments, immutable financial events, and normalized event legs. Authenticated
clients can select and append only rows whose `user_id` equals `auth.uid()`;
they cannot update or delete ledger history. Database constraints preserve
owner-consistent references, Decimal-backed numeric facts, source identity, and
the event/leg shapes established in ADR 0005. No browser or API ledger-writing
endpoint is introduced by this schema boundary.

P4.2 adds five application-owned staged-import tables for the private Trade
Republic CSV workflow: imports, exactly-one file metadata records, source and
normalized rows, diagnostics, and immutable state events. The raw CSV itself
continues to live only in the existing private `raw-imports` Storage bucket;
Alembic stores its owner-prefixed path and metadata, never a public URL or raw
content. As decided in ADR 0006, clients can select only their own staged data;
the Python API persists parser output and validation evidence through its
server-only database connection. The database enforces
`staged → parsed → validated → review_ready → confirmed` or
`staged → parsed → validated → blocked`; confirmation additionally requires
server-written provenance, so a client cannot manufacture a review-ready batch.

P6.2 adds a separate owner-scoped market-data store for approved instruments,
versioned provider mappings, ingestion-run evidence, immutable daily EOD bar
revisions, and replay provenance. It does not join market observations into the
financial ledger or snapshots. Authenticated clients can read only their own
metadata, and can read provider Content only while the server-controlled access
record is enabled, its licensing review is current, and the bar remains inside
its retention deadline. Clients have no write access. The trusted Python
gateway rejects disabled providers, reuses identical observations, appends
changed OHLCV values as correction revisions, and retains only normalized
Decimal-backed values plus non-secret provenance and response hashes.

P6.3 adds private watchlist membership linked to the same owner-scoped resolved
instrument identities. Authenticated clients may select only their own entries
and cannot write membership directly; authenticated list/add/remove workflows
use the trusted Python gateway. Invalid, unsupported, ambiguous, duplicate,
temporarily unavailable, and provider-disabled resolutions remain distinct.
The portfolio-candidate read uses the latest immutable financial snapshot,
preserves its exact source instrument ID and evidence event IDs, and refuses to
infer an ISIN from a broker symbol. The runtime resolver is disabled by default,
so no provider call or credential is introduced by this issue.

To run the approved local-Supabase integration suite after starting the stack
and applying migrations, run from the repository root:

```sh
pnpm run test:api:local-supabase
```

The command deliberately enumerates the owner/RLS, manual-account,
immutable-snapshot, private market-data, and watchlist suites. This keeps the
financial and security boundary explicit while ensuring every test requires the
`PIA_RUN_LOCAL_SUPABASE_TESTS` opt-in and uses only ephemeral local-Supabase
data.

## Pull-request security integration

The `Supabase security integration` job in the pull-request workflow creates
only ephemeral local Supabase state. It prepares the ignored local signing key,
starts Supabase, applies the infrastructure and Alembic migration histories,
and runs `pnpm check` plus the full opt-in Phase 5 suite for ownership and RLS,
manual accounts, and immutable snapshots. A failure in any included test fails
the pull-request check.

After this job succeeds on a pull request, configure `Supabase security
integration` as a required check for `develop` and `main`. Do not make it a
required check before its first successful run.

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
