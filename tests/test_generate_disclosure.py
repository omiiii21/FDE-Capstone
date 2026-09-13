"""FR-15: the customer is told a machine drafted the reply.

Ravi Menon asked for this in the customer interview and gave the reason, which
is the part that matters: "If I know a person wrote it I will act without
checking. If I know a machine drafted it I will verify first. Hiding that would
be the thing that annoys me."

It went into version two of the requirements and it had no test until the
traceability matrix pointed that out, which is a fair thing for a traceability
matrix to be for.
"""

from __future__ import annotations

import pytest

from src.generate import (
    DISCLOSURE,
    DISCLOSURE_WITHOUT_SOURCES,
    ExtractiveGenerator,
    ModelGenerator,
    finalise,
)
from src.guardrails import check_grounding
from src.ingest import normalise_ticket
from src.provider import OfflineProvider
from src.schema import Passage


@pytest.fixture()
def ticket():
    return normalise_ticket(
        {
            "ticket_id": "FR15-1",
            "channel": "email",
            "subject": "Rolling back a release",
            "body": "We shipped this morning and need to get back to the previous revision.",
        }
    )


def test_a_drafted_reply_says_a_machine_drafted_it(retriever, ticket):
    passages = retriever.search(ticket.text)
    draft = ExtractiveGenerator(retriever).draft(ticket, passages)

    assert draft.answered
    assert "drafted automatically" in draft.body
    assert draft.body.endswith(DISCLOSURE)


def test_the_model_path_carries_the_same_disclosure(retriever, ticket):
    # Both generators go through finalise() precisely so this cannot be attached
    # to one path and forgotten on the other.
    passages = retriever.search(ticket.text)
    draft = ModelGenerator(OfflineProvider(), retriever=retriever).draft(ticket, passages)

    assert DISCLOSURE in draft.body


def test_the_disclosure_does_not_trip_the_grounding_check(retriever, ticket):
    # It is delivery-layer text, not a claim about the product, so it carries no
    # citation and must not be read as an uncited factual sentence. Getting this
    # wrong blocked every reply the first time.
    passages = retriever.search(ticket.text)
    draft = ExtractiveGenerator(retriever).draft(ticket, passages)

    result = check_grounding(draft.body, passages, draft.citations)

    assert result.passed, result.detail


def test_an_empty_draft_is_left_alone(retriever):
    # Nothing to disclose, and a greeting on its own is not a reply.
    assert finalise("") == ""
    assert finalise("   ") == ""


def test_the_declining_reply_is_not_dressed_up_as_an_answer(retriever):
    # When there is nothing to say, the system says so and does not append a
    # disclosure implying it drafted something from documentation.
    empty = normalise_ticket(
        {"ticket_id": "FR15-2", "channel": "chat", "subject": "", "body": "Any update on this?"}
    )
    draft = ExtractiveGenerator(retriever).draft(empty, [])

    assert not draft.answered
    assert DISCLOSURE not in draft.body


# --- the disclosure has to be true where it is read ---------------------
# It said the source articles were "listed above". In the console they are,
# beside the response. In the body the customer receives there was nothing but
# bare [1] markers and no key to them, so the sentence was false in the only
# place it gets read. The list is now in the body, above the disclosure.
def test_the_source_articles_really_are_listed_above_the_disclosure(retriever, ticket):
    passages = retriever.search(ticket.text)
    draft = ExtractiveGenerator(retriever).draft(ticket, passages)

    assert "listed above" in draft.body
    body_before_disclosure = draft.body[: draft.body.index(DISCLOSURE)]
    assert "Sources:" in body_before_disclosure
    for citation in draft.citations:
        assert f"[{citation['marker']}] " in body_before_disclosure
        assert citation["title"] in body_before_disclosure
        assert citation["doc_id"] in body_before_disclosure


