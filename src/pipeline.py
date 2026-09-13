"""The ticket pipeline: one ticket in, one response out, everything logged.

Ingest -> classify -> retrieve -> route -> generate -> validate.

Routing happens before generation rather than after, which is not the obvious
order. Generating first and then deciding whether to send would give the
escalation note a drafted answer to include, and it was how I built it in week
two. I changed it after measuring the cost: two thirds of escalations are
policy classes that could never have been sent, so the system was drafting
answers it had already decided to throw away. Escalations still get the
retrieved sources and a summary; they no longer get a wasted model call.

The one guarantee this module makes is that every ticket produces a Response.
There is no path that raises, and no path that returns None. A ticket that
breaks something still comes out as an escalation with the error recorded,
because A9 requires that none are silently dropped and a crash on ticket forty
is the failure mode that ends projects.
"""

from __future__ import annotations

import logging
import time
import traceback

from .classify import Classifier, ClassifierUnavailable, PROMPT_VERSION, get_classifier
from .config import settings
from .generate import ExtractiveGenerator, ModelGenerator
from .guardrails import check_injection, run_all
from .logging_store import DecisionLog
from .provider import Provider, get_provider
from .retrieve import Retriever
from .route import AUTO, ESCALATE, route
from .schema import Classification, DecisionRecord, GuardrailResult, Response, Ticket

log = logging.getLogger(__name__)

# Which requirement each stage implements. Carried into the decision log so the
# traceability chain is visible from a log row rather than only from the PRD.
STAGE_REQUIREMENTS = {
    "classification": ["FR-02", "FR-03"],
    "retrieval": ["FR-04", "FR-05"],
    "routing": ["FR-05", "FR-13", "FR-16"],
    "generation": ["FR-06", "FR-07", "FR-08", "FR-09", "FR-14", "FR-15"],
    "validation": ["FR-10", "NFR-04"],
}


