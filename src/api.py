"""HTTP surface for the triage system.

This is the demonstration and integration surface. It is how you show the
system handling one ticket, how an auditor pulls the decision trail for a
ticket_id, and how an operator reaches the kill switch without a shell on the
box. It is not what the acceptance gate measures - that is the evaluation
harness, which runs the whole validation set offline and writes the metrics
report. Nothing in here is on the path the gate tests, and nothing in here
should ever be the only way to run the system.

Three decisions worth stating:

- The pipeline is built once, at startup. It loads the classifier artefact and
  indexes the corpus, which is a few hundred milliseconds, and paying that per
  request would make the API look slower than the system is.
- The endpoints are plain sync functions, so FastAPI runs them in its
  threadpool. The pipeline is blocking and CPU-bound; on the event loop it
  would serialise every other request behind whichever ticket is in flight.
- /healthz returns 200 even when the model provider is unreachable. Liveness is
  not readiness, and a health check that fails during an outage would pull the
  service out of rotation while it is still answering tickets through the
  extractive path (A11).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, metrics
from .config import settings
from .console import router as console_router
from .ingest import IngestError, normalise_ticket
from .logging_store import DecisionLog
from .pipeline import Pipeline
from .schema import new_id, utcnow

log = logging.getLogger(__name__)

# Built in the lifespan handler, read by the endpoints. Module-level rather
# than app.state because the harness and the tests import them directly.
_pipeline: Pipeline | None = None
_decision_log: DecisionLog | None = None
_startup_error: str = ""
_metrics_serving: bool = False
RUN_ID = new_id("API")

# Columns the decision log stores as JSON text. Decoded on the way out so an
# auditor reading /decisions gets a document rather than escaped strings.
_JSON_COLUMNS = ("alternatives", "sources_used", "guardrail_results", "requirement_ids")


class TicketRequest(BaseModel):
    """One raw ticket, the same shape as a record in data/*.json.

    Unknown fields are kept rather than rejected: ingest preserves the original
    payload on the Ticket, and a source system that sends an extra field should
    not get a 422 for it.
    """

    model_config = ConfigDict(extra="allow")

    ticket_id: str
    channel: str = "email"
    subject: str = ""
    body: str = ""
    received_at: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_tier: str = "standard"
    customer_region: str = ""
    language_fluency: str = "fluent"
    labels: dict[str, Any] = Field(default_factory=dict)


def build_pipeline() -> Pipeline:
    global _decision_log
    _decision_log = DecisionLog(run_id=RUN_ID)
    return Pipeline(decision_log=_decision_log, threshold=settings.confidence_threshold)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _pipeline, _startup_error, _metrics_serving
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
    try:
        _pipeline = build_pipeline()
    except Exception as exc:  # noqa: BLE001 - a missing corpus must not stop /healthz answering
        _startup_error = f"{type(exc).__name__}: {exc}"
        log.exception("pipeline could not be built; the API will serve health only")
    _metrics_serving = metrics.start_metrics_server(settings.metrics_port)
    yield
    if _decision_log is not None:
        _decision_log.close()


app = FastAPI(
    title="CloudServe support triage",
    version=__version__,
    description="Demonstration and integration surface for the triage pipeline.",
    lifespan=lifespan,
)

# The operator console. It is a page and three read-only routes that drive the
# endpoints below rather than replacing them, so what it shows is what an
# integrator would get. See src/console.py.
app.include_router(console_router)


def _require_pipeline() -> Pipeline:
    if _pipeline is None:
        raise HTTPException(
            status_code=503,
            detail=f"the pipeline is not available: {_startup_error or 'not started'}",
        )
    return _pipeline


def _require_admin(token: str | None) -> None:
    """Shared secret on the admin routes.

    An unset ADMIN_TOKEN refuses rather than defaults to open. A kill switch
    that anyone who finds the port can flip is worse than no kill switch,
    because it looks like a control.
    """
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_TOKEN is not set, so the admin endpoints are disabled",
        )
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="missing or invalid X-Admin-Token")


@app.post("/tickets")
def process_ticket(payload: TicketRequest) -> dict[str, Any]:
    """Run one ticket through the pipeline and return what the system decided."""
    pipeline = _require_pipeline()
    try:
        ticket = normalise_ticket(payload.model_dump())
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = pipeline.process(ticket)
    metrics.record_response(ticket, response)
    return response.to_dict()


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness, plus enough detail to tell a degraded system from a broken one.

    Always 200. The degraded_because list is the part worth reading.
    """
    degraded: list[str] = []
    provider = {"name": "none", "available": False, "version": ""}
    corpus: dict[str, Any] = {}
    classifier_loaded = False

    if _pipeline is None:
        degraded.append(f"pipeline unavailable: {_startup_error or 'not started'}")
    else:
        try:
            provider = {
                "name": _pipeline.provider.name,
                "available": bool(_pipeline.provider.available),
                "version": _pipeline.provider.version,
            }
        except Exception as exc:  # noqa: BLE001 - a provider probe must not fail the check
            provider = {"name": "unknown", "available": False, "version": "", "error": str(exc)}
        corpus = dict(_pipeline.retriever.stats)
        classifier_loaded = _pipeline.classifier is not None
        if not provider["available"]:
            degraded.append("model provider unavailable; answers come from the extractive path")
        if not classifier_loaded:
            degraded.append("classifier artefact not loaded; every ticket will escalate")

    halted = metrics.refresh_kill_switch()
    if halted:
        degraded.append("automatic responses halted by an operator")

    return {
        "status": "degraded" if degraded else "ok",
        "degraded_because": degraded,
        "version": __version__,
        "run_id": RUN_ID,
        "provider": provider,
        "corpus": corpus,
        "classifier_loaded": classifier_loaded,
        "kill_switch": {"engaged": halted, "path": str(settings.kill_switch_path)},
        "confidence_threshold": settings.confidence_threshold,
        "metrics": {"serving": _metrics_serving, "port": settings.metrics_port},
        "checked_at": utcnow(),
    }


@app.get("/decisions/{ticket_id}")
def decisions_for_ticket(ticket_id: str) -> dict[str, Any]:
    """Every logged decision for one ticket, in the order they were taken.

    This is the reconstruction path: classification, retrieval, routing,
    generation and validation each wrote a row, and together they are why the
    system did what it did.
    """
    if _decision_log is None:
        raise HTTPException(status_code=503, detail="the decision log is not available")

    rows = _decision_log.for_ticket(ticket_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"no decisions logged for {ticket_id}")

    return {
        "ticket_id": ticket_id,
        "stages": [row["stage"] for row in rows],
        "decisions": [_decode_row(row) for row in rows],
    }


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for column in _JSON_COLUMNS:
        try:
            decoded[column] = json.loads(decoded.get(column) or "null")
        except (TypeError, json.JSONDecodeError):
            pass  # leave the raw text; a row that will not parse is still evidence
    return decoded


@app.post("/admin/halt")
def halt(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    """Stop auto-responding. Everything routes to a human until resumed."""
    _require_admin(x_admin_token)
    path = settings.kill_switch_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"halted at {utcnow()} via POST /admin/halt\n", encoding="utf-8")
    metrics.refresh_kill_switch()
    return {
        "halted": True,
        "kill_switch_path": str(path),
        "effective_from": "next ticket",
        "detail": (
            "Routing reads this file once per ticket, so the next ticket accepted escalates. "
            "A ticket already in flight finishes under the previous setting."
        ),
    }


@app.post("/admin/resume")
def resume(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> dict[str, Any]:
    """Allow auto-responding again."""
    _require_admin(x_admin_token)
    path = settings.kill_switch_path
    path.unlink(missing_ok=True)
    metrics.refresh_kill_switch()
    return {
        "halted": False,
        "kill_switch_path": str(path),
        "effective_from": "next ticket",
        "detail": "The next ticket accepted is eligible for an automatic response again.",
    }
