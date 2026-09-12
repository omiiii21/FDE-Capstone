#!/usr/bin/env python3
"""Fairness audit, including a matched-pair test on language fluency.

Run:  python -m scripts.fairness_audit

Comparing segment averages is the obvious way to do this and it is weak, because
the segments differ in more than the thing being tested. Non-fluent tickets are
also shorter and skew towards different intents, so a gap between the two
averages could be about length or topic rather than about phrasing.

The development set happens to make a better test possible. Many questions
appear twice, once in fluent English and once in non-fluent English, with the
same intent and the same expected documents. 29 such groups survive the filter
this script applies, which is stricter than a first count suggests: the group
also has to carry an expected document, or there is no retrieval outcome to
compare. That gives near-matched pairs, and running both halves of a pair
isolates phrasing from length and topic, which is the comparison the governance
framework is actually asking for.

Both analyses are reported. The segment averages are what the framework's table
wants; the matched pairs are what I would defend.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classify import get_classifier
from src.ingest import load_tickets
from src.retrieve import Retriever
from src.route import route


def body_key(ticket) -> str:
    return re.sub(r"\s+", " ", ticket.body.strip().lower())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/development_tickets.json")
    parser.add_argument("--output", default="evaluation/results/fairness_audit.json")
    args = parser.parse_args()

    tickets = load_tickets(args.input)
    retriever = Retriever()
    classifier = get_classifier()

    # One pass; everything below reads from this.
    observations = {}
    for ticket in tickets:
        passages = retriever.search(ticket.text)
        classification = classifier.classify(ticket)
        decision = route(classification, passages)
        expected = set(ticket.labels.get("expected_doc_ids") or [])
        retrieved = [p.doc_id for p in passages]
        observations[ticket.ticket_id] = {
            "intent_correct": classification.intent == ticket.labels.get("intent"),
            "confidence": classification.intent_confidence,
            "retrieval_hit": bool(expected and set(retrieved) & expected),
            "top1_correct": bool(expected and retrieved and retrieved[0] in expected),
            "answered": decision.action == "auto_respond",
            "top_score": passages[0].score if passages else 0.0,
            "expected": expected,
        }

    # --- segment comparison -------------------------------------------
    def segment(key_fn, name):
        groups = collections.defaultdict(list)
        for ticket in tickets:
            groups[key_fn(ticket)].append(ticket)
        rows = {}
        for key, group in sorted(groups.items()):
            answerable = [t for t in group if t.labels.get("expected_doc_ids")]
            rows[key] = {
                "n": len(group),
                "automation_rate": round(
                    sum(1 for t in group if observations[t.ticket_id]["answered"]) / len(group), 4
                ),
                "intent_accuracy": round(
                    sum(1 for t in group if observations[t.ticket_id]["intent_correct"]) / len(group), 4
                ),
                "retrieval_recall": (
                    round(
                        sum(1 for t in answerable if observations[t.ticket_id]["retrieval_hit"])
                        / len(answerable),
                        4,
                    )
                    if answerable
                    else None
                ),
                "answerable_n": len(answerable),
            }
        comparable = [v["retrieval_recall"] for v in rows.values() if v["answerable_n"] >= 25]
        spread = round(100 * (max(comparable) - min(comparable)), 2) if len(comparable) > 1 else None
        return {"segments": rows, "retrieval_recall_spread_points": spread, "name": name}

    segments = {
        "language_fluency": segment(lambda t: t.language_fluency, "language fluency"),
        "customer_tier": segment(lambda t: t.customer_tier, "customer tier"),
        "customer_region": segment(lambda t: t.customer_region, "region"),
        "channel": segment(lambda t: t.channel, "channel"),
    }

    # --- matched pairs on fluency --------------------------------------
    buckets = collections.defaultdict(lambda: collections.defaultdict(list))
    for ticket in tickets:
        key = (ticket.labels.get("intent"), tuple(sorted(ticket.labels.get("expected_doc_ids") or [])))
        buckets[key][ticket.language_fluency].append(ticket)

    pairs = []
    for (intent, docs), by_fluency in sorted(buckets.items()):
        fluent = by_fluency.get("fluent") or []
        non_fluent = by_fluency.get("non_fluent") or []
        if not fluent or not non_fluent or not docs:
            continue
        # Deduplicate by body so a phrasing repeated eleven times does not
        # count eleven times.
        fluent_bodies = {body_key(t): t for t in fluent}
        non_fluent_bodies = {body_key(t): t for t in non_fluent}
        f_hit = statistics.mean(
            1.0 if observations[t.ticket_id]["retrieval_hit"] else 0.0 for t in fluent_bodies.values()
        )
        n_hit = statistics.mean(
            1.0 if observations[t.ticket_id]["retrieval_hit"] else 0.0 for t in non_fluent_bodies.values()
        )
        f_score = statistics.mean(observations[t.ticket_id]["top_score"] for t in fluent_bodies.values())
        n_score = statistics.mean(observations[t.ticket_id]["top_score"] for t in non_fluent_bodies.values())
        pairs.append(
            {
                "intent": intent,
                "expected_docs": list(docs),
                "fluent_phrasings": len(fluent_bodies),
                "non_fluent_phrasings": len(non_fluent_bodies),
                "fluent_hit_rate": round(f_hit, 4),
                "non_fluent_hit_rate": round(n_hit, 4),
                "hit_gap_points": round(100 * (f_hit - n_hit), 2),
                "fluent_mean_top_score": round(f_score, 2),
                "non_fluent_mean_top_score": round(n_score, 2),
                "score_gap": round(f_score - n_score, 2),
            }
        )

    worse = [p for p in pairs if p["hit_gap_points"] > 0]
    better = [p for p in pairs if p["hit_gap_points"] < 0]
    equal = [p for p in pairs if p["hit_gap_points"] == 0]
    mean_gap = round(statistics.mean(p["hit_gap_points"] for p in pairs), 2) if pairs else 0.0
    mean_score_gap = round(statistics.mean(p["score_gap"] for p in pairs), 2) if pairs else 0.0

    matched = {
        "pairs_compared": len(pairs),
        "mean_retrieval_hit_gap_points": mean_gap,
        "mean_top_score_gap": mean_score_gap,
        "pairs_worse_for_non_fluent": len(worse),
        "pairs_better_for_non_fluent": len(better),
        "pairs_equal": len(equal),
        "largest_gaps": sorted(pairs, key=lambda p: -p["hit_gap_points"])[:6],
        "pairs": pairs,
    }

    fluency = segments["language_fluency"]["segments"]
    print("Segment comparison, language fluency")
    for key, row in fluency.items():
        print(
            f"  {key:12s} n={row['n']:4d}  automation {row['automation_rate']:.1%}  "
            f"intent {row['intent_accuracy']:.1%}  retrieval recall {row['retrieval_recall']:.1%} "
            f"(n={row['answerable_n']})"
        )
    gap = 100 * (fluency["fluent"]["retrieval_recall"] - fluency["non_fluent"]["retrieval_recall"])
    print(f"  retrieval recall gap: {gap:.1f} points\n")

    print(f"Matched pairs: {len(pairs)} (intent, expected-document) groups carry both phrasings")
    print(f"  mean retrieval hit gap, fluent minus non-fluent: {mean_gap:+.1f} points")
    print(f"  mean top-1 BM25 score gap:                       {mean_score_gap:+.1f}")
    print(f"  groups worse for non-fluent: {len(worse)}   better: {len(better)}   equal: {len(equal)}")
    if worse:
        print("\n  Where the gap is widest:")
        for row in matched["largest_gaps"]:
            if row["hit_gap_points"] <= 0:
                continue
            print(
                f"    {row['intent']:24s} fluent {row['fluent_hit_rate']:.0%} vs "
                f"non-fluent {row['non_fluent_hit_rate']:.0%}  ({row['hit_gap_points']:+.0f} pts)"
            )

    print("\nOther dimensions, retrieval recall spread across segments with n>=25:")
    for name, block in segments.items():
        if name == "language_fluency":
            continue
        spread = block["retrieval_recall_spread_points"]
        print(f"  {name:16s} {spread if spread is not None else 'not comparable'}")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps({"segments": segments, "matched_pairs": matched}, indent=2), encoding="utf-8"
    )
    print(f"\nwritten to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
