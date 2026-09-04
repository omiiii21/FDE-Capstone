"""Metric calculations for the evaluation run.

Everything here is computed from the responses the system produced and the
labels on the input file. Nothing is calculated by hand afterwards, which is
what A10 requires, and it also means the numbers in the report cannot quietly
drift from the numbers the system produced.

Two calculations are worth reading the comments on: first contact resolution,
which has an operational reading and a verified reading that differ by a lot,
and time to first reply, which cannot be measured for escalated tickets and is
reported with the assumption named rather than silently averaged away.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Any, Sequence

# What CloudServe achieve today, from the history block of the development set.
# Used as the comparison in the report rather than the round numbers in the
# brief, because the data disagrees slightly with the brief and the data wins.
BASELINE = {
    "first_contact_resolution": 0.438,
    "escalation_rate": 0.562,
    "csat": 2.97,
    "median_resolution_minutes": 214.0,
    "mean_resolution_minutes": 421.7,
    "repeat_contact_rate": 0.216,
}

# The interval a human ticket waits before a substantive reply, taken from the
# median of history.resolution_time_minutes in the development set. Escalated
# tickets still wait for a person, so any blended response time has to use a
# figure like this and say that it did.
HUMAN_QUEUE_MINUTES = 214.0


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[index]


def volume_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    actions = Counter(r["action"] for r in rows)
    return {
        "tickets_processed": len(rows),
        "answered_automatically": actions.get("auto_respond", 0),
        "escalated": actions.get("escalate", 0),
        "blocked_by_guardrails": actions.get("blocked", 0),
        "processing_errors": sum(1 for r in rows if r.get("error")),
        "by_channel": dict(Counter(r["channel"] for r in rows)),
        "by_action_and_channel": {
            f"{channel}/{action}": count
            for (channel, action), count in Counter((r["channel"], r["action"]) for r in rows).items()
        },
    }


def business_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The measures CloudServe already watch.

    First contact resolution is reported twice on purpose.

    The operational figure counts every ticket the system answered without a
    human. That is the number that changes CloudServe's cost base, and it is
    the one that maps onto the 42% they report today.

    The verified figure counts only the ones where the answer cited a document
    the labels agree should have answered it. It is the honest ceiling: a
    confidently wrong answer closes a ticket operationally and reopens it as a
    complaint. Reporting only the first would be the kind of number the brief
    warns about.
    """
    total = len(rows)
    auto = [r for r in rows if r["action"] == "auto_respond"]
    labelled = [r for r in rows if r.get("expected_doc_ids") is not None]

    verified = [
        r
        for r in auto
        if r.get("expected_doc_ids") and set(r.get("cited_doc_ids") or []) & set(r["expected_doc_ids"])
    ]
    auto_with_labels = [r for r in auto if r.get("expected_doc_ids")]

    # Automated replies go out in the time the pipeline took. Escalated tickets
    # still wait for the human queue, so the blended figure below uses
    # HUMAN_QUEUE_MINUTES for them and the report states that assumption.
    automated_minutes = [r["latency_seconds"] / 60.0 for r in auto]
    blended = automated_minutes + [HUMAN_QUEUE_MINUTES] * (total - len(auto))

    return {
        "first_contact_resolution": round(_safe_div(len(auto), total), 4),
        "first_contact_resolution_verified": round(_safe_div(len(verified), len(auto_with_labels)), 4),
        "first_contact_resolution_verified_basis": len(auto_with_labels),
        "escalation_rate": round(_safe_div(total - len(auto), total), 4),
        "automated_reply_seconds_mean": round(
            statistics.mean([r["latency_seconds"] for r in auto]) if auto else 0.0, 4
        ),
        "automated_reply_seconds_median": round(
            statistics.median([r["latency_seconds"] for r in auto]) if auto else 0.0, 4
        ),
        "blended_reply_minutes_mean": round(statistics.mean(blended) if blended else 0.0, 2),
        "blended_reply_minutes_median": round(statistics.median(blended) if blended else 0.0, 2),
        "blended_reply_assumption": (
            f"escalated tickets are assumed to wait {HUMAN_QUEUE_MINUTES:.0f} minutes, the median "
            "human resolution time in the development set; the system does not control that queue"
        ),
        "baseline_first_contact_resolution": BASELINE["first_contact_resolution"],
        "baseline_escalation_rate": BASELINE["escalation_rate"],
        "baseline_median_reply_minutes": BASELINE["median_resolution_minutes"],
        "routing_agreement_with_labels": round(
            _safe_div(
                sum(
                    1
                    for r in labelled
                    if r.get("expected_route")
                    and ((r["action"] == "auto_respond") == (r["expected_route"] == "auto_respond"))
                ),
                sum(1 for r in labelled if r.get("expected_route")),
            ),
            4,
        ),
    }


