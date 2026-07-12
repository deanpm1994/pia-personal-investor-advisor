# PIA Agent Instructions

Before changing the repository, read PROJECT_BIBLE.md and the assigned GitHub Issue.

## Required workflow

1. Confirm the issue is approved in the GitHub Project.
2. Read linked ADRs and dependency issues.
3. Work on one issue only.
4. Keep changes inside the issue scope.
5. Add or update tests before financial or behavioral changes.
6. Run the exact verification listed in the issue.
7. Add a concise verification comment to the issue.
8. Move the issue to Review; do not self-approve it.

## Stop and ask for direction when

- a product, cost, legal, provider, or architecture decision is missing;
- an external credential or paid plan is required;
- a data migration can reinterpret or remove financial history;
- calculations disagree with fixtures;
- data licensing is unclear;
- an AI response cannot be grounded in evidence.

## Never

- commit secrets, personal financial data, raw broker exports, or screenshots containing them;
- use floats for money;
- bypass migration, authentication, or row-level security checks;
- create automatic trading behavior;
- claim a return is guaranteed;
- start an unapproved issue;
- use a hosted AI provider with private user data without an approved issue.
