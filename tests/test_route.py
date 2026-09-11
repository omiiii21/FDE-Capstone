"""Routing: a threshold, applied in a fixed order, with the same answer twice.

A5 is the criterion. The rules are evaluated in order and the first match wins,
so most of these tests are about which rule fires when more than one could -
that ordering is the policy, and an accidental reorder would not change any
single-rule test.
"""

from __future__ import annotations

import pytest

from src.config import HIGH_COST_INTENTS, NEVER_AUTO_RESPOND, settings
from src.route import AUTO, ESCALATE, route
from src.schema import Classification, Passage

THRESHOLD = settings.confidence_threshold
HIGH_COST_THRESHOLD = settings.high_cost_threshold


@pytest.fixture
def passage():
    return Passage(
        doc_id="DOC-DEPLOY-001",
        title="Container deployments failing during the health check phase",
        category="deployment",
        chunk_id="DOC-DEPLOY-001#2",
        text="Confirm the container is listening on the port declared in the service definition.",
        score=12.15,
    )


def _classification(intent: str, confidence: float) -> Classification:
    return Classification(intent, confidence, "medium", 0.5, [("unclear_request", 0.01)])


# A5: same input, same decision. Running the whole validation set twice catches
# an ordering or dictionary-iteration dependency that a single ticket would not.
def test_routing_the_validation_set_twice_gives_identical_decisions(classifier, retriever, sample_tickets):
    def run():
        decisions = []
        for ticket in sample_tickets:
            classification = classifier.classify(ticket)
            passages = retriever.search(ticket.text)
            decision = route(classification, passages, halted=False)
            decisions.append((ticket.ticket_id, decision.action, decision.rule))
        return decisions

    assert run() == run()


# A5/R-01: this is the test that proves no confidence is high enough for the
# policy classes. 0.99 is above every threshold in the system.
@pytest.mark.parametrize("intent", sorted(NEVER_AUTO_RESPOND))
def test_policy_classes_escalate_at_the_highest_confidence(intent, passage):
    decision = route(_classification(intent, 0.99), [passage], halted=False)

    assert decision.action == ESCALATE
    assert decision.rule == "R-01-policy-class"


# R-02. Nothing retrieved means nothing to ground an answer in, whatever the
# classifier thinks of the ticket.
def test_no_passages_escalates_for_want_of_grounding():
    decision = route(_classification("deployment_failure", 0.99), [], halted=False)

    assert decision.action == ESCALATE
    assert decision.rule == "R-02-no-grounding"


# R-03. Marcus: "I would rather it said nothing than said something wrong."
@pytest.mark.parametrize("intent", sorted(HIGH_COST_INTENTS))
def test_a_high_cost_intent_below_the_higher_bar_escalates(intent, passage):
    confidence = (THRESHOLD + HIGH_COST_THRESHOLD) / 2
    assert THRESHOLD < confidence < HIGH_COST_THRESHOLD  # otherwise this proves nothing

    decision = route(_classification(intent, confidence), [passage], halted=False)

    assert decision.action == ESCALATE
    assert decision.rule == "R-03-high-cost-class"
    assert decision.threshold_applied == HIGH_COST_THRESHOLD


@pytest.mark.parametrize("intent", sorted(HIGH_COST_INTENTS))
def test_a_high_cost_intent_above_the_higher_bar_is_answered(intent, passage):
    decision = route(_classification(intent, HIGH_COST_THRESHOLD + 0.01), [passage], halted=False)

    assert decision.action == AUTO
    assert decision.rule == "R-05-auto-respond"


# R-04.
def test_confidence_below_the_threshold_escalates(passage):
    decision = route(_classification("deployment_failure", THRESHOLD - 0.01), [passage], halted=False)

    assert decision.action == ESCALATE
    assert decision.rule == "R-04-below-threshold"
    assert decision.threshold_applied == THRESHOLD


def test_a_confident_grounded_ticket_is_answered(passage):
    decision = route(_classification("deployment_failure", 0.95), [passage], halted=False)

    assert decision.action == AUTO
    assert decision.rule == "R-05-auto-respond"
    assert passage.doc_id in decision.reason


# R-00. The kill switch is first in the order for a reason: when an operator has
# switched automation off, nothing else about the ticket is allowed to matter.
@pytest.mark.parametrize("intent", ["deployment_failure", "billing_query", "security_incident"])
@pytest.mark.parametrize("confidence", [0.0, 0.62, 0.99])
@pytest.mark.parametrize("has_passages", [True, False])
def test_the_kill_switch_overrides_every_other_rule(intent, confidence, has_passages, passage):
    passages = [passage] if has_passages else []

    decision = route(_classification(intent, confidence), passages, halted=True)

    assert decision.action == ESCALATE
    assert decision.rule == "R-00-kill-switch"


# The reasons end up in front of a support manager and in a compliance review.
# "confidence 0.58 below threshold 0.62" is a reason; "routing_rule_4" is not.
def test_every_reason_is_written_for_a_person_not_for_a_log_parser(passage):
    intents = sorted(NEVER_AUTO_RESPOND | HIGH_COST_INTENTS | {"deployment_failure"})
    for intent in intents:
        for confidence in (0.0, 0.5, 0.75, 0.99):
            for passages in ([passage], []):
                for halted in (True, False):
                    reason = route(_classification(intent, confidence), passages, halted=halted).reason

                    assert reason.strip()
                    assert "_" not in reason, reason
                    assert reason[0].isupper()
                    assert reason.endswith(".")


def test_the_rule_identifier_is_recorded_even_though_the_reason_avoids_it(passage):
    # The identifier is what you group by when auditing a run; the reason is
    # what you read. Both are on the decision, and they are not the same field.
    decision = route(_classification("unclear_request", 0.99), [passage], halted=False)

    assert decision.rule.startswith("R-")
    assert decision.rule not in decision.reason
