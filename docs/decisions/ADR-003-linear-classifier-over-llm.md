# ADR-003: A calibrated linear classifier instead of a model call

## Status

Accepted, 28 August 2026.

## Context

Intent classification over 22 classes is the first decision the pipeline takes and everything after it depends on the number attached to that decision. Routing compares a confidence against 0.62, or against 0.85 for the high-cost classes, so the confidence has to mean something. If it does not, the threshold is decoration.

Zero-shot classification through the provider was the first thing I tried, because it needs no training data and it handles a new class the day someone invents it. It scored lower than the linear model, which I half expected. The part that ruled it out is that the confidence it reported did not move with correctness: wrong answers came back as assured as right ones, so there was nothing to threshold. It is also not deterministic in a way I can show a compliance reviewer, and A5 requires the same ticket to route the same way twice.

The alternative was scikit-learn. I did not use that either. The only thing this project needs from it is a linear model over tf-idf features, that is about ninety lines, and pinning it puts a compiled dependency into a system whose first acceptance criterion is installing from a clean checkout on somebody else's machine. `src/linear.py` holds the vectoriser, the regression and the calibrator in numpy, and the artefact serialises to JSON, so it is readable and diffable.

## Decision

tf-idf over word unigrams and bigrams, sublinear term frequency, L2 normalised, feeding a multinomial logistic regression trained with full-batch gradient descent and class weighting. Bigrams matter more here than they usually would: "rate limit", "health check" and "connection pool" are what separate several of the 22 classes, and on unigrams alone those classes bleed together.

Confidence is produced by `ConfidenceCalibrator`, a two-feature logistic regression on the top probability's logit and the top-one-to-top-two margin, predicting P(correct). It is fitted on a held-out split of the shipped model's own logits.

## Consequences

Body-disjoint five-fold accuracy is 92.2% (standard deviation 3.5%), macro precision 92.7%, weighted precision 92.9%. It runs in about a millisecond and during an outage.

Calibration is where the work went, and where I was wrong twice. Expected calibration error uncalibrated is 0.161. Temperature scaling was what I reached for first, and it gets ECE to 0.062, which looks like a success until you see what it does: the only temperature that calibrates a 22-way distribution this peaked is one that sharpens it, around 0.3 to 0.4, and at that setting more than 95% of predictions read 1.000. The metric improves and the routing threshold is left with nothing to separate. The shipped calibrator gets ECE to 0.028 and keeps the confidences spread out, which is the property the router actually needs.

The second mistake is worth recording because it cost a day. I fitted the temperature on pooled out-of-fold logits, on the reasoning that this used all 500 tickets rather than a fifth of them. It does not transfer. Fold models are trained on 80% of the data, produce flatter logits than the model trained on everything, and the temperature that calibrates them sharpens the full model until every prediction is 1.000. Calibration has to be fitted against the logits of the model that will actually ship, even though that costs training data.

The cost is that a new intent class needs labelled examples and a retraining run, where a zero-shot approach would have needed a sentence in a prompt.

## What would change my mind

Hold out the five rarest intents entirely — train on the other seventeen, then score both approaches on tickets from the held-out classes. If a few-shot model call beats the linear model there, and its confidence separates right from wrong well enough to reach an ECE near the 0.028 the shipped calibrator achieves, the hybrid becomes the right build: linear for the classes with data, a model call for the tail.

The other trigger is class growth. At 22 classes and 500 tickets this is comfortable. At 60 classes on the same volume, per-class support drops below what the 92.7% macro precision can survive.
