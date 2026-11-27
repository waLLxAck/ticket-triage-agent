"""Retrieval: find the help-centre articles relevant to a ticket.

This is the "R" in RAG (retrieval-augmented generation). The LLM is only allowed to answer from
what we retrieve, which is what keeps it from inventing fees or policies.

Real systems use embeddings and a vector store (pgvector, OpenSearch, Azure AI Search). Here we
use a tiny keyword scorer plus a category filter: it is free, deterministic and easy to read,
and the interface (`search(text, category) -> articles`) is the same either way, so you could
swap in embeddings later without touching the agent.
"""

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

KB_DIR = Path(__file__).resolve().parents[2] / "data" / "kb"
STOPWORDS = frozenset(
    "a an the i my me is are was were be been to of in on for and or but it this that with "
    "you your we our can how what why when where do does did not have has had from at".split()
)


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    category: str
    body: str


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS}


@cache
def load_articles(kb_dir: Path = KB_DIR) -> tuple[Article, ...]:
    articles = []
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        category = re.search(r"^Category:\s*(\S+)", text, re.MULTILINE).group(1)
        article_id = "-".join(path.stem.split("-")[:2])  # kb-001-card-declined -> kb-001
        articles.append(Article(article_id, title, category, text))
    return tuple(articles)


def search(text: str, category: str | None = None, k: int = 3) -> list[Article]:
    """Return up to k articles, best first. Same-category articles get a boost."""
    query = _tokens(text)
    scored = []
    for article in load_articles():
        score = len(query & _tokens(article.title + " " + article.body))
        if category and article.category == category:
            score += 3
        if score > 0:
            scored.append((score, article))
    scored.sort(key=lambda pair: -pair[0])
    return [article for _, article in scored[:k]]
