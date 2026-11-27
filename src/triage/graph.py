"""The agent as a LangGraph state machine.

LangGraph models an agent as a graph:
- **State** is a typed dict that every node reads from and writes to.
- **Nodes** are plain functions: take the state, return the keys they changed.
- **Edges** say which node runs next. Conditional edges pick the next node from the state,
  which is how an agent "decides": here, a ticket the classifier can't place skips drafting.

Why a graph instead of one big prompt? Each step is small, testable and traced on its own,
you can add steps (guardrails in Phase 2) without rewriting the others, and the path a ticket
took is visible in the decision trace and in Langfuse.

    START → classify ─┬─(known category)→ retrieve → draft → policy → END
                      └─(unknown)──────────────────────────→ policy → END
"""

import uuid
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from triage import knowledge, policy
from triage.llm import TriageLLM
from triage.schemas import Category, Classification, Draft, TriageResult


class State(TypedDict, total=False):
    ticket_id: str
    text: str
    classification: Classification
    articles: list[knowledge.Article]
    draft: Draft
    result: TriageResult
    trace: list[str]


def build_graph(llm: TriageLLM):
    def classify(state: State) -> State:
        c = llm.classify(state["text"])
        return {
            "classification": c,
            "trace": state["trace"]
            + [f"classify: {c.category} ({c.urgency}, {c.confidence:.2f}) - {c.reasoning}"],
        }

    def retrieve(state: State) -> State:
        arts = knowledge.search(state["text"], state["classification"].category)
        return {
            "articles": arts,
            "trace": state["trace"] + [f"retrieve: {[a.id for a in arts] or 'nothing relevant'}"],
        }

    def draft(state: State) -> State:
        d = llm.draft(state["text"], state["classification"], state["articles"])
        return {
            "draft": d,
            "trace": state["trace"]
            + [f"draft: answerable={d.answerable}, cites {d.cited_article_ids}"],
        }

    def decide(state: State) -> State:
        c, d = state["classification"], state.get("draft")
        action, reasons = policy.decide(c, d)
        trace = state["trace"] + [
            f"policy: {action}" + (f" because {'; '.join(reasons)}" if reasons else "")
        ]
        result = TriageResult(
            ticket_id=state["ticket_id"],
            category=c.category,
            urgency=c.urgency,
            confidence=c.confidence,
            action=action,
            escalation_reasons=reasons,
            reply=d.reply if d else "Thanks for getting in touch. A specialist will reply shortly.",
            cited_article_ids=d.cited_article_ids if d else [],
            missing_info=d.missing_info if d else [],
            decision_trace=trace,
        )
        return {"result": result, "trace": trace}

    def after_classify(state: State) -> str:
        return "policy" if state["classification"].category == Category.unknown else "retrieve"

    g = StateGraph(State)
    g.add_node("classify", classify)
    g.add_node("retrieve", retrieve)
    g.add_node("draft", draft)
    g.add_node("policy", decide)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", after_classify, ["retrieve", "policy"])
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "policy")
    g.add_edge("policy", END)
    return g.compile()


def triage(text: str, llm: TriageLLM, ticket_id: str | None = None, config=None) -> TriageResult:
    graph = build_graph(llm)
    state = graph.invoke(
        {"ticket_id": ticket_id or uuid.uuid4().hex[:8], "text": text, "trace": []},
        config=config,
    )
    return state["result"]
