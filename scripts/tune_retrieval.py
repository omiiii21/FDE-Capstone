#!/usr/bin/env python3
"""Sweep the retrieval parameters against labels.expected_doc_ids.

Run:  python -m scripts.tune_retrieval [--full]

The floor is the interesting one. Every point it comes down buys recall on the
tickets that are answerable and costs precision on the 28.6% that are not,
where the correct behaviour is to return nothing. The chosen value is in
src/config.py and the reasoning is in ADR-002.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest import load_tickets
from src.retrieve import Retriever


def evaluate(retriever: Retriever, tickets, top_k: int, floor: float) -> dict:
    hit = total_answerable = 0
    top1 = 0
    false_positive = total_unanswerable = 0
    returned = 0
    for ticket in tickets:
        expected = set(ticket.labels.get("expected_doc_ids") or [])
        passages = retriever.search(ticket.text, top_k=top_k, floor=floor)
        returned += len(passages)
        got = [p.doc_id for p in passages]
        if expected:
            total_answerable += 1
            if expected & set(got):
                hit += 1
            if got and got[0] in expected:
                top1 += 1
        else:
            total_unanswerable += 1
            if got:
                false_positive += 1
    return {
        "top_k": top_k,
        "floor": floor,
        "recall_at_k": hit / total_answerable if total_answerable else 0.0,
        "precision_at_1": top1 / total_answerable if total_answerable else 0.0,
        "false_positive_rate": false_positive / total_unanswerable if total_unanswerable else 0.0,
        "mean_passages": returned / len(tickets),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/development_tickets.json")
    parser.add_argument("--full", action="store_true", help="sweep top_k as well as the floor")
    args = parser.parse_args()

    tickets = load_tickets(args.input)
    retriever = Retriever()
    floors = [0.0, 2.0, 3.0, 3.5, 4.0, 4.2, 4.5, 5.0, 5.5, 6.0, 7.0, 8.0]
    ks = [3, 4, 5, 6, 8] if args.full else [5]

    rows = []
    print(
        f"{'k':>3} {'floor':>6} {'recall@k':>9} {'prec@1':>8} {'fp(unanswerable)':>17} {'mean passages':>14}"
    )
    for k, floor in itertools.product(ks, floors):
        row = evaluate(retriever, tickets, k, floor)
        rows.append(row)
        print(
            f"{k:>3} {floor:>6.1f} {row['recall_at_k']:>9.1%} {row['precision_at_1']:>8.1%} "
            f"{row['false_positive_rate']:>17.1%} {row['mean_passages']:>14.2f}"
        )

    out = Path("evaluation/results/retrieval_sweep.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"input": args.input, "rows": rows}, indent=2), encoding="utf-8")
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
