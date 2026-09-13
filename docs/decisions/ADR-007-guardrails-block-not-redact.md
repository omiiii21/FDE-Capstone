# ADR-007: Guardrails block, and none of them call the model

## Status

Accepted, 5 September 2026.

## Context

Five checks run on everything the system is about to send: private data, injection, grounding, commitments and the confidence floor. Two questions about how they work have an obvious answer that I think is wrong.

The first is whether a check that finds something should redact it or stop the message. Redaction is appealing because it keeps the automation rate up — strip the email address, send the rest, nobody waits. It assumes something I cannot support: that finding one identifying string means I understand what else is in the text. If a reply contains a customer identifier, the interesting question is not how to remove it but why it is there, and that needs a person.

The second is whether the checks can use the language model. Asking a model whether a draft is grounded is more capable than any regular expression, and PR-03 does exactly that in evaluation, where being slow and needing a provider is fine. As a runtime control it is a bad idea for one reason: a guardrail that depends on an external provider fails open during exactly the outage it exists to protect against, when drafts are coming from a degraded path and nobody is watching.

## Decision

Everything in `src/guardrails.py` is deterministic, local and blocking. No network calls, no model, no warn-only mode — a guardrail that only warns is not a guardrail. All five run every time, even after one has already failed, so the decision log records what each one found; knowing that a blocked reply also made a commitment is what tells you whether the block was lucky or intended.

Blocked means a person receives the ticket with the block reason on the front of the handover note. It never means a redacted reply reaches the customer.

Injection is checked on the incoming ticket rather than the outgoing response, and recorded whether or not the ticket would have been answered: the attempt is the signal, and the second attempt is usually better than the first.

Grounding checks citations at paragraph level, not sentence level. That is a correction rather than an initial design. Sentence-level checking flagged the very claim the citation was attached to: the marker sits at the end of the passage it supports, so the sentences carrying the facts scan as uncited. The paragraph is the unit that matches how the prose is written.

## Consequences

On the 500-ticket development run, nothing was blocked at all. Not one of the 407 released responses failed a check, and the 93 escalations were diverted by routing before there was a reply to check. Private data fired zero times, and there were zero breaches of the 87 tickets flagged `must_not_auto_respond`.

That zero is worth reading twice, because it is not evidence that the checks are strong. It is evidence that nothing unsafe reached them: routing takes the policy classes and the high-cost classes out first, and the extractive generator can only emit sentences that already exist in the corpus. The evidence that these block is `tests/test_guardrails.py` and the fault-injection run described below.

The patterns are narrow on purpose. The customer identifier rule matches `CUST-nnnn` rather than any long digit string, because a broad rule flagged port numbers and byte counts in every second deployment answer, and a guardrail that cries wolf gets switched off.

What these checks cannot do is tell you that a cited passage fails to support the sentence attached to it. The grounding check knows a sentence claims something and points at nothing; it does not read. Semantic entailment is PR-03's job, in evaluation only. I would rather state that gap than let the word "grounding" imply more than the code does.

## What would change my mind

There is no sample to draw from on the default path, which is itself the finding. Once the escalation path stopped being checked as though it were an outgoing reply, the dev run blocked nothing at all: routing diverts the policy classes and the high-cost classes before anything is drafted, and the extractive generator can only emit sentences that are already in the corpus. The checks are not weak; nothing unsafe reaches them.

So the experiment has to be run against drafts that could be unsafe. `PROVIDER=unsafe_demo` produces them deliberately, and the way to change my mind is to point the model path at the full development set once a key is available, then have Ines mark every blocked reply as one she would have sent unchanged. If more than a third are sendable, the paragraph rule is over-blocking, and I would keep the paragraph as the unit but carry a citation forward across paragraph breaks within the same retrieved passage rather than resetting at every blank line.

On redaction, the private-data check fired zero times on 500 tickets, so the argument is currently theoretical. What would make me revisit it is a run where it blocks a material share of replies and every single block is the customer's own identifier quoted back to them. That is one narrow pattern with a safe fix, and at that point a redact-and-send exception for that pattern alone would be worth arguing for. Any other match still goes to a human.
