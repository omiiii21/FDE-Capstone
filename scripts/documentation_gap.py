#!/usr/bin/env python3
"""Where the knowledge base does not cover the queue.

Run:  python -m scripts.documentation_gap

This is the analysis that answers the question the architecture cannot. Roughly
29% of tickets are labelled as not answerable from the existing documentation,
and scripts/answerability_probe.py shows that the system cannot reliably tell
which ones those are before it answers them. That leaves one lever: write the
missing articles.

The output is a ranked list of what Ines should write next, weighted by how much
of the queue each gap accounts for and by what those tickets cost today. It is
the most useful thing in this repository that is not code.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import NEVER_AUTO_RESPOND
from src.ingest import load_tickets


AGENTS = 6  # CloudServe's support team, from the brief
HOURS_PER_WEEK = 40
WORKING_WEEKS = 46


def _capacity_check(elapsed_hours: float) -> dict:
    """Guard the headline figure against being read as labour.

    The whole team can work about eleven thousand hours a year. Any figure larger
    than that is elapsed ticket time, not effort, and saying so here means the
    number carries its own correction wherever it is quoted.
    """
    capacity = AGENTS * HOURS_PER_WEEK * WORKING_WEEKS
    return {
        "support_agents": AGENTS,
        "team_hours_available_per_year": capacity,
        "elapsed_hours_as_multiple_of_team_capacity": round(elapsed_hours / capacity, 1),
        "reading": (
            "This is elapsed ticket time, not agent labour. It exceeds the entire "
            "team's annual capacity, which is the proof that it cannot be labour. "
            "The dataset records resolution time from arrival to close and carries "
            "no handling time, so agent effort cannot be derived from it at all. "
            "Quote the weekly ticket count instead, and quote this only as the "
            "elapsed burden the queue carries."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/development_tickets.json")
    parser.add_argument("--corpus", default="data/documentation.json")
    parser.add_argument("--output", default="evaluation/results/documentation_gap.json")
    args = parser.parse_args()

    tickets = load_tickets(args.input)
    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    covered_docs = {d["doc_id"] for d in corpus}

    gaps: dict[str, list] = defaultdict(list)
    for ticket in tickets:
        if not ticket.labels.get("answerable_from_docs"):
            gaps[ticket.labels["intent"]].append(ticket)

    total = len(tickets)
    rows = []
    for intent, group in gaps.items():
        history = [t.raw.get("history", {}) for t in group]
        resolution_times = [
            h.get("resolution_time_minutes") for h in history if h.get("resolution_time_minutes")
        ]
        csats = [h.get("csat_rating") for h in history if h.get("csat_rating")]
        repeats = sum(1 for h in history if h.get("repeat_contact"))
        in_scope = intent not in NEVER_AUTO_RESPOND
        rows.append(
            {
                "intent": intent,
                "uncovered_tickets": len(group),
                "share_of_all_tickets": round(len(group) / total, 4),
                "share_of_this_intent": round(
                    len(group) / sum(1 for t in tickets if t.labels["intent"] == intent), 4
                ),
                "median_resolution_minutes": (
                    round(statistics.median(resolution_times), 1) if resolution_times else None
                ),
                "mean_csat": round(statistics.mean(csats), 2) if csats else None,
                "repeat_contact_rate": round(repeats / len(group), 4),
                # ELAPSED ticket-hours across a year, if the 500 ticket sample is
                # representative of a 500 per week queue. This is NOT agent labour
                # and an earlier version of this script called it that. The data
                # records `resolution_time_minutes`, which is wall-clock time from
                # arrival to resolution and includes every minute a ticket sat in a
                # queue. Six agents cannot work the number this produces: see
                # `capacity_check` in the summary, which exists so the figure can
                # never be quoted as headcount again.
                "annual_elapsed_ticket_hours": round(
                    (statistics.median(resolution_times) if resolution_times else 0) * len(group) * 52 / 60,
                    0,
                ),
                "would_be_automatable": in_scope,
                "note": "" if in_scope else "policy class; an article helps the agent, not the automation",
            }
        )

    # Rank by agent hours recovered, but only count the classes automation could
    # actually take. An article on security incidents is worth writing and will
    # not reduce the escalation rate by one ticket.
    rows.sort(key=lambda r: (r["would_be_automatable"], r["annual_elapsed_ticket_hours"]), reverse=True)

    automatable = [r for r in rows if r["would_be_automatable"]]
    recoverable = sum(r["uncovered_tickets"] for r in automatable)

    summary = {
        "tickets_examined": total,
        "uncovered_tickets": sum(r["uncovered_tickets"] for r in rows),
        "uncovered_share": round(sum(r["uncovered_tickets"] for r in rows) / total, 4),
        "articles_in_corpus": len(covered_docs),
        "uncovered_in_automatable_classes": recoverable,
        "automation_ceiling_today": round(1 - sum(r["uncovered_tickets"] for r in rows) / total, 4),
        "automation_ceiling_if_gaps_closed": round(
            1 - (sum(r["uncovered_tickets"] for r in rows) - recoverable) / total, 4
        ),
        "annual_elapsed_ticket_hours_in_scope": round(
            sum(r["annual_elapsed_ticket_hours"] for r in automatable), 0
        ),
        "weekly_tickets_in_scope": recoverable,
        "capacity_check": _capacity_check(sum(r["annual_elapsed_ticket_hours"] for r in automatable)),
        "by_intent": rows,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(
        f"{summary['uncovered_tickets']} of {total} tickets ({summary['uncovered_share']:.1%}) "
        f"are not answerable from the existing {len(covered_docs)} articles.\n"
    )
    print(
        f"{'intent':26s} {'n':>4s} {'% of class':>11s} {'med mins':>9s} {'csat':>5s} "
        f"{'repeat':>7s} {'elapsed h/yr':>13s}"
    )
    for row in rows[:14]:
        marker = " " if row["would_be_automatable"] else "*"
        print(
            f"{marker}{row['intent']:25s} {row['uncovered_tickets']:>4d} "
            f"{row['share_of_this_intent']:>10.0%} "
            f"{(row['median_resolution_minutes'] or 0):>9.0f} "
            f"{(row['mean_csat'] or 0):>5.2f} {row['repeat_contact_rate']:>7.0%} "
            f"{row['annual_elapsed_ticket_hours']:>13,.0f}"
        )
    print("\n* policy class: an article helps the agent but will not raise the automation rate.")
    print(
        f"\nAutomation ceiling with today's corpus: {summary['automation_ceiling_today']:.1%}. "
        f"Closing the gaps in automatable classes would raise it to "
        f"{summary['automation_ceiling_if_gaps_closed']:.1%}."
    )
    hours = summary["annual_elapsed_ticket_hours_in_scope"]
    check = summary["capacity_check"]
    print(
        f"Those gaps hold {summary['weekly_tickets_in_scope']} tickets a week, carrying roughly "
        f"{hours:,.0f} elapsed ticket-hours a year."
    )
    print(
        f"  That is {check['elapsed_hours_as_multiple_of_team_capacity']}x the "
        f"{check['team_hours_available_per_year']:,} hours {check['support_agents']} agents have in a "
        "year, which is how you know it is elapsed time and not labour. The data carries no "
        "handling time, so agent effort cannot be derived from it."
    )
    print(f"\nwritten to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
