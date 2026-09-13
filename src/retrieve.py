"""Retrieval over the CloudServe knowledge base.

Two things drive the design here, both from discovery:

1. Ines maintains 29 articles with a regular internal structure (symptoms,
   common causes, numbered resolution steps, notes). Splitting inside a
   numbered resolution sequence produces passages that score well and read as
   nonsense, so chunking follows the headings rather than a character count.

2. The reason nobody uses the existing search is vocabulary, not content. That
   is handled in text.py and applied to the query here.

The default backend is lexical (BM25 with field boosts). A dense backend using
all-MiniLM-L6-v2 sits behind RETRIEVAL_BACKEND=dense. Measured over the same 500
tickets by scripts/compare_retrieval.py, it is 0.3 points worse on recall@5, 1.1
points better on precision@1 and about thirty-six times slower per query, so
neither is clearly better at finding the right article; the 800MB of
dependencies is what settles it for a system that has to install from a clean
checkout. Dense does narrow the non-fluent fairness gap from 5.8 points to 3.9,
which is an argument against this choice rather than for it, and
docs/decisions/ADR-002-retrieval-backend.md does not pretend otherwise.

Serves FR-04, FR-05. Acceptance criterion A4.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .config import settings
from .schema import Passage
from .text import expand_query, tokenise

# BM25 parameters. k1 controls how fast term frequency saturates, b how much
# document length is penalised. These are the standard values; I tried a small
# sweep (scripts/tune_retrieval.py) and the corpus is too uniform in length for
# b to matter much.
K1 = 1.4
B = 0.72

# Matching in the title or the category is worth more than matching in the
# body, because Ines writes titles that name the problem. Values from the sweep.
TITLE_BOOST = 2.6
CATEGORY_BOOST = 1.4

_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    title: str
    category: str
    section: str
    text: str
    tokens: list[str]
    title_tokens: list[str]
    category_tokens: list[str]


def chunk_article(doc: dict[str, Any]) -> list[Chunk]:
    """Split one article on its section headings.

    A section shorter than 200 characters is folded into the following one.
    Standalone "Symptoms" lists retrieve well on the customer's own words and
    then tell the customer nothing they did not already know, which is how you
    get a confident, useless answer.
    """
    content = doc.get("content", "") or ""
    doc_id = doc["doc_id"]
    title = doc.get("title", "")
    category = doc.get("category", "")

    matches = list(_HEADING.finditer(content))
    sections: list[tuple[str, str]] = []
    if not matches:
        sections.append(("", content.strip()))
    else:
        preamble = content[: matches[0].start()].strip()
        if preamble:
            sections.append(("", preamble))
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            sections.append((match.group(1).strip(), content[match.end() : end].strip()))

    merged: list[tuple[str, str]] = []
    for heading, body in sections:
        if not body:
            continue
        if merged and len(body) < 200:
            prev_heading, prev_body = merged[-1]
            joined = f"{prev_body}\n\n## {heading}\n{body}" if heading else f"{prev_body}\n\n{body}"
            merged[-1] = (prev_heading, joined)
        else:
            merged.append((heading, body))

    title_tokens = tokenise(title)
    category_tokens = tokenise(category.replace("_", " "))
    chunks = []
    for index, (heading, body) in enumerate(merged):
        # The title is prepended to every chunk's text so that a passage quoted
        # on its own still says what it is about.
        text = f"{title}\n\n## {heading}\n\n{body}" if heading else f"{title}\n\n{body}"
        chunks.append(
            Chunk(
                doc_id=doc_id,
                chunk_id=f"{doc_id}#{index}",
                title=title,
                category=category,
                section=heading or "overview",
                text=text.strip(),
                tokens=tokenise(f"{heading} {body}"),
                title_tokens=title_tokens,
                category_tokens=category_tokens,
            )
        )
    return chunks


class LexicalIndex:
    """BM25 over the chunked corpus, with boosted title and category fields.

    Built in memory in well under a second for 29 articles. Persisting it would
    be premature; if the corpus grows past a few thousand articles this is the
    component to replace, and nothing outside this class would need to change.
    """

    def __init__(self, chunks: Sequence[Chunk]):
        self.chunks = list(chunks)
        self.n = len(self.chunks)
        self._tf: list[Counter[str]] = []
        self._len: list[float] = []
        df: Counter[str] = Counter()
        for chunk in self.chunks:
            counts: Counter[str] = Counter(chunk.tokens)
            for token in chunk.title_tokens:
                counts[token] += TITLE_BOOST
            for token in chunk.category_tokens:
                counts[token] += CATEGORY_BOOST
            self._tf.append(counts)
            self._len.append(sum(counts.values()))
            df.update(set(counts))
        self._avg_len = (sum(self._len) / self.n) if self.n else 0.0
        # Robertson/Sparck Jones idf with the +1 that keeps it non-negative.
        self._idf = {
            term: math.log(1.0 + (self.n - count + 0.5) / (count + 0.5)) for term, count in df.items()
        }

    def score(self, query_tokens: Sequence[str]) -> list[float]:
        scores = [0.0] * self.n
        query_counts = Counter(query_tokens)
        for term, q_count in query_counts.items():
            idf = self._idf.get(term)
            if idf is None:
                continue
            # A term repeated in the query counts for a little more, but not
            # linearly - customers repeat themselves when frustrated.
            q_weight = 1.0 + 0.3 * math.log(q_count)
            for i, counts in enumerate(self._tf):
                freq = counts.get(term, 0.0)
                if not freq:
                    continue
                # Parenthesised deliberately. Written as `x / avg or 1.0` this
                # binds as `(x / avg) or 1.0`, so the guard fires when the
                # division happens to be zero and not when avg_len is, which is
                # the case it was put there for and the one that divides by it.
                normalised_length = self._len[i] / self._avg_len if self._avg_len else 1.0
                denom = freq + K1 * (1 - B + B * normalised_length)
                scores[i] += q_weight * idf * (freq * (K1 + 1)) / denom
        return scores


class Retriever:
    """The interface the rest of the system uses. Backend-agnostic on purpose."""

    def __init__(
        self,
        corpus_path: str | Path | None = None,
        *,
        backend: str | None = None,
        top_k: int | None = None,
        floor: float | None = None,
    ):
        self.corpus_path = Path(corpus_path or settings.corpus_path)
        self.backend = (backend or settings.retrieval_backend).lower()
        self.top_k = top_k if top_k is not None else settings.retrieval_top_k
        self.floor = floor if floor is not None else settings.retrieval_floor

        docs = json.loads(self.corpus_path.read_text(encoding="utf-8"))
        self.documents: dict[str, dict[str, Any]] = {d["doc_id"]: d for d in docs}
        self.chunks: list[Chunk] = []
        for doc in docs:
            self.chunks.extend(chunk_article(doc))
        self._lexical = LexicalIndex(self.chunks)
        self._dense = None
        if self.backend == "dense":
            self._dense = _load_dense_backend(self.chunks)
            if self._dense is None:
                # Never fail closed on a missing optional dependency. A11.
                self.backend = "lexical"

    # -- public API ------------------------------------------------------

    def search(self, query: str, *, top_k: int | None = None, floor: float | None = None) -> list[Passage]:
        """Ranked passages above the relevance floor, best first.

        Returns an empty list when nothing clears the floor. That is a real
        answer: the Build Specification is explicit that retrieval which always
        returns something hides its own failures.
        """
        k = top_k if top_k is not None else self.top_k
        cut = floor if floor is not None else self.floor

        tokens = expand_query(tokenise(query))
        if not tokens:
            return []

        if self._dense is not None:
            scores = self._dense.score(query)
        else:
            scores = self._lexical.score(tokens)

        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        passages: list[Passage] = []
        seen_docs: set[str] = set()
        for i in ranked:
            if scores[i] < cut:
                break
            chunk = self.chunks[i]
            # At most two passages from any one article. Three chunks of the
            # same document look like corroboration and are not.
            if list(p.doc_id for p in passages).count(chunk.doc_id) >= 2:
                continue
            passages.append(
                Passage(
                    doc_id=chunk.doc_id,
                    title=chunk.title,
                    category=chunk.category,
                    chunk_id=chunk.chunk_id,
                    text=chunk.text,
                    score=round(float(scores[i]), 4),
                )
            )
            seen_docs.add(chunk.doc_id)
            if len(passages) >= k:
                break
        return passages

    def resolve(self, doc_id: str) -> dict[str, Any] | None:
        """A citation is only worth anything if it resolves. Used by the
        grounding guardrail and by the tests behind A6."""
        return self.documents.get(doc_id)

    def chunks_for(self, doc_id: str) -> list[Chunk]:
        """Every chunk of one article, in document order.

        The generator needs this because the chunk that ranks best is often not
        the chunk worth quoting. A customer's words match the symptoms section,
        which describes the problem they already know they have; the resolution
        section is the part that helps them.
        """
        return [c for c in self.chunks if c.doc_id == doc_id]

    def passage_for(self, chunk_id: str) -> Chunk | None:
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk
        return None

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "documents": len(self.documents),
            "chunks": len(self.chunks),
            "top_k": self.top_k,
            "floor": self.floor,
        }


def _load_dense_backend(chunks: Sequence[Chunk]):  # pragma: no cover - optional path
    """Chroma + all-MiniLM-L6-v2, only if the optional extras are installed.

    Kept because the comparison is reported in ADR-002 and somebody will want
    to reproduce it. Returns None rather than raising when the packages are not
    present, so a clean checkout with only requirements.txt still runs.
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        return None

    model = SentenceTransformer("all-MiniLM-L6-v2")
    matrix = model.encode([c.text for c in chunks], normalize_embeddings=True)

    class _Dense:
        def score(self, query: str) -> list[float]:
            vector = model.encode([query], normalize_embeddings=True)[0]
            # Cosine similarity in [-1, 1], rescaled so the configured floor
            # means roughly the same thing across both backends.
            return [float(s) * 12.0 for s in np.dot(matrix, vector)]

    return _Dense()
