"""The only module that knows which LLM vendor we use.

Everything else talks to the `TriageLLM` protocol. That seam is what lets us:
- run unit tests with a fake LLM (free, instant, deterministic), and
- swap Bedrock for another provider by writing one new class.
"""

import os
from typing import Protocol

from langchain_aws import ChatBedrockConverse

from triage.knowledge import Article
from triage.prompts import CLASSIFY_SYSTEM, DRAFT_SYSTEM, format_articles
from triage.schemas import Classification, Draft


class TriageLLM(Protocol):
    model_id: str

    def classify(self, ticket_text: str, config=None) -> Classification: ...

    def draft(
        self,
        ticket_text: str,
        classification: Classification,
        articles: list[Article],
        config=None,
    ) -> Draft: ...


class BedrockLLM:
    """Calls Amazon Bedrock through its Converse API.

    `with_structured_output(Model)` turns the Pydantic model into a tool definition and forces the
    model to "call" that tool, so we get back a validated object instead of free text to parse.
    """

    def __init__(self, model_id: str | None = None, region: str | None = None):
        self.model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
        chat = ChatBedrockConverse(
            model=self.model_id,
            region_name=region or os.environ.get("AWS_REGION", "us-east-1"),
            temperature=0,  # triage should be repeatable, not creative
            max_tokens=800,
        )
        self._classifier = chat.with_structured_output(Classification)
        self._drafter = chat.with_structured_output(Draft)

    def classify(self, ticket_text: str, config=None) -> Classification:
        return self._classifier.invoke(
            [("system", CLASSIFY_SYSTEM), ("human", f"<ticket>\n{ticket_text}\n</ticket>")],
            config=config,
        )

    def draft(self, ticket_text, classification, articles, config=None) -> Draft:
        human = (
            f"<articles>\n{format_articles(articles)}\n</articles>\n\n"
            f"<ticket category='{classification.category}'>\n{ticket_text}\n</ticket>"
        )
        return self._drafter.invoke([("system", DRAFT_SYSTEM), ("human", human)], config=config)
