"""A small multinomial logistic regression, trained and scored in numpy.

Why not scikit-learn: the only thing this project needs from it is a linear
model over tf-idf features, and that is about ninety lines. Pinning
scikit-learn adds a compiled dependency to a system whose first acceptance
criterion is that it installs from a clean checkout on somebody else's machine.
The trade is written up in ADR-003.

The model serialises to JSON so that the trained artefact is readable, diffable
and committed alongside the code that produced it.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Sequence

import numpy as np

from .text import tokenise


class TfidfVectoriser:
    """Word unigrams and bigrams, sublinear term frequency, L2 normalised.

    Bigrams matter here more than they usually would: "rate limit", "health
    check" and "connection pool" are the phrases that separate several of the
    22 intents, and on unigrams alone those classes bleed into each other.
    """

    def __init__(self, min_df: int = 2, max_features: int = 20000, ngram: int = 2):
        self.min_df = min_df
        self.max_features = max_features
        self.ngram = ngram
        self.vocabulary: dict[str, int] = {}
        self.idf: np.ndarray = np.zeros(0)

    def _features(self, text: str) -> list[str]:
        tokens = tokenise(text)
        out = list(tokens)
        for n in range(2, self.ngram + 1):
            out.extend(" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1))
        return out

    def fit(self, texts: Sequence[str]) -> "TfidfVectoriser":
        df: Counter[str] = Counter()
        for text in texts:
            df.update(set(self._features(text)))
        kept = [term for term, count in df.most_common(self.max_features) if count >= self.min_df]
        kept.sort()
        self.vocabulary = {term: i for i, term in enumerate(kept)}
        n = len(texts)
        self.idf = np.array([math.log((1 + n) / (1 + df[term])) + 1.0 for term in kept], dtype=np.float64)
        return self

    def transform(self, texts: Sequence[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), len(self.vocabulary)), dtype=np.float64)
        for row, text in enumerate(texts):
            counts = Counter(self._features(text))
            for term, count in counts.items():
                index = self.vocabulary.get(term)
                if index is not None:
                    matrix[row, index] = 1.0 + math.log(count)  # sublinear tf
        matrix *= self.idf
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return matrix / norms

    def fit_transform(self, texts: Sequence[str]) -> np.ndarray:
        return self.fit(texts).transform(texts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_df": self.min_df,
            "max_features": self.max_features,
            "ngram": self.ngram,
            "vocabulary": self.vocabulary,
            "idf": [round(v, 6) for v in self.idf.tolist()],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TfidfVectoriser":
        obj = cls(payload["min_df"], payload["max_features"], payload.get("ngram", 2))
        obj.vocabulary = payload["vocabulary"]
        obj.idf = np.array(payload["idf"], dtype=np.float64)
        return obj


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


class LogisticRegression:
    """Multinomial logistic regression, full-batch gradient descent with
    momentum and L2. Small enough data that batching would only add code."""

    def __init__(self, l2: float = 1e-4, learning_rate: float = 0.9, epochs: int = 900):
        self.l2 = l2
        self.learning_rate = learning_rate
        self.epochs = epochs
        self.classes: list[str] = []
        self.weights: np.ndarray = np.zeros((0, 0))
        self.bias: np.ndarray = np.zeros(0)
        # Temperature for calibration; 1.0 means uncalibrated. Fitted
        # separately on held-out data - see fit_temperature.
        self.temperature: float = 1.0

    def fit(self, X: np.ndarray, y: Sequence[str], *, class_weight: bool = True) -> "LogisticRegression":
        self.classes = sorted(set(y))
        index = {c: i for i, c in enumerate(self.classes)}
        n, d = X.shape
        k = len(self.classes)
        Y = np.zeros((n, k))
        for row, label in enumerate(y):
            Y[row, index[label]] = 1.0

        # The intent distribution is uneven (rate_limit has 13 tickets,
        # data_export 29). Without this the rare classes are quietly ignored.
        if class_weight:
            counts = Y.sum(axis=0)
            weights = np.where(counts > 0, n / (k * np.maximum(counts, 1)), 0.0)
            sample_w = (Y * weights).sum(axis=1)[:, None]
        else:
            sample_w = np.ones((n, 1))

        self.weights = np.zeros((d, k))
        self.bias = np.zeros(k)
        velocity_w = np.zeros_like(self.weights)
        velocity_b = np.zeros_like(self.bias)
        for _ in range(self.epochs):
            probs = _softmax(X @ self.weights + self.bias)
            error = (probs - Y) * sample_w
            grad_w = X.T @ error / n + self.l2 * self.weights
            grad_b = error.sum(axis=0) / n
            velocity_w = 0.9 * velocity_w - self.learning_rate * grad_w
            velocity_b = 0.9 * velocity_b - self.learning_rate * grad_b
            self.weights += velocity_w
            self.bias += velocity_b
        return self

    def decision(self, X: np.ndarray) -> np.ndarray:
        return X @ self.weights + self.bias

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return _softmax(self.decision(X) / self.temperature)

    def fit_temperature(
        self, logits: np.ndarray, y: Sequence[str], grid: Sequence[float] | None = None
    ) -> float:
        """Temperature scaling (Guo et al., 2017), fitted by minimising negative
        log likelihood.

        Takes logits rather than features, because the logits that matter are
        the out-of-fold ones: a temperature fitted on predictions the weights
        have already seen comes out near 1.0 and calibrates nothing.

        This is the step that makes the routing threshold mean something. The
        uncalibrated model on this data puts almost every prediction above 0.99,
        which turns any threshold into a coin flip.
        """
        grid = grid or [round(0.2 + 0.05 * i, 2) for i in range(80)]
        index = {c: i for i, c in enumerate(self.classes)}
        targets = np.array([index[label] for label in y])
        best_t, best_nll = 1.0, float("inf")
        for t in grid:
            probs = _softmax(logits / t)
            nll = -np.log(np.clip(probs[np.arange(len(targets)), targets], 1e-12, 1.0)).mean()
            if nll < best_nll:
                best_nll, best_t = nll, t
        self.temperature = best_t
        return best_t

    def to_dict(self) -> dict[str, Any]:
        return {
            "classes": self.classes,
            "l2": self.l2,
            "learning_rate": self.learning_rate,
            "epochs": self.epochs,
            "temperature": self.temperature,
            # Rounded to keep the committed artefact readable. The effect on
            # predictions is below 1e-5 and is checked in tests.
            "weights": [[round(v, 5) for v in row] for row in self.weights.tolist()],
            "bias": [round(v, 5) for v in self.bias.tolist()],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LogisticRegression":
        obj = cls(payload["l2"], payload["learning_rate"], payload["epochs"])
        obj.classes = payload["classes"]
        obj.weights = np.array(payload["weights"], dtype=np.float64)
        obj.bias = np.array(payload["bias"], dtype=np.float64)
        obj.temperature = payload.get("temperature", 1.0)
        return obj
