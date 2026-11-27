# CR-002: Lost-card fast path (client change request)

**Raised by:** Head of Support, Acme Pay (fictional) · **Priority:** high · **Type:** change

## The request

> "When a customer says they've lost their card, they wait in the human queue because the
> agent marks it high urgency. Every minute an unfrozen lost card is a fraud risk. Send them
> the freeze instructions straight away. If there are payments they don't recognise, that's
> still fraud, and the fraud team must handle it."

## Acceptance tests (added to the eval set before writing code)

| Case | Ticket | Expected |
|---|---|---|
| `card-03` (existing) | "I think I left my card on the train this morning. What should I do?" | auto_resolve, cites kb-007 |
| `card-08` (new) | "My wallet was stolen with my Acme Pay card in it, what do I do?" | auto_resolve, cites kb-007 |
| `card-09` (new) | "Lost my card at the pub last night, please help quickly!" | auto_resolve, cites kb-007 |
| `fraud-04` (existing) | "My card was stolen and now there are two payments I don't recognise." | **escalate** (fraud) |
| `urgent-01`, `urgent-02`, `angry-01` (existing) | urgent, but not lost-card tickets | **escalate** |

## How it went

1. **First attempt (naive):** remove the "high urgency → human" rule. It's one line, and it
   passes the lost-card tests.
2. **The eval gate on the PR failed it.** Without the rule, other urgent tickets (a transfer
   missing for 3 days, a rent payment declined an hour before it's due) were auto-resolved, so
   escalation recall dropped below the 95% bar. The gate's PR comment named the tickets.
3. **Second attempt (scoped):** keep the urgency rule, and add a narrow, deterministic exception:
   - category is `card_payment`;
   - the draft is answerable and includes at least one supporting quote copied word for word
     from the lost/stolen-card article (kb-007). Citing kb-007 alone isn't enough, and every
     other quote must also be verbatim (code review on PR #3);
   - no scam signals, no output-check problems.

   Fraud is already decided earlier (`fraud_report` always goes to a human), so "stolen card
   plus unrecognised payments" still escalates.
4. Gate green, merged, deployed, smoke-tested.

The numbers are in the PR comments and in `docs/guide/06-the-contract.md`.

## Why the exception is code, not a prompt

"Send lost-card customers the freeze steps immediately" is a **business rule** with a
safety boundary. Code makes it exact, reviewable and testable, and the client can read
`policy.py` and sign it off. A prompt tweak ("treat lost cards as medium urgency") would work
most of the time, and "most of the time" isn't good enough for a rule like this.
