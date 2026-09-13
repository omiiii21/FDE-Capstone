"""Unit tests for the metric calculations.

Everything here runs on small hand-built row lists where the answer can be
worked out on paper, because the point of these tests is to check the
arithmetic rather than the system. A metric that is wrong is worse than a metric
that is missing: it gets quoted in the report and nobody rechecks it.
"""

from __future__ import annotations

import pytest

from evaluation.metrics import (
    business_metrics,
    calibration_table,
    classification_metrics,
    fairness_segments,
    governance_metrics,
    percentile,
    retrieval_metrics,
    volume_metrics,
)


def row(**overrides):
    """One harness row with everything defaulted, so each test sets only the
    fields it is actually asserting on."""
    base = {
        "ticket_id": "T-1",
        "channel": "email",
        "customer_tier": "standard",
        "customer_region": "europe",
        "language_fluency": "fluent",
        "body_length": 200,
        "action": "escalate",
        "predicted_intent": "",
        "confidence": 0.5,
        "predicted_urgency": "medium",
        "routing_rule": "R-04-below-threshold",
        "routing_reason": "not confident enough",
        "retrieved_doc_ids": [],
        "cited_doc_ids": [],
        "citations_resolve": True,
        "guardrails_failed": [],
        "guardrails_blocked": [],
        "latency_seconds": 0.1,
        "generation_method": "extractive",
        "response_chars": 0,
        "error": "",
        "true_intent": None,
        "true_urgency": None,
        "expected_route": None,
        "expected_doc_ids": None,
        "answerable_from_docs": None,
        "must_not_auto_respond": None,
    }
    base.update(overrides)
    return base


def answered(**overrides):
    return row(action="auto_respond", response_chars=400, **overrides)


def test_first_contact_resolution_counts_only_automatic_answers():
    rows = [answered(), answered(), row(), row(action="blocked")]

    result = business_metrics(rows)

    assert result["first_contact_resolution"] == 0.5
    assert result["escalation_rate"] == 0.5


def test_a_blocked_response_is_an_escalation_not_a_resolution():
    # Blocked means a person gets it. Counting it as an automatic answer would
    # flatter the headline figure by exactly the cases the guardrails caught.
    rows = [answered(), row(action="blocked")]

    assert business_metrics(rows)["first_contact_resolution"] == 0.5
    assert volume_metrics(rows)["blocked_by_guardrails"] == 1


def test_verified_resolution_only_counts_answers_citing_an_expected_document():
    # The operational figure counts anything answered without a human. The
    # verified figure is the honest ceiling: a confidently wrong answer closes a
    # ticket and reopens it as a complaint.
    rows = [
        answered(expected_doc_ids=["DOC-A"], cited_doc_ids=["DOC-A"]),
        answered(expected_doc_ids=["DOC-B"], cited_doc_ids=["DOC-Z"]),
        answered(expected_doc_ids=None, cited_doc_ids=["DOC-C"]),
        row(expected_doc_ids=["DOC-D"]),
    ]

    result = business_metrics(rows)

    assert result["first_contact_resolution"] == 0.75
    assert result["verified_fcr_over_answerable_auto_answers_only"] == 0.5
    assert result["verified_fcr_over_answerable_auto_answers_only_basis"] == 2


def test_an_answer_on_a_ticket_no_article_covers_counts_against_verified_resolution():
    # The defect this holds shut: expected_doc_ids == [] means the labels say
    # nothing in the corpus answers this ticket, so an automatic answer there
    # can never cite a document they agree with. Dividing by the answerable ones
    # only dropped those tickets out of the measurement entirely and moved the
    # development figure from 71.3% to 90.3% without anything improving.
    rows = [
        answered(expected_doc_ids=["DOC-A"], cited_doc_ids=["DOC-A"]),
        answered(expected_doc_ids=["DOC-B"], cited_doc_ids=["DOC-Z"]),
        answered(expected_doc_ids=[], cited_doc_ids=["DOC-C"]),
        answered(expected_doc_ids=[], cited_doc_ids=["DOC-D"]),
    ]

    result = business_metrics(rows)

    assert result["verified_fcr_over_all_labelled_auto_answers"] == 0.25
    assert result["verified_fcr_over_all_labelled_auto_answers_basis"] == 4
    assert result["verified_fcr_over_answerable_auto_answers_only"] == 0.5
    assert result["verified_fcr_over_answerable_auto_answers_only_basis"] == 2
    assert result["auto_answers_on_tickets_the_corpus_does_not_cover"] == 2