class Pipeline:
    def __init__(
        self,
        *,
        classifier: Classifier | None = None,
        retriever: Retriever | None = None,
        provider: Provider | None = None,
        decision_log: DecisionLog | None = None,
        threshold: float | None = None,
    ):
        self.provider = provider or get_provider()
        self.retriever = retriever or Retriever()
        self.threshold = settings.confidence_threshold if threshold is None else threshold
        self.decision_log = decision_log
        try:
            self.classifier = classifier or get_classifier()
        except ClassifierUnavailable:
            # Without the artefact the system cannot classify, and everything
            # escalating is the correct degraded behaviour rather than a crash.
            log.error("classifier artefact missing; every ticket will escalate")
            self.classifier = None
        self.generator = ModelGenerator(self.provider, retriever=self.retriever)
        self.extractive = ExtractiveGenerator(self.retriever)

    # -- main entry point ------------------------------------------------

    def process(self, ticket: Ticket) -> Response:
        started = time.perf_counter()
        records: list[DecisionRecord] = []
        try:
            response = self._process(ticket, records)
        except Exception as exc:  # noqa: BLE001 - this is the last line of defence
            log.exception("ticket %s failed", ticket.ticket_id)
            response = Response(
                ticket_id=ticket.ticket_id,
                action=ESCALATE,
                body="",
                escalation_note=(
                    "This ticket could not be processed automatically and needs a human. "
                    f"The system failed during processing: {type(exc).__name__}: {exc}"
                ),
                error=f"{type(exc).__name__}: {exc}",
            )
            records.append(
                DecisionRecord.create(
                    ticket.ticket_id,
                    "routing",
                    prediction="error",
                    action_taken=ESCALATE,
                    reason=f"Processing failed and the ticket was escalated: {type(exc).__name__}",
                    input_summary=_summarise(ticket),
                    threshold_applied=self.threshold,
                    requirement_ids=["FR-12"],
                    guardrail_results={"traceback": traceback.format_exc(limit=3)[:900]},
                )
            )
        response.latency_seconds = round(time.perf_counter() - started, 4)

        if self.decision_log is not None:
            self.decision_log.write_many(records)
        return response

    # -- the stages ------------------------------------------------------

    def _process(self, ticket: Ticket, records: list[DecisionRecord]) -> Response:
        # 1. classify ----------------------------------------------------
        if self.classifier is None:
            classification = Classification("unclear_request", 0.0, "medium", 0.0, [], "unavailable")
        else:
            classification = self.classifier.classify(ticket)
        records.append(
            DecisionRecord.create(
                ticket.ticket_id,
                "classification",
                input_summary=_summarise(ticket),
                model_name="tfidf+multinomial-logistic",
                model_version=PROMPT_VERSION,
                prediction=classification.intent,
                confidence=classification.intent_confidence,
                alternatives=[{"value": v, "confidence": c} for v, c in classification.alternatives],
                action_taken="classified",
                reason=(
                    f"Classified as {classification.intent} at {classification.intent_confidence:.2f} "
                    f"confidence; urgency {classification.urgency} via {classification.method}."
                ),
                threshold_applied=self.threshold,
                requirement_ids=STAGE_REQUIREMENTS["classification"],
                prompt_version=PROMPT_VERSION,
            )
        )

        # 2. retrieve ----------------------------------------------------
        passages = self.retriever.search(ticket.text)
        records.append(
            DecisionRecord.create(
                ticket.ticket_id,
                "retrieval",
                input_summary=_summarise(ticket),
                model_name=f"retrieval:{self.retriever.backend}",
                model_version=f"top_k={self.retriever.top_k},floor={self.retriever.floor}",
                prediction=f"{len(passages)} passages",
                confidence=passages[0].score if passages else 0.0,
                sources_used=[p.to_dict() for p in passages],
                action_taken="retrieved" if passages else "no_match",
                reason=(
                    f"Retrieved {len(passages)} passage(s) above the relevance floor of "
                    f"{self.retriever.floor}."
                    if passages
                    else f"Nothing scored above the relevance floor of {self.retriever.floor}."
                ),
                threshold_applied=self.retriever.floor,
                requirement_ids=STAGE_REQUIREMENTS["retrieval"],
            )
        )

        # 2a. injection is checked on the way in, not on the way out ------
        # A ticket trying to reprogram the system is recorded whether or not it
        # would have been answered, because the attempt is the signal.
        injection = check_injection(ticket)

        # 3. route -------------------------------------------------------
        routing = route(classification, passages, threshold=self.threshold)
        if not injection.passed and routing.action == AUTO:
            routing.action = ESCALATE
            routing.rule = "R-01b-injection"
            routing.reason = (
                "The ticket text contains something written to look like an instruction to the "
                "system rather than a question for it. It has been held back for a person to "
                f"look at. Detected: {injection.detail}"
            )
        records.append(
            DecisionRecord.create(
                ticket.ticket_id,
                "routing",
                input_summary=_summarise(ticket),
                model_name="rules",
                model_version=routing.rule,
                prediction=routing.action,
                confidence=classification.intent_confidence,
                sources_used=[p.to_dict() for p in passages],
                action_taken=routing.action,
                reason=routing.reason,
                threshold_applied=routing.threshold_applied,
                requirement_ids=STAGE_REQUIREMENTS["routing"],
            )
        )

        # 4. generate ----------------------------------------------------
        if routing.action == AUTO:
            draft = self.generator.draft(
                ticket,
                passages,
                confidence=classification.intent_confidence,
                intent=classification.intent,
            )
            body, citations, method = draft.body, draft.citations, draft.method
            uncertain = draft.uncertain_about
            if not draft.answered:
                # The generator declining is a routing outcome, not a failure.
                routing.action = ESCALATE
                routing.rule = "R-06-generator-declined"
                routing.reason = (
                    "The system could not write an answer it could stand behind from the "
                    f"documentation it found, so a support engineer has it. {uncertain}".strip()
                )
        else:
            body, citations, method, uncertain = "", [], "none", ""

        escalation_note = ""
        if routing.action == ESCALATE:
            escalation_note = self.generator.escalation_note(
                ticket,
                passages,
                intent=classification.intent,
                confidence=classification.intent_confidence,
                routing_reason=routing.reason,
            )

        records.append(
            DecisionRecord.create(
                ticket.ticket_id,
                "generation",
                input_summary=_summarise(ticket),
                model_name=self.provider.name,
                model_version=self.provider.version,
                prediction=method,
                confidence=classification.intent_confidence,
                sources_used=[{"doc_id": c["doc_id"], "score": c.get("score", 0.0)} for c in citations],
                action_taken="drafted" if body else "escalation_summary",
                reason=(
                    f"Drafted via {method} with {len(citations)} citation(s)."
                    if body
                    else "No customer-facing answer drafted; wrote a handover note for the engineer."
                ),
                threshold_applied=self.threshold,
                requirement_ids=STAGE_REQUIREMENTS["generation"],
                prompt_version="PR-01 v1.3" if method.startswith("model") else "extractive v1.2",
            )
        )

        # 5. validate ----------------------------------------------------
        # Guardrails run on whatever is going out. For an escalation that is the
        # handover note, which a person reads - it must not leak private data
        # either, and it is still a place a commitment could appear.
        checked_text = body if routing.action == AUTO else escalation_note
        guardrails = run_all(
            checked_text,
            ticket=ticket,
            passages=passages,
            citations=citations,
            confidence=classification.intent_confidence,
            threshold=self.threshold,
        )
        if routing.action != AUTO:
            # Nothing blocks on this path, and the reasoning is worth writing
            # down because I had it wrong first time.
            #
            # A guardrail exists to stop a response reaching a customer. An
            # escalation does not reach a customer; it reaches a CloudServe
            # engineer who is handling the ticket and is entitled to see the
            # customer's own name, account and key. Blocking the handover note
            # for containing those was the guardrail firing at the wrong
            # audience, and it is why the first dev run reported ninety-three
            # activations while stopping nothing.
            #
            # Two of the checks cannot pass here by construction: the note
            # carries no citations, and it is not a confidence-gated answer.
            # A third, commitments, is reading the customer's own words back,
            # because the note quotes their opening line. All three are recorded
            # as not-applicable rather than as failures, and pii is recorded
            # honestly but does not block.
            reasons = {
                "grounding": "not applicable: internal handover note, carries no citations",
                "confidence_floor": "not applicable: the note is not a confidence-gated answer",
                "commitments": "not applicable: the note quotes the customer's own wording",
            }
            guardrails = [
                GuardrailResult(
                    g.name,
                    True if g.name in reasons else g.passed,
                    reasons.get(g.name, g.detail),
                    blocking=False,
                )
                for g in guardrails
            ]

        blocked = [g for g in guardrails if g.blocked]
        final_action = routing.action
        if blocked and routing.action == AUTO:
            final_action = "blocked"

        records.append(
            DecisionRecord.create(
                ticket.ticket_id,
                "validation",
                input_summary=_summarise(ticket),
                model_name="guardrails",
                model_version="v2.1",
                prediction="blocked" if blocked else "released",
                confidence=classification.intent_confidence,
                sources_used=[{"doc_id": c["doc_id"]} for c in citations],
                action_taken=final_action,
                reason=(
                    "Blocked before sending by: " + "; ".join(f"{g.name} ({g.detail})" for g in blocked)
                    if blocked
                    else f"All {len(guardrails)} checks passed; response released."
                ),
                threshold_applied=self.threshold,
                requirement_ids=STAGE_REQUIREMENTS["validation"],
                guardrail_results={g.name: ("pass" if g.passed else "fail") for g in guardrails},
            )
        )

        if final_action == "blocked":
            # Blocked means a person gets it, with the reason attached. It never
            # means the customer gets a redacted version.
            escalation_note = (
                "A draft answer was produced and then blocked before sending.\n\n"
                + "Blocked by: "
                + "; ".join(f"{g.name} - {g.detail}" for g in blocked)
                + "\n\n"
                + self.generator.escalation_note(
                    ticket,
                    passages,
                    intent=classification.intent,
                    confidence=classification.intent_confidence,
                    routing_reason=routing.reason,
                )
            )

        return Response(
            ticket_id=ticket.ticket_id,
            action=final_action,
            body=body if final_action == AUTO else "",
            citations=citations if final_action == AUTO else [],
            classification=classification,
            routing=routing,
            guardrails=guardrails,
            passages=passages,
            escalation_note=escalation_note,
            generation_method=method,
        )


def _summarise(ticket: Ticket, limit: int = 160) -> str:
    """What goes in the log's input_summary. Deliberately the ticket text rather
    than a hash: an auditor reading a decision needs to see what it was about,
    and the text is already held elsewhere in the same system."""
    text = " ".join(ticket.text.split())
    return text if len(text) <= limit else text[: limit - 3].rsplit(" ", 1)[0] + "..."
