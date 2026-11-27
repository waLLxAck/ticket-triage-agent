"""Unit tests with a fake LLM: free, instant and deterministic.

They test *our* logic (routing, retrieval, the policy gate), not the model. Model quality is
measured separately by the eval suite, which calls the real LLM.
"""

from triage.graph import triage
from triage.knowledge import search
from triage.schemas import Action, Category, Classification, Draft, Urgency


class FakeLLM:
    model_id = "fake"

    def __init__(self, classification: Classification, draft: Draft | None = None):
        self._c, self._d = classification, draft
        self.draft_calls = 0

    def classify(self, ticket_text, config=None):
        return self._c

    def draft(self, ticket_text, classification, articles, config=None):
        self.draft_calls += 1
        return self._d


def c(category, confidence=0.9, urgency=Urgency.low):
    return Classification(
        category=category, urgency=urgency, confidence=confidence, reasoning="test"
    )


GOOD_DRAFT = Draft(answerable=True, reply="Here's how...", cited_article_ids=["kb-002"])


def test_confident_answerable_ticket_is_auto_resolved():
    r = triage("charged twice", FakeLLM(c(Category.card_payment), GOOD_DRAFT))
    assert r.action == Action.auto_resolve
    assert r.escalation_reasons == []


def test_fraud_always_goes_to_a_human():
    r = triage("payments I didn't make", FakeLLM(c(Category.fraud_report), GOOD_DRAFT))
    assert r.action == Action.escalate


def test_low_confidence_escalates():
    r = triage("hmm", FakeLLM(c(Category.card_payment, confidence=0.4), GOOD_DRAFT))
    assert r.action == Action.escalate
    assert any("confidence" in reason for reason in r.escalation_reasons)


def test_unknown_skips_drafting():
    llm = FakeLLM(c(Category.unknown))
    r = triage("asdf", llm)
    assert llm.draft_calls == 0
    assert r.action == Action.escalate


def test_uncited_reply_is_treated_as_possible_hallucination():
    draft = Draft(answerable=True, reply="Fees are £1.", cited_article_ids=[])
    r = triage("fees?", FakeLLM(c(Category.fees_limits), draft))
    assert r.action == Action.escalate


def test_decision_trace_records_every_step():
    r = triage("charged twice", FakeLLM(c(Category.card_payment), GOOD_DRAFT))
    steps = [line.split(":")[0] for line in r.decision_trace]
    assert steps == ["classify", "retrieve", "draft", "policy"]


def test_retrieval_finds_the_double_charge_article():
    ids = [a.id for a in search("I was charged twice for the same purchase", "card_payment")]
    assert ids[0] == "kb-002"
