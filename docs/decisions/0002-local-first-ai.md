# ADR 0002 — Use local-first AI and deterministic decisions

## Context

The product needs explanations, news summaries, educational content, and contextual review notes, but it must operate at zero required recurring cost and protect personal financial data.

## Decision

Use deterministic Python calculations and rule evaluation as the source of financial truth. Treat AI as an optional explanation layer. The default AI adapter is local Ollama; hosted adapters are optional and require an approved issue.

## Alternatives considered

- OpenAI API as the default: rejected because API usage is separately billed.
- AWS Bedrock as the default: rejected because it is pay-per-use and operationally excessive for the initial product.
- Hosted Hugging Face inference as the default: rejected because free credits and availability are not a durable product dependency.
- AI-driven recommendations without rules: rejected because facts, evidence, and auditability would be too weak.

## Consequences

PIA must remain useful with AI disabled. AI output must be structured, grounded in evidence IDs, validated, cached, and auditable.
