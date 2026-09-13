# ADR-004: The model provider is a component that is allowed to be absent

## Status

Accepted, 1 September 2026.

## Context

Support automation that stops working when a third-party API has a bad morning is worse than no automation, because the queue has already been sized on the assumption that it works. CloudServe are a 150-person company buying inference from a vendor on a free or near-free tier. Rate limits, timeouts and 503s are the normal operating condition, not the exception.

The usual handling is a `try`/`except` at each call site with a sensible-looking message in the `except` branch. That fails twice over: the handling drifts apart between sites, and the fallback ends up being whatever was cheapest to write there, which is usually an apology. An apology is not a support reply.

There is a second requirement pulling in the same direction. The evaluation harness has to run unattended on a machine I do not own, pointed at a file I have not seen, with nobody watching and no API key present. If the system needs a key to complete a run, it fails the gate.

## Decision

The provider is a component with one interface and four implementations in `src/provider.py`. `OpenRouterProvider` does the real work, with exponential backoff and jitter, `Retry-After` honoured when the provider sends one, a prompt-keyed response cache, and a circuit breaker that stops calling for 60 seconds after repeated failure so that a long run does not rediscover the same outage on every ticket. `OfflineProvider` is unavailable on purpose, for the tests and for CI. `NullProvider` is selected automatically when no key is configured, and logged as a normal condition.

Nothing above this module asks whether a provider is available. It asks for a completion and receives either text or a `ProviderUnavailable`, and callers do not retry, because retrying is the module's job and it has already tried.

The part that makes this work is not the provider abstraction, it is `ExtractiveGenerator` in `src/generate.py`. It composes the reply from sentences that exist in the retrieved passages and attaches the same citation markers the model path would. It cannot hallucinate, because it cannot write a sentence the documentation does not contain. That is the floor the system degrades to, and it is a designed answer path, not a stub — the model path is preferred when a provider is reachable, and the extractive path is what remains when it is not.

## Consequences

The whole evaluation run completes with no key at all. Both `evaluation/results/dev_run/` and `evaluation/results/validation_run/` were produced that way, which is why the 80-ticket validation file finishes in 0.07 seconds, around a thousand tickets a second: on the default path nothing makes a network call. The 500-ticket run reached 81.4% first contact resolution and 90.3% citation accuracy on that path.

`/healthz` in `src/api.py` returns 200 even when the provider is unreachable, which is deliberate. Liveness is not readiness, and a health check that failed during an outage would pull the service out of rotation while it was still answering tickets.

What this costs is prose quality. Extractive replies read stiffly — they are stitched from resolution steps and one cause sentence — and a customer can tell. I am comfortable with that trade because the alternative degradation is silence or invention, but it is the honest downside and it is not small.

## What would change my mind

Run the same 500 tickets twice, once with a key and once without, and compare verified resolution — answered automatically and citing a document the labels agree with — against the 90.3% the extractive path achieves. If the model path is not meaningfully better, `ModelGenerator` is carrying dependency and prompt-maintenance cost for nothing and should be deleted rather than kept as the preferred path.

The reverse would also move me. The extractive run blocks nothing, because nothing it can write is unsafe, so the comparison to make is how often the model path is blocked on the same tickets. If the model path is clearly better on verified resolution and is blocked on fewer than a few per cent of them, then degrading to the extractive path during an outage is a real quality regression rather than a graceful one, and CloudServe should be told to budget for a paid tier with an uptime commitment instead of relying on it.
