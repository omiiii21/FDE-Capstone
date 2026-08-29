# ADR-005: Every reported figure uses a body-disjoint split

## Status

Accepted, 30 August 2026.

## Context

The development set has 500 tickets and 215 distinct ticket bodies. The same text appears under different ticket identifiers, with different customers, channels and tiers attached. The validation set has 80 tickets and 60 distinct bodies, and 42 of those bodies also appear verbatim in the development set.

I did not notice this for the first few days. I noticed it because the classifier reported 99.2% accuracy on a random five-fold split, which is the sort of number that should make you suspicious rather than pleased. On a split grouped by normalised ticket body, so that no text can appear on both sides of the line, the same model and the same features give 92.2% with a standard deviation of 3.5% across folds. Seven points of the original figure were the model recognising strings it had already been shown.

Nothing about this is exotic. It is the oldest leakage failure there is, and it is invisible if you look only at the ticket identifier, because by that measure all 500 rows are distinct.

## Decision

`grouped_folds` in `scripts/train_classifier.py` groups by `re.sub(r"\s+", " ", body.strip().lower())` and assigns whole groups to folds. Every classifier figure quoted anywhere in this project — accuracy, macro precision, weighted precision, per-class support — comes from that split. The random-split number is recorded only as a contrast, never as a result.

The same rule governs calibration. The calibrator is fitted on the held-out fold's bodies, using the logits of the model that ships, not on training data and not on out-of-fold predictions pooled across folds.

## Consequences

The headline classifier figure is 92.2%, not 99.2%, and I have to explain the gap to anyone who runs a random split and gets a better answer. That is the correct direction for the honesty of the project and the wrong direction for how it reads at a glance.

It also means one figure in the evaluation output is inflated, and rather than suppress it I would rather name it. The full 500-ticket run reports intent accuracy of 97.8%, because the shipped model was fitted on 411 of those 500 tickets. That number is a smoke test — it tells you the artefact loaded and the pipeline wired the classifier up correctly — and it is not evidence about accuracy. The honest figure is 92.2%. The validation run's 98.8% carries the same caveat for a different reason: 42 of its 60 distinct bodies appear verbatim in the training data.

The decision cost training data as well. Because the calibrator needs held-out logits from the shipped model, the shipped weights are fitted on four folds rather than all five. Fitting the temperature on pooled out-of-fold logits would have used all 500 tickets, and it is what I tried first; it does not transfer, because fold models trained on 80% of the data produce flatter logits than the full model, so the temperature that calibrates them sharpens the full model until every prediction reads 1.000. Losing a fifth of the data was the cheaper error.

One thing this split cannot fix: it makes the accuracy figure honest about duplicate text, but it says nothing about whether a queue of 215 distinct problems generalises to whatever CloudServe see next quarter.

## What would change my mind

Measure the duplication rate on live traffic. Take a month of real tickets and compute the ratio of distinct normalised bodies to total tickets; here it is 215 in 500, or 43%. If live traffic comes back above roughly 90% distinct, then the grouped and random splits converge, the caveat stops mattering, and the simpler split is fine to report.

If it comes back at 43% or lower, the opposite follows and the grouped split is not strict enough on its own. Near-duplicate bodies — the same problem retyped rather than copied — would be passing the exact-match grouping and leaking anyway, and I would move to grouping by a similarity threshold and expect the 92.2% to fall again.
