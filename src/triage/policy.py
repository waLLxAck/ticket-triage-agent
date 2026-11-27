"""The policy gate: plain code, not an LLM, decides whether a reply goes out unreviewed.

This is the most important idea in production agents: the model *proposes*, deterministic
rules *dispose*. Rules are cheap, auditable, and a regulator can read them.
"""

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


def lost_card_fast_path(c: Classification, d: Draft | None, scam_signals, output_problems) -> bool:
    return (
        c.category == Category.card_payment
        and d is not None
        and d.answerable
        and LOST_CARD_ARTICLE in d.cited_article_ids
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
    elif (
        draft is not None
        and draft.answerable
        and not any(quote_is_verbatim(q, draft.cited_article_ids) for q in draft.supporting_quotes)
    ):
        reasons.append("reply has no verbatim quote from its cited articles")
    return (Action.escalate if reasons else Action.auto_resolve), reasons