def test_the_two_verified_denominators_agree_when_every_ticket_is_covered():
    rows = [
        answered(expected_doc_ids=["DOC-A"], cited_doc_ids=["DOC-A"]),
        answered(expected_doc_ids=["DOC-B"], cited_doc_ids=["DOC-Z"]),
    ]

    result = business_metrics(rows)

    assert (
        result["verified_fcr_over_all_labelled_auto_answers"]
        == result["verified_fcr_over_answerable_auto_answers_only"]
        == 0.5
    )
    assert result["auto_answers_on_tickets_the_corpus_does_not_cover"] == 0


def test_routing_agreement_compares_against_the_expected_route():
    rows = [
        answered(expected_doc_ids=["DOC-A"], expected_route="auto_respond"),
        row(expected_doc_ids=["DOC-B"], expected_route="escalate"),
        answered(expected_doc_ids=["DOC-C"], expected_route="escalate"),
        row(expected_doc_ids=["DOC-D"], expected_route="auto_respond"),
    ]

    assert business_metrics(rows)["routing_agreement_with_labels"] == 0.5


CLASSIFICATION_ROWS = [
    row(true_intent="billing_query", predicted_intent="billing_query"),
    row(true_intent="billing_query", predicted_intent="rate_limit"),
    row(true_intent="rate_limit", predicted_intent="rate_limit"),
    row(true_intent="rate_limit", predicted_intent="rate_limit"),
    row(true_intent="data_export", predicted_intent="rate_limit"),
]


# billing_query: 1 true positive, 0 false positives, 1 false negative.
# rate_limit:    2 true positives, 2 false positives, 0 false negatives.
# data_export:   never predicted, so nothing above zero.
@pytest.mark.parametrize(
    ("intent", "precision", "recall", "support"),
    [
        ("billing_query", 1.0, 0.5, 2),
        ("rate_limit", 0.5, 1.0, 2),
        ("data_export", 0.0, 0.0, 1),
    ],
)
def test_per_class_precision_and_recall(intent, precision, recall, support):
    per_class = classification_metrics(CLASSIFICATION_ROWS)["per_class"]

    assert per_class[intent]["precision"] == precision
    assert per_class[intent]["recall"] == recall
    assert per_class[intent]["support"] == support


def test_accuracy_and_the_two_precision_averages_differ_as_expected():
    result = classification_metrics(CLASSIFICATION_ROWS)

    assert result["accuracy"] == 0.6
    # Macro treats the three classes equally: (1.0 + 0.5 + 0.0) / 3.
    assert result["macro_precision"] == 0.5
    # Weighted by support: (1.0 * 2 + 0.5 * 2 + 0.0 * 1) / 5.
    assert result["weighted_precision"] == 0.6


def test_classification_says_so_when_the_file_carried_no_labels():
    result = classification_metrics([row(), row()])

    assert "note" in result
    assert "accuracy" not in result


def test_retrieval_recall_and_citation_accuracy():
    rows = [
        answered(expected_doc_ids=["DOC-A"], retrieved_doc_ids=["DOC-A"], cited_doc_ids=["DOC-A"]),
        answered(expected_doc_ids=["DOC-B"], retrieved_doc_ids=["DOC-Z"], cited_doc_ids=["DOC-Z"]),
        row(expected_doc_ids=["DOC-C"], retrieved_doc_ids=["DOC-C"]),
        row(expected_doc_ids=[], retrieved_doc_ids=[]),
    ]

    result = retrieval_metrics(rows)

    assert result["recall_at_k"] == 0.6667  # two of the three answerable tickets, rounded
    assert result["precision_at_1"] == 0.6667
    assert result["citation_accuracy_over_answerable_replies_only"] == 0.5
    assert result["citation_accuracy_over_answerable_replies_only_basis"] == 2
    # The unanswerable row retrieved nothing, which is the correct answer.
    assert result["returned_nothing_when_unanswerable"] == 1.0


