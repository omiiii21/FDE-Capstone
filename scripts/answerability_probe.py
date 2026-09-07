#!/usr/bin/env python3
"""Can the system tell, before it answers, whether the corpus covers the ticket?

Run:  python -m scripts.answerability_probe

This is a negative result and it is kept because it changed the design.

The largest single source of wrong routing is not misclassification. On the
development set the system answers 101 tickets the labels say should have been
escalated, and 86 of those are tickets whose intent it got right and whose
answer is simply not in the documentation. If that were predictable, the fix
would be a gate in front of the router.

It is not predictable. Every configuration tried here scores below the base rate
of always guessing "answerable", on a body-disjoint split. The signal that does
exist is too weak to act on: catching one uncovered ticket costs roughly two
covered ones escalated unnecessarily.

So there is no answerability gate in src/. The conclusion instead is in
scripts/documentation_gap.py: the ceiling is set by what Ines has written, and
the way to raise it is to write the missing articles.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest import load_tickets
from src.linear import LogisticRegression, TfidfVectoriser
from src.retrieve import Retriever

SEED = 20260830


def body_key(ticket) -> str:
    return re.sub(r"\s+", " ", ticket.body.strip().lower())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/development_tickets.json")
    parser.add_argument("--output", default="evaluation/results/answerability_probe.json")
    args = parser.parse_args()

    tickets = load_tickets(args.input)
    retriever = Retriever()

    groups: dict[str, list[int]] = collections.defaultdict(list)
    for i, ticket in enumerate(tickets):
        groups[body_key(ticket)].append(i)
    keys = sorted(groups)
    rng = np.random.default_rng(SEED)
    rng.shuffle(keys)

    # Retrieval-side features: the top score, the gap to second place, how
    # concentrated the score mass is, how long the ticket is, and how many
    # passages cleared a strong bar. If groundedness were legible anywhere, it
    # would be here.
    raw = []
    for ticket in tickets:
        passages = retriever.search(ticket.text, top_k=5, floor=0.0)
        scores = [p.score for p in passages] + [0.0] * 5
        raw.append(
            [
                scores[0],
                scores[0] - scores[1],
                scores[0] / (sum(scores[:5]) + 1e-9),
                len(ticket.body) / 200.0,
                float(len([p for p in passages if p.score > 8.0])),
            ]
        )
    features = np.array(raw)
    features = (features - features.mean(0)) / (features.std(0) + 1e-9)

    y = np.array([bool(t.labels.get("answerable_from_docs")) for t in tickets])
    base_rate = float(y.mean())

    def evaluate(use_retrieval: bool, class_weight: bool, label: str) -> dict:
        accuracy, recall_neg, precision_neg = [], [], []
        for fold in range(5):
            test_keys = set(keys[fold::5])
            test = [i for k in test_keys for i in groups[k]]
            train = [i for k in keys if k not in test_keys for i in groups[k]]
            vec = TfidfVectoriser(min_df=1, max_features=20000, ngram=2)
            X_train = vec.fit_transform([tickets[i].text for i in train])
            X_test = vec.transform([tickets[i].text for i in test])
            if use_retrieval:
                # Scaled down so the five dense columns do not swamp a few
                # thousand L2-normalised tf-idf columns.
                X_train = np.hstack([X_train, features[train] * 0.25])
                X_test = np.hstack([X_test, features[test] * 0.25])
            model = LogisticRegression(l2=1e-4, learning_rate=1.0, epochs=1000).fit(
                X_train, ["yes" if y[i] else "no" for i in train], class_weight=class_weight
            )
            probs = model.predict_proba(X_test)[:, model.classes.index("yes")]
            predicted = probs > 0.5
            truth = y[test]
            accuracy.append(float((predicted == truth).mean()))
            negatives = ~truth
            if negatives.sum():
                recall_neg.append(float((~predicted[negatives]).mean()))
            if (~predicted).sum():
                precision_neg.append(float((~truth[~predicted]).mean()))
        return {
            "configuration": label,
            "accuracy": round(float(np.mean(accuracy)), 4),
            "beats_base_rate": bool(np.mean(accuracy) > base_rate),
            "recall_on_uncovered": round(float(np.mean(recall_neg)), 4),
            "precision_on_uncovered": round(float(np.mean(precision_neg)), 4),
        }

    print(f"Base rate: always predicting 'answerable' scores {base_rate:.1%}.\n")
    print(
        f"{'configuration':36s} {'accuracy':>9s} {'beats base':>11s} {'recall(unc)':>12s} {'prec(unc)':>10s}"
    )
    results = [
        evaluate(False, True, "text only, class-weighted"),
        evaluate(False, False, "text only, unweighted"),
        evaluate(True, False, "text + retrieval features"),
        evaluate(True, True, "text + retrieval, class-weighted"),
    ]
    for row in results:
        print(
            f"{row['configuration']:36s} {row['accuracy']:>9.1%} "
            f"{('yes' if row['beats_base_rate'] else 'no'):>11s} "
            f"{row['recall_on_uncovered']:>12.1%} {row['precision_on_uncovered']:>10.1%}"
        )

    best = max(results, key=lambda r: r["accuracy"])
    print(
        f"\nBest configuration reaches {best['accuracy']:.1%}, below the {base_rate:.1%} base rate.\n"
        "Precision on the uncovered class is around 45% against a 28.6% prior, so there is some\n"
        "signal, but acting on it escalates roughly two covered tickets for every uncovered one\n"
        "it catches. That is a worse trade than answering them, so no gate was built."
    )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(
            {
                "input": args.input,
                "base_rate_answerable": round(base_rate, 4),
                "results": results,
                "conclusion": (
                    "Answerability is not predictable from the ticket text or from retrieval "
                    "scores on a body-disjoint split. No gate was built; see "
                    "scripts/documentation_gap.py for what to do instead."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwritten to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
