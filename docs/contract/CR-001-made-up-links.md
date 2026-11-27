# CR-001: Made-up links in live replies (defect found in production)

**Raised by:** contractor, from the post-deploy smoke test · **Severity:** high · **Type:** defect

## What happened

The first ticket through the deployed service ("my card … was charged twice") was
auto-resolved, and the reply ended with:

> You can find more details in our article [kb-002](https://example.com/kb-002).

The model invented a URL. In a bank's message, a link is the classic phishing pattern
customers are told never to trust, and a made-up one is worse. The eval suite didn't catch it,
because no check looked at links.

## Why the evals missed it

The gate checked *which* article a reply cited (`cited_article_ids`), not *what the reply
text contained*. That's the general lesson: a metric only protects you from what it measures.

## Change

1. **Output guard:** `guardrails.output_violations` now flags any URL, `www.` address or
   internal article ID (`kb-002`) in the reply text. A flagged reply is escalated, never sent.
2. **Prompt v5:** the draft prompt forbids URLs, markdown links and article IDs in the reply.
   It describes where things are in the app instead ("Activity → select the payment"), and
   citations go only in `cited_article_ids`.
3. **New eval metrics:**
   - `clean_draft_rate` (quality, ≥ 95%): the share of drafted replies that would pass the
     output check. It measures the *prompt*.
   - `unsafe_auto_replies` (limit, = 0): auto-sent replies failing the output check. It
     measures the *guard*, and must always be zero.
4. A unit test with the exact reply from production.

The guard alone would have made it safe, but it would have cost automation (every reply with a
link escalated). The prompt fix restores automation. The two metrics show each effect
separately.

## Result

See the PR for this change request: the eval gate's comment shows the before and after.
