# Phase 1: The agent, running locally

## The pieces and how they connect

```
ticket text
   │
   ▼
graph.py ── LangGraph state machine. It owns the order of steps and nothing else.
   │  classify ──► llm.py ──► Bedrock (Nova)   returns a Classification (schemas.py)
   │  retrieve ──► knowledge.py ──► data/kb/*.md   returns help articles
   │  draft    ──► llm.py ──► Bedrock (Nova)   returns a Draft, grounded in those articles
   │  policy   ──► policy.py (plain Python rules)   auto_resolve or escalate
   ▼
TriageResult: category, urgency, confidence, action, reasons, reply, citations, decision_trace
```

| File | Concept | Why it matters on a contract |
|---|---|---|
| `schemas.py` | **Structured output**: Pydantic models the LLM must fill in | Downstream code gets typed fields, not prose to parse with regex. A bad answer fails validation loudly. |
| `prompts.py` | Versioned prompts | A prompt change is a code change: reviewed, diffed and evaluated. |
| `llm.py` | **Provider seam** (`TriageLLM` protocol) | Tests use a fake model. Switching vendor means writing one class. |
| `knowledge.py` | **Retrieval (RAG)** | The model answers only from your client's real policies, not from what it remembers. |
| `graph.py` | **Orchestration** (LangGraph) | Small steps, conditional routing, every step traced. |
| `policy.py` | **Deterministic gate** | The model proposes; code decides what reaches a customer. |

## Key ideas

**1. Structured output through tool calling.** `chat.with_structured_output(Classification)`
converts the Pydantic model into a JSON-schema "tool" and tells Nova it must call that tool.
Bedrock's Converse API returns the tool arguments, and LangChain validates them back into a
`Classification`. The `Field(description=...)` text travels inside the schema, so it acts as
part of the prompt.

**2. "Unknown" is a valid answer.** The `Category.unknown` value, the `answerable` flag and the
confidence score give the model a way to say "I can't do this" instead of guessing. A
production agent fails safe. It isn't judged by how often it answers, but by how rarely it is
confidently wrong.

**3. The graph.** A LangGraph node is a function `state -> partial state`. Conditional edges
read the state to choose the next node. Here, an `unknown` ticket jumps straight to the policy
step and skips drafting, which saves an LLM call and removes a chance to hallucinate.

**4. Temperature 0.** Triage should give the same output for the same input, so evals compare
like with like.

**5. Two kinds of test.**
- `tests/` holds **unit tests** with a fake LLM. They're free and instant, and they check our
  logic.
- `evals/` (Phase 3) holds **evals** against the real model. They cost a few pence, and they
  measure quality. You need both.

## Try it

```bash
uv run pytest -q                                    # unit tests, no AWS needed
uv run triage "I was charged twice for a coffee"     # real run, needs AWS (aws login)
```

## First live run (Nova Lite, eu-north-1)

| Ticket | Result | Why |
|---|---|---|
| "My coffee shows up twice" | `card_payment`, 0.90 → **auto_resolve**, cites kb-002 | Confident, answerable, grounded |
| "Three casino payments I never made" | `fraud_report`, high → **escalate** | Fraud always goes to a human, and the help centre doesn't cover it |
| "What's the weather in Leeds?" | `unknown` → **escalate**, drafting skipped | Out of scope, so the conditional edge skipped the draft step |

Each run took about 3 seconds and two LLM calls, at a cost of roughly $0.0002 each.

**A weakness to note:** for the fraud ticket, retrieval returned kb-003, kb-004 and kb-005,
none of which is relevant. The most relevant article is kb-007 ("report fraud"), and it was
missed. Keyword retrieval is brittle. The eval suite in Phase 3 will put a number on this, and
we'll fix it the way you would on a contract: measure, change, and measure again.
