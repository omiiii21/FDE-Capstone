# ADR-001: Build triage with assisted escalation, not a chatbot

## Status

Accepted, 24 August 2026.

## Context

The brief from CloudServe asks for a support chatbot. Marcus Adeyemi wants the ticket queue under control; Ravi Menon, on the customer side, wants an answer faster than the hour he thinks enterprise already gets. A conversational surface is the obvious shape for both of those wishes, and it is the shape I would have built if I had started coding in week one.

Discovery changed my mind, and then the data confirmed it. Sofia Restrepo, on tier one, said seven out of ten tickets she could answer without looking anything up. She was very nearly exactly right: 71.4% of the development set is labelled answerable from the existing documentation. The other 28.6% are not answerable from anything the company has written down, and no interface fixes that. Separately, 87 of the 500 tickets carry `must_not_auto_respond` — security incidents, compliance requests, feature requests and unclear requests — where the correct behaviour is a fast, well-prepared handover, not a good reply.

Then there is the result that decided it. I expected the retrieval score to act as a coverage signal: high score means the corpus covers this, low score means it does not. It does not work. The median top-1 BM25 score is 22.8 on answerable tickets and 20.9 on unanswerable ones, and at the floor of 4.2 that I ship, 95.1% of unanswerable tickets still get a passage back. There is no cutoff that keeps recall and rejects the tickets the documentation cannot answer.

A chatbot is a delivery mechanism. It specifies where the customer types and it specifies nothing about where the answer comes from, which tickets deserve one, or who is accountable when it is wrong. Given a corpus with a 28.6% hole in it and a retrieval score that cannot see the hole, "always reply, conversationally" is the one design guaranteed to fill the gap with fluent invention.

## Decision

The system is a triage pipeline with assisted escalation. Every ticket is classified, grounded against the corpus if possible, routed by an ordered rule set in `src/route.py`, and logged at every stage. Tickets that clear the rules get a cited reply. Tickets that do not get a handover note written for the engineer who picks them up — what the customer is asking, what documentation looked relevant, and what specifically was uncertain (`prompts/build/PR-02_escalation_summary.v1.1.txt`). The escalation path is a product output, not a failure branch.

A chat surface can sit on top of this later and would change nothing underneath it. The reverse is not true, which is the asymmetry the decision rests on.

## Consequences

On the 500-ticket development run, 81.4% of tickets were answered automatically and 18.6% escalated, against CloudServe's current 43.8% first contact resolution and 56.2% escalation. Zero of the 87 policy-class tickets were auto-answered.

The cost is that the system has no conversational state and cannot ask a clarifying question. An `unclear_request` goes to a human rather than being resolved with one exchange, and follow-up messages on the same thread arrive as new tickets. For this dataset that is the right trade, because the escalation note makes the human handover cheap, but it is a real limitation and not a virtue.

The second consequence is scope discipline. Because escalation is a designed output, I could not treat it as the place to dump anything hard, which forced the honest coverage analysis in `scripts/documentation_gap.py`.

## What would change my mind

Take the 93 escalations from the development run and have Sofia answer each with exactly one clarifying question, no lookups. If more than a quarter of them close without reaching tier two, then a single conversational turn is worth more than the whole routing apparatus adds, and the triage framing is too static — I would build a clarification loop in front of R-02 and re-run the cost model.

The other trigger is corpus coverage. If CloudServe close the documentation gaps and answerability rises past roughly 90%, the case for aggressive triage weakens because the expensive failure mode it exists to prevent becomes rare.
