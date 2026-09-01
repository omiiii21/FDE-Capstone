"""Access to the language model, and what happens when there isn't any.

The system has to keep working when the provider is down, throttling or simply
not configured (A11). That is handled by making the provider a component with
three implementations behind one interface, rather than by wrapping calls in
try/except at every site:

  OpenRouterProvider  the real thing, with backoff and a response cache
  OfflineProvider     always unavailable, used in tests and in CI
  NullProvider        chosen automatically when no key is configured

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
import time
from abc import ABC, abstractmethod
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

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
            self.calls += 1
            try:
                response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=self.timeout)
            except Exception as exc:  # network layer: timeouts, DNS, reset connections
                last_error = f"{type(exc).__name__}: {exc}"
                self.failures += 1
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
                raise ProviderUnavailable(last_error)
            # Honour Retry-After when the provider sends one; free tiers do.
            wait = response.headers.get("Retry-After")
            self._sleep(attempt, float(wait) if wait and wait.isdigit() else None)

        # Repeated failure means the provider is having a bad morning. Stop
        # asking for sixty seconds so the rest of the run is not held up.
        self._circuit_open_until = time.monotonic() + 60.0
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
    if settings.openrouter_api_key:
        return OpenRouterProvider()
    log.info("no OPENROUTER_API_KEY found, running with the extractive generator only")
    return NullProvider()
