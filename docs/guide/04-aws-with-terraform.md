# Phase 4: AWS with Terraform

## What runs where

```
customer ──HTTPS──► Lambda Function URL ──► triage-api  (FastAPI + Mangum, 512 MB, 15 s)
                                              │ 1. check x-api-key
                                              │ 2. redact PII (the raw text is never stored)
                                              │ 3. DynamoDB put  {status: queued}
                                              │ 4. SQS send      {ticket_id, redacted text}
                                              ▼ 202 Accepted in ~100 ms (warm)
                     SQS triage-tickets  (long polling, visibility 12 min)
                                              │ event source mapping: batch 1, max 2 workers
                                              ▼
                     triage-worker  (the LangGraph agent, 512 MB, 120 s)
                                              │ Bedrock Nova Lite ×3 · Langfuse traces
                                              ▼
                     DynamoDB {status: auto_resolved | awaiting_review, result}
                                              │
                     review page (GET /) ◄────┘  a human approves or edits escalated replies
   3 failed attempts ─► SQS triage-tickets-dlq ─► CloudWatch alarm ─► SNS email
```

The code is in `src/triage/api.py`, `worker.py`, `store.py` and `settings.py`. The
infrastructure is in `infra/app/*.tf`, with a one-off setup in `infra/bootstrap/main.tf`.

## Concepts, one at a time

**Infrastructure as code (Terraform).** Every resource is written in `.tf` files.
`terraform plan` shows the difference between the files and what actually exists, and
`terraform apply` makes AWS match the files. The benefits:
- reviewable: infrastructure changes go through a PR like code;
- repeatable: the same files build a staging copy;
- disposable: `terraform destroy` removes everything.

**State.** Terraform records what it created in a *state file*. Ours lives in S3
(`triage-tfstate-<account>`) so your laptop and CI share one copy. `use_lockfile = true` writes
a lock object, which stops two applies from colliding. State can contain secrets (the
generated API key), so the bucket is private, encrypted and versioned.

**Bootstrap vs app.** The state bucket and the CI roles must exist *before* CI can deploy, and
a bucket can't hold the state of the config that creates it. So `infra/bootstrap` runs once
from the laptop with local state, and `infra/app` is everything else, deployed by CI.

**Lambda.** You upload code and AWS runs it on demand. You pay per request and per GB-second,
and the always-free tier is 1M requests plus 400,000 GB-s a month. Two functions share one zip
built by `scripts/build_lambda.py`. It contains Linux wheels downloaded on Windows by
`uv pip install --python-platform x86_64-manylinux_2_28`, so there's no Docker. At 48.7 MB it's
just under the 50 MB direct-upload limit, so it goes through S3, which is the safer path.

**Cold start.** The first request to a new Lambda instance loads Python and every library:
3.5 s here (LangChain is heavy). Warm requests skip that. It's a real trade-off: smaller
dependencies, more memory (which also brings more CPU), or provisioned concurrency (which
isn't free).

**Function URL + Mangum.** A Function URL is a free HTTPS endpoint attached to a Lambda, so
API Gateway isn't needed. Mangum turns the Lambda event into an ASGI request, so ordinary
FastAPI code runs unchanged locally and in AWS.

**SQS between the API and the agent.** This decouples the two:
- The customer gets `202 Accepted` in about 100 ms, instead of waiting about 5 s for three LLM
  calls.
- Bursts queue up instead of overrunning Bedrock's 20 requests a minute.
  `maximum_concurrency = 2` caps the workers, and each worker's token bucket allows 9 a minute,
  so 2 × 9 = 18, under the 20 quota.
- Failures retry. A failed message reappears after the visibility timeout, and after 3
  attempts it moves to the **dead-letter queue**, where an alarm emails you. Nothing is
  silently lost.
- `ReportBatchItemFailures` retries only the failed messages in a batch.

**DynamoDB.** A key-value table with one item per ticket. Provisioned at 5 read and 5 write
capacity units, it stays inside the always-free 25/25. On-demand mode would be simpler, but it
bills per request. Numbers must be `Decimal`, hence the converters in `store.py`. The review
queue uses a `Scan`, which is fine at demo volume. At scale you'd add a GSI (secondary index)
on `status`.

**IAM least privilege.** Each function has its own role with only the actions it needs, on
only its own resources:
- the **API** can write tickets and send to the queue, but **cannot call Bedrock**, so a
  compromised API can't run up a model bill;
- the **worker** can invoke **one model in one Region**.

**Secrets in SSM Parameter Store.** The Langfuse keys and the API key are SecureString
parameters (KMS-encrypted, free on the standard tier). The Lambda environment holds only the
parameter *names*, and `settings.load()` fetches the values at cold start. Terraform marks them
`sensitive`, so they never appear in plan output.

**Operations.**
- CloudWatch log groups with 7-day retention.
- Two alarms (DLQ not empty, worker errors) that email through SNS. Click AWS's confirmation
  email once to start receiving them.
- A $1/month budget that emails at 50% actual and 100% forecast.

## What the live smoke test showed

| Step | Result |
|---|---|
| POST without a key | `401` |
| POST "My card 4539 1488 0343 6467 was charged twice…" | `202`, `pii_redacted: ["CARD"]` |
| What DynamoDB stores | "My card [CARD] was charged twice…" |
| Worker | `auto_resolved`, card_payment, 1.9 s of model time, $0.0002 |
| Cold start (Init Duration) | 3.5 s |
| Langfuse | trace arrived with `environment = prod` |

**Two findings from production that the evals didn't catch:**
1. **The rate limiter was too strict.** A single ticket took 19.5 s in Lambda for 1.9 s of work,
   because the limiter spaced *every* call 6.7 s apart. It became a token bucket: a ticket's 3
   calls burst straight through, and the average rate still respects the quota.
2. **The reply contained a made-up link:** `[kb-002](https://example.com/kb-002)`. The model
   invented a URL. That's Phase 6's first bug report.

## Cost

| Resource | Free allowance | Our usage |
|---|---|---|
| Lambda | 1M requests + 400k GB-s/month (always free) | ~2.5 GB-s per ticket |
| SQS | 1M requests/month (always free) | the event source mapping long-polls, so there's a steady background of empty receives; still well under |
| DynamoDB | 25 GB + 25 RCU/WCU (always free) | 5/5 provisioned |
| CloudWatch | 5 GB logs, 10 alarms | 2 alarms, 7-day retention |
| SSM standard parameters | free | 3 |
| S3 | tiny (state + zips, 14-day expiry) | covered by credits |
| Bedrock Nova Lite | credits | ~$0.0002 per ticket |
