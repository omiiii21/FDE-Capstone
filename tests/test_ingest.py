"""Ingest: four channels, one representation.

A2 is the criterion these tests are evidence for. The interesting cases are all
at the edges - a chat ticket with no subject, an email that is nothing but a
quoted reply chain, a record whose ticket_id never arrived - because the middle
of the distribution normalises correctly by construction and proves very little.
"""

from __future__ import annotations

import json

import pytest

from src.ingest import (
    IngestError,
    clean_text,
    load_tickets,
    normalise_channel,
    normalise_ticket,
    strip_quoted_reply,
)

# Written as chr() rather than pasted in, so that a reader can see what is being
# tested without trusting their editor to render an invisible character.
ZERO_WIDTH_SPACE = chr(0x200B)
ZERO_WIDTH_NON_JOINER = chr(0x200C)
BYTE_ORDER_MARK = chr(0xFEFF)
LIGATURE_FI = chr(0xFB01)
FULLWIDTH_401 = chr(0xFF14) + chr(0xFF10) + chr(0xFF11)


CHANNEL_RECORDS = {
    "email": {
        "ticket_id": "E-1",
        "channel": "email",
        "subject": "Deployment rolling back",
        "body": "Our container deployment rolls back at the health check every time.",
        "customer_id": "CUST-1001",
    },
    "chat": {
        "ticket_id": "C-1",
        "channel": "chat",
        "subject": "",
        "body": "hi, my api calls started returning 429 about ten minutes ago",
    },
    "docs_comment": {
        "ticket_id": "D-1",
        "channel": "docs_comment",
        "subject": "Step 3 is out of date",
        "body": "The screenshot in step 3 does not match the current console.",
    },
    "forum": {
        "ticket_id": "F-1",
        "channel": "forum",
        "subject": "Re: cursor pagination expiry",
        "body": "How long does a pagination cursor stay valid for?",
    },
}


# A2: all four channels arrive as the same object, with the channel recorded but
# nothing else about the shape depending on it.
@pytest.mark.parametrize("channel", sorted(CHANNEL_RECORDS))
def test_every_channel_normalises_to_the_same_representation(channel):
    ticket = normalise_ticket(CHANNEL_RECORDS[channel])

    assert ticket.channel == channel
    assert ticket.ticket_id
    assert ticket.body.strip()
    assert ticket.text.strip()
    assert ticket.customer_tier  # defaulted rather than left blank
    assert ticket.raw is CHANNEL_RECORDS[channel]


# A2. Chat is the channel that breaks the naive "subject plus body" assumption,
# and it is a quarter of the validation set.
def test_chat_tickets_have_no_subject_and_still_normalise(sample_tickets):
    chat = [t for t in sample_tickets if t.channel == "chat"]
    assert chat, "the validation set should contain chat tickets"

    for ticket in chat:
        assert ticket.subject == ""
        assert ticket.text == ticket.body.strip()
        assert ticket.text
        assert ticket.is_realtime


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Live-Chat", "chat"),
        ("live chat", "chat"),
        ("messenger", "chat"),
        ("E-Mail", "email"),
        ("Documentation", "docs_comment"),
        ("doc_comment", "docs_comment"),
        ("community forum", "forum"),
        ("FORUM", "forum"),
        # Anything unrecognised falls back to email, which is the channel with
        # the most conservative handling. Dropping the ticket is not an option.
        ("carrier pigeon", "email"),
        ("", "email"),
        (None, "email"),
    ],
)
def test_channel_aliases_map_onto_the_four_channels(raw, expected):
    assert normalise_channel(raw) == expected


def test_html_entities_are_unescaped():
    assert clean_text("Tom &amp; Jerry sent &lt;none&gt;") == "Tom & Jerry sent <none>"


def test_zero_width_characters_are_removed():
    mangled = "dep" + ZERO_WIDTH_SPACE + "loy" + ZERO_WIDTH_NON_JOINER + "ment" + BYTE_ORDER_MARK
    assert clean_text(mangled) == "deployment"


def test_nfkc_folds_ligatures_and_fullwidth_digits():
    # These arrive from console output pasted out of a terminal. Left alone they
    # tokenise as their own terms and never match the documentation.
    assert clean_text(LIGATURE_FI + "le " + FULLWIDTH_401) == "file 401"