def test_a_citation_on_an_uncovered_ticket_counts_against_citation_accuracy():
    # Same defect as verified resolution, same shape, same fix. A reply that
    # cites an article on a ticket the labels say no article covers has cited
    # the wrong thing, and it used to disappear from the denominator instead.
    rows = [
        answered(expected_doc_ids=["DOC-A"], retrieved_doc_ids=["DOC-A"], cited_doc_ids=["DOC-A"]),
        answered(expected_doc_ids=[], retrieved_doc_ids=["DOC-Z"], cited_doc_ids=["DOC-Z"]),
        answered(expected_doc_ids=[], retrieved_doc_ids=["DOC-Y"], cited_doc_ids=["DOC-Y"]),
    ]

    result = retrieval_metrics(rows)

    assert result["citation_accuracy_over_all_labelled_replies"] == 0.3333
    assert result["citation_accuracy_over_all_labelled_replies_basis"] == 3
    assert result["citation_accuracy_over_answerable_replies_only"] == 1.0
    assert result["citation_accuracy_over_answerable_replies_only_basis"] == 1
    assert result["cited_replies_on_tickets_the_corpus_does_not_cover"] == 2


# percentile() is nearest-rank rather than interpolated, so it always returns a
# value that was actually observed. Worth knowing when reading p95 against the
# three second target: on a small run it is one real ticket's latency, not a
# smoothed estimate.
@pytest.mark.parametrize(
    ("p", "expected"),
    [(0.0, 1), (0.5, 5), (0.9, 9), (0.95, 10), (1.0, 10)],
)
def test_percentile_picks_the_nearest_observed_rank(p, expected):
    assert percentile(list(range(1, 11)), p) == expected


def test_percentile_of_nothing_is_zero():
    assert percentile([], 0.95) == 0.0


def test_percentile_does_not_care_about_input_order():
    assert percentile([9, 1, 5, 3, 7], 0.5) == 5


def test_the_calibration_gap_is_stated_minus_observed_in_points():
    # Four tickets in the 0.8-1.0 band, all claiming 0.9, two of them right:
    # stated 0.9, observed 0.5, so the band is 40 points overconfident.
    rows = [
        row(confidence=0.9, true_intent="a", predicted_intent="a"),
        row(confidence=0.9, true_intent="a", predicted_intent="a"),
        row(confidence=0.9, true_intent="a", predicted_intent="b"),
        row(confidence=0.9, true_intent="a", predicted_intent="b"),
        row(confidence=0.1, true_intent="a", predicted_intent="b"),
    ]

    table = calibration_table(rows)
    bands = {band["band"]: band for band in table["bands"]}

    assert bands["0.8-1.0"]["stated"] == 0.9
    assert bands["0.8-1.0"]["observed"] == 0.5
    assert bands["0.8-1.0"]["gap_points"] == 40.0
    assert bands["0.0-0.2"]["gap_points"] == 10.0
    # Both bands are under the size floor, so the headline gap excludes them and
    # the arithmetic is checked on the all-bands figure instead.
    assert table["worst_band_gap_all_bands"] == 40.0
    assert table["predictions_in_thin_bands"] == 5
    # Expected calibration error is the support-weighted mean gap:
    # (4 * 0.4 + 1 * 0.1) / 5.
    assert table["expected_calibration_error"] == 0.34


