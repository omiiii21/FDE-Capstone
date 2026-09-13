"""The operator console: one page that shows what happened to one ticket.

The API is the integration surface and the harness is what the acceptance
criteria are measured against. Neither of them is watchable. Marcus needs to see
why a ticket was escalated without reading JSON, Daniel asked for the system to
show its working, and a video of `curl | jq` persuades nobody. So this serves a
single page that drives the endpoints that already exist.

Three things are deliberate.

The console calls `/tickets` and `/decisions/{ticket_id}` directly rather than
through a convenience endpoint of its own. Those are the routes an integrator
would use, and a demonstration that quietly runs on a private path is not a
demonstration of the thing being integrated. What is added here is only what the
page cannot get from the existing API: the curated ticket list, the corpus behind
a citation, and the headline figures.

Nothing here computes a metric. `/api/stats` reads the two metrics reports the
harness wrote and passes the numbers through. A figure on the screen that does
not exist in a file on disk is a figure nobody can check.

The page is read from disk on each request. It is around forty kilobytes and this
is a demonstration surface, so the cost is irrelevant next to being able to edit
the file and reload the browser without restarting the server.

Serves no requirement on its own. It is how FR-11, FR-13 and FR-16 get looked at.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from .config import REPO_ROOT, settings
from .retrieve import chunk_article

router = APIRouter()

CONSOLE_HTML = REPO_ROOT / "static" / "console.html"
DEV_METRICS = REPO_ROOT / "evaluation" / "results" / "dev_run" / "metrics.json"
VALIDATION_METRICS = REPO_ROOT / "evaluation" / "results" / "validation_run" / "metrics.json"

# Where each curated ticket comes from, and how the page labels the source.
TICKET_FILES: tuple[tuple[str, Path], ...] = (
    ("validation set", REPO_ROOT / "data" / "validation_tickets.json"),
    ("guardrail probe file", REPO_ROOT / "data" / "guardrail_probe_tickets.json"),
)

# Eight tickets, chosen because between them they fire six of the seven routing
# rules and put every guardrail state on the screen at least once. The rule
# against each one is what it actually does, not what I would like it to do -
# routing is deterministic, so if one of these ever changes it is a regression
# and the console will say so by disagreeing with itself.
SAMPLES: tuple[dict[str, str], ...] = (
    {
        "ticket_id": "VAL-0011",
        "expected_rule": "R-05-auto-respond",
        "demonstrates": (
            "Answered from the knowledge base, with one citation that resolves to a real article."
        ),
    },
    {
        "ticket_id": "VAL-0043",
        "expected_rule": "R-01-policy-class",
        "demonstrates": (
            "A security incident. Never answered automatically, however confident the model is."
        ),
    },
    {
        "ticket_id": "PROBE-INJECT-02",
        "expected_rule": "R-01b-injection",
        "demonstrates": (
            "Ticket text written to look like an instruction to the system rather than a question "
            "for it. Held back for a person."
        ),
    },
    {
        "ticket_id": "PROBE-NOGROUND-01",
        "expected_rule": "R-04-below-threshold",
        "demonstrates": (
            "Nothing in the 29 articles covers this. One weak passage comes back and the confidence "
            "falls well under the bar."
        ),
    },
    {
        "ticket_id": "VAL-0032",
        "expected_rule": "R-03-high-cost-class",
        "demonstrates": (
            "A class where a wrong answer costs more than a slow one, so it is held to the higher "
            "confidence bar and misses it."
        ),
    },
    {
        "ticket_id": "PROBE-PII-01",
        "expected_rule": "R-04-below-threshold",
        "demonstrates": (
            "The customer's own email address, account reference and card number. The private data "
            "check records what it found; the handover note still reaches an engineer."
        ),
    },
    {
        "ticket_id": "VAL-0028",
        "expected_rule": "R-05-auto-respond",
        "demonstrates": (
            "Written by someone who is not a fluent English speaker, and answered anyway. The "
            "vocabulary bridge is doing the work here."
        ),
    },
    {
        "ticket_id": "VAL-0046",
        "expected_rule": "R-04-below-threshold",
        "demonstrates": (
            "Understood well enough to guess and not well enough to answer. This is the threshold "
            "doing its job rather than a policy class."
        ),
    },
)

_cache: dict[str, Any] = {}


def _load_json(path: Path) -> Any:
    """Read and keep. These files do not change while the server is running."""
    key = str(path)
    if key not in _cache:
        _cache[key] = json.loads(path.read_text(encoding="utf-8"))
    return _cache[key]


def _corpus() -> dict[str, dict[str, Any]]:
    if "corpus" not in _cache:
        docs = _load_json(settings.corpus_path)
        _cache["corpus"] = {doc["doc_id"]: doc for doc in docs}
    return _cache["corpus"]


def _tickets() -> dict[str, tuple[str, dict[str, Any]]]:
    if "tickets" not in _cache:
        index: dict[str, tuple[str, dict[str, Any]]] = {}
        for label, path in TICKET_FILES:
            for record in _load_json(path):
                index[record["ticket_id"]] = (label, record)
        _cache["tickets"] = index
    return _cache["tickets"]


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def console_page() -> HTMLResponse:
    """The console itself. One file, no build step, no package manager."""
    try:
        return HTMLResponse(CONSOLE_HTML.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"the console page could not be read: {exc}") from exc


@router.get("/api/samples")
def samples() -> dict[str, Any]:
    """The curated tickets, each labelled with what it is there to show.

    The whole record goes back, including the labels block where there is one,
    because the console posts it to /tickets unchanged and that is the ticket as
    it arrived. The pipeline never reads the labels; only the harness does, and
    only after the fact.
    """
    index = _tickets()
    out = []
    for sample in SAMPLES:
        found = index.get(sample["ticket_id"])
        if found is None:  # a ticket file was edited; say so rather than 500
            continue
        source, record = found
        out.append(
            {
                "ticket_id": record["ticket_id"],
                "source": source,
                "demonstrates": sample["demonstrates"],
                "expected_rule": sample["expected_rule"],
                "channel": record.get("channel", ""),
                "customer_tier": record.get("customer_tier", ""),
                "language_fluency": record.get("language_fluency", ""),
                "subject": record.get("subject", ""),
                "labels": record.get("labels") or None,
                "probe": record.get("probe", ""),
                "ticket": record,
            }
        )
    return {"count": len(out), "samples": out}


@router.get("/api/corpus/{doc_id}")
def corpus_article(doc_id: str) -> dict[str, Any]:
    """One knowledge base article, plus the chunks it was split into.

    The chunks are here so that clicking a citation shows the passage that was
    actually retrieved rather than the whole article with the reader left to
    find it. They are produced by the same chunk_article() the retriever uses,
    so the chunk_id on a citation resolves against this list exactly.
    """
    doc = _corpus().get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no such article: {doc_id}")
    return {
        "doc_id": doc["doc_id"],
        "title": doc.get("title", ""),
        "category": doc.get("category", ""),
        "applies_to": doc.get("applies_to", ""),
        "related_docs": doc.get("related_docs", []),
        "last_reviewed_days_ago": doc.get("last_reviewed_days_ago"),
        "content": doc.get("content", ""),
        "chunks": [
            {"chunk_id": c.chunk_id, "section": c.section, "text": c.text} for c in chunk_article(doc)
        ],
    }


def _first(block: dict[str, Any], *names: str) -> Any:
    """The first of these keys the report actually carries.

    The metrics report has been through two naming rounds: `citation_accuracy`
    became `citation_accuracy_over_all_labelled_replies` once there were two
    bases to tell apart. Reading both means a report written by either version
    of the harness still fills the console rather than leaving a blank where a
    measured figure should be.
    """
    for name in names:
        value = block.get(name)
        if value is not None:
            return value
    return None


def _run_summary(path: Path) -> dict[str, Any]:
    """One metrics report, flattened to the figures the console shows.

    Everything is read out of the file. A missing report says so rather than
    being filled in from memory, because a number on a screen that nobody can
    trace back to a run is worse than a blank.
    """
    try:
        report = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "path": str(path.relative_to(REPO_ROOT)), "error": str(exc)}

    run = report.get("run", {})
    volume = report.get("volume", {})
    business = report.get("business", {})
    technical = report.get("technical", {})
    retrieval = technical.get("retrieval", {})
    calibration = technical.get("calibration", {})
    latency = technical.get("latency", {})
    governance = report.get("governance", {})

    return {
        "available": True,
        "path": str(path.relative_to(REPO_ROOT)),
        "run_id": run.get("run_id"),
        "input_path": run.get("input_path"),
        "duration_seconds": run.get("duration_seconds"),
        "model_provider": run.get("model_provider"),
        "tickets_processed": volume.get("tickets_processed"),
        "answered_automatically": volume.get("answered_automatically"),
        "escalated": volume.get("escalated"),
        "blocked_by_guardrails": volume.get("blocked_by_guardrails"),
        "processing_errors": volume.get("processing_errors"),
        "first_contact_resolution": business.get("first_contact_resolution"),
        "verified_fcr_over_all_labelled_auto_answers": _first(
            business,
            "verified_fcr_over_all_labelled_auto_answers",
            "first_contact_resolution_verified",
        ),
        "verified_fcr_over_answerable_auto_answers_only": _first(
            business,
            "verified_fcr_over_answerable_auto_answers_only",
            "first_contact_resolution_verified_answerable_only",
        ),
        "baseline_first_contact_resolution": business.get("baseline_first_contact_resolution"),
        "escalation_rate": business.get("escalation_rate"),
        "baseline_escalation_rate": business.get("baseline_escalation_rate"),
        "routing_agreement_with_labels": business.get("routing_agreement_with_labels"),
        "citation_accuracy_over_all_labelled_replies": _first(
            retrieval,
            "citation_accuracy_over_all_labelled_replies",
            "citation_accuracy",
        ),
        "citation_accuracy_over_all_labelled_replies_basis": _first(
            retrieval,
            "citation_accuracy_over_all_labelled_replies_basis",
            "citation_accuracy_basis",
        ),
        "citation_accuracy_over_answerable_replies_only": retrieval.get(
            "citation_accuracy_over_answerable_replies_only"
        ),
        "citation_accuracy_over_answerable_replies_only_basis": retrieval.get(
            "citation_accuracy_over_answerable_replies_only_basis"
        ),
        "responses_with_citations": retrieval.get("responses_with_citations"),
        "retrieval_recall_at_k": retrieval.get("recall_at_k"),
        "expected_calibration_error": calibration.get("expected_calibration_error"),
        "worst_band_gap_points": calibration.get("worst_band_gap_points"),
        "p95_latency_seconds": latency.get("p95_seconds"),
        "decisions_logged": governance.get("decisions_logged"),
        "tickets_with_decisions": governance.get("tickets_with_decisions"),
        "decision_log_reconciles": governance.get("decision_log_reconciles"),
        "private_data_detections": governance.get("private_data_detections"),
        "must_not_auto_respond_tickets": governance.get("must_not_auto_respond_tickets"),
        "must_not_auto_respond_breaches": governance.get("must_not_auto_respond_breaches"),
    }


@router.get("/api/stats")
def stats() -> dict[str, Any]:
    """The headline figures, read from the two reports the harness wrote.

    `live` is separate on purpose: it is what this process is configured with
    right now, which is not necessarily what the recorded runs used.
    """
    development = _run_summary(DEV_METRICS)
    validation = _run_summary(VALIDATION_METRICS)
    recorded = _load_json(DEV_METRICS).get("run", {}) if development["available"] else {}
    return {
        "development": development,
        "validation": validation,
        "corpus": recorded.get("retrieval", {}),
        "live": {
            "confidence_threshold": settings.confidence_threshold,
            "high_cost_threshold": settings.high_cost_threshold,
            "retrieval_top_k": settings.retrieval_top_k,
            "retrieval_floor": settings.retrieval_floor,
            "retrieval_backend": settings.retrieval_backend,
        },
    }
