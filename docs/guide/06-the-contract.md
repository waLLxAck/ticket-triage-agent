# Phase 6: The simulated contract

The first five phases built the system. This phase works on it the way a forward-deployed
engineer does: under a statement of work, with change requests and code review, where every
change has to get through the eval gate before it reaches production.

The paperwork is in `docs/contract/`:
- `SOW.md`: problem, outcome, deliverables, and acceptance criteria, which **are** the eval
  thresholds;
- `CR-001-made-up-links.md`: a defect found in production;
- `CR-002-lost-card-fast-path.md`: a change the client asked for;
- `RUNBOOK.md`: the handover: how to operate, alert, change, roll back and tear down.

## Timeline (all of it really happened, on GitHub)

| # | What | Gate result | Lesson |
|---|---|---|---|
| PR #1 | Phases 4–5 (AWS, CI/CD) | ❌ the eval job couldn't assume its AWS role | GitHub's OIDC token now uses an **immutable subject** (`repo:waLLxAck@15944458/ticket-triage-agent@1474551602:…`). CloudTrail showed the exact claim. |
| PR #1 | 18 code-review findings from CodeRabbit | 17 fixed, 1 declined with a reason | Review found an **IAM privilege escalation**: deploy could create an admin role and hand it to a Lambda. Fixed with a **permissions boundary**. It also found a redelivered SQS message that could overwrite a human's decision, fixed with **conditional writes**. |
| PR #1 | re-run | ✅ 53/53, every safety bar 100% | merged |
| main | first automatic deploy | ❌ then ❌ then ✅ | The deploy role lacked `s3:GetObjectTagging`, then `s3:ListBucketVersions`. Least privilege, found one denial at a time, fixed in PR #4. The smoke test then passed against the live URL. |
| PR #2 | CR-001: no made-up links (prompt v5 + output guard) | ❌ `clean_draft_rate` 94.x% | The gate caught **my own guard being wrong**: "Please *do not* click the link" was flagged as unsafe because the regex ignored negation. It also caught the **made-up fee policy coming back** under the new prompt. |
| PR #2 | fix: verbatim supporting quotes (prompt v6), negation-aware check | ✅ `clean_draft_rate` 100%, `unsafe_auto_replies` 0, automation 93% → 81.5% | Grounding moved from "the prompt says so" to "code checks the quote is really in the article". It costs automation, because some correct answers now escalate when the quote isn't word-perfect. |
| PR #3 | CR-002 attempt 1: drop the high-urgency rule | ❌ `escalation_recall` **96% → 81%** | The lost-card tests passed, and so did all 43 unit tests. But 4 other urgent tickets were auto-resolved: an angry customer whose transfer had been missing 3 days, a rent payment declined an hour before it was due, and two others. **Only the eval gate saw it.** |
| PR #3 | attempt 1 again, rebased onto prompt v6 (the queue kept the run) | ❌ `escalation_recall` 84.6% | A second, independent confirmation of the same regression. |
| PR #3 | CR-002 attempt 2: keep the rule, narrow fast path in code | ✅ escalation 96.2%, automation 89.7% | A business rule with a safety boundary belongs in `policy.py`, where the client can read it and sign it off. The lost-card tickets also won back the automation the quote check had cost. |

## Things this phase teaches that the others couldn't

1. **A metric only protects you from what it measures.** CR-001's made-up link passed every
   gate, because no metric looked at links. After a production finding, the first step is to
   add the check that would have caught it, and only then fix it.
2. **Guards can be wrong too.** The output guard over-blocked correct safety advice, and that
   showed up because `clean_draft_rate` dropped: drafts the *prompt* produced were judged
   unclean. A caution from code review: `unsafe_auto_replies` uses the same
   `output_violations` function as the policy, so a value of 0 only proves the policy
   *enforces* the check. It can't reveal an unsafe reply the check doesn't recognise. Finding
   those needs labelled examples in the eval set, or a second, independent checker.
3. **Prompts drift, structure holds.** The made-up-fee bug was fixed by a prompt in v3 and came
   back in v5 after an unrelated prompt edit. Asking for `supporting_quotes` and checking them
   in code (`knowledge.quote_is_verbatim`) is a structural fix: the model can't make up a quote
   that passes a substring check.
4. **Unit tests and evals catch different bugs.** Unit tests prove the code does what you
   wrote. Evals prove what you wrote is what the client needs. CR-002 attempt 1 passed every
   unit test and was still wrong.
5. **Code review is part of the deliverable.** On a fintech contract, an IAM escalation path
   or a lost human decision isn't a nit. It's a finding the client's security team would
   raise in week two.
6. **Least privilege costs some iteration.** The deploy role took two failed runs to get
   right. Each failure named the exact missing action, which is the point: the role holds
   exactly what the deploy needs and nothing more.

## Final numbers (prompt v6, 55 tickets, CI run `v6-20261127-171956`)

| Metric | Phase 3 baseline (v2) | Final (v6 + CR-002) | Bar |
|---|---|---|---|
| critical_escalation_recall | 93% ❌ | 100% | 100% |
| injection_recall | 100% | 100% | 100% |
| pii_redaction_accuracy | 100% | 100% | 100% |
| category_accuracy | 93% | 100% | ≥ 90% |
| escalation_recall | 92% ❌ | 96.2% | ≥ 95% |
| automation_rate | 81% | 89.7% | ≥ 70% |
| retrieval_hit_rate | 97% | 100% | ≥ 90% |
| clean_draft_rate | (not measured) | 100% | ≥ 95% |
| unsafe_auto_replies | (not measured) | 0 | = 0 |
| p95 latency | 9.8 s ❌ | 2.7 s | ≤ 8 s |
| cost per ticket | $0.00015 | $0.00019 | ≤ $0.001 |

A full eval run costs about $0.01 and takes 8 minutes.

## Still open: the honest backlog

- **`fees-05` is intermittent.** "Do you charge a fee for receiving money from abroad?" is
  sometimes still auto-answered "no fee". The model copies a real sentence from the fees
  article as its "quote", and that passes the verbatim check while not supporting the claim.
  A quote check proves the text exists, not that it *entails* the answer. The next change
  request (CR-003) is an **LLM-as-judge groundedness check**: a separate, narrow call that
  asks whether each claim in the reply follows from the quotes, run only on auto-resolve
  candidates, at about one extra call per routine ticket. It hasn't passed the safety bars
  only because fee questions aren't in the "critical" set. A real client should decide
  whether they should be.
- **`card-06`, `pii-01`:** correct answers that escalate because the quote wasn't word-perfect.
  This costs automation, not safety.
- **`access-03`:** expired reset link. The help centre does cover it (the link lasts 30
  minutes), but the model judges it not answerable. Fix the article wording, or tune the
  prompt.

## How a change flows now

```
client asks ─► write the CR, add eval cases for the behaviour wanted (before any code)
            ─► branch, change prompt / policy / guardrails
            ─► PR: unit tests (1 min, free) ─► eval gate (8 min, ~1p) ─► report on the PR
            ─► red: read which metric and which tickets, fix, push again
            ─► green + review ─► merge ─► deploy job: terraform apply ─► smoke test
            ─► Langfuse: compare this prompt version's experiment with the last one
```
