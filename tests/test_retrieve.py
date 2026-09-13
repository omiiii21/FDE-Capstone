"""Retrieval against the real corpus.

A4 asks for identifiable source passages from the real documentation, so every
test here runs against data/documentation.json rather than a fixture corpus. A
retriever that only works on a corpus written to suit it is not evidence of
anything.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

QUERIES = [
    "my deployment keeps dying",
    "we are getting 429 responses from the api",
    "how long is a pagination cursor valid",
    "sso login redirects back to the sign in page",
    "the export job has been pending for hours",
    "invoice is higher than last month",
]

_NUMBERED_STEP = re.compile(r"^\s*(\d+)\.\s")


# A4: every passage names a document that exists, so a citation built from it
# can be followed back to the article.
@pytest.mark.parametrize("query", QUERIES)
def test_every_returned_passage_resolves_to_a_real_document(retriever, query):
    passages = retriever.search(query)
    assert passages, f"expected at least one passage for {query!r}"

    for passage in passages:
        document = retriever.resolve(passage.doc_id)
        assert document is not None
        assert document["title"] == passage.title
        assert retriever.passage_for(passage.chunk_id) is not None


# A4. Returning nothing is a valid answer. A retriever that always returns its
# top five hides its own failures behind a plausible-looking citation, and the
# routing rule for "no grounding" then never fires.
def test_an_off_topic_query_returns_nothing(retriever):
    assert retriever.search("what is the weather in paris") == []


# The vocabulary bridge, which is the whole reason the existing internal search
# went unused: the customer says "dying", the article says "health check".
def test_customer_words_reach_an_article_titled_in_documentation_words(retriever):
    passages = retriever.search("my deployment keeps dying")

    assert "DOC-DEPLOY-001" in {p.doc_id for p in passages}
    title = retriever.resolve("DOC-DEPLOY-001")["title"]
    assert "dying" not in title.lower()
    assert "health check" in title.lower()


# Chunking follows the article headings rather than a character count, so that a
# numbered resolution sequence is never cut in half. A passage starting at step
# four reads as an answer and is not one.
def test_no_chunk_starts_part_way_through_a_numbered_sequence(retriever):
    with_steps = 0
    for chunk in retriever.chunks:
        steps = [int(m.group(1)) for line in chunk.text.splitlines() if (m := _NUMBERED_STEP.match(line))]
        if not steps:
            continue
        with_steps += 1
        assert steps == list(range(1, len(steps) + 1)), f"{chunk.chunk_id} is missing steps: {steps}"

    assert with_steps, "the corpus should contain numbered resolution sequences"


# Three chunks of one article cited together look like three sources agreeing.
def test_at_most_two_passages_come_from_any_one_document(retriever, sample_tickets):
    for ticket in sample_tickets:
        counts = Counter(p.doc_id for p in retriever.search(ticket.text))
        assert not counts or max(counts.values()) <= 2


# A5 depends on this: routing cannot be deterministic if what it routes on moves.
@pytest.mark.parametrize("query", QUERIES)
def test_repeated_searches_return_the_same_passages_in_the_same_order(retriever, query):
    first = retriever.search(query)
    second = retriever.search(query)

    assert [(p.chunk_id, p.score) for p in first] == [(p.chunk_id, p.score) for p in second]


def test_scores_are_ranked_and_clear_the_relevance_floor(retriever):
    passages = retriever.search("we are getting 429 responses from the api")

    scores = [p.score for p in passages]
    assert scores == sorted(scores, reverse=True)
    assert all(score >= retriever.floor for score in scores)
    assert len(passages) <= retriever.top_k


def test_a_query_of_nothing_but_stopwords_retrieves_nothing(retriever):
    # tokenise() drops these entirely, and an empty query must not be treated as
    # a match-everything query.
    assert retriever.search("of the and to it") == []


# --- the length normalisation guard ------------------------------------
# `self._len[i] / self._avg_len or 1.0` binds as `(x / avg) or 1.0`, so the
# fallback fired when the division came out at zero and never when the average
# length did, which is the case it was written for. Through the public API that
# combination cannot arise - an index with an average length of zero holds no
# terms, so the division is never reached - but the guard was still testing the
# wrong thing, and an empty corpus is the shape of input it was meant to survive.
def test_an_empty_index_scores_nothing_rather_than_dividing_by_zero():
    from src.retrieve import LexicalIndex

    index = LexicalIndex([])

    assert index._avg_len == 0.0
    assert index.score(["deployment", "failure"]) == []


def test_an_index_of_empty_chunks_scores_zero_rather_than_dividing_by_zero():
    from src.retrieve import Chunk, LexicalIndex

    index = LexicalIndex([Chunk("DOC-X", "DOC-X#0", "", "", "overview", "", [], [], [])])

    assert index._avg_len == 0.0
    assert index.score(["deployment"]) == [0.0]


def test_a_zero_average_length_falls_back_to_one_instead_of_raising():
    # The state the guard exists for, reached directly because the constructor
    # cannot produce it. Before the fix this raised ZeroDivisionError.
    from src.retrieve import Chunk, LexicalIndex

    index = LexicalIndex([Chunk("DOC-X", "DOC-X#0", "t", "c", "overview", "body", ["timeout"], [], [])])
    index._avg_len = 0.0

    scores = index.score(["timeout"])

    assert len(scores) == 1
    assert scores[0] > 0.0


def test_a_retriever_over_an_empty_corpus_returns_nothing(tmp_path):
    import json

    from src.retrieve import Retriever

    corpus = tmp_path / "empty.json"
    corpus.write_text(json.dumps([]), encoding="utf-8")

    empty = Retriever(corpus)

    assert empty.stats["documents"] == 0
    assert empty.search("my deployment keeps dying") == []
