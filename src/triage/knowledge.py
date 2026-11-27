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

_HERE = Path(__file__).resolve()
# Local checkout: <repo>/src/triage/knowledge.py -> <repo>/data/kb. Lambda zip: /var/task/data/kb.
KB_DIR = next(
    p / "data" / "kb" for p in (_HERE.parents[2], _HERE.parents[1]) if (p / "data").exists()
)
STOPWORDS = frozenset(
    "a an the i my me is are was were be been to of in on for and or but it this that with "
    "you your we our can how what why when where do does did not have has had from at".split()
)


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    categories: tuple[str, ...]
    body: str


def _tokens(text: str) -> set[str]:
    # Fold simple plurals so "payments" matches "payment". Real systems use stemming or embeddings.
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in STOPWORDS}


@cache
def load_articles(kb_dir: Path = KB_DIR) -> tuple[Article, ...]:
    articles = []
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        line = re.search(r"^Categor(?:y|ies):\s*(.+)$", text, re.MULTILINE).group(1)
        categories = tuple(c.strip() for c in line.split(","))
        article_id = "-".join(path.stem.split("-")[:2])  # kb-001-card-declined -> kb-001
        articles.append(Article(article_id, title, categories, text))
    return tuple(articles)


def quote_is_verbatim(quote: str, article_ids: list[str]) -> bool:
    """True if the quote appears, word for word (ignoring case and whitespace), in a cited article.

    This turns "is the reply grounded?" from a judgement call into something code can check.
    It doesn't prove the quote *supports* the reply, but a model that has to produce a real
    quote tends to answer "not answerable" instead of inventing a policy.
    """

    def norm(s: str) -> str:
        return " ".join(re.findall(r"[a-z0-9£%.,'-]+", s.lower())).strip(" .")

    q = norm(quote)
    if len(q) < 12:
        return False  # "you can" is not evidence
    by_id = {a.id: a for a in load_articles()}
    return any(q in norm(by_id[i].body) for i in article_ids if i in by_id)


def search(text: str, category: str | None = None, k: int = 3) -> list[Article]:
    """Return up to k articles, best first. Same-category articles get a boost."""
    query = _tokens(text)
    scored = []
    for article in load_articles():
        score = len(query & _tokens(article.title + " " + article.body))
        if category and category in article.categories:
            score += 3
        if score > 0:
            scored.append((score, article))
    scored.sort(key=lambda pair: -pair[0])
    return [article for _, article in scored[:k]]
