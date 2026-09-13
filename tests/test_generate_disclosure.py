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

from src.generate import DISCLOSURE, ExtractiveGenerator, ModelGenerator, finalise
from src.guardrails import check_grounding
from src.ingest import normalise_ticket
from src.provider import OfflineProvider


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
