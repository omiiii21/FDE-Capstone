"""The unattended run and the report it produces.

A9 (the full set processed in one unattended run) and A10 (that run produces a
metrics report with no further work) are the criteria. The most common way to
fail A9 is not a crash - it is a harness that only works against the paths it
was developed with, so the last test here points it at a file it has never seen,
in a directory that did not exist a second ago.

Everything writes into tmp_path. A test run must not leave anything in
evaluation/results/.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation import harness

SLICE = 20


@pytest.fixture(scope="module")
def completed_run(repo_root, tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("harness") / "results"
    report, exit_code = harness.run(
        repo_root / "data" / "validation_tickets.json",
        output_dir,
        limit=SLICE,
        run_id="test-harness-run",
        progress_every=0,
    )
    return report, exit_code, output_dir


# A9: the run completes on its own and says so in its exit code. 2 would mean it
# finished but the decision log did not reconcile, which CI treats as a failure.
def test_the_run_exits_zero(completed_run):
    _, exit_code, _ = completed_run

    assert exit_code == 0


# A10: the report is written by the run, not assembled afterwards by hand.
@pytest.mark.parametrize("filename", ["metrics.json", "summary.md", "responses.jsonl", "decision_log.jsonl"])
def test_the_run_writes_the_artefacts_the_report_quotes(completed_run, filename):
    _, _, output_dir = completed_run
    path = output_dir / filename

    assert path.exists()
    assert path.stat().st_size > 0


# A9: one row per ticket, with no ticket missing. A silently dropped ticket is
# worse than a failed one, so the count is asserted rather than eyeballed.
def test_every_ticket_appears_exactly_once_in_the_responses_file(completed_run, sample_tickets):
    _, _, output_dir = completed_run
    lines = (output_dir / "responses.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]

    assert len(rows) == SLICE
    assert [row["ticket_id"] for row in rows] == [t.ticket_id for t in sample_tickets[:SLICE]]
    assert all(row["action"] for row in rows)


def test_each_response_row_carries_the_answer_or_the_handover_note(completed_run):
    _, _, output_dir = completed_run
    rows = [json.loads(line) for line in (output_dir / "responses.jsonl").read_text().splitlines()]

    for row in rows:
        if row["action"] == "auto_respond":
            assert row["response_body"].strip()
            assert row["cited_doc_ids"]
            assert row["citations_resolve"] is True
        else:
            assert row["escalation_note"].strip()


# A10: the blocks the Build Specification asks the report to carry.
@pytest.mark.parametrize("block", ["run", "volume", "business", "technical", "governance", "fairness"])
def test_metrics_json_carries_the_required_blocks(completed_run, block):
    _, _, output_dir = completed_run
    metrics = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))

    assert block in metrics
    assert metrics[block]


def test_the_metrics_file_matches_the_report_the_run_returned(completed_run):
    report, _, output_dir = completed_run
    on_disk = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))

    assert on_disk == json.loads(json.dumps(report))


def test_the_measures_the_business_case_is_argued_on_are_present(completed_run):
    report, _, _ = completed_run

    for key in (
        "first_contact_resolution",
        "first_contact_resolution_verified",
        "escalation_rate",
        "baseline_first_contact_resolution",
        "blended_reply_assumption",
    ):
        assert key in report["business"]
    for key in ("classification", "retrieval", "latency", "calibration"):
        assert key in report["technical"]
    assert report["volume"]["tickets_processed"] == SLICE


# A8 read back through A10: the report shows the log reconciling, so a
# reconciliation gap is visible in the deliverable rather than only in sqlite.
def test_the_governance_block_shows_the_log_reconciling(completed_run):
    report, _, _ = completed_run
    governance = report["governance"]

    assert governance["decision_log_reconciles"] is True
    assert governance["tickets_with_decisions"] == SLICE
    assert governance["decisions_logged"] == SLICE * 5
    assert governance["must_not_auto_respond_breaches"] == 0


def test_the_run_block_records_what_it_ran_with(completed_run):
    report, _, _ = completed_run
    run = report["run"]

    assert run["run_id"] == "test-harness-run"
    assert run["unattended"] is True
    assert run["model_provider"] == "offline"
    assert run["provider_available"] is False
    assert run["harness_level_failures"] == 0


def test_the_summary_is_readable_without_opening_the_json(completed_run):
    # A support manager is not going to open metrics.json, and the figures in
    # the report have to come from the same place the system wrote them.
    _, _, output_dir = completed_run
    summary = (output_dir / "summary.md").read_text(encoding="utf-8")

    assert summary.startswith("# Evaluation run test-harness-run")
    for heading in ("## Volume", "## Business outcomes", "## Technical", "## Governance", "## Fairness"):
        assert heading in summary


# A9. The failure mode the brief calls the most common avoidable one: a harness
# that works against the paths it was developed with and nothing else.
def test_the_harness_accepts_an_input_file_and_output_directory_it_has_never_seen(tmp_path):
    tickets = [
        {
            "ticket_id": "NEW-1",
            "channel": "Live-Chat",
            "subject": "",
            "body": "my deployment keeps dying at the health check",
        },
        {
            "ticket_id": "NEW-2",
            "channel": "email",
            "subject": "Rate limited",
            "body": "we started getting 429 responses on every call this morning",
        },
        {
            "ticket_id": "NEW-3",
            "channel": "forum",
            "subject": "Re: cursor expiry",
            "body": "how long does a pagination cursor stay valid?",
        },
    ]
    input_path = tmp_path / "inbox" / "unseen_tickets.json"
    input_path.parent.mkdir(parents=True)
    input_path.write_text(json.dumps(tickets), encoding="utf-8")
    output_dir = tmp_path / "somewhere" / "else" / "results"

    report, exit_code = harness.run(input_path, output_dir, progress_every=0)

    assert exit_code == 0
    assert report["volume"]["tickets_processed"] == 3
    assert report["governance"]["decision_log_reconciles"] is True
    rows = [json.loads(line) for line in (output_dir / "responses.jsonl").read_text().splitlines()]
    assert [row["ticket_id"] for row in rows] == ["NEW-1", "NEW-2", "NEW-3"]


def test_an_input_file_with_no_labels_still_produces_a_report(tmp_path):
    # The file the harness is pointed at in a real handover will not carry the
    # labels the development set does. The scored sections have to say so rather
    # than reporting a score of zero.
    input_path = tmp_path / "unlabelled.json"
    input_path.write_text(
        json.dumps([{"ticket_id": "U-1", "channel": "email", "body": "how do I rotate an api key?"}]),
        encoding="utf-8",
    )

    report, exit_code = harness.run(input_path, tmp_path / "out", progress_every=0)

    assert exit_code == 0
    assert "note" in report["technical"]["classification"]


def test_a_missing_input_file_is_reported_rather_than_traced_back(tmp_path):
    exit_code = harness.main(
        ["--input", str(tmp_path / "does_not_exist.json"), "--output", str(tmp_path / "out"), "--quiet"]
    )

    assert exit_code == 1
    assert not (tmp_path / "out").exists()


def test_the_command_line_entry_point_runs_the_whole_slice(repo_root, tmp_path):
    # A12: the documented command is what is tested, not a private helper.
    output_dir = tmp_path / "cli"
    exit_code = harness.main(
        [
            "--input",
            str(repo_root / "data" / "validation_tickets.json"),
            "--output",
            str(output_dir),
            "--limit",
            "5",
            "--quiet",
        ]
    )

    assert exit_code == 0
    assert Path(output_dir / "metrics.json").exists()
    assert len((output_dir / "responses.jsonl").read_text().splitlines()) == 5