def classification_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Precision and recall per class, plus the confusion pairs worth reading.

    Per class rather than overall, because an overall figure hides a system that
    is excellent on the two commonest intents and useless on the rest.
    """
    pairs = [(r["true_intent"], r["predicted_intent"]) for r in rows if r.get("true_intent")]
    if not pairs:
        return {"note": "input file carried no intent labels, so classification was not scored"}

    classes = sorted({t for t, _ in pairs} | {p for _, p in pairs})
    per_class: dict[str, dict[str, float]] = {}
    for c in classes:
        tp = sum(1 for t, p in pairs if t == c and p == c)
        fp = sum(1 for t, p in pairs if t != c and p == c)
        fn = sum(1 for t, p in pairs if t == c and p != c)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        per_class[c] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(_safe_div(2 * precision * recall, precision + recall), 4),
            "support": tp + fn,
        }

    support_total = sum(v["support"] for v in per_class.values())
    present = [c for c in classes if per_class[c]["support"] > 0]
    confusions = Counter((t, p) for t, p in pairs if t != p)

    return {
        "accuracy": round(_safe_div(sum(1 for t, p in pairs if t == p), len(pairs)), 4),
        "macro_precision": round(_safe_div(sum(per_class[c]["precision"] for c in present), len(present)), 4),
        "macro_recall": round(_safe_div(sum(per_class[c]["recall"] for c in present), len(present)), 4),
        "weighted_precision": round(
            _safe_div(
                sum(per_class[c]["precision"] * per_class[c]["support"] for c in classes), support_total
            ),
            4,
        ),
        "per_class": per_class,
        "top_confusions": [
            {"true": t, "predicted": p, "count": n} for (t, p), n in confusions.most_common(10)
        ],
    }


def retrieval_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    answerable = [r for r in rows if r.get("expected_doc_ids")]
    unanswerable = [r for r in rows if r.get("expected_doc_ids") == []]
    hits = [r for r in answerable if set(r["retrieved_doc_ids"]) & set(r["expected_doc_ids"])]
    top1 = [
        r for r in answerable if r["retrieved_doc_ids"] and r["retrieved_doc_ids"][0] in r["expected_doc_ids"]
    ]
    cited = [r for r in rows if r.get("cited_doc_ids")]
    cited_correct = [
        r for r in cited if r.get("expected_doc_ids") and set(r["cited_doc_ids"]) & set(r["expected_doc_ids"])
    ]
    cited_with_labels = [r for r in cited if r.get("expected_doc_ids")]

    return {
        "recall_at_k": round(_safe_div(len(hits), len(answerable)), 4),
        "precision_at_1": round(_safe_div(len(top1), len(answerable)), 4),
        "citation_accuracy": round(_safe_div(len(cited_correct), len(cited_with_labels)), 4),
        "citation_accuracy_basis": len(cited_with_labels),
        "responses_with_citations": len(cited),
        "citations_all_resolve": all(r.get("citations_resolve", True) for r in rows),
        "returned_nothing_when_unanswerable": round(
            _safe_div(sum(1 for r in unanswerable if not r["retrieved_doc_ids"]), len(unanswerable)), 4
        ),
        "mean_passages_returned": round(
            _safe_div(sum(len(r["retrieved_doc_ids"]) for r in rows), len(rows)), 3
        ),
    }


def latency_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    values = [r["latency_seconds"] for r in rows]
    by_channel = defaultdict(list)
    for r in rows:
        by_channel[r["channel"]].append(r["latency_seconds"])
    return {
        "mean_seconds": round(statistics.mean(values) if values else 0.0, 4),
        "median_seconds": round(statistics.median(values) if values else 0.0, 4),
        "p95_seconds": round(percentile(values, 0.95), 4),
        "p99_seconds": round(percentile(values, 0.99), 4),
        "max_seconds": round(max(values) if values else 0.0, 4),
        "p95_by_channel": {k: round(percentile(v, 0.95), 4) for k, v in by_channel.items()},
    }


def governance_metrics(rows: Sequence[dict[str, Any]], log_summary: dict[str, Any]) -> dict[str, Any]:
    activations: Counter[str] = Counter()
    for r in rows:
        for name in r.get("guardrails_failed", []):
            activations[name] += 1

    # must_not_auto_respond is a governance condition, not a score. Any ticket
    # in that set that got an automatic answer is a failure regardless of how
    # good the answer was.
    policy_tickets = [r for r in rows if r.get("must_not_auto_respond")]
    breaches = [r for r in policy_tickets if r["action"] == "auto_respond"]

    return {
        "decisions_logged": log_summary.get("total_decisions", 0),
        "tickets_with_decisions": log_summary.get("tickets_with_decisions", 0),
        "decision_log_reconciles": log_summary.get("reconciles", False),
        "guardrail_activations": dict(activations),
        "private_data_detections": activations.get("pii", 0),
        "responses_released": sum(1 for r in rows if r["action"] == "auto_respond"),
        "must_not_auto_respond_tickets": len(policy_tickets),
        "must_not_auto_respond_breaches": len(breaches),
        "must_not_auto_respond_breach_ids": [r["ticket_id"] for r in breaches][:20],
        "injection_attempts_detected": activations.get("injection", 0),
    }


def calibration_table(rows: Sequence[dict[str, Any]], bands: int = 5) -> dict[str, Any]:
    """Stated confidence against observed accuracy, per band.

    The governance framework asks for stated confidence within five points of
    observed accuracy. This is where that is checked, and the gap column is
    what the report quotes.
    """
    scored = [r for r in rows if r.get("true_intent")]
    table = []
    for i in range(bands):
        low, high = i / bands, (i + 1) / bands
        if i == bands - 1:
            high = 1.01
        group = [r for r in scored if low <= r["confidence"] < high]
        if not group:
            continue
        stated = sum(r["confidence"] for r in group) / len(group)
        observed = sum(1 for r in group if r["predicted_intent"] == r["true_intent"]) / len(group)
        table.append(
            {
                "band": f"{low:.1f}-{min(high, 1.0):.1f}",
                "n": len(group),
                "stated": round(stated, 4),
                "observed": round(observed, 4),
                "gap_points": round(100 * (stated - observed), 2),
            }
        )
    total = sum(row["n"] for row in table)
    ece = _safe_div(sum(row["n"] * abs(row["stated"] - row["observed"]) for row in table), total)
    worst = max((abs(row["gap_points"]) for row in table), default=0.0)
    return {
        "bands": table,
        "expected_calibration_error": round(ece, 4),
        "worst_band_gap_points": round(worst, 2),
        "within_five_points": worst <= 5.0,
    }


def fairness_segments(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Quality across customer groups.

    The measure compared across segments is verified resolution: answered
    automatically and citing a document the labels agree with. Comparing raw
    automation rate would reward a segment the system answers more readily
    without checking whether it answered correctly.
    """

    def score(group: Sequence[dict[str, Any]]) -> dict[str, Any]:
        auto = [r for r in group if r["action"] == "auto_respond"]
        with_labels = [r for r in auto if r.get("expected_doc_ids")]
        verified = [r for r in with_labels if set(r["cited_doc_ids"]) & set(r["expected_doc_ids"])]
        answerable = [r for r in group if r.get("expected_doc_ids")]
        hits = [r for r in answerable if set(r["retrieved_doc_ids"]) & set(r["expected_doc_ids"])]
        return {
            "n": len(group),
            "automation_rate": round(_safe_div(len(auto), len(group)), 4),
            "verified_resolution_rate": round(_safe_div(len(verified), len(with_labels)), 4),
            "verified_basis": len(with_labels),
            "retrieval_recall": round(_safe_div(len(hits), len(answerable)), 4),
            "classification_accuracy": round(
                _safe_div(
                    sum(
                        1 for r in group if r.get("true_intent") and r["predicted_intent"] == r["true_intent"]
                    ),
                    sum(1 for r in group if r.get("true_intent")),
                ),
                4,
            ),
        }

    dimensions = {
        "customer_tier": lambda r: r.get("customer_tier") or "unknown",
        "language_fluency": lambda r: r.get("language_fluency") or "unknown",
        "customer_region": lambda r: r.get("customer_region") or "unknown",
        "channel": lambda r: r["channel"],
        "ticket_length": lambda r: "short" if r.get("body_length", 0) < 120 else "long",
    }

    out: dict[str, Any] = {}
    for name, key in dimensions.items():
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            groups[key(r)].append(r)
        scored = {k: score(v) for k, v in sorted(groups.items())}
        # Only compare segments with enough tickets to mean anything. A segment
        # of four will swing twenty points on one ticket.
        comparable = {
            k: v["verified_resolution_rate"] for k, v in scored.items() if v["verified_basis"] >= 10
        }
        spread = (
            round(100 * (max(comparable.values()) - min(comparable.values())), 2)
            if len(comparable) > 1
            else None
        )
        out[name] = {
            "segments": scored,
            "verified_resolution_spread_points": spread,
            "segments_compared": sorted(comparable),
            "within_five_points": (spread is not None and spread < 5.0),
        }
    return out
