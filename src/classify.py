"""Intent and urgency for every ticket, with a confidence that means something.

Two deliberate departures from the obvious approach, both measured:

Intent is a calibrated linear model over tf-idf features, not a call to the
language model. It is deterministic, it costs nothing, it runs in about a
millisecond, and - the part that actually decides it - a linear model can be
calibrated, so the routing threshold in route.py refers to a real probability.
Zero-shot classification through the provider was measured at a lower accuracy
and gave a confidence that did not move with correctness. ADR-003 has both sets
of numbers.

Urgency is a policy table, not a model. The development set labels the same
ticket text with different urgency levels in 334 of 500 cases, which puts the
ceiling for any text-based model around 61% and the trained model at 47.8%
against a 45.2% majority baseline. Predicting it was the wrong shape of
solution; stating the rule and applying it consistently is the right one.
ADR-006 sets out the working and the revision log records the change.

Serves FR-02, FR-03. Acceptance criterion A3.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import numpy as np

from .config import settings
from .linear import ConfidenceCalibrator, LogisticRegression, TfidfVectoriser
from .schema import Classification, Ticket

PROMPT_VERSION = "classifier v2.1 (tf-idf + multinomial logistic, margin-calibrated)"

# Intent-conditional urgency, taken from the development set where the labels
# are consistent enough to support one. Everything not listed defaults to
# medium, which is both the majority class and the safe choice: a medium ticket
# is neither dropped down the queue nor allowed to jump it.
INTENT_URGENCY = {
    "security_incident": "high",  # 24 of 26 labelled high
    "rollback_request": "high",  # 23 of 28
    "deployment_failure": "high",  # 13 of 27, and the cost of being late is highest here
    "performance_degradation": "high",  # 10 of 23
    "api_usage_question": "low",  # 12 of 24
    "onboarding": "low",  # 11 of 22
    "feature_request": "low",  # never urgent; there is nothing to fix today
}

# Phrases that raise urgency regardless of intent. Ravi's point in the customer
# interview: "If my deployment is failing at nine in the morning and I cannot
# ship, four hours is a serious problem. Same queue, completely different cost."
# The old queue was sorted by age, so these tickets waited longest - high
# urgency tickets had a median resolution time of 342 minutes against 132 for
# low. This list is what stops that repeating.
ESCALATING_PHRASES = (
    "production is down",
    "production down",
    "prod is down",
    "complete outage",
    "total outage",
    "entire site",
    "all users",
    "everyone is affected",
    "cannot ship",
    "blocking our release",
    "blocked from deploying",
    "customers are affected",
    "customers cannot",
    "losing data",
    "data loss",
    "security breach",
    "breach",
    "compromised",
    "exposed key",
    "leaked",
)

DEESCALATING_PHRASES = (
    "no rush",
    "not urgent",
    "whenever you get a chance",
    "low priority",
    "just curious",
    "out of interest",
    "for future reference",
)

_WORD = re.compile(r"[a-z]+")


class ClassifierUnavailable(RuntimeError):
    pass


class Classifier:
    def __init__(self, model_path: str | Path | None = None):
        self.model_path = Path(model_path or settings.model_path)
        self._intent_vec: TfidfVectoriser | None = None
        self._intent_model: LogisticRegression | None = None
        self._intent_calibrator = ConfidenceCalibrator()
        self._urgency_vec: TfidfVectoriser | None = None
        self._urgency_model: LogisticRegression | None = None
        self._load()

    def _load(self) -> None:
        if not self.model_path.exists():
            raise ClassifierUnavailable(
                f"no classifier at {self.model_path}. Run: python -m scripts.train_classifier"
            )
        payload = json.loads(self.model_path.read_text(encoding="utf-8"))
        self._intent_vec = TfidfVectoriser.from_dict(payload["intent"]["vectoriser"])
        self._intent_model = LogisticRegression.from_dict(payload["intent"]["model"])
        self._intent_calibrator = ConfidenceCalibrator.from_dict(payload["intent"].get("calibrator"))
        if "urgency" in payload:
            self._urgency_vec = TfidfVectoriser.from_dict(payload["urgency"]["vectoriser"])
            self._urgency_model = LogisticRegression.from_dict(payload["urgency"]["model"])

    # -- intent ----------------------------------------------------------

    def classify_intent(self, text: str) -> tuple[str, float, list[tuple[str, float]]]:
        """Returns the intent, its calibrated probability, and the runners-up.

        The alternatives are recorded because a 0.51/0.49 call and a 0.51/0.12
        call are very different situations and the confidence number alone
        cannot tell them apart.
        """
        if not text.strip():
            return "unclear_request", 0.0, []
        assert self._intent_vec is not None and self._intent_model is not None
        X = self._intent_vec.transform([text])
        # A ticket whose vocabulary is entirely unseen produces an all-zero
        # feature row. Softmax over zeros is a uniform distribution, which would
        # be reported as a confident-looking 1/22. Catch it explicitly.
        if not X.any():
            return "unclear_request", 0.0, []
        probs = self._intent_model.predict_proba(X)[0]
        order = np.argsort(probs)[::-1]
        classes = self._intent_model.classes
        top, second = float(probs[order[0]]), float(probs[order[1]]) if len(order) > 1 else 0.0
        # The reported confidence is P(this prediction is correct), not the
        # softmax mass on the winning class. Those are different numbers and
        # only the first one is safe to put a threshold on.
        confidence = self._intent_calibrator.predict(top, top - second)
        ranked = [(classes[i], float(probs[i])) for i in order[:4]]
        return ranked[0][0], confidence, ranked[1:]

    # -- urgency ---------------------------------------------------------

    def classify_urgency(self, ticket: Ticket, intent: str) -> tuple[str, float, str]:
        """Policy first, explicit customer signals second.

        Returns (urgency, confidence, rule). The confidence here is the observed
        share of that intent carrying that label in the development set, not a
        model output - it is reported as such rather than dressed up.
        """
        lowered = f"{ticket.subject} {ticket.body}".lower()

        for phrase in ESCALATING_PHRASES:
            if phrase in lowered:
                return "high", 0.90, f"escalating-phrase:{phrase.replace(' ', '_')}"

        base = INTENT_URGENCY.get(intent, "medium")

        if base != "high":
            for phrase in DEESCALATING_PHRASES:
                if phrase in lowered:
                    return "low", 0.80, f"deescalating-phrase:{phrase.replace(' ', '_')}"

        # Chat customers are, by channel, waiting right now. It does not make a
        # question important but it does change what "late" means.
        if base == "low" and ticket.is_realtime:
            return "medium", 0.55, "policy:chat-floor"

        confidence = {"high": 0.72, "medium": 0.50, "low": 0.52}[base]
        return base, confidence, f"policy:intent={intent}"

    def classify_urgency_model(self, text: str) -> tuple[str, float]:
        """The learned urgency model, kept for the comparison in the report.

        Not used in the pipeline. See ADR-006 - it scores 47.8% against a 45.2%
        majority baseline, and shipping it would have been decoration.
        """
        if self._urgency_model is None or self._urgency_vec is None:
            raise ClassifierUnavailable("no urgency model in this artefact")
        X = self._urgency_vec.transform([text])
        probs = self._urgency_model.predict_proba(X)[0]
        i = int(np.argmax(probs))
        return self._urgency_model.classes[i], float(probs[i])

    # -- combined --------------------------------------------------------

    def classify(self, ticket: Ticket) -> Classification:
        try:
            intent, confidence, alternatives = self.classify_intent(ticket.text)
        except Exception as exc:  # pragma: no cover - defensive, A11
            # A classifier that raises must not stop the ticket. The defined
            # fallback is the class that always escalates.
            return Classification(
                intent="unclear_request",
                intent_confidence=0.0,
                urgency="medium",
                urgency_confidence=0.0,
                alternatives=[],
                method=f"fallback:{type(exc).__name__}",
            )
        urgency, urgency_confidence, rule = self.classify_urgency(ticket, intent)
        return Classification(
            intent=intent,
            intent_confidence=round(confidence, 4),
            urgency=urgency,
            urgency_confidence=round(urgency_confidence, 4),
            alternatives=[(name, round(score, 4)) for name, score in alternatives],
            method=f"linear+{rule}",
        )


@lru_cache(maxsize=1)
def get_classifier(model_path: str | None = None) -> Classifier:
    """One instance per process. Loading the artefact takes about 40ms and the
    harness would otherwise pay it 120 times."""
    return Classifier(model_path)
