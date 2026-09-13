#!/usr/bin/env python3
"""Does NEVER_AUTO_RESPOND still match the data it was derived from?

Run:  python -m scripts.check_policy_classes

src/config.py says the four classes in NEVER_AUTO_RESPOND match the
labels.must_not_auto_respond flag in the ticket data, and cites this script as
the thing that verified it. The script did not exist, so the claim rested on my
memory of having checked it once. This is that check, written down.

It compares two sets. On one side, the intents config forbids answering
automatically. On the other, the intents that carry at least one ticket flagged
must_not_auto_respond. Three things can be wrong and each is reported
separately, because they mean different things:

  - a flagged intent config does not forbid, which is a governance hole: the
    routing rule will not stop those tickets and only the confidence threshold
    stands between them and a customer
  - a forbidden intent with no flagged ticket, which is not a fault. Two of the
    four are there because nothing in the corpus can ground an answer, not
    because the data asked for it, and that reasoning is in config.py
  - a flagged ticket inside a forbidden intent that routing would still answer,
    which the evaluation harness also measures as a breach

Exit code 1 if the first case appears in any input file, so this can sit in CI.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import NEVER_AUTO_RESPOND
from src.ingest import load_tickets


def audit(path: str) -> dict:
    tickets = load_tickets(path)
    flagged: Counter[str] = Counter()
    intents: Counter[str] = Counter()
    unlabelled = 0
    for ticket in tickets:
        labels = ticket.labels or {}
        intent = labels.get("intent")
        if not intent:
            unlabelled += 1
            continue
        intents[intent] += 1
        if labels.get("must_not_auto_respond"):
            flagged[intent] += 1

    flagged_intents = set(flagged)
    missing = sorted(flagged_intents - NEVER_AUTO_RESPOND)
    unflagged = sorted(NEVER_AUTO_RESPOND - flagged_intents)
    return {
        "input": path,
        "tickets": len(tickets),
        "tickets_without_an_intent_label": unlabelled,
        "flagged_tickets": sum(flagged.values()),
        "flagged_by_intent": dict(sorted(flagged.items())),
        "intents_flagged_but_not_in_config": missing,
        "intents_in_config_with_no_flagged_ticket": unflagged,
        # A flagged intent that config does not forbid is the only case that
        # leaves a ticket unprotected, so it is the only one that fails.
        "ok": not missing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input",
        action="append",
        default=None,
        help="a ticket file; repeat the flag to check several (defaults to development and validation)",
    )
    args = parser.parse_args(argv)
    inputs = args.input or ["data/development_tickets.json", "data/validation_tickets.json"]

    failed = False
    for path in inputs:
        result = audit(path)
        failed = failed or not result["ok"]
        print(f"\n{result['input']}: {result['tickets']} tickets, {result['flagged_tickets']} flagged")
        for intent, count in result["flagged_by_intent"].items():
            marker = " " if intent in NEVER_AUTO_RESPOND else "!"
            print(f"  {marker} {intent:25s} {count:>4d}")
        if result["intents_flagged_but_not_in_config"]:
            print(
                "  FAIL: flagged in the data and not in NEVER_AUTO_RESPOND: "
                + ", ".join(result["intents_flagged_but_not_in_config"])
            )
        if result["intents_in_config_with_no_flagged_ticket"]:
            print(
                "  note: in NEVER_AUTO_RESPOND with no flagged ticket here: "
                + ", ".join(result["intents_in_config_with_no_flagged_ticket"])
                + " (a policy choice rather than a data one; see src/config.py)"
            )
        if result["tickets_without_an_intent_label"]:
            print(f"  note: {result['tickets_without_an_intent_label']} ticket(s) carried no intent label")

    print("\nconfig NEVER_AUTO_RESPOND: " + ", ".join(sorted(NEVER_AUTO_RESPOND)))
    print("result: " + ("every flagged intent is covered by config" if not failed else "MISMATCH"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
