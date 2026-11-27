# ticket-triage-agent

> **Demo / learning project.** "Acme Pay" is a fictional fintech. No real customers or data.

An LLM agent that triages customer support tickets: it redacts PII, blocks prompt injection,
classifies the ticket, drafts a grounded reply, and either auto-resolves it or hands it to a human.
Every change is gated by an eval suite in CI.

```
ticket → API (FastAPI on Lambda) → SQS → worker Lambda
           → redact PII → injection guard (regex + LLM judge) → LangGraph agent (Bedrock / Nova)
           → policy gate (rules + output checks) → auto-resolve | human review
         → DynamoDB (state) · Langfuse (traces, costs, eval scores)
```

| Layer | Tool | Free tier used |
|---|---|---|
| Agent orchestration | LangGraph | open source |
| LLM | Amazon Bedrock, Nova Lite | AWS Free-plan credits |
| API | FastAPI + Mangum on Lambda Function URL | Lambda always-free |
| Queue | SQS | always-free (1M requests/month) |
| State | DynamoDB | always-free (25 GB) |
| Tracing and evals | Langfuse Cloud | Hobby (50k units/month) |
| Infrastructure | Terraform | open source |
| CI/CD | GitHub Actions + AWS OIDC | free on public repos |

Status: Phase 0 accounts · Phase 1 agent · Phase 2 guardrails are done. Learning notes for each phase are in [`docs/guide/`](docs/guide/).
