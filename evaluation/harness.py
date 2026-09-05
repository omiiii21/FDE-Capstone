#!/usr/bin/env python3
"""The unattended evaluation run.

    python -m evaluation.harness --input data/validation_tickets.json \
                                 --output evaluation/results/

This is the component the project is gated on (A9, A10), so it is written with
one assumption: it will be pointed at a file nobody here has seen, on a machine
nobody here owns, and nobody will be watching it.

That assumption produces the rules this module follows:

  - the input and output paths are arguments, never constants
  - every ticket produces a row, including the ones that fail; a ticket that
    raises is recorded as an escalation with the error attached rather than
    skipped, because a silently dropped ticket is worse than a failed one
  - nothing waits for input, and nothing needs a key; with no provider
    configured the pipeline uses its extractive generator and the run completes
  - the metrics report is written by this code, not calculated afterwards
  - partial results are flushed as the run proceeds, so an interrupted run
    still leaves evidence of how far it got

Exit codes: 0 if the run completed, 1 if it could not start, 2 if it completed
but the decision log did not reconcile. The last one exists so that CI notices
a reconciliation gap rather than a human having to spot it.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluation.metrics import (  # noqa: E402
    business_metrics,
    calibration_table,
    classification_metrics,
    fairness_segments,
    governance_metrics,
    latency_metrics,
    retrieval_metrics,
    volume_metrics,
)
from src import __version__  # noqa: E402
from src.config import settings  # noqa: E402
from src.ingest import load_tickets  # noqa: E402
from src.logging_store import DecisionLog  # noqa: E402
from src.pipeline import Pipeline  # noqa: E402
from src.schema import Response, Ticket  # noqa: E402

log = logging.getLogger("harness")


def build_row(ticket: Ticket, response: Response, retriever_docs: set[str]) -> dict[str, Any]:
    """One flat record per ticket: what came in, what the system did, and the
    label if the file carried one. The metrics functions read only this."""
    labels = ticket.labels or {}
    cited = [c["doc_id"] for c in response.citations]
    retrieved = [p.doc_id for p in response.passages]
    return {
        "ticket_id": ticket.ticket_id,
        "channel": ticket.channel,
        "customer_tier": ticket.customer_tier,
        "customer_region": ticket.customer_region,
        "language_fluency": ticket.language_fluency,
        "body_length": len(ticket.body),
        "action": response.action,
        "predicted_intent": response.classification.intent if response.classification else "",
        "confidence": response.classification.intent_confidence if response.classification else 0.0,
        "predicted_urgency": response.classification.urgency if response.classification else "",
        "routing_rule": response.routing.rule if response.routing else "",
        "routing_reason": response.routing.reason if response.routing else "",
        "retrieved_doc_ids": retrieved,
        "cited_doc_ids": cited,
        # A6 is checked by following the citation, so the harness checks it too
        # rather than trusting the pipeline to have done so.
        "citations_resolve": all(d in retriever_docs for d in cited),
        "guardrails_failed": [g.name for g in response.guardrails if not g.passed],
        "guardrails_blocked": response.blocked_by,
        "latency_seconds": response.latency_seconds,
        "generation_method": response.generation_method,
        "response_chars": len(response.body),
        "error": response.error,
        # labels, present only when the input file carries them
        "true_intent": labels.get("intent"),
        "true_urgency": labels.get("urgency"),
        "expected_route": labels.get("expected_route"),
        "expected_doc_ids": labels.get("expected_doc_ids"),
        "answerable_from_docs": labels.get("answerable_from_docs"),
        "must_not_auto_respond": labels.get("must_not_auto_respond"),
    }


def run(
    input_path: Path,
    output_dir: Path,
    *,
    limit: int | None = None,
    run_id: str | None = None,
    threshold: float | None = None,
    progress_every: int = 10,
) -> tuple[dict[str, Any], int]:
    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    run_id = run_id or f"run-{started_wall.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    output_dir.mkdir(parents=True, exist_ok=True)

    tickets = load_tickets(input_path)
    if limit:
        tickets = tickets[:limit]
    log.info("loaded %d tickets from %s", len(tickets), input_path)

    decision_log = DecisionLog(output_dir / "decisions.db", run_id=run_id)
    pipeline = Pipeline(decision_log=decision_log, threshold=threshold)
    retriever_docs = set(pipeline.retriever.documents)

    rows: list[dict[str, Any]] = []
    responses_path = output_dir / "responses.jsonl"
    failures = 0

    with responses_path.open("w", encoding="utf-8") as handle:
        for index, ticket in enumerate(tickets, start=1):
            try:
                response = pipeline.process(ticket)
            except Exception as exc:  # noqa: BLE001
                # Pipeline.process already catches everything; this is the
                # belt-and-braces layer. If it ever fires, the run continues.
                failures += 1
                log.error("ticket %s could not be processed: %s", ticket.ticket_id, exc)
                response = Response(
                    ticket_id=ticket.ticket_id,
                    action="escalate",
                    body="",
                    escalation_note="Harness-level failure; this ticket needs a human.",
                    error=f"{type(exc).__name__}: {exc}",
                )
            row = build_row(ticket, response, retriever_docs)
            rows.append(row)
            handle.write(
                json.dumps(
                    {**row, "response_body": response.body, "escalation_note": response.escalation_note}
                )
                + "\n"
            )
            handle.flush()  # so an interrupted run still leaves usable evidence
            if progress_every and index % progress_every == 0:
                elapsed = time.perf_counter() - started
                log.info(
                    "%d/%d processed (%.1f tickets/sec)", index, len(tickets), index / max(elapsed, 1e-6)
                )

    duration = time.perf_counter() - started
    reconciliation = decision_log.reconcile(len(rows), run_id=run_id)
    decision_log.export_jsonl(output_dir / "decision_log.jsonl", run_id=run_id)

    report = {
        "run": {
            "run_id": run_id,
            "started_at": started_wall.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "duration_seconds": round(duration, 2),
            "tickets_per_second": round(len(rows) / duration, 2) if duration else 0.0,
            "input_path": str(input_path),
            "output_dir": str(output_dir),
            "system_version": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "model_provider": pipeline.provider.name,
            "provider_available": pipeline.provider.available,
            "provider_model": pipeline.provider.version,
            "retrieval": pipeline.retriever.stats,
            "confidence_threshold": pipeline.threshold,
            "high_cost_threshold": settings.high_cost_threshold,
            "harness_level_failures": failures,
            "unattended": True,
        },
        "volume": volume_metrics(rows),
        "business": business_metrics(rows),
        "technical": {
            "classification": classification_metrics(rows),
            "retrieval": retrieval_metrics(rows),
            "latency": latency_metrics(rows),
            "calibration": calibration_table(rows),
        },
        "governance": governance_metrics(rows, reconciliation),
        "fairness": fairness_segments(rows),
    }

    (output_dir / "metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (output_dir / "summary.md").write_text(render_summary(report), encoding="utf-8")
    decision_log.close()

    exit_code = 0 if reconciliation["reconciles"] else 2
    return report, exit_code


def render_summary(report: dict[str, Any]) -> str:
    """A readable version of metrics.json.

    This exists because a support manager is not going to open a JSON file, and
    because the figures I quote in the report should come from the same place
    the system wrote them.
    """
    run = report["run"]
    volume = report["volume"]
    business = report["business"]
    classification = report["technical"]["classification"]
    retrieval = report["technical"]["retrieval"]
    latency = report["technical"]["latency"]
    calibration = report["technical"]["calibration"]
    governance = report["governance"]

    lines = [
        f"# Evaluation run {run['run_id']}",
        "",
        f"Input: `{run['input_path']}`  ",
        f"Started: {run['started_at']}  ",
        f"Duration: {run['duration_seconds']}s ({run['tickets_per_second']} tickets/sec)  ",
        f"Provider: {run['model_provider']} (available: {run['provider_available']})  ",
        f"Retrieval: {run['retrieval']['backend']}, top_k={run['retrieval']['top_k']}, "
        f"floor={run['retrieval']['floor']}  ",
        f"Confidence threshold: {run['confidence_threshold']}",
        "",
        "## Volume",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Tickets processed | {volume['tickets_processed']} |",
        f"| Answered automatically | {volume['answered_automatically']} |",
        f"| Escalated | {volume['escalated']} |",
        f"| Blocked by a guardrail | {volume['blocked_by_guardrails']} |",
        f"| Processing errors | {volume['processing_errors']} |",
        "",
        "## Business outcomes",
        "",
        "| Measure | Baseline | Target | This run |",
        "| --- | --- | --- | --- |",
        f"| First contact resolution | {business['baseline_first_contact_resolution']:.1%} | 60% | "
        f"{business['first_contact_resolution']:.1%} |",
        f"| First contact resolution, verified | - | - | "
        f"{business['first_contact_resolution_verified']:.1%} "
        f"(n={business['first_contact_resolution_verified_basis']}) |",
        f"| Escalation rate | {business['baseline_escalation_rate']:.1%} | 30% | "
        f"{business['escalation_rate']:.1%} |",
        f"| Median reply, automated | {business['baseline_median_reply_minutes']:.0f} min | 5 min | "
        f"{business['automated_reply_seconds_median']:.3f} s |",
        f"| Median reply, blended | {business['baseline_median_reply_minutes']:.0f} min | 5 min | "
        f"{business['blended_reply_minutes_median']:.1f} min |",
        f"| Routing agreement with labels | - | - | {business['routing_agreement_with_labels']:.1%} |",
        "",
        f"Blended figure assumption: {business['blended_reply_assumption']}.",
        "",
        "## Technical",
        "",
        "| Measure | Target | This run |",
        "| --- | --- | --- |",
        f"| Intent accuracy | - | {classification.get('accuracy', 0):.1%} |",
        f"| Weighted precision | 85% | {classification.get('weighted_precision', 0):.1%} |",
        f"| Macro precision | - | {classification.get('macro_precision', 0):.1%} |",
        f"| Retrieval recall@k | - | {retrieval['recall_at_k']:.1%} |",
        f"| Citation accuracy | 95% | {retrieval['citation_accuracy']:.1%} "
        f"(n={retrieval['citation_accuracy_basis']}) |",
        f"| Latency p95 | 3 s | {latency['p95_seconds']:.3f} s |",
        f"| Calibration, worst band gap | 5 pts | {calibration['worst_band_gap_points']:.1f} pts |",
        "",
        "## Governance",
        "",
        "| Condition | Requirement | This run |",
        "| --- | --- | --- |",
        f"| Decisions logged | complete | {governance['decisions_logged']} across "
        f"{governance['tickets_with_decisions']} tickets |",
        f"| Log reconciles with tickets | must match | "
        f"{'yes' if governance['decision_log_reconciles'] else 'NO'} |",
        f"| Private data in outbound text | zero | {governance['private_data_detections']} |",
        f"| Never-auto-respond breaches | zero | {governance['must_not_auto_respond_breaches']} of "
        f"{governance['must_not_auto_respond_tickets']} |",
        f"| Injection attempts detected | - | {governance['injection_attempts_detected']} |",
        "",
        "Guardrail activations: "
        + (", ".join(f"{k} {v}" for k, v in sorted(governance["guardrail_activations"].items())) or "none"),
        "",
        "## Fairness",
        "",
        "| Dimension | Spread in verified resolution | Under 5 points |",
        "| --- | --- | --- |",
    ]
    for name, block in report["fairness"].items():
        spread = block["verified_resolution_spread_points"]
        spread_text = f"{spread:.1f} pts" if spread is not None else "not comparable"
        lines.append(f"| {name} | {spread_text} | {'yes' if block['within_five_points'] else 'no'} |")

    lines += [
        "",
        "Segments with fewer than ten labelled automatic answers are excluded from the spread, "
        "because one ticket moves them by more than the threshold being tested.",
        "",
        f"Generated by evaluation/harness.py, system version {run['system_version']}.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Process a ticket file end to end and write a metrics report."
    )
    parser.add_argument("--input", required=True, help="path to a ticket file in the standard schema")
    parser.add_argument("--output", required=True, help="directory to write results into")
    parser.add_argument("--limit", type=int, default=None, help="process only the first N tickets")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--threshold", type=float, default=None, help="override the confidence threshold")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    input_path = Path(args.input)
    if not input_path.exists():
        log.error("input file does not exist: %s", input_path)
        return 1

    try:
        report, exit_code = run(
            input_path,
            Path(args.output),
            limit=args.limit,
            run_id=args.run_id,
            threshold=args.threshold,
            progress_every=0 if args.quiet else 10,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("the run could not start: %s", exc)
        return 1

    if not args.quiet:
        print()
        print(render_summary(report))
    if exit_code == 2:
        log.error(
            "decision log did not reconcile: %d tickets, %d with decisions",
            report["volume"]["tickets_processed"],
            report["governance"]["tickets_with_decisions"],
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