def test_every_marker_in_the_answer_has_a_line_in_the_source_list(retriever, ticket):
    import re

    passages = retriever.search(ticket.text)
    draft = ExtractiveGenerator(retriever).draft(ticket, passages)
    answer = draft.body[: draft.body.index("Sources:")]

    for marker in {int(m) for m in re.findall(r"\[(\d+)\]", answer)}:
        assert marker in {c["marker"] for c in draft.citations}


def test_a_draft_with_no_citations_does_not_claim_a_list(retriever):
    # "Listed above" with nothing above it is the defect, so the claim is
    # dropped rather than made. A reply in this state does not pass grounding
    # anyway; the wording still has to be true on the way there.
    body = finalise("The retention period is ninety days on every plan. [1]", [])

    assert "listed above" not in body
    assert "Sources:" not in body
    assert DISCLOSURE_WITHOUT_SOURCES in body
    assert "drafted automatically" in body


# --- a procedure is quoted whole ----------------------------------------
# Every article in the corpus has four numbered resolution steps and
# twenty-seven of twenty-nine write them as more than four sentences, so a cap
# counted in sentences cut the procedure in half. DEV-0025 was getting steps one
# and two of DOC-DEPLOY-001 with nothing to say the rest existed.
def test_a_numbered_procedure_is_not_cut_in_half(retriever):
    import json

    from src.config import REPO_ROOT

    ticket = normalise_ticket(
        {
            "ticket_id": "PROC-1",
            "channel": "email",
            "subject": "Deployment rolls back at the health check",
            "body": "Our container deployment reaches running and then rolls back every time.",
        }
    )
    passages = retriever.search(ticket.text)
    draft = ExtractiveGenerator(retriever).draft(ticket, passages)

    assert "DOC-DEPLOY-001" in {c["doc_id"] for c in draft.citations}
    article = [
        d
        for d in json.loads((REPO_ROOT / "data" / "documentation.json").read_text(encoding="utf-8"))
        if d["doc_id"] == "DOC-DEPLOY-001"
    ][0]
    steps = [
        line.strip()
        for line in article["content"].splitlines()
        if line.strip()[:2] in {"1.", "2.", "3.", "4."}
    ]
    assert len(steps) == 4
    for step in steps:
        # The reply reflows the article's line breaks, so compare on the first
        # clause of each step rather than the whole line.
        opening = step.split(".", 1)[1].strip().split(",")[0].split(".")[0]
        assert opening in draft.body, f"step missing from the reply: {opening}"


def test_every_article_with_a_numbered_procedure_is_quoted_whole(retriever):
    # The property, over the whole corpus rather than one article: if a chunk
    # has numbered steps, none of them are dropped.
    from src.generate import _STEP, _useful_sentences

    checked = 0
    for chunk in retriever.chunks:
        step_lines = [line.strip() for line in chunk.text.splitlines() if _STEP.match(line)]
        if len(step_lines) < 2:
            continue
        checked += 1
        passage = Passage(
            doc_id=chunk.doc_id,
            title=chunk.title,
            category=chunk.category,
            chunk_id=chunk.chunk_id,
            text=chunk.text,
            score=10.0,
        )
        quoted = " ".join(_STEP.sub("", s) for s in _useful_sentences(passage, set()))
        for line in step_lines:
            opening = _STEP.sub("", line).split(",")[0].split(".")[0]
            assert opening in quoted, f"{chunk.chunk_id} dropped: {opening}"
    assert checked >= 25


def test_prose_without_numbered_steps_is_still_capped(retriever):
    # The cap is not removed, only stopped from cutting a procedure. A section
    # of loose prose still gets a few sentences rather than the whole article.
    from src.generate import MAX_SENTENCES, _useful_sentences

    passage = Passage(
        doc_id="DOC-X",
        title="A long overview",
        category="deployment",
        chunk_id="DOC-X#0",
        text="\n".join(f"This is sentence number {n} and it is long enough to count." for n in range(12)),
        score=10.0,
    )

    assert len(_useful_sentences(passage, set())) <= MAX_SENTENCES
