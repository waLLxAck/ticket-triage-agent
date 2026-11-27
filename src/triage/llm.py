"""The only module that knows which LLM vendor we use.

Everything else talks to the `TriageLLM` protocol. That seam is what lets us:
- run unit tests with a fake LLM (free, instant, deterministic), and
- swap Bedrock for another provider by writing one new class.
"""

import os
import threading
import time
from typing import Protocol

from botocore.config import Config
from langchain_aws import ChatBedrockConverse

from triage.knowledge import Article
from triage.prompts import CLASSIFY_SYSTEM, DRAFT_SYSTEM, GUARD_SYSTEM, format_articles
from triage.schemas import Classification, Draft, InjectionCheck


class TriageLLM(Protocol):
    model_id: str

    def check_injection(self, ticket_text: str, config=None) -> InjectionCheck: ...

    def classify(self, ticket_text: str, config=None) -> Classification: ...

    def draft(
        self,
        ticket_text: str,
        classification: Classification,
        articles: list[Article],
        config=None,
    ) -> Draft: ...


class RateLimiter:
    """Token bucket that keeps requests under a requests-per-minute quota, shared across threads.

    Bedrock quotas are per account, per Region, per model. This account gets 20 requests a
    minute for Nova Lite on demand. Going over it returns ThrottlingException. Retries help with
    a short spike, but a steady eval run or a traffic burst needs pacing on the client side.

    The bucket holds up to `burst` tokens and refills at `per_minute`. A single ticket's 3
    calls therefore go out back to back, while a steady stream averages out to the quota. (The
    first version spaced *every* call evenly, so one ticket in Lambda took about 15 s of
    waiting for 2 s of work.) In production, SQS plus a capped worker concurrency keeps the
    aggregate rate within the quota.
    """

    def __init__(self, per_minute: float, burst: int = 3, clock=time.monotonic, sleep=time.sleep):
        if per_minute <= 0 or burst < 1:
            raise ValueError(f"need per_minute > 0 and burst >= 1, got {per_minute}, {burst}")
        self._clock, self._sleep = clock, sleep
        self.rate = per_minute / 60.0  # tokens per second
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._updated = clock()
        self._lock = threading.Lock()
        self._local = threading.local()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            self._tokens -= 1  # take a token now; if that goes negative, wait for the debt
            delay = -self._tokens / self.rate if self._tokens < 0 else 0.0
        if delay > 0:
            self._sleep(delay)
        self._local.waited = self.waited() + delay

    def waited(self) -> float:
        """Seconds this thread has spent waiting for a slot (used to report queueing time)."""
        return getattr(self._local, "waited", 0.0)


LIMITER = RateLimiter(float(os.environ.get("BEDROCK_RPM", "18")))  # 18 of 20: keep headroom


class BedrockLLM:
    """Calls Amazon Bedrock through its Converse API.

    `with_structured_output(Model)` turns the Pydantic model into a tool definition and forces the
    model to "call" that tool, so we get back a validated object instead of free text to parse.
    """

    limiter = LIMITER

    def __init__(self, model_id: str | None = None, region: str | None = None):
        self.model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
        chat = ChatBedrockConverse(
            model=self.model_id,
            region_name=region or os.environ.get("AWS_REGION", "eu-north-1"),
            temperature=0,  # triage should be repeatable, not creative
            max_tokens=800,
            # New accounts have low request quotas: back off and retry instead of failing.
            config=Config(retries={"mode": "adaptive", "max_attempts": 8}),
        )
        self._guard = chat.with_structured_output(InjectionCheck)
        self._classifier = chat.with_structured_output(Classification)
        self._drafter = chat.with_structured_output(Draft)

    def check_injection(self, ticket_text: str, config=None) -> InjectionCheck:
        self.limiter.wait()
        return self._guard.invoke(
            [("system", GUARD_SYSTEM), ("human", f"<message>\n{ticket_text}\n</message>")],
            config=config,
        )

    def classify(self, ticket_text: str, config=None) -> Classification:
        self.limiter.wait()
        return self._classifier.invoke(
            [("system", CLASSIFY_SYSTEM), ("human", f"<ticket>\n{ticket_text}\n</ticket>")],
            config=config,
        )

    def draft(self, ticket_text, classification, articles, config=None) -> Draft:
        human = (
            f"<articles>\n{format_articles(articles)}\n</articles>\n\n"
            f"<ticket category='{classification.category}'>\n{ticket_text}\n</ticket>"
        )
        self.limiter.wait()
        return self._drafter.invoke([("system", DRAFT_SYSTEM), ("human", human)], config=config)


# USD per million tokens (input, output), AWS list prices. Used for cost reporting only.
PRICES_PER_MTOK = {
    "amazon.nova-micro-v1:0": (0.035, 0.14),
    "amazon.nova-lite-v1:0": (0.06, 0.24),
    "amazon.nova-pro-v1:0": (0.80, 3.20),
}


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = PRICES_PER_MTOK.get(model_id, (0.0, 0.0))
    return (input_tokens * inp + output_tokens * out) / 1_000_000
