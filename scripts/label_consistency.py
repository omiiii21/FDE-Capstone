#!/usr/bin/env python3
"""Do the labels agree with themselves?

    python -m scripts.label_consistency

The development set holds 500 tickets and 215 distinct ticket bodies. Where the
same text appears more than once, every label attached to it should say the same
thing, and four of the six do not.

I ran exactly this check on `urgency` in week two, found 334 tickets carrying
contradictory urgency labels, and shipped a policy table instead of a model
because of it (ADR-006). I did not run it on `answerable_from_docs`, which is the
label the headline finding of this project rests on. That was the mistake this
script exists to correct, and it is ten lines of real work over data I already
had.

The output is written to `evaluation/results/label_consistency.json` so the
report can quote it rather than restate it.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = (
    "intent",
    "must_not_auto_respond",
    "expected_route",
    "answerable_from_docs",
    "urgency",
    "expected_doc_ids",
)


def body_key(ticket: dict) -> str:
    """The same normalisation the duplicate-body analysis uses elsewhere."""
    return re.sub(r"\s+", " ", (ticket.get("body") or "")).strip().lower()


def audit(tickets: list[dict]) -> dict:
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for t in tickets:
        groups[body_key(t)].append(t)

    out: dict[str, object] = {
        "tickets": len(tickets),
        "distinct_bodies": len(groups),
        "labels": {},
    }
    for label in LABELS:
        bodies = tickets_affected = 0
        examples = []
        for key, group in groups.items():
            values = {json.dumps(t["labels"].get(label), sort_keys=True) for t in group}
            if len(values) > 1:
                bodies += 1
                tickets_affected += len(group)
                if len(examples) < 3:
                    examples.append(
                        {
                            "ticket_ids": [t["ticket_id"] for t in group],
                            "values": sorted(values),
                        }
                    )
        out["labels"][label] = {
            "bodies_with_conflicting_values": bodies,
            "tickets_affected": tickets_affected,
            "self_consistent": bodies == 0,
            "examples": examples,
        }

    # What the coverage label does to the headline, read three ways.
    any_answerable = {k: any(t["labels"]["answerable_from_docs"] for t in g) for k, g in groups.items()}
    all_answerable = {k: all(t["labels"]["answerable_from_docs"] for t in g) for k, g in groups.items()}
    n = len(tickets)
    out["answerability_ceiling"] = {
        "per_row": round(sum(1 for t in tickets if t["labels"]["answerable_from_docs"]) / n, 4),
        "body_any": round(sum(1 for t in tickets if any_answerable[body_key(t)]) / n, 4),
        "body_all": round(sum(1 for t in tickets if all_answerable[body_key(t)]) / n, 4),
        "reading": (
            "per_row is what the report quotes. body_any and body_all are what the same "
            "labels give if a conflict is resolved generously or strictly. The true "
            "coverage gap lies between them and this data cannot narrow it further."
        ),
    }
    return out


def rescore(tickets: list[dict], responses_path: Path) -> dict | None:
    """Citation accuracy scored per row, and scored against identical text."""
    if not responses_path.exists():
        return None
    groups: dict[str, set[str]] = collections.defaultdict(set)
    for t in tickets:
        groups[body_key(t)] |= set(t["labels"].get("expected_doc_ids") or [])
    body_of = {t["ticket_id"]: body_key(t) for t in tickets}

    auto = [
        json.loads(line)
        for line in responses_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["action"] == "auto_respond"
    ]
    if not auto:
        return None

    def cited(r):
        return set(r.get("cited_doc_ids") or [])

    per_row = sum(1 for r in auto if cited(r) & set(r.get("expected_doc_ids") or []))
    by_union = sum(1 for r in auto if cited(r) & groups[body_of[r["ticket_id"]]])
    uncovered = [r for r in auto if r.get("expected_doc_ids") == []]
    with_twin = [r for r in uncovered if groups[body_of[r["ticket_id"]]]]
    twin_hit = [r for r in with_twin if cited(r) & groups[body_of[r["ticket_id"]]]]
    return {
        "automatic_answers": len(auto),
        "citation_accuracy_per_row": round(per_row / len(auto), 4),
        "citation_accuracy_over_identical_text": round(by_union / len(auto), 4),
        "swing_points": round(100 * (by_union - per_row) / len(auto), 2),
        "replies_on_rows_labelled_uncovered": len(uncovered),
        "of_those_an_identical_body_names_a_document": len(with_twin),
        "and_the_system_cited_that_document": len(twin_hit),
        "reading": (
            "The per-row figure is the one the report leads with, because it is the "
            "convention the run was scored under and it is the less flattering of the "
            "two. Neither reaches the 95% target, so the recommendation does not change."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "data" / "development_tickets.json"))
    parser.add_argument(
        "--responses",
        default=str(ROOT / "evaluation" / "results" / "dev_run" / "responses.jsonl"),
    )
    parser.add_argument("--output", default=str(ROOT / "evaluation" / "results" / "label_consistency.json"))
    args = parser.parse_args(argv)

    tickets = json.loads(Path(args.input).read_text(encoding="utf-8"))
    report = audit(tickets)
    scored = rescore(tickets, Path(args.responses))
    if scored:
        report["citation_accuracy_rescored"] = scored

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"{report['tickets']} tickets, {report['distinct_bodies']} distinct bodies\n")
    print(f"  {'label':26}{'bodies':>8}{'tickets':>9}")
    for label, block in report["labels"].items():
        mark = "" if block["self_consistent"] else "  <- disagrees with itself"
        print(
            f"  {label:26}{block['bodies_with_conflicting_values']:>8}"
            f"{block['tickets_affected']:>9}{mark}"
        )
    ceiling = report["answerability_ceiling"]
    print(
        f"\n  answerability ceiling: {100 * ceiling['per_row']:.1f}% per row, "
        f"{100 * ceiling['body_any']:.1f}% generous, {100 * ceiling['body_all']:.1f}% strict"
    )
    if scored:
        print(
            f"  citation accuracy: {100 * scored['citation_accuracy_per_row']:.1f}% per row, "
            f"{100 * scored['citation_accuracy_over_identical_text']:.1f}% over identical text "
            f"({scored['swing_points']:+.1f} points)"
        )
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
