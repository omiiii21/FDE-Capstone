"""Intent and urgency, with a confidence that means something.

A3 is the criterion: every ticket gets an intent, an urgency and a numeric
confidence in [0, 1]. The tests that matter here are the ones about what the
classifier says when it does not know, because a confident answer to an
unanswerable question is the failure this system is meant to avoid.
"""

from __future__ import annotations

import pytest

from src.classify import DEESCALATING_PHRASES, ESCALATING_PHRASES
from src.config import INTENTS, URGENCIES
from src.ingest import normalise_ticket


def _ticket(body: str, *, subject: str = "", channel: str = "email"):
    return normalise_ticket({"ticket_id": "T-1", "channel": channel, "subject": subject, "body": body})


# A3: a numeric confidence in [0, 1] on every ticket, not only the ones that
# classify cleanly.
def test_confidence_is_always_a_float_between_zero_and_one(classifier, sample_tickets):
    for ticket in sample_tickets:
        result = classifier.classify(ticket)

        assert isinstance(result.intent_confidence, float)
        assert 0.0 <= result.intent_confidence <= 1.0
        assert isinstance(result.urgency_confidence, float)
        assert 0.0 <= result.urgency_confidence <= 1.0
        assert result.intent in INTENTS


def test_empty_text_is_an_unclear_request_at_zero_confidence(classifier):
    intent, confidence, alternatives = classifier.classify_intent("")

    assert intent == "unclear_request"
    assert confidence == 0.0
    assert alternatives == []


# A3. Pure gibberish produces an all-zero feature row, and softmax over zeros is
# a uniform distribution - which would be reported as a confident-looking 1/22
# rather than as "I have no idea". This is the test that the special case exists.
def test_unknown_vocabulary_is_unclear_rather_than_a_falsely_confident_guess(classifier):
    intent, confidence, alternatives = classifier.classify_intent("zzxqvw plfgh mnbvcx qwrtyp")

    assert intent == "unclear_request"
    assert confidence == 0.0
    assert confidence != pytest.approx(1 / len(INTENTS), abs=0.01)
    assert alternatives == []


def test_alternatives_are_returned_and_ranked_below_the_prediction(classifier, sample_tickets):
    # Worth being candid about: the top figure is the calibrated probability
    # that the prediction is correct, while the alternatives carry raw softmax
    # mass. They are not on the same scale, so the second assertion is an
    # observed property of the shipped model on this set rather than an
    # invariant of the code. The ordering of the alternatives is an invariant.
    for ticket in sample_tickets:
        result = classifier.classify(ticket)
        if not result.alternatives:
            continue

        scores = [score for _, score in result.alternatives]
        assert scores == sorted(scores, reverse=True)
        assert result.intent not in [name for name, _ in result.alternatives]
        assert result.intent_confidence >= scores[0]


def test_urgency_is_one_of_the_three_levels(classifier, sample_tickets):
    assert {classifier.classify(t).urgency for t in sample_tickets} <= set(URGENCIES)


# Ravi's point from the customer interview: the same intent at nine in the
# morning with nothing shipping is a different cost from the same intent on a
# Friday afternoon. The phrase overrides the intent policy, not the other way
# round, so this is checked against an intent whose policy urgency is medium.
@pytest.mark.parametrize("phrase", ["production is down", "complete outage", "we are losing data"])
def test_an_escalating_phrase_raises_urgency_to_high(classifier, phrase):
    ticket = _ticket(f"Hello, {phrase} and we need help.")

    urgency, confidence, rule = classifier.classify_urgency(ticket, "billing_query")

    assert urgency == "high"
    assert confidence > 0.5
    assert rule.startswith("escalating-phrase:")


@pytest.mark.parametrize("phrase", ["no rush", "just curious", "for future reference"])
def test_a_deescalating_phrase_lowers_urgency(classifier, phrase):
    ticket = _ticket(f"{phrase.capitalize()}, how does cursor pagination work?")

    urgency, _, rule = classifier.classify_urgency(ticket, "api_usage_question")

    assert urgency == "low"
    assert rule.startswith("deescalating-phrase:")


def test_a_deescalating_phrase_cannot_lower_an_intent_that_is_always_high(classifier):
    # A customer saying "no rush" about a security incident is being polite, not
    # telling us the incident is unimportant.
    ticket = _ticket("No rush, but it looks like one of our keys has been leaked.")

    urgency, _, _ = classifier.classify_urgency(ticket, "security_incident")

    assert urgency == "high"


def test_a_low_urgency_chat_ticket_is_floored_at_medium(classifier):
    # Chat customers are waiting while we decide. It does not make the question
    # important, but it does change what being late means.
    ticket = _ticket("how do I invite my team?", channel="chat")

    urgency, _, rule = classifier.classify_urgency(ticket, "onboarding")

    assert urgency == "medium"
    assert rule == "policy:chat-floor"


# A3/A5: the same ticket must classify the same way every time, or the routing
# threshold is being applied to a moving number.
def test_classification_is_deterministic(classifier, sample_tickets):
    first = [classifier.classify(t) for t in sample_tickets]
    second = [classifier.classify(t) for t in sample_tickets]

    assert first == second


def test_the_phrase_lists_are_lowercase(classifier):
    # classify_urgency lowercases the ticket before matching, so an uppercase
    # entry in either list would be dead code that silently never fires.
    assert all(phrase == phrase.lower() for phrase in ESCALATING_PHRASES + DEESCALATING_PHRASES)
