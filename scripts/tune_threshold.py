#!/usr/bin/env python3
"""Choose the confidence threshold from a cost model rather than by eye.

Run:  python -m scripts.tune_threshold

The brief is explicit that a threshold picked because it looked reasonable is
not a threshold that has been designed. So this puts a number on each of the
three outcomes and sweeps for the one that costs least.

There are four outcomes, not three, and separating the third from the fourth is
what makes the model worth running. The costs come from the interviews:

  resolved automatically     1.0  the unit; what a resolved ticket costs today
  escalated                  4.0  Marcus: "every ticket that bounces to tier two
                                  costs us roughly four times what a resolved
                                  one costs"
  answered but incomplete    6.0  the ticket was not covered by the corpus, so
                                  the customer gets a partial answer and comes
                                  back. Escalation plus the friction of a second
                                  contact. 21.6% of tickets today are repeat
                                  contacts, so this outcome is not hypothetical.
  answered and wrong        12.0  the documentation did cover it and the system
                                  cited the wrong article. Marcus: "our customers
                                  are engineers, they will screenshot a
                                  confidently incorrect answer and put it on the
                                  internet within the hour."

The last number has no invoice behind it, so it is a judgement rather than a
measurement. It is exposed as --wrong-cost and the sensitivity of the chosen
threshold to it is printed, which is the honest way to use a number I made up.

The two thresholds are swept independently. Tying them together, which is what I
did first, produced a cliff at 0.78 that looked like a property of the data and
was actually the high-cost bar crossing 1.0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classify import get_classifier
from src.ingest import load_tickets
from src.retrieve import Retriever
from src.route import route


def simulate(tickets, cached, threshold: float, high_cost: float) -> dict:
    resolved = incomplete = wrong = escalated = 0
    policy_breach = 0
    for ticket in tickets:
        classification, passages = cached[ticket.ticket_id]
        decision = route(
            classification, passages, threshold=threshold, high_cost_threshold=high_cost, halted=False
        )
        if decision.action != "auto_respond":
            escalated += 1
            continue
        if ticket.labels.get("must_not_auto_respond"):
            policy_breach += 1
        expected = set(ticket.labels.get("expected_doc_ids") or [])
        cited = {p.doc_id for p in passages[:2]}
        if not ticket.labels.get("answerable_from_docs"):
            # The corpus does not cover this. The reply will be partial and the
            # customer will be back; it is not a wrong answer, it is an
            # incomplete one, and it costs differently.
            incomplete += 1
        elif cited & expected:
            resolved += 1
        else:
            wrong += 1
    return {
        "threshold": round(threshold, 3),
        "high_cost_threshold": round(high_cost, 3),
        "resolved": resolved,
        "incomplete": incomplete,
        "wrong": wrong,
        "escalated": escalated,
        "policy_breaches": policy_breach,
        "automation_rate": round((resolved + incomplete + wrong) / len(tickets), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/development_tickets.json")
    parser.add_argument("--wrong-cost", type=float, default=12.0)
    parser.add_argument("--incomplete-cost", type=float, default=6.0)
    parser.add_argument("--escalation-cost", type=float, default=4.0)
    parser.add_argument("--correct-cost", type=float, default=1.0)
    parser.add_argument("--output", default="evaluation/results/threshold_sweep.json")
    args = parser.parse_args()

    tickets = load_tickets(args.input)
    classifier = get_classifier()
    retriever = Retriever()

    # Classification and retrieval do not depend on the threshold, so do them
    # once. Without this the sweep takes minutes instead of seconds.
    cached = {t.ticket_id: (classifier.classify(t), retriever.search(t.text)) for t in tickets}
    del classifier, retriever

    def total_cost(row: dict, wrong_cost: float) -> float:
        return (
            row["resolved"] * args.correct_cost
            + row["incomplete"] * args.incomplete_cost
            + row["wrong"] * wrong_cost
            + row["escalated"] * args.escalation_cost
        )

    thresholds = [round(0.30 + 0.05 * i, 2) for i in range(15)]
    high_costs = [round(0.50 + 0.05 * i, 2) for i in range(11)]
    rows = []
    for threshold in thresholds:
        for high_cost in high_costs:
            if high_cost < threshold:
                continue
            row = simulate(tickets, cached, threshold, high_cost)
            row["total_cost"] = round(total_cost(row, args.wrong_cost), 1)
            row["cost_per_ticket"] = round(row["total_cost"] / len(tickets), 3)
            rows.append(row)

    print(
        f"{'thresh':>7} {'high':>6} {'resolved':>9} {'incomplete':>11} {'wrong':>6} "
        f"{'escalated':>10} {'automation':>11} {'cost':>8}"
    )
    for row in rows:
        if row["high_cost_threshold"] not in (0.65, 0.85, 1.0):
            continue
        print(
            f"{row['threshold']:>7.2f} {row['high_cost_threshold']:>6.2f} {row['resolved']:>9d} "
            f"{row['incomplete']:>11d} {row['wrong']:>6d} {row['escalated']:>10d} "
            f"{row['automation_rate']:>11.1%} {row['total_cost']:>8.1f}"
        )

    best = min(rows, key=lambda r: r["total_cost"])
    print(
        f"\nLowest cost at threshold {best['threshold']} / high-cost bar "
        f"{best['high_cost_threshold']}: {best['automation_rate']:.1%} automation, "
        f"{best['wrong']} wrong answers, {best['incomplete']} incomplete, "
        f"{best['cost_per_ticket']:.2f} cost per ticket."
    )
    baseline = args.escalation_cost
    print(
        f"Doing nothing (escalate everything) costs {baseline:.2f} per ticket, so the "
        f"system is worth {baseline - best['cost_per_ticket']:+.2f} per ticket at these costs."
    )

    # How much does the choice depend on the number I invented?
    print("\nSensitivity to the cost of a wrong answer:")
    sensitivity = []
    for wrong_cost in (4.0, 8.0, 12.0, 20.0, 40.0, 100.0):
        pick = min(rows, key=lambda r: total_cost(r, wrong_cost))
        sensitivity.append(
            {
                "wrong_cost": wrong_cost,
                "threshold": pick["threshold"],
                "high_cost_threshold": pick["high_cost_threshold"],
                "automation_rate": pick["automation_rate"],
            }
        )
        print(
            f"  wrong answer costs {wrong_cost:>5.0f}x -> threshold {pick['threshold']:.2f} / "
            f"{pick['high_cost_threshold']:.2f}, automation {pick['automation_rate']:.1%}"
        )

    flat = all(s["threshold"] == sensitivity[0]["threshold"] for s in sensitivity)
    if flat:
        print(
            "\nThe threshold does not move across that range. That is not a sign the sweep worked;\n"
            "it is a sign the confidence distribution is degenerate on this data - almost every\n"
            "prediction sits above any threshold worth setting, so the threshold is not the control\n"
            "doing the work. The policy classes and the grounding guardrail are. This is written up\n"
            "in ADR-006 and it is the single most important caveat on the routing numbers."
        )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(
            {
                "input": args.input,
                "costs": {
                    "correct": args.correct_cost,
                    "escalation": args.escalation_cost,
                    "wrong": args.wrong_cost,
                },
                "rows": rows,
                "chosen": best,
                "sensitivity": sensitivity,
                "threshold_is_inert_on_this_data": flat,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwritten to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
