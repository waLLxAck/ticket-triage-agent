"""The policy gate: plain code, not an LLM, decides whether a reply goes out unreviewed.

This is the most important idea in production agents: the model *proposes*, deterministic
rules *dispose*. Rules are cheap, auditable, and a regulator can read them.
"""

from triage.schemas import Action, Category, Classification, Draft, Urgency

ALWAYS_HUMAN = {Category.fraud_report, Category.complaint, Category.unknown}
MIN_CONFIDENCE = 0.7


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
    if classification.urgency == Urgency.high:
        reasons.append("high urgency")
    if draft is not None and not draft.answerable:
        reasons.append("help centre doesn't cover it")
    if draft is not None and draft.answerable and not draft.cited_article_ids:
        reasons.append("reply cites no article (possible hallucination)")
    return (Action.escalate if reasons else Action.auto_resolve), reasons
