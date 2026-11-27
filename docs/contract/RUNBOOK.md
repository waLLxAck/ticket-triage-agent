# Runbook: ticket triage service (handover)

> Demo system for a fictional client. Written as the handover document a forward-deployed
> engineer leaves behind.

## At a glance

| Thing | Where |
|---|---|
| API (public, needs `x-api-key`) | `terraform -chdir=infra/app output -raw api_url` |
| API key | `terraform -chdir=infra/app output -raw api_key` (also SSM `/triage/api_key`) |
| Human review page | `<api_url>/` (paste the API key) |
| Region | eu-north-1 (Stockholm) |
| Traces, evals, experiments | Langfuse Cloud (EU), project "My Project", dataset `acme-triage` |
| Logs | CloudWatch `/aws/lambda/triage-api`, `/aws/lambda/triage-worker` (7 days) |
| Alerts | SNS topic `triage-alerts`, emailed (confirm the subscription email once) |
| Budget | `triage-monthly`, $1/month, email at 50% actual and 100% forecast |

## Everyday operations

**Submit a ticket**
```bash
curl -X POST "$URL/tickets" -H "x-api-key: $KEY" -H 'content-type: application/json' \
     -d '{"text":"My card was declined at Tesco"}'
```

**Check a ticket:** `curl "$URL/tickets/<id>" -H "x-api-key: $KEY"`
Statuses: `queued` → `auto_resolved` | `awaiting_review` → `human_approved` | `human_edited`, or
`failed`.

**Review escalated tickets:** open `<api_url>/` in a browser and enter the key. Each card
shows why it was escalated, the drafted reply and the decision trace.

**Find a ticket's trace:** in Langfuse, filter traces by `metadata.ticket_id`.

**Query costs and latency:** CloudWatch Logs Insights on `/aws/lambda/triage-worker`:
```
fields @timestamp, ticket_id, status, category, cost_usd, latency_ms
| filter ispresent(ticket_id) | stats avg(cost_usd), pct(latency_ms, 95), count() by status
```

## Alarms and what to do

| Alarm | Meaning | Action |
|---|---|---|
| `triage-dlq-not-empty` | a ticket failed 3 times | Read the message in `triage-tickets-dlq` and check the worker logs for its `ticket_id`. Fix the cause, then redrive (SQS console → DLQ → "Start DLQ redrive"). |
| `triage-worker-errors` | the worker threw or timed out | Check the logs. `ThrottlingException` means the Bedrock quota (20/min) was exceeded; lower `per_worker_rpm` or `worker_concurrency` in `infra/app/main.tf`. |
| Budget email | spend is trending over $1/month | Check Cost Explorer by service. Bedrock is the usual one; check for a loop or a flood of tickets. |

## Changing the agent safely

1. Branch, change the prompt (`prompts.py`, and bump `PROMPT_VERSION`), the policy
   (`policy.py`) or the guardrails.
2. **Add eval cases for the behaviour you want first** (`evals/dataset.jsonl`).
3. Open a PR. CI runs unit tests, then the eval gate (~8 min, ~1p) and comments the report.
4. Merge only on green (branch protection enforces it). `main` deploys automatically and
   smoke-tests.

**Rollback:** revert the merge commit on `main`; the pipeline redeploys the previous version.
Terraform state is versioned in S3 if infrastructure needs restoring.

## Limits and known risks

- **Bedrock quota:** 20 requests a minute for Nova Lite on this account, about 6 tickets a minute.
  Bursts queue in SQS, so they're delayed rather than lost. Request a quota increase before real volume.
- **Cold start:** about 3.5 s on the first ticket after idle.
- **Retrieval** is keyword-based over 8 articles. It's fine at this size; move to embeddings
  before the help centre grows.
- **PII redaction** is regex-based (cards with Luhn, IBAN, email, UK phone, sort code, account
  number). It doesn't catch names or addresses; add an NER service (Comprehend, Presidio) for
  production.
- **Review queue** uses a DynamoDB Scan; add a GSI on `status` at volume.
- **The API key** is a single shared secret. For production, put the review page behind SSO and
  give each integration its own key.
- **Grounding is checked by quotes, not by meaning.** A reply can quote a real sentence that
  doesn't support its claim (the eval case `fees-05` does this intermittently). CR-003
  (planned) adds an LLM-as-judge groundedness check before auto-send.
- **Free plan:** ends 27 May 2027, or when the $100 of credits runs out. The account is closed
  unless it's upgraded.

## Tear down

```bash
terraform -chdir=infra/app destroy        # app resources (uses the S3 state)
terraform -chdir=infra/bootstrap destroy  # state bucket, OIDC provider, CI roles
```