def test_control_characters_and_runs_of_whitespace_are_collapsed():
    assert clean_text("a\r\nb\r\n\r\n\r\n\r\nc   d\te") == "a\nb\n\nc d e"


def test_quoted_reply_chains_are_stripped_from_email_bodies():
    body = (
        "The deployment is still failing.\n\n"
        "On Tue, 3 Mar 2026 at 09:02, CloudServe Support wrote:\n"
        "> Have you checked the health endpoint?\n"
        "> Let us know."
    )
    assert strip_quoted_reply(body) == "The deployment is still failing."


def test_a_body_that_is_only_a_quote_chain_is_kept_rather_than_emptied():
    # The customer replied without adding anything above the quote. A noisy
    # ticket still retrieves something; an empty one is a ticket we have lost.
    body = "On Tue, 3 Mar 2026 at 09:02, CloudServe Support wrote:\n> Have you checked the health endpoint?"
    assert strip_quoted_reply(body) == body


def test_an_empty_body_falls_back_to_the_subject():
    # Docs comments frequently are a single sentence in the subject line. The
    # subject is not copied into the body to achieve this: Ticket.text already
    # joins the two, so copying would double those terms into retrieval and
    # classification.
    ticket = normalise_ticket(
        {"ticket_id": "D-2", "channel": "docs_comment", "subject": "This step is wrong", "body": ""}
    )
    assert ticket.body == ""
    assert ticket.text == "This step is wrong"


def test_forum_reply_prefixes_are_stripped_from_the_subject():
    ticket = normalise_ticket(CHANNEL_RECORDS["forum"])
    assert ticket.subject == "cursor pagination expiry"


# A9: nothing is silently skipped. A record with no identifier is repaired with
# a positional one so that it still appears in the output and can be chased.
def test_a_record_with_no_ticket_id_gets_a_positional_id(tmp_path):
    path = tmp_path / "tickets.json"
    path.write_text(
        json.dumps([{"body": "who am I"}, {"ticket_id": "REAL-1", "body": "identified"}]),
        encoding="utf-8",
    )

    tickets = load_tickets(path)

    assert [t.ticket_id for t in tickets] == ["UNIDENTIFIED-0000", "REAL-1"]


def test_normalise_ticket_alone_still_rejects_a_record_with_no_identifier():
    # The repair is load_tickets' job, deliberately - a caller normalising a
    # single record should hear about a missing id rather than invent one.
    with pytest.raises(IngestError):
        normalise_ticket({"body": "no id"})


def test_load_tickets_accepts_a_json_array(tmp_path):
    path = tmp_path / "array.json"
    path.write_text(json.dumps([{"ticket_id": "A-1", "body": "one"}]), encoding="utf-8")

    assert [t.ticket_id for t in load_tickets(path)] == ["A-1"]


def test_load_tickets_accepts_a_tickets_wrapper(tmp_path):
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({"tickets": [{"ticket_id": "B-1", "body": "two"}]}), encoding="utf-8")

    assert [t.ticket_id for t in load_tickets(path)] == ["B-1"]


# A .jsonl file starts with "{" like any JSON object, so choosing the container
# by first character alone sent it into json.loads() on the whole text and it
# failed on line two. load_tickets now tries the single-document reading and
# falls back to line by line. A handover file in JSON Lines is a plausible input
# for the unattended run, so this is an A9 concern rather than a tidiness one.
def test_load_tickets_accepts_json_lines(tmp_path):
    path = tmp_path / "lines.jsonl"
    path.write_text(
        '{"ticket_id": "C-1", "body": "three"}\n{"ticket_id": "C-2", "body": "four"}\n',
        encoding="utf-8",
    )

    assert [t.ticket_id for t in load_tickets(path)] == ["C-1", "C-2"]


def test_the_validation_set_loads_with_every_ticket_identified(sample_tickets):
    assert len(sample_tickets) == 80
    assert len({t.ticket_id for t in sample_tickets}) == len(sample_tickets)
    assert {t.channel for t in sample_tickets} == {"email", "chat", "docs_comment", "forum"}
