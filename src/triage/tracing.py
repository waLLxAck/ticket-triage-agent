"""Langfuse tracing: what the agent did, step by step, with tokens, latency and cost.

How it plugs in: LangChain and LangGraph emit callback events (a node started, an LLM was
called, an LLM finished, and so on). Langfuse's `CallbackHandler` turns those events into a
**trace**: a tree of observations, one per graph node and one per LLM call (a "generation"),
which is sent to Langfuse Cloud in the background over OpenTelemetry.

Privacy: the raw ticket enters the graph before the `redact` node runs, so the graph's *input*
would contain PII. The `mask` function below runs on everything before it leaves the process,
so Langfuse only ever stores redacted text. That's defence in depth: redaction inside the graph
*and* at the telemetry boundary.
"""

import os
from functools import cache

from triage.guardrails import redact


def _mask(*, data, **_):
    if isinstance(data, str):
        return redact(data)[0]
    if isinstance(data, dict):
        return {k: _mask(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_mask(data=v) for v in data]
    return data


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


@cache
def client():
    from langfuse import Langfuse

    return Langfuse(
        base_url=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        mask=_mask,
        environment=os.environ.get("APP_ENV", "local"),
    )


def run_config(ticket_id: str, tags: list[str] | None = None) -> dict:
    """LangChain run config that sends this run to Langfuse, or {} if it isn't configured."""
    if not enabled():
        return {}
    from langfuse.langchain import CallbackHandler

    client()  # create the masked client first, so the handler uses it
    return {
        "callbacks": [CallbackHandler()],
        "run_name": "triage",
        "metadata": {"ticket_id": ticket_id, "langfuse_tags": tags or []},
    }
