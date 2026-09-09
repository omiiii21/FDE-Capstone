# Decision records

These are the seven decisions on this project that were arguable, where I picked one option over another that a reasonable person would have picked instead, and where the reasoning is not visible from reading the code. Each one carries the numbers that decided it, including the ones that argue against the choice. Three of them record a false start: temperature scaling in ADR-003, the retrieval score as a coverage signal in ADR-002, and sentence-level citation checking in ADR-007. Things that were never in doubt — SQLite for the decision log, JSON for the model artefact — are explained in the module docstrings instead and are not written up here.

| ADR | Date | Decision |
| --- | --- | --- |
| [ADR-001](ADR-001-triage-not-chatbot.md) | 24 Aug 2026 | Build triage with assisted escalation rather than the chatbot the brief asked for; 28.6% of tickets have no grounded answer available and no interface fixes that. |
| [ADR-002](ADR-002-retrieval-backend.md) | 27 Aug 2026 | Lexical BM25 over section-aware chunks stays the default; the dense backend is optional because 800MB of dependencies, not a decisive quality margin, settled it. |
| [ADR-003](ADR-003-linear-classifier-over-llm.md) | 28 Aug 2026 | Intent comes from a calibrated linear model in numpy rather than a model call, because a threshold needs a confidence that moves with correctness. |
| [ADR-004](ADR-004-provider-degradation.md) | 1 Sep 2026 | The provider is a component with three implementations and an extractive floor behind it, so the whole evaluation run completes with no API key. |
| [ADR-005](ADR-005-body-disjoint-evaluation.md) | 30 Aug 2026 | Every classifier figure uses a split grouped by ticket body; the random split reports 99.2% against a true 92.2%. |
| [ADR-006](ADR-006-urgency-policy-not-model.md) | 3 Sep 2026 | Urgency ships as a policy table, superseding FR-03: the labels contradict themselves on 67 of 215 distinct bodies and the oracle ceiling is 61.0%. |
| [ADR-007](ADR-007-guardrails-block-not-redact.md) | 5 Sep 2026 | Guardrails block rather than redact, and none of them call the model, because a check that needs a provider fails open during an outage. |
