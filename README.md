# PIA — Personal Investor Advisor

PIA is a private, chart-led personal financial cockpit for savings, investments, early warnings, opportunity discovery, and financial learning.

It is a decision-support tool. It does not execute trades or promise returns.

## Status

The repository is in Phase 1: foundation. Product code has not started.

## Local setup

PIA uses [pnpm](https://pnpm.io/) for JavaScript workspaces and
[uv](https://docs.astral.sh/uv/) for the Python API workspace. Install Node
22.22.2 and Python 3.11 (pinned in `.node-version` and `.python-version`), then:

```sh
corepack enable
pnpm run setup
```

`pnpm run setup` installs the JavaScript workspace and synchronizes the API Python
environment. No private credentials are needed. Safe, example-only environment
files live at `.env.example`, `apps/web/.env.example`, and `apps/api/.env.example`.

Run the current workspace verification from the repository root:

```sh
pnpm check
```

The web and API packages are boundaries only until their separately approved shell
issues are implemented.

## How work is managed

- [Project Bible](PROJECT_BIBLE.md): stable rules, architecture, guardrails, and agent workflow.
- [Project management guide](docs/project-management.md): GitHub Project fields, milestones, and issue conventions.
- [PIA Roadmap](https://github.com/users/deanpm1994/projects/1): live execution tracker.
- [Architecture overview](docs/architecture/system-overview.md): proposed system boundaries.
- [Decision records](docs/decisions): durable technical decisions.
- GitHub Issues: executable work.

## Intended stack

- Next.js and TypeScript
- FastAPI and Python
- Supabase PostgreSQL, Auth, and private Storage
- TradingView Lightweight Charts
- Local-first AI through Ollama, with AI optional

## Privacy

Never add real broker exports, API keys, or personal financial data to this repository. Use synthetic fixtures and demo data only.

## License

The source code is available under the [MIT License](LICENSE). Market data, news, and third-party provider content remain subject to their own licenses and terms.
