"""The shapes of data that flow through the agent.

Pydantic models do two jobs here:
1. They are the *contract* the LLM must fill in (structured output). The field descriptions
   are sent to the model as part of the tool schema, so they double as instructions.
2. They validate whatever comes back, so a malformed LLM answer fails loudly instead of
   leaking garbage downstream.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class Category(StrEnum):
    card_payment = "card_payment"
    account_access = "account_access"
    transfer = "transfer"
    fees_limits = "fees_limits"
    account_closure = "account_closure"
    account_admin = "account_admin"
    fraud_report = "fraud_report"
    complaint = "complaint"
    unknown = "unknown"  # an explicit "I don't know" beats a confident wrong label


class Urgency(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class Action(StrEnum):
    auto_resolve = "auto_resolve"
    escalate = "escalate"


class Classification(BaseModel):
    """Step 1: what is this ticket about? (filled in by the LLM)"""

    category: Category = Field(description="Best-fitting category, or 'unknown' if none fits.")
    urgency: Urgency = Field(
        description="high = money at risk or customer locked out; medium = needs action soon; "
        "low = informational."
    )
    confidence: float = Field(
        ge=0, le=1, description="How sure you are of the category, from 0 to 1."
    )
    reasoning: str = Field(description="One sentence explaining the category choice.")


class Draft(BaseModel):
    """Step 2: a reply grounded in help-centre articles (filled in by the LLM)."""

    answerable: bool = Field(
        description="True only if the provided articles fully answer the customer. "
        "False if the answer would need information that is not in the articles."
    )
    reply: str = Field(
        description="The reply to the customer. Use only facts from the articles. "
        "If not answerable, write a short holding reply saying a specialist will follow up."
    )
    cited_article_ids: list[str] = Field(
        default_factory=list, description="IDs of the articles the reply relies on."
    )
    missing_info: list[str] = Field(
        default_factory=list,
        description="What a human would need to know or do that the articles don't cover.",
    )


class TriageResult(BaseModel):
    """The final record: what the agent proposed, and what the policy decided."""

    ticket_id: str
    category: Category
    urgency: Urgency
    confidence: float
    action: Action
    escalation_reasons: list[str]
    reply: str
    cited_article_ids: list[str]
    missing_info: list[str]
    decision_trace: list[str]  # human-readable log of every step, for auditors and debugging
