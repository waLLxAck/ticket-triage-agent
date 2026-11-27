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

import time
import uuid
from typing import TypedDict

from langchain_core.callbacks import get_usage_metadata_callback
from langgraph.graph import END, START, StateGraph

from triage import guardrails, knowledge, policy
from triage.llm import TriageLLM, cost_usd
from triage.prompts import PROMPT_VERSION
from triage.schemas import Category, Classification, Draft, TriageResult, Urgency


class State(TypedDict, total=False):
    ticket_id: str
    text: str  # raw on input; replaced by the redacted version in the first node
    pii_found: list[str]
    injection_reasons: list[str]
    classification: Classification
    articles: list[knowledge.Article]
    draft: Draft
    result: TriageResult
    trace: list[str]


NOT_CLASSIFIED = Classification(
    category=Category.unknown,
    urgency=Urgency.high,
    confidence=0.0,
    reasoning="not classified: blocked by the injection guard",
)


def build_graph(llm: TriageLLM):
    def redact(state: State) -> State:
        clean, found = guardrails.redact(state["text"])
        note = f"redacted {', '.join(found)}" if found else "no personal data found"
        return {"text": clean, "pii_found": found, "trace": state["trace"] + [f"redact: {note}"]}

    def guard(state: State) -> State:
        # Layer 1: free regex heuristics. Layer 2: an LLM judge for subtler attempts.
        reasons = [
            f"suspicious phrase '{p}'" for p in guardrails.injection_heuristics(state["text"])
        ]
        check = llm.check_injection(state["text"])
        if check.is_injection:
            reasons.append(f"LLM guard: {check.reason}")
        verdict = "BLOCKED: " + "; ".join(reasons) if reasons else "clean"
        return {"injection_reasons": reasons, "trace": state["trace"] + [f"guard: {verdict}"]}

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
        c, d = state.get("classification", NOT_CLASSIFIED), state.get("draft")
        action, reasons = policy.decide(
            c,
            d,
            injection_reasons=state.get("injection_reasons", []),
            output_problems=guardrails.output_violations(d.reply) if d else [],
            scam_signals=guardrails.scam_signals(state["text"]),
        )
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
            drafted=d is not None,
            cited_article_ids=d.cited_article_ids if d else [],
            retrieved_article_ids=[a.id for a in state.get("articles", [])],
            missing_info=d.missing_info if d else [],
            pii_redacted=state.get("pii_found", []),
            injection_flagged=bool(state.get("injection_reasons")),
            decision_trace=trace,
        )
        return {"result": result, "trace": trace}

    def after_guard(state: State) -> str:
        return "policy" if state["injection_reasons"] else "classify"

    def after_classify(state: State) -> str:
        return "policy" if state["classification"].category == Category.unknown else "retrieve"

    g = StateGraph(State)
    g.add_node("redact", redact)
    g.add_node("guard", guard)
    g.add_node("classify", classify)
    g.add_node("retrieve", retrieve)
    g.add_node("draft", draft)
    g.add_node("policy", decide)
    g.add_edge(START, "redact")
    g.add_edge("redact", "guard")
    g.add_conditional_edges("guard", after_guard, ["classify", "policy"])
    g.add_conditional_edges("classify", after_classify, ["retrieve", "policy"])
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "policy")
    g.add_edge("policy", END)
    return g.compile()


def triage(text: str, llm: TriageLLM, ticket_id: str | None = None, config=None) -> TriageResult:
    """Run one ticket through the graph and attach cost, latency and version metadata.

    `config` is LangChain's run config. Pass `{"callbacks": [langfuse_handler]}` to trace the
    run; every LLM call made inside the graph inherits it automatically.
    """
    graph = build_graph(llm)
    limiter = getattr(llm, "limiter", None)
    waited_before = limiter.waited() if limiter else 0.0
    started = time.perf_counter()
    with get_usage_metadata_callback() as usage:  # adds up tokens across every LLM call
        state = graph.invoke(
            {"ticket_id": ticket_id or uuid.uuid4().hex[:8], "text": text, "trace": []},
            config=config,
        )
    queued = (limiter.waited() if limiter else 0.0) - waited_before
    tokens_in = sum(u["input_tokens"] for u in usage.usage_metadata.values())
    tokens_out = sum(u["output_tokens"] for u in usage.usage_metadata.values())
    return state["result"].model_copy(
        update={
            "model_id": llm.model_id,
            "prompt_version": PROMPT_VERSION,
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "cost_usd": cost_usd(llm.model_id, tokens_in, tokens_out),
            "latency_ms": int((time.perf_counter() - started - queued) * 1000),
            "queued_ms": int(queued * 1000),
        }
    )
