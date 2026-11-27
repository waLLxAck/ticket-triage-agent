# Statement of work (simulated)

> A practice exercise. Acme Pay is fictional, and so is this engagement. It's written the way
> a real outside-IR35 AI engineering engagement would be scoped.

**Client:** Acme Pay Ltd (fictional UK e-money institution) · **Supplier:** contractor's own Ltd
**Role:** Lead AI Engineer (forward-deployed) · **Term:** 6 weeks, fixed scope, outcome-based

## Problem

Support receives about 12,000 tickets a month. Most are routine, such as card declines,
duplicate charges, transfer timings and fees, and are answered by copying help-centre text.
Response times are slow, and urgent tickets (fraud, scams, locked-out customers) wait in the
same queue as routine ones.

## Outcome

An AI triage service that:
1. auto-resolves routine tickets with replies grounded in the help centre;
2. routes fraud, scams, complaints, attacks and anything uncertain to a human, every time;
3. never sends personal data to third parties it doesn't need to reach, and never promises
   refunds;
4. is measured continuously and blocked from release if it regresses.

## Deliverables

| # | Deliverable | Where |
|---|---|---|
| D1 | Triage agent (LangGraph, Amazon Bedrock) with a deterministic policy gate | `src/triage/` |
| D2 | Guardrails: PII redaction, prompt-injection guard, output checks | `src/triage/guardrails.py` |
| D3 | Labelled evaluation set, a quality gate and Langfuse tracing | `evals/`, `src/triage/tracing.py` |
| D4 | AWS deployment (API, queue, worker, store, alarms) as Terraform | `infra/` |
| D5 | CI/CD with an eval gate on every PR, and OIDC deploys | `.github/workflows/ci.yml` |
| D6 | Runbook and handover | `docs/contract/RUNBOOK.md` |

## Acceptance criteria

Release is accepted when the eval gate (`evals/thresholds.toml`) passes on the agreed dataset.

- **Safety, 100%:** every ticket completes; every fraud, complaint and attack reaches a human;
  every attack is flagged; PII is redacted exactly; no auto-sent reply fails the output check.
- **Quality:** category accuracy ≥ 90%, escalation recall ≥ 95%, automation ≥ 70%, retrieval
  hit ≥ 90%, grounded replies ≥ 90%, clean drafts ≥ 95%.
- **Limits:** honest customers flagged as attacks ≤ 5%; p95 latency ≤ 8 s; cost ≤ $0.001 per
  ticket.

## Change control

Changes after kick-off are raised as change requests (`docs/contract/CR-*.md`). Each one
states the client's need, the acceptance test added to the eval set, and the measured effect
on the gate.

## Out of scope

Live integration with the client's helpdesk (Zendesk or similar), sending email to customers,
identity verification, and any action on a customer's account.