def test_an_empty_band_is_left_out_rather_than_reported_as_perfect():
    rows = [row(confidence=0.9, true_intent="a", predicted_intent="a")]

    table = calibration_table(rows)

    assert [band["band"] for band in table["bands"]] == ["0.8-1.0"]
    assert table["worst_band_gap_all_bands"] == 10.0


def test_a_band_holding_one_ticket_does_not_decide_the_headline_gap():
    # This is the case that made the rule necessary. On the 80-ticket validation
    # run a single ticket landed alone in the 0.4-0.6 band, read 100% observed
    # accuracy against 55% stated, and reported a 44.5 point gap against a
    # five point condition - while the band holding the other 76 predictions was
    # out by 0.08 points. One ticket is not evidence of miscalibration.
    rows = [row(confidence=0.55, true_intent="a", predicted_intent="a")]
    rows += [row(confidence=0.99, true_intent="a", predicted_intent="a") for _ in range(30)]

    table = calibration_table(rows)

    assert table["worst_band_gap_all_bands"] > 40.0
    assert table["worst_band_gap_points"] < 5.0
    assert table["within_five_points"] is True
    assert table["predictions_in_thin_bands"] == 1
    assert table["bands_counted"] == 1


def test_fairness_spread_excludes_segments_below_the_ten_ticket_floor():
    # enterprise: 10 verified out of 10. standard: 8 of 10. free: 0 of 3, which
    # is below the floor and must not be compared - one ticket moves a segment
    # of three by 33 points, which is more than the threshold being tested.
    def segment(tier: str, cited: str, count: int):
        return [
            answered(customer_tier=tier, expected_doc_ids=["D"], cited_doc_ids=[cited]) for _ in range(count)
        ]

    rows = segment("enterprise", "D", 10) + segment("standard", "D", 8)
    rows += segment("standard", "X", 2) + segment("free", "X", 3)

    block = fairness_segments(rows)["customer_tier"]

    assert block["segments"]["free"]["verified_resolution_rate"] == 0.0
    assert block["segments_compared"] == ["enterprise", "standard"]
    assert block["verified_resolution_spread_points"] == 20.0
    assert block["within_five_points"] is False


def test_a_dimension_with_only_one_comparable_segment_reports_no_spread():
    rows = [
        answered(customer_tier="enterprise", expected_doc_ids=["D"], cited_doc_ids=["D"]) for _ in range(12)
    ]

    block = fairness_segments(rows)["customer_tier"]

    assert block["verified_resolution_spread_points"] is None
    assert block["within_five_points"] is False


def test_governance_counts_a_policy_breach_however_good_the_answer_was():
    rows = [
        answered(must_not_auto_respond=True, ticket_id="BREACH-1"),
        row(must_not_auto_respond=True),
        row(guardrails_failed=["pii", "injection"]),
    ]

    summary = {"total_decisions": 15, "tickets_with_decisions": 3, "reconciles": True}
    result = governance_metrics(rows, summary)

    assert result["must_not_auto_respond_tickets"] == 2
    assert result["must_not_auto_respond_breaches"] == 1
    assert result["must_not_auto_respond_breach_ids"] == ["BREACH-1"]
    assert result["private_data_detections"] == 1
    assert result["injection_attempts_detected"] == 1
    assert result["decision_log_reconciles"] is True


def test_governance_reports_a_reconciliation_gap_rather_than_hiding_it():
    summary = {"total_decisions": 10, "tickets_with_decisions": 2, "reconciles": False}

    result = governance_metrics([row(), row(), row()], summary)

    assert result["decision_log_reconciles"] is False
    assert result["tickets_with_decisions"] == 2


def test_volume_counts_split_by_channel_and_action():
    rows = [
        answered(channel="chat"),
        row(channel="chat"),
        row(channel="email", error="RuntimeError: something"),
    ]

    result = volume_metrics(rows)

    assert result["tickets_processed"] == 3
    assert result["by_channel"] == {"chat": 2, "email": 1}
    assert result["by_action_and_channel"]["chat/auto_respond"] == 1
    assert result["processing_errors"] == 1
