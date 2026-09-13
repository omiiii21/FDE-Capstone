"""Input shapes the harness might be handed, and was not surviving.

The evaluation harness is pointed at a file nobody here has seen, produced by
somebody else's export, with nobody watching when it runs. An adversarial review
of this repository fed it a handful of plausible shapes and four of them either
threw away every ticket in the file or silently collapsed it to one. Each case
below is one of those, kept so it cannot come back.

A9: every ticket produces a row and none are silently dropped.
"""

from __future__ import annotations

import json

import pytest

from src.ingest import IngestError, load_tickets


def write(tmp_path, text, name="tickets.json"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def ticket(tid, body="my deployment keeps failing"):
    return {"ticket_id": tid, "channel": "email", "subject": "", "body": body}


def test_a_byte_order_mark_does_not_abort_the_run(tmp_path):
    # A BOM is what you get when the file has been through Windows or Excel. It
    # used to raise before a single ticket was processed, taking the whole run
    # with it and writing no output at all.
    path = write(tmp_path, "﻿" + json.dumps([ticket("B-1"), ticket("B-2")]))

    assert [t.ticket_id for t in load_tickets(path)] == ["B-1", "B-2"]


def test_a_byte_order_mark_on_json_lines_is_also_fine(tmp_path):
    body = "﻿" + "\n".join(json.dumps(ticket(f"J-{i}")) for i in range(3))

    assert len(load_tickets(write(tmp_path, body))) == 3


@pytest.mark.parametrize("key", ["tickets", "data", "items", "records", "results", "rows", "payload"])
def test_tickets_are_found_under_any_reasonable_wrapper_key(tmp_path, key):
    path = write(tmp_path, json.dumps({key: [ticket("W-1"), ticket("W-2")]}))

    assert [t.ticket_id for t in load_tickets(path)] == ["W-1", "W-2"]


def test_an_unfamiliar_wrapper_key_still_works(tmp_path):
    # There is no list of every word an export might use, so the fallback is
    # structural: one list of objects in the file is the list of tickets.
    path = write(tmp_path, json.dumps({"supportTicketBatch": [ticket("U-1"), ticket("U-2")]}))

    assert len(load_tickets(path)) == 2


def test_an_object_keyed_by_ticket_id_is_read_as_tickets(tmp_path):
    # How a database export arrives more often than not. It used to collapse to
    # a single ticket called UNIDENTIFIED-0000 and the run reported one row.
    path = write(tmp_path, json.dumps({"T-1": ticket("T-1"), "T-2": ticket("T-2")}))

    assert sorted(t.ticket_id for t in load_tickets(path)) == ["T-1", "T-2"]


def test_a_single_unwrapped_ticket_is_a_file_of_one(tmp_path):
    assert len(load_tickets(write(tmp_path, json.dumps(ticket("S-1"))))) == 1


def test_one_bad_record_does_not_discard_the_good_ones(tmp_path):
    # This is the one that mattered most. A stray null in the middle of the
    # array used to raise after the loop had already normalised everything,
    # throwing away every valid ticket in the file.
    path = write(tmp_path, json.dumps([ticket("N-1"), None, ticket("N-2"), 42]))

    tickets = load_tickets(path)

    assert [t.ticket_id for t in tickets] == ["N-1", "N-2"]
    assert load_tickets.skipped == 2


def test_a_repeated_identifier_is_disambiguated_rather_than_merged(tmp_path):
    # Two tickets sharing an id make the A8 reconciliation fail and merge two
    # customers into one audit trail. The original is kept on the record.
    path = write(tmp_path, json.dumps([ticket("D-1", "first"), ticket("D-1", "second")]))

    tickets = load_tickets(path)

    assert [t.ticket_id for t in tickets] == ["D-1", "D-1--2"]
    assert tickets[1].raw["original_ticket_id"] == "D-1"
    assert len({t.ticket_id for t in tickets}) == 2


def test_a_file_with_nothing_usable_in_it_says_so(tmp_path):
    with pytest.raises(IngestError):
        load_tickets(write(tmp_path, json.dumps([None, 7, "nope"])))


def test_something_that_is_not_json_at_all_fails_clearly(tmp_path):
    with pytest.raises(IngestError) as exc:
        load_tickets(write(tmp_path, "this is a spreadsheet, not a ticket file"))

    assert "not JSON" in str(exc.value)


def test_the_supplied_files_still_load(repo_root):
    # The repairs above must not have changed the normal path.
    assert len(load_tickets(repo_root / "data" / "validation_tickets.json")) == 80
    assert len(load_tickets(repo_root / "data" / "development_tickets.json")) == 500
