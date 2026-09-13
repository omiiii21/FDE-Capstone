"""Access to the language model, and what happens when there isn't any.

The system has to keep working when the provider is down, throttling or simply
not configured (A11). That is handled by making the provider a component with
four implementations behind one interface, rather than by wrapping calls in
try/except at every site:

  OpenRouterProvider  the real thing, with backoff and a response cache
  OfflineProvider     always unavailable, used in tests and in CI
  NullProvider        chosen automatically when no key is configured
  UnsafeDemoProvider  fault injection, reachable only through PROVIDER=unsafe_demo

The fourth is there because of an awkward property of the first three. On the
default path nothing unsafe reaches the guardrails, so they never block, and
"we could not make it fire" is not a demonstration. It returns a draft that
deserves blocking so that A7 can be shown rather than argued.

Nothing above this module asks whether a provider is available. It asks for a
completion and gets either text or a ProviderUnavailable, and generate.py has a
grounded path that does not need a model at all. That is why an outage degrades
the system rather than stopping it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

# Serves FR-14: the handover note is produced whether or not a provider answers.

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class ProviderUnavailable(RuntimeError):
    """The model could not be reached, or refused. Callers degrade; they do not
    retry, because retrying is this module's job and it has already tried."""


class Provider(ABC):
    name = "abstract"

    @abstractmethod
    def complete(self, prompt: str, *, max_tokens: int = 700, temperature: float = 0.0) -> str: ...

    @property
    def available(self) -> bool:
        return True

    @property
    def version(self) -> str:
        return "n/a"


class NullProvider(Provider):
    """No key configured. Not an error - the extractive path in generate.py
    produces cited answers without a model, and a clean checkout with no key
    still processes the whole evaluation set."""

    name = "none"

    def complete(self, prompt: str, *, max_tokens: int = 700, temperature: float = 0.0) -> str:
        raise ProviderUnavailable("no model provider configured")

    @property
    def available(self) -> bool:
        return False


class OfflineProvider(NullProvider):
    """Deliberately unavailable. Used by the tests that prove A11 and by CI,
    which has no key and must not need one."""

    name = "offline"


class UnsafeDemoProvider(Provider):
    """A provider that deliberately returns a response the guardrails must stop.

    This exists because of an awkward property of the system as built. The
    extractive generator can only emit sentences that are already in the
    corpus, and the corpus contains no customer data; routing diverts the
    policy classes and the high-cost classes before anything is drafted. Between
    them, those two facts mean that on the default path the guardrails have
    nothing to catch. Across 500 development tickets, not one response was
    blocked - not because the checks are weak, but because nothing unsafe ever
    reached them.

    That is a good operational result and it is useless as evidence. A7 asks for
    a guardrail that blocks when triggered, and "we could not make it fire" is
    not a demonstration. So this returns what a language model having a bad day
    would return: the customer's own details quoted back, a refund promised, a
    delivery date invented, and a claim with no citation behind it.

    It is selected only by PROVIDER=unsafe_demo, it is named so that nobody
    reaches for it by accident, and it makes the system worse rather than
    better. Run:

        PROVIDER=unsafe_demo python -m evaluation.harness \
            --input data/guardrail_probe_tickets.json \
            --output evaluation/results/guardrail_demo
    """

    name = "unsafe_demo"

    @property
    def version(self) -> str:
        return "fault-injection, not a real provider"

    def complete(self, prompt: str, *, max_tokens: int = 700, temperature: float = 0.0) -> str:
        # Pull the customer's own details back out of the prompt, which is
        # exactly the mistake a model makes when a ticket is quoted into it.
        name_match = re.search(r"Body:\s*\n(.{0,400})", prompt, re.DOTALL)
        quoted = (name_match.group(1).strip().replace("\n", " ") if name_match else "")[:220]
        answer = (
            "Thanks for getting in touch. I can see the details you sent: "
            f"{quoted} "
            "I have issued a refund to the card on file and this will be fixed by the end of "
            "next week. The retention period is ninety days on every plan."
        )
        return json.dumps({"answer": answer, "citations": [1], "answered": True, "uncertain_about": ""})


