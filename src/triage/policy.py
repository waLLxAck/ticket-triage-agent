"""The policy gate: plain code, not an LLM, decides whether a reply goes out unreviewed.

This is the most important idea in production agents: the model *proposes*, deterministic
rules *dispose*. Rules are cheap, auditable, and a regulator can read them.
"""

import re

from triage.knowledge import quote_is_verbatim
from triage.schemas import Action, Category, Classification, Draft, Urgency

ALWAYS_HUMAN = {Category.fraud_report, Category.complaint, Category.unknown}
MIN_CONFIDENCE = 0.7

# CR-002: lost or stolen cards are urgent because every minute unfrozen is a fraud risk, so
# the fastest safe help is the self-serve freeze steps (kb-007), sent immediately. The exception
# is deliberately narrow. It covers only card tickets answered from the freeze article, with no
# scam signals. Unrecognised payments are classified fraud_report, which always goes to a human
# above. (Attempt 1 removed the urgency rule entirely, and the eval gate caught 4 other urgent
# tickets being auto-resolved.)
LOST_CARD_ARTICLE = "kb-007"
FREEZE_INSTRUCTION = re.compile(r"\bfreez(e|ing)\b", re.IGNORECASE)
_NEGATED = re.compile(r"\b(not|never|don't|don’t|avoid|no need to)\s+$", re.IGNORECASE)


def tells_customer_to_freeze(reply: str) -> bool:
    """True if the reply contains an affirmative freeze instruction. "Don't freeze your card"
    mentions freezing but says the opposite (code review, round 3)."""
    return any(
        not _NEGATED.search(reply[max(0, m.start() - 25) : m.start()])
        for m in FREEZE_INSTRUCTION.finditer(reply)
    )


def lost_card_fast_path(c: Classification, d: Draft | None, scam_signals, output_problems) -> bool:
    """The draft must actually give the freeze steps: at least one supporting quote must be
    verbatim from kb-007. Citing kb-007 alone isn't enough (a code-review finding: a declined-card
    answer citing kb-001 *and* kb-007 would otherwise skip the urgency rule)."""
    return (
        c.category == Category.card_payment
        and d is not None
        and d.answerable
        and any(quote_is_verbatim(q, [LOST_CARD_ARTICLE]) for q in d.supporting_quotes)
        and tells_customer_to_freeze(d.reply)  # the reply itself must say to freeze
        and not scam_signals
        and not output_problems
    )


def decide(
    classification: Classification,
    draft: Draft | None,
    *,
    injection_reasons: list[str] = (),
    output_problems: list[str] = (),
    scam_signals: list[str] = (),
) -> tuple[Action, list[str]]:
    if injection_reasons:  # never act on a ticket that tried to hijack the agent
        return Action.escalate, ["possible prompt injection: " + "; ".join(injection_reasons)]
    reasons = list(output_problems)
    if scam_signals:
        reasons.append(f"possible scam ({', '.join(scam_signals)}): fraud team must review")
    if classification.category in ALWAYS_HUMAN:
        reasons.append(f"category '{classification.category}' always goes to a human")
    if classification.confidence < MIN_CONFIDENCE:
        reasons.append(f"confidence {classification.confidence:.2f} below {MIN_CONFIDENCE}")
    if classification.urgency == Urgency.high and not lost_card_fast_path(
        classification, draft, scam_signals, output_problems
    ):
        reasons.append("high urgency")
    if draft is not None and not draft.answerable:
        reasons.append("help centre doesn't cover it")
    if draft is not None and draft.answerable and not draft.cited_article_ids:
        reasons.append("reply cites no article (possible hallucination)")
    elif draft is not None and draft.answerable and not draft.supporting_quotes:
        reasons.append("reply has no supporting quote")
    elif draft is not None and draft.answerable:
        # Every quote must be real: one valid quote can't vouch for an invented second one.
        bad = [
            q for q in draft.supporting_quotes if not quote_is_verbatim(q, draft.cited_article_ids)
        ]
        if bad:
            reasons.append(
                f"{len(bad)} supporting quote(s) not found verbatim in the cited articles"
            )
    return (Action.escalate if reasons else Action.auto_resolve), reasons
