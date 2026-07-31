# PIA API

The API is a typed FastAPI service. Its only public endpoint today is a
credential-free health check:

```text
GET /health
```

It returns a deterministic, non-financial response:

```json
{"status":"ok","environment":"development"}
```

`PIA_ENVIRONMENT` controls the response environment and accepts `development`
(the default), `test`, or `production`. The service does not load dotenv files.

From the repository root, run the API locally and verify it with:

```sh
pnpm dev:api
pnpm lint:api
pnpm format:api
pnpm test:api
pnpm check:api
```

With the server running, check the route using:

```sh
curl http://127.0.0.1:8000/health
```

## Browser import flow

The web import-review flow calls the API from `http://localhost:3000`. Start
local Supabase, then both services during local development:

```sh
supabase start
pnpm dev:api
pnpm dev:web
```

`pnpm dev:api` obtains the running local stack's public anon key for the API
process. It never reads a service-role key or browser environment file.

`PIA_WEB_ORIGIN` defaults to `http://localhost:3000` and is the only browser
origin allowed to send authenticated import requests. Set it explicitly in the
server environment when the web app uses a different origin; do not use a
wildcard origin.
