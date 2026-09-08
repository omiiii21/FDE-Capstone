#!/usr/bin/env python3
"""Lexical BM25 against dense embeddings, on the same tickets.

Run:  pip install -r requirements.txt -r requirements-dense.txt
      python -m scripts.compare_retrieval

This is the measurement behind ADR-002. It needs the optional dense extras; if
they are not installed it says so and exits rather than pretending.

The comparison reports recall and precision overall and split by language
fluency, because the fluency gap is the one place where an embedding model has
an obvious theoretical advantage over keyword matching and it is worth knowing
whether that advantage is real on this corpus.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest import load_tickets
from src.retrieve import Retriever


def score_backend(retriever, tickets, floor: float, top_k: int) -> dict:
    hit = top1 = 0
    answerable = 0
    false_positive = unanswerable = 0
    by_fluency = {"fluent": [0, 0], "non_fluent": [0, 0]}
    latencies = []

    for ticket in tickets:
        expected = set(ticket.labels.get("expected_doc_ids") or [])
        started = time.perf_counter()
        passages = retriever.search(ticket.text, top_k=top_k, floor=floor)
        latencies.append(time.perf_counter() - started)
        got = [p.doc_id for p in passages]
        if expected:
            answerable += 1
            bucket = by_fluency.setdefault(ticket.language_fluency, [0, 0])
            bucket[1] += 1
            if set(got) & expected:
                hit += 1
                bucket[0] += 1
            if got and got[0] in expected:
                top1 += 1
        else:
            unanswerable += 1
            if got:
                false_positive += 1

    return {
        "recall_at_k": round(hit / answerable, 4) if answerable else 0.0,
        "precision_at_1": round(top1 / answerable, 4) if answerable else 0.0,
        "false_positive_rate_on_unanswerable": (
            round(false_positive / unanswerable, 4) if unanswerable else 0.0
        ),
        "recall_fluent": (
            round(by_fluency["fluent"][0] / by_fluency["fluent"][1], 4) if by_fluency["fluent"][1] else 0.0
        ),
        "recall_non_fluent": (
            round(by_fluency["non_fluent"][0] / by_fluency["non_fluent"][1], 4)
            if by_fluency["non_fluent"][1]
            else 0.0
        ),
        "median_query_ms": round(1000 * statistics.median(latencies), 3),
        "p95_query_ms": round(1000 * sorted(latencies)[int(0.95 * len(latencies))], 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/development_tickets.json")
    parser.add_argument("--output", default="evaluation/results/retrieval_backend_comparison.json")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        print(
            "The dense extras are not installed, so there is nothing to compare against.\n"
            "  pip install -r requirements-dense.txt\n"
            "Skipping rather than reporting a comparison that did not happen."
        )
        return 1

    tickets = load_tickets(args.input)
    print(f"{len(tickets)} tickets from {args.input}\n")

    lexical = Retriever(backend="lexical")
    dense = Retriever(backend="dense")
    if dense.backend != "dense":
        print("The dense backend failed to load; not reporting a comparison.")
        return 1

    # The two backends score on different scales, so comparing them at one
    # shared floor would measure the floor rather than the ranking. Each is
    # given the floor that maximises recall without returning everything, found
    # by a short sweep on its own scale.
    results = {}
    for name, retriever, floors in (
        ("lexical", lexical, [3.0, 4.2, 5.0, 6.0]),
        ("dense", dense, [3.0, 4.0, 4.8, 5.4, 6.0]),
    ):
        best = None
        for floor in floors:
            row = score_backend(retriever, tickets, floor, args.top_k)
            row["floor"] = floor
            if best is None or row["recall_at_k"] > best["recall_at_k"]:
                best = row
        results[name] = best

    print(
        f"{'backend':>9} {'floor':>6} {'recall@5':>9} {'prec@1':>8} {'fluent':>8} "
        f"{'non-fluent':>11} {'gap':>6} {'p95 ms':>8}"
    )
    for name, row in results.items():
        gap = 100 * (row["recall_fluent"] - row["recall_non_fluent"])
        print(
            f"{name:>9} {row['floor']:>6.1f} {row['recall_at_k']:>9.1%} {row['precision_at_1']:>8.1%} "
            f"{row['recall_fluent']:>8.1%} {row['recall_non_fluent']:>11.1%} {gap:>5.1f}p "
            f"{row['p95_query_ms']:>8.1f}"
        )

    delta = 100 * (results["dense"]["recall_at_k"] - results["lexical"]["recall_at_k"])
    lex_gap = 100 * (results["lexical"]["recall_fluent"] - results["lexical"]["recall_non_fluent"])
    dense_gap = 100 * (results["dense"]["recall_fluent"] - results["dense"]["recall_non_fluent"])
    print(f"\nDense minus lexical on recall@5: {delta:+.1f} points.")
    print(f"Non-fluent gap: {lex_gap:.1f} points lexical, {dense_gap:.1f} points dense.")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(
            {
                "input": args.input,
                "top_k": args.top_k,
                "results": results,
                "dense_minus_lexical_recall_points": round(delta, 2),
                "non_fluent_gap_points": {"lexical": round(lex_gap, 2), "dense": round(dense_gap, 2)},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwritten to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
