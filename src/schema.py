"""The internal representations that flow between components.

One normalised Ticket regardless of which of the four channels it arrived on
(FR-01), and one Decision record per stage that can be reconstructed months
later (FR-11, NFR-05).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Ticket:
    """A support ticket after normalisation. Channel is kept because it changes
    what a good answer looks like, not just where the answer is delivered."""

    ticket_id: str
    channel: str
    subject: str
    body: str
    received_at: str
    customer_id: str = ""
    customer_name: str = ""
    customer_tier: str = "standard"
    customer_region: str = ""
    language_fluency: str = "fluent"
    # Original payload, kept verbatim. Ingest must not be lossy - if a
    # downstream component wants a field I did not normalise, it is still here.
    raw: dict[str, Any] = field(default_factory=dict)
    # Populated only when the input file carries labels. The pipeline never
    # reads these; the harness does, after the fact.
    labels: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Subject and body as one string. Chat tickets have no subject."""
        parts = [self.subject.strip(), self.body.strip()]
        return "\n\n".join(p for p in parts if p)

    @property
    def is_realtime(self) -> bool:
        """Chat customers leave if nothing happens. Affects the latency budget."""
        return self.channel == "chat"


@dataclass
class Passage:
    """A retrieved chunk. doc_id resolves to a real article in the corpus -
    that is checked in tests, because a citation that does not resolve is worse
    than no citation at all (A6)."""

    doc_id: str
    title: str
    category: str
    chunk_id: str
    text: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "title": self.title,
            "score": round(self.score, 4),
        }


@dataclass
class Classification:
    intent: str
    intent_confidence: float
    urgency: str
    urgency_confidence: float
    # Everything the classifier considered, not only what it picked. Without
    # this you cannot tell a confident call from a coin flip after the fact.
    alternatives: list[tuple[str, float]] = field(default_factory=list)
    method: str = "linear"


@dataclass
class RoutingDecision:
    action: str  # auto_respond | escalate
    reason: str  # written for a support manager, not for me
    rule: str  # the identifier of the rule that fired
    threshold_applied: float = 0.0


@dataclass
class GuardrailResult:
    name: str
    passed: bool
    detail: str = ""
    # A guardrail that only warns is not a guardrail (Build Spec, section 3).
    blocking: bool = True

    @property
    def blocked(self) -> bool:
        return self.blocking and not self.passed


@dataclass
class Response:
    """What the system produced for one ticket, whatever happened to it."""

    ticket_id: str
    action: str  # auto_respond | escalate | blocked
    body: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    classification: Classification | None = None
    routing: RoutingDecision | None = None
    guardrails: list[GuardrailResult] = field(default_factory=list)
    passages: list[Passage] = field(default_factory=list)
    escalation_note: str = ""
    latency_seconds: float = 0.0
    generation_method: str = ""
    error: str = ""

    @property
    def blocked_by(self) -> list[str]:
        return [g.name for g in self.guardrails if g.blocked]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["passages"] = [p.to_dict() for p in self.passages]
        out["blocked_by"] = self.blocked_by
        return out


@dataclass
class DecisionRecord:
    """The minimum record from the governance framework, section 1.

    prompt_version and requirement_ids are on here deliberately: they are what
    let you answer "was this behaviour intended" after something goes wrong,
    rather than "was this behaviour possible".
    """

    decision_id: str
    timestamp: str
    ticket_id: str
    stage: str  # ingest | classification | retrieval | routing | generation | validation
    input_summary: str
    model_name: str
    model_version: str
    prediction: str
    confidence: float
    alternatives: list[dict[str, Any]]
    sources_used: list[dict[str, Any]]
    threshold_applied: float
    action_taken: str
    reason: str
    guardrail_results: dict[str, str]
    prompt_version: str
    requirement_ids: list[str]

    @classmethod
    def create(cls, ticket_id: str, stage: str, **kw: Any) -> "DecisionRecord":
        return cls(
            decision_id=new_id("DEC"),
            timestamp=utcnow(),
            ticket_id=ticket_id,
            stage=stage,
            input_summary=kw.pop("input_summary", ""),
            model_name=kw.pop("model_name", ""),
            model_version=kw.pop("model_version", ""),
            prediction=kw.pop("prediction", ""),
            confidence=float(kw.pop("confidence", 0.0)),
            alternatives=kw.pop("alternatives", []),
            sources_used=kw.pop("sources_used", []),
            threshold_applied=float(kw.pop("threshold_applied", 0.0)),
            action_taken=kw.pop("action_taken", ""),
            reason=kw.pop("reason", ""),
            guardrail_results=kw.pop("guardrail_results", {}),
            prompt_version=kw.pop("prompt_version", ""),
            requirement_ids=kw.pop("requirement_ids", []),
        )
