"""Prometheus instrumentation, and what happens when prometheus_client is absent.

Four series, from the setup guide: what arrived and what happened to it, how
long it took, what the guardrails stopped, and the distribution of classifier
confidence. The last one is the reason this module is worth having. The
counters tell you the system is alive; the confidence histogram is where drift
shows up first, sliding left for a week or two before the escalation rate moves
far enough for anybody to notice it on a dashboard.

Metrics are served on their own port (METRICS_PORT) rather than as a route on
the API, so scraping does not depend on the application being healthy and the
endpoint is not sitting next to the customer-facing routes.

If prometheus_client is not installed every collector becomes a no-op. Nothing
above this module needs to know, and nothing above it should be wrapping
record_response() in a try/except.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import settings
from .schema import Response, Ticket

log = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the no-dependency install
    PROMETHEUS_AVAILABLE = False


class _NoOp:
    """Stands in for a collector when the client library is not installed.

    It accepts every call the real collectors accept and does nothing with any
    of them, which keeps the absence of prometheus_client a deployment detail
    rather than a branch in the pipeline.
    """

    def labels(self, *args: Any, **kwargs: Any) -> "_NoOp":
        return self

    def inc(self, amount: float = 1.0) -> None:
        pass

    def observe(self, amount: float) -> None:
        pass

    def set(self, value: float) -> None:
        pass


def _counter(name: str, documentation: str, labelnames: tuple[str, ...] = ()) -> Any:
    if not PROMETHEUS_AVAILABLE:
        return _NoOp()
    return Counter(name, documentation, labelnames)


def _histogram(name: str, documentation: str, buckets: tuple[float, ...]) -> Any:
    if not PROMETHEUS_AVAILABLE:
        return _NoOp()
    return Histogram(name, documentation, buckets=buckets)


def _gauge(name: str, documentation: str) -> Any:
    if not PROMETHEUS_AVAILABLE:
        return _NoOp()
    return Gauge(name, documentation)


# Both labels are bounded - four channels, three outcomes - so this is twelve
# series at most. Ticket id or intent as a label would be unbounded and is the
# usual way a Prometheus instance falls over.
TICKETS_PROCESSED = _counter(
    "tickets_processed_total",
    "Tickets processed, by arrival channel and what the system decided to do",
    ("channel", "outcome"),
)

# Tighter below a second because the chat path has somebody waiting on the
# other end; above five seconds the exact number stops mattering.
RESPONSE_SECONDS = _histogram(
    "response_seconds",
    "Wall-clock seconds from ticket accepted to response returned",
    (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0),
)

GUARDRAIL_BLOCKS = _counter(
    "guardrail_blocks_total",
    "Guardrail failures that stopped a response being sent, by guardrail",
    ("guardrail",),
)

# Ten even buckets: the shape matters more than the precision, and a shift in
# the shape is the earliest signal that the corpus or the traffic has moved.
CLASSIFICATION_CONFIDENCE = _histogram(
    "classification_confidence",
    "Calibrated intent confidence, whatever the routing decision was",
    (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

KILL_SWITCH_ENGAGED = _gauge(
    "kill_switch_engaged",
    "1 when an operator has halted automatic responses, 0 otherwise",
)


def refresh_kill_switch() -> bool:
    """Re-read the kill switch file and publish its state. Returns the state."""
    halted = settings.auto_responses_halted()
    KILL_SWITCH_ENGAGED.set(1 if halted else 0)
    return halted


def record_response(ticket: Ticket, response: Response) -> None:
    """Record one processed ticket. Safe to call on any Response, including the
    error path, where classification and guardrails are both missing."""
    TICKETS_PROCESSED.labels(channel=ticket.channel, outcome=response.action).inc()
    RESPONSE_SECONDS.observe(response.latency_seconds)

    if response.classification is not None:
        CLASSIFICATION_CONFIDENCE.observe(response.classification.intent_confidence)

    # Counted per guardrail rather than per blocked response, because a
    # response stopped by two checks is two different things going wrong.
    for guardrail in response.guardrails:
        if guardrail.blocked:
            GUARDRAIL_BLOCKS.labels(guardrail=guardrail.name).inc()

    refresh_kill_switch()


def start_metrics_server(port: int | None = None) -> bool:
    """Start the scrape endpoint. Returns whether it is actually listening.

    Never raises. A metrics port already in use, or a missing client library,
    is not a reason to stop serving tickets - it is a reason to log loudly and
    carry on blind.
    """
    port = settings.metrics_port if port is None else port
    if not PROMETHEUS_AVAILABLE:
        log.warning("prometheus_client is not installed; metrics are being discarded")
        return False
    try:
        start_http_server(port)
    except OSError as exc:
        log.warning("could not start the metrics server on port %s: %s", port, exc)
        return False
    refresh_kill_switch()
    log.info("metrics available on port %s", port)
    return True
