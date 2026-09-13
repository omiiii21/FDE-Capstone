"""The decision to answer or to escalate, and the reason it was taken.

The rules are evaluated in a fixed order and the first one that matches decides.
That ordering is the design: policy comes before confidence, because there are
classes of ticket where a confident model is exactly the dangerous case.

Deterministic by construction - no randomness, no clock, no model call - so the
same ticket produces the same decision every time (A5). The test for that runs
every ticket in the validation set twice and compares.

Every branch returns a reason written for a support manager rather than for me.
Marcus has a compliance review in the autumn and said he needs to be able to say
why it did what it did; "confidence 0.58 below threshold 0.62" is a reason,
"routing_rule_4" is not.

Serves FR-05, FR-13, FR-16. Acceptance criterion A5.
"""

from __future__ import annotations

from typing import Sequence

from .config import HIGH_COST_INTENTS, NEVER_AUTO_RESPOND, settings
from .schema import Classification, Passage, RoutingDecision

AUTO = "auto_respond"
ESCALATE = "escalate"


def _readable(intent: str) -> str:
    """Intent as a support manager would say it, with the right article in
    front of it. Small thing, but these reasons end up in front of the customer
    support lead and "a authentication failure" reads as machine output."""
    words = intent.replace("_", " ")
    return f"an {words}" if words[0] in "aeiou" else f"a {words}"


def route(
    classification: Classification,
    passages: Sequence[Passage],
    *,
    threshold: float | None = None,
    high_cost_threshold: float | None = None,
    halted: bool | None = None,
) -> RoutingDecision:
    """Decide what happens to this ticket.

    Order matters and is worth stating plainly:

      R-00  kill switch                - operator has halted automation
      R-01  policy class               - this class never auto-responds
      R-02  no grounding               - retrieval found nothing usable
      R-03  high-cost class            - answer, but on a higher bar
      R-04  confidence below threshold - the model is not sure enough
      R-05  otherwise                  - answer
    """
    threshold = settings.confidence_threshold if threshold is None else threshold
    high_cost_threshold = settings.high_cost_threshold if high_cost_threshold is None else high_cost_threshold
    halted = settings.auto_responses_halted() if halted is None else halted

    intent = classification.intent
    confidence = classification.intent_confidence

    # R-00 -------------------------------------------------------------
    if halted:
        return RoutingDecision(
            action=ESCALATE,
            reason=(
                "Automatic responses are currently switched off by an operator, so this "
                "ticket is going straight to a support engineer with its draft and sources attached."
            ),
            rule="R-00-kill-switch",
            threshold_applied=threshold,
        )

    # R-01 -------------------------------------------------------------
    # No confidence is high enough here. A security incident the system
    # understands perfectly is still a security incident, and a feature request
    # has nothing in the corpus to ground an answer in.
    if intent in NEVER_AUTO_RESPOND:
        return RoutingDecision(
            action=ESCALATE,
            reason=(
                f"This is {_readable(intent)}, which is one of the categories we never answer "
                f"automatically regardless of how confident the system is. A support engineer "
                f"has it, with a summary and the documentation that looked relevant."
            ),
            rule="R-01-policy-class",
            threshold_applied=threshold,
        )

    # R-02 -------------------------------------------------------------
    if not passages:
        return RoutingDecision(
            action=ESCALATE,
            reason=(
                "Nothing in the knowledge base was close enough to this question to answer it "
                "from, so rather than improvising, it has gone to a support engineer."
            ),
            rule="R-02-no-grounding",
            threshold_applied=threshold,
        )

    # R-03 -------------------------------------------------------------
    if intent in HIGH_COST_INTENTS and confidence < high_cost_threshold:
        return RoutingDecision(
            action=ESCALATE,
            reason=(
                f"This is {_readable(intent)}. Getting one of these wrong costs more than answering "
                f"slowly, so we hold them to a higher bar: {confidence:.0%} confident against "
                f"the {high_cost_threshold:.0%} we require for this category."
            ),
            rule="R-03-high-cost-class",
            threshold_applied=high_cost_threshold,
        )

    # R-04 -------------------------------------------------------------
    if confidence < threshold:
        return RoutingDecision(
            action=ESCALATE,
            reason=(
                f"The system was {confidence:.0%} confident it understood what this ticket was "
                f"about, against the {threshold:.0%} we require before answering. It has gone to "
                f"a support engineer with what it did work out."
            ),
            rule="R-04-below-threshold",
            threshold_applied=threshold,
        )

    # R-05 -------------------------------------------------------------
    top = passages[0]
    return RoutingDecision(
        action=AUTO,
        reason=(
            f"Recognised as {_readable(intent)} with {confidence:.0%} confidence, and "
            f'the knowledge base has an article that covers it ({top.doc_id}, "{top.title}"). '
            f"Answered automatically with that article cited."
        ),
        rule="R-05-auto-respond",
        threshold_applied=threshold,
    )
