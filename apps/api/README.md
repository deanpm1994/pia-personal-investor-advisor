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
(the default), `test`, or `production`. The service does not load dotenv files or
require credentials.

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
