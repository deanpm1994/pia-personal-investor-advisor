# GitHub Project setup and operating model

GitHub Issues are the live backlog. The GitHub Project named **PIA Roadmap** is the operational source of truth for status and prioritization. PROJECT_BIBLE.md remains the compact constitution.

## Create the project

Create a user Project named **PIA Roadmap** and link it to this repository. Use the board, table, and roadmap layouts.

## Required fields

| Field | Type | Values |
|---|---|---|
| Status | Single select | Proposed, Approved, In Progress, Review, Blocked, Done, Deferred, Rejected |
| Phase | Single select | 0 through 12 |
| Area | Single select | Governance, Frontend, API, Database, Finance, Import, Market Data, Alerts, News, AI, Learning, Security, DevOps |
| Priority | Single select | Critical, High, Medium, Low |
| Risk | Single select | Financial, Security, Privacy, Provider, Cost, UX, Low |
| Approval | Single select | Required, Approved, Not applicable |
| Size | Single select | Small, Medium, Large |
| Target date | Date | optional |
| Ralph iteration | Number | starts at 0 |

## Required views

1. **Founder roadmap** — Roadmap layout, grouped by Phase, ordered by Target date.
2. **Agent queue** — Table filtered to Approval: Approved and Status: Proposed or In Progress.
3. **Review queue** — Board filtered to Status: Review.
4. **Blocked decisions** — Table filtered to Status: Blocked.
5. **Risk view** — Table grouped by Risk.
6. **Completed work** — Table filtered to Status: Done.

## Built-in automation

- Auto-add issues from this repository to the Project.
- Set new items to Proposed and Approval to Required.
- Set Status to Done when an issue is closed.
- Set Status to Done when a linked pull request is merged.
- Archive Done items after 30 days if the history is preserved in GitHub Issues.

## Branches and pull requests

Use a Gitflow-lite workflow:

- `main` contains release-ready work only. Promote an accepted phase or release with a pull request from `develop` to `main`, merged with a merge commit.
- `develop` is the shared integration branch. Every approved issue begins from the current `develop` and returns to it through a pull request.
- Name task branches `feat/<issue>-<slug>`, `fix/<issue>-<slug>`, or `chore/<issue>-<slug>`.
- Squash-merge task pull requests into `develop` and delete the task branch after merging.
- Protect `main` and `develop`: pull requests and resolved conversations are required; direct pushes, force pushes, and deletions are prohibited.
- Do not require status checks until the Phase 1 CI task provides them. Add those checks through that approved task.

## Milestones

Create one milestone per roadmap phase:

| Milestone | Purpose |
|---|---|
| Phase 0 — Governance | Repository rules, decisions, and agent contract |
| Phase 1 — Foundation | Local tooling, configuration, and CI |
| Phase 2 — Security and database | Auth, RLS, migrations, audit baseline |
| Phase 3 — Financial domain | Canonical data model and fixtures |
| Phase 4 — Trade Republic import | Staged parsing, review, confirmation, idempotency |
| Phase 5 — Financial picture | Accounting, savings, snapshots, dashboard |
| Phase 6 — Market analysis | Price ingestion, indicators, charts |
| Phase 7 — Alerts | Thresholds, evaluation, inbox |
| Phase 8 — News intelligence | Ingestion, linking, provenance |
| Phase 9 — Advisor and radar | Deterministic evidence and discovery |
| Phase 10 — Local AI | Ollama provider and AI validation |
| Phase 11 — Learning and notifications | Journal, coach, notification channels |
| Phase 12 — Hardening | Demo, security review, deployment, recruiter materials |

## Recommended labels

Create these labels in the repository once and use them in addition to Project fields:

- `type:task`, `type:decision`, `type:bug`, `type:spike`
- `area:frontend`, `area:api`, `area:database`, `area:finance`, `area:import`, `area:market-data`, `area:news`, `area:ai`, `area:security`, `area:devops`
- `risk:financial`, `risk:security`, `risk:privacy`, `risk:provider`, `risk:cost`, `risk:ux`
- `blocked`, `needs-founder-decision`, `good-first-issue`

## Issue conventions

- One issue is one independently reviewable unit of work.
- Create a parent issue for each phase and child issues for executable tasks.
- Do not create all future issues at once. Seed the active phase, then add the next phase after its predecessor is accepted.
- Every issue links to PROJECT_BIBLE.md and the relevant ADRs.
- Issues must carry acceptance criteria, verification, dependencies, risk, and cost impact.
- Completion evidence belongs in an issue comment, not only in a pull request.

## Initial Phase 0 backlog

1. Approve product charter and guardrails.
2. Configure GitHub Project fields, views, milestones, and automations.
3. Add the agent contract and issue/PR templates.
4. Record architecture decision records.
5. Define the Phase 1 foundation backlog after Phase 0 acceptance.
