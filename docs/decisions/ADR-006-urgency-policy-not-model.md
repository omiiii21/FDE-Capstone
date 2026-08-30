# ADR-006: Urgency is a policy table, not a prediction

## Status

Accepted, superseded FR-03 in PRD v1.0. 3 September 2026.

## Context

FR-03 as I originally wrote it said the system would predict urgency from the ticket text alongside intent. I trained that model. It reaches 47.8% accuracy on a body-disjoint split against a majority-class baseline of 45.2%, which is 2.6 points of signal over answering "medium" every time.

Before throwing it away I wanted to know whether the fault was the model or the labels, so I measured the ceiling. An oracle allowed to pick the single best urgency label per intent scores 55.2%. An oracle allowed the best label per (intent, channel) pair scores 61.0%. Those are upper bounds no classifier of this kind can pass, and they sit far enough below anything useful that a better model was never going to rescue it.

Then I looked at the labels. Of the 500 development tickets, 334 have body text that carries more than one urgency label somewhere in the dataset, and 67 of the 215 distinct bodies are labelled inconsistently — the same words, low in one row and high in another. Urgency here is not a function of the ticket text. It is a function of who triaged it, and a model fitted on it learns the triager rather than the ticket.

The discovery interviews said the same thing from the other side. Ravi Menon complained that urgent and trivial tickets sit in the same queue, and the history confirms it: high-urgency tickets have a median resolution of 342 minutes against 132 for low-urgency ones. Sofia Restrepo explained why without being asked — she sorts her queue by age, so the urgent tickets wait longest. The problem CloudServe have is not that urgency is hard to predict. It is that nobody applies it consistently.

## Decision

Urgency ships as a documented policy table in `classify_urgency`, not as a model. Three parts, applied in order. An intent prior, taken from the development set where the labels are consistent enough to support one (`security_incident` is labelled high in 24 of 26 cases, `rollback_request` in 23 of 28). Explicit escalating phrases — "production is down", "losing data", "cannot ship", "security breach" — which raise urgency regardless of intent. De-escalating phrases, which lower it unless the intent prior is already high. Then a chat floor: a low-urgency ticket arriving on chat becomes medium, because a chat customer is waiting right now and that changes what "late" means even when the question is not important.

The confidence returned alongside it is the observed share of that intent carrying that label, reported as such rather than dressed up as a model probability.

The trained model stays in the artefact and is reachable through `classify_urgency_model`, used by nothing in the pipeline. It exists so the comparison can be re-run.

## Consequences

The rule is inspectable, arguable and changeable by a support manager without retraining anything. Marcus can read the escalating phrase list and add to it. That is the main benefit, and it is worth more here than 2.6 points over a baseline.

Consistency is the other. Whatever the policy gets wrong, it gets wrong the same way every time, which the human queue does not.

The cost is that the table is a snapshot of one dataset. The intent priors come from 500 tickets and will drift; the phrase lists are English and idiomatic, and a customer writing carefully in a second language may describe an outage without using any of them. That is a fairness concern I can name and have not solved.

## What would change my mind

Relabel a sample against a written rubric, independently, by two people, and measure agreement on the 67 inconsistent bodies specifically. If they agree on more than 90% of those, the labels are recoverable rather than noisy, the 61.0% oracle ceiling was an artefact of bad annotation rather than a property of the problem, and a model trained on clean labels deserves another run at it.

If agreement comes back near the 47.8% the model manages, the labels are irreducibly subjective and the policy table is not a compromise but the correct answer.