class ResponseCache:
    """Cache keyed on the prompt. Saves the free tier during development, and
    makes a repeated run reproducible, which matters because A5 requires the
    same input to produce the same routing decision."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, prompt: str, model: str) -> str | None:
        key = hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()[:32]
        path = self._path(key)
        if path.exists():
            try:
                completion = json.loads(path.read_text(encoding="utf-8"))["completion"]
            except (json.JSONDecodeError, KeyError):
                path.unlink(missing_ok=True)
            else:
                self.hits += 1
                return completion
        self.misses += 1
        return None

    def put(self, prompt: str, model: str, completion: str) -> None:
        key = hashlib.sha256(f"{model}\n{prompt}".encode()).hexdigest()[:32]
        self._path(key).write_text(json.dumps({"model": model, "completion": completion}), encoding="utf-8")


class OpenRouterProvider(Provider):
    name = "openrouter"

    # 429 and the 5xx family are worth retrying; 400 and 401 are not, because
    # the same request will fail the same way and the backoff only delays the
    # moment the ticket gets a human.
    RETRYABLE = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    # How long the breaker stays open. Long enough that a run of a few hundred
    # tickets stops asking, short enough that a brief outage does not cost the
    # rest of the run its model path.
    CIRCUIT_SECONDS = 60.0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        cache: ResponseCache | None = None,
    ):
        self.api_key = api_key or settings.openrouter_api_key
        self.model = model or settings.model_name
        self.timeout = timeout if timeout is not None else settings.provider_timeout
        self.max_retries = max_retries if max_retries is not None else settings.provider_max_retries
        self.cache = cache
        if self.cache is None and settings.provider_cache:
            self.cache = ResponseCache(settings.storage_path / "provider_cache")
        self.calls = 0
        self.failures = 0
        # Set when a call fails in a way that means the next one will too, so
        # that a 120 ticket run does not spend twenty minutes discovering the
        # same outage 120 times. Reset by reset_circuit().
        self._circuit_open_until = 0.0

    @property
    def available(self) -> bool:
        return bool(self.api_key) and time.monotonic() >= self._circuit_open_until

    @property
    def version(self) -> str:
        return self.model

    def reset_circuit(self) -> None:
        self._circuit_open_until = 0.0

    def _open_circuit(self) -> None:
        """Stop asking for a minute.

        This is reached from two places and it used to be reached from one. A
        non-retryable status raises straight out of the loop, so it skipped the
        line at the bottom that opened the breaker, and a provider answering 401
        to everything was re-asked once per ticket for the whole run. That is
        the case the breaker was written for: a credential or a payload the
        provider will reject identically every time is the most persistent
        failure there is, not the least.
        """
        self._circuit_open_until = time.monotonic() + self.CIRCUIT_SECONDS

    def complete(self, prompt: str, *, max_tokens: int = 700, temperature: float = 0.0) -> str:
        if not self.api_key:
            raise ProviderUnavailable("OPENROUTER_API_KEY is not set")

        if self.cache is not None:
            cached = self.cache.get(prompt, self.model)
            if cached is not None:
                return cached

        if time.monotonic() < self._circuit_open_until:
            raise ProviderUnavailable("provider circuit open after repeated failures")

        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise ProviderUnavailable("requests is not installed") from exc

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter asks for these and they are not credentials.
            "HTTP-Referer": "https://github.com/omiiii21/cloudserve-triage",
            "X-Title": "CloudServe support triage",
        }

        last_error = "unknown"
        for attempt in range(self.max_retries):
            # Backing off after the last attempt spends the delay on a call that
            # is never made. With three retries that was up to sixteen seconds of
            # waiting per ticket to reach a conclusion already reached.
            more_attempts_to_come = attempt < self.max_retries - 1
            self.calls += 1
            try:
                response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=self.timeout)
            except Exception as exc:  # network layer: timeouts, DNS, reset connections
                last_error = f"{type(exc).__name__}: {exc}"
                self.failures += 1
                if more_attempts_to_come:
                    self._sleep(attempt)
                continue

            if response.status_code == 200:
                try:
                    text = response.json()["choices"][0]["message"]["content"]
                except (KeyError, IndexError, ValueError) as exc:
                    # A 200 with a shape we did not expect is a provider
                    # problem, not a bug here. Degrade rather than crash.
                    raise ProviderUnavailable(f"unexpected response shape: {exc}") from exc
                if self.cache is not None:
                    self.cache.put(prompt, self.model, text)
                return text

            last_error = f"HTTP {response.status_code}"
            self.failures += 1
            if response.status_code not in self.RETRYABLE:
                self._open_circuit()
                raise ProviderUnavailable(last_error)
            # Honour Retry-After when the provider sends one; free tiers do.
            wait = response.headers.get("Retry-After")
            if more_attempts_to_come:
                self._sleep(attempt, float(wait) if wait and wait.isdigit() else None)

        # Repeated failure means the provider is having a bad morning. Stop
        # asking for sixty seconds so the rest of the run is not held up.
        self._open_circuit()
        raise ProviderUnavailable(f"{self.max_retries} attempts failed, last was {last_error}")

    def _sleep(self, attempt: int, override: float | None = None) -> None:
        if override is not None:
            delay = min(override, 30.0)
        else:
            # Exponential with jitter. The jitter matters when the harness is
            # running tickets in parallel, or every worker retries in step.
            delay = min(2.0**attempt, 16.0) * (0.5 + random.random())
        time.sleep(delay)


def get_provider(force: str | None = None) -> Provider:
    """Pick a provider. PROVIDER=offline in the environment forces the degraded
    path, which is how the A11 tests and the CI pipeline run without a key."""
    choice = (force or os.getenv("PROVIDER", "")).strip().lower()
    if choice in ("offline", "none", "null"):
        return OfflineProvider()
    if choice in ("unsafe_demo", "unsafe"):
        log.warning(
            "PROVIDER=unsafe_demo selected. This returns deliberately unsafe drafts so that the "
            "guardrails can be seen blocking them. Never use it for anything else."
        )
        return UnsafeDemoProvider()
    if settings.openrouter_api_key:
        return OpenRouterProvider()
    log.info("no OPENROUTER_API_KEY found, running with the extractive generator only")
    return NullProvider()
