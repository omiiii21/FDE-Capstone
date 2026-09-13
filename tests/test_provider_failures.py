"""What happens when the model provider does not cooperate.

A11 is the criterion: no-retrieval-hit, provider timeout or outage, rate
limiting and malformed input, none of which may crash the run. The provider is
a component with implementations behind one interface precisely so that these
paths can be tested without a network, and every test here proves the same
thing from a different direction - the ticket still gets an answer or a human,
and the answer still carries citations that resolve.

requests.post is patched in every test that needs it. Nothing here makes a real
call; conftest replaces requests.post with something that refuses, so an
accidental live call fails rather than hanging on a socket.
"""

from __future__ import annotations

import json

import pytest
import requests

from src.generate import ExtractiveGenerator, ModelGenerator
from src.pipeline import Pipeline
from src.provider import (
    OfflineProvider,
    OpenRouterProvider,
    Provider,
    ProviderUnavailable,
    ResponseCache,
    get_provider,
)
from src.schema import Ticket

TICKET = Ticket(
    "PRV-1",
    "email",
    "Deployment rolling back",
    "My deployment keeps dying at the health check phase and I cannot ship.",
    "",
)


class FakeResponse:
    """The parts of requests.Response that OpenRouterProvider actually reads."""

    def __init__(self, status_code, payload=None, headers=None, body_is_not_json=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self._body_is_not_json = body_is_not_json

    def json(self):
        if self._body_is_not_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def completion(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


class UnavailableProvider(Provider):
    """Reports itself as available and then fails - the outage you find out
    about mid-run rather than at startup."""

    name = "unavailable"

    def complete(self, prompt, *, max_tokens=700, temperature=0.0):
        raise ProviderUnavailable("the provider is having a bad morning")


class ProseProvider(Provider):
    """Returns confident prose where the prompt asked for JSON. Small models do
    this however firmly you ask them not to."""

    name = "prose"

    def complete(self, prompt, *, max_tokens=700, temperature=0.0):
        return "Certainly! Here is the answer you asked for, with no JSON in sight."


class UnexpectedShapeProvider(Provider):
    """Valid JSON, none of the fields the prompt specified."""

    name = "unexpected-shape"

    def complete(self, prompt, *, max_tokens=700, temperature=0.0):
        return json.dumps({"status": "ok", "data": [1, 2, 3]})


@pytest.fixture
def make_pipeline(classifier, retriever, tmp_log):
    def build(provider):
        return Pipeline(
            classifier=classifier,
            retriever=retriever,
            provider=provider,
            decision_log=tmp_log,
        )

    return build


@pytest.fixture
def provider_factory(tmp_path):
    """OpenRouterProvider with its cache pointed at tmp_path.

    Passing the cache explicitly matters: left to itself the constructor builds
    one under STORAGE_PATH, and the test suite would start writing into the
    repository's storage/ directory.
    """
    created = []

    def build(**kwargs):
        provider = OpenRouterProvider(
            api_key="test-key-not-a-real-one",
            cache=ResponseCache(tmp_path / f"cache-{len(created)}"),
            **kwargs,
        )
        created.append(provider)
        return provider

    return build


# A11: an outage degrades the answer, it does not lose the ticket.
def test_a_provider_outage_still_produces_a_cited_reply(make_pipeline):
    response = make_pipeline(UnavailableProvider()).process(TICKET)

    assert response.action == "auto_respond"
    assert response.generation_method == "extractive:provider-unavailable"
    assert response.citations
    retrieved = {p.doc_id for p in response.passages}
    assert all(c["doc_id"] in retrieved for c in response.citations)


def test_unparseable_model_output_falls_back_to_the_extractive_draft(make_pipeline):
    response = make_pipeline(ProseProvider()).process(TICKET)

    assert response.action == "auto_respond"
    assert response.generation_method == "extractive:unparseable-model-output"
    assert response.citations


def test_a_payload_with_no_answer_field_falls_back_like_any_other_fault(make_pipeline):
    # Valid JSON with no "answer" field is a malformed payload, not a considered
    # refusal. It used to be read as the model declining, which sent the ticket
    # to a human and lost an answer the extractive generator could have produced
    # from the same passages. It now degrades the same way every other provider
    # fault does. A model that genuinely sets answered=false is a different case
    # and still escalates, which the next test covers.
    response = make_pipeline(UnexpectedShapeProvider()).process(TICKET)

    assert response.action == "auto_respond"
    assert response.generation_method == "extractive:model-returned-no-answer-field"
    assert response.citations
    assert response.error == ""


def test_the_offline_provider_is_never_available():
    provider = OfflineProvider()

    assert provider.available is False
    assert provider.name == "offline"
    with pytest.raises(ProviderUnavailable):
        provider.complete("anything")


def test_the_environment_forces_the_offline_provider():
    # conftest sets PROVIDER=offline for the session; this is the assertion that
    # the rest of the suite is running the degraded path it claims to be.
    assert isinstance(get_provider(), OfflineProvider)


# A9/A11: a full run completes with no provider at all.
def test_a_twenty_ticket_run_completes_with_the_offline_provider(make_pipeline, sample_tickets):
    responses = [make_pipeline(OfflineProvider()).process(t) for t in sample_tickets[:20]]

    assert len(responses) == 20
    assert all(r.action in {"auto_respond", "escalate", "blocked"} for r in responses)
    assert all(not r.error for r in responses)
    assert any(r.action == "auto_respond" for r in responses)


# A11: rate limiting. The 429 is retried, Retry-After is honoured, and the
# second attempt returns the answer.
def test_a_rate_limited_call_is_retried_and_then_succeeds(provider_factory, monkeypatch):
    attempts = []

    def post(url, **kwargs):
        attempts.append(url)
        if len(attempts) == 1:
            return FakeResponse(429, headers={"Retry-After": "0"})
        return FakeResponse(200, completion("the drafted answer"))

    monkeypatch.setattr(requests, "post", post)
    provider = provider_factory(max_retries=3)

    assert provider.complete("a prompt") == "the drafted answer"
    assert len(attempts) == 2
    assert provider.failures == 1


def test_a_timeout_is_retried_and_then_reported_as_unavailable(provider_factory, monkeypatch):
    attempts = []

    def post(url, **kwargs):
        attempts.append(url)
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr(requests, "post", post)
    provider = provider_factory(max_retries=2)
    monkeypatch.setattr(provider, "_sleep", lambda *args, **kwargs: None)

    with pytest.raises(ProviderUnavailable) as caught:
        provider.complete("a prompt")

    assert len(attempts) == 2
    assert "2 attempts failed" in str(caught.value)


def test_a_200_whose_body_is_not_json_degrades(provider_factory, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: FakeResponse(200, body_is_not_json=True))
    provider = provider_factory(max_retries=2)

    with pytest.raises(ProviderUnavailable) as caught:
        provider.complete("a prompt")

    assert "unexpected response shape" in str(caught.value)


def test_a_200_with_an_unexpected_shape_degrades(provider_factory, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: FakeResponse(200, {"result": "nope"}))
    provider = provider_factory(max_retries=2)

    with pytest.raises(ProviderUnavailable):
        provider.complete("a prompt")


def test_a_non_retryable_status_is_not_retried(provider_factory, monkeypatch):
    # 401 fails the same way every time; retrying only delays the moment the
    # ticket reaches a human.
    attempts = []

    def post(url, **kwargs):
        attempts.append(url)
        return FakeResponse(401)

    monkeypatch.setattr(requests, "post", post)
    provider = provider_factory(max_retries=3)

    with pytest.raises(ProviderUnavailable):
        provider.complete("a prompt")

    assert len(attempts) == 1


# A11: the circuit breaker is what stops a 120 ticket run discovering the same
# outage 120 times.
def test_the_circuit_opens_after_max_retries_and_reset_closes_it(provider_factory, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: FakeResponse(503))
    provider = provider_factory(max_retries=2)
    monkeypatch.setattr(provider, "_sleep", lambda *args, **kwargs: None)
    assert provider.available is True

    with pytest.raises(ProviderUnavailable):
        provider.complete("a prompt")

    assert provider.available is False
    with pytest.raises(ProviderUnavailable, match="circuit open"):
        provider.complete("a different prompt")

    provider.reset_circuit()
    assert provider.available is True


def test_the_response_cache_round_trips(tmp_path):
    cache = ResponseCache(tmp_path / "cache")

    assert cache.get("a prompt", "a-model") is None
    cache.put("a prompt", "a-model", "a completion")

    assert cache.get("a prompt", "a-model") == "a completion"
    # Keyed on the model as well as the prompt, so switching models does not
    # silently serve the previous model's answers.
    assert cache.get("a prompt", "another-model") is None


def test_a_corrupt_cache_entry_is_discarded_rather_than_raised(tmp_path):
    cache = ResponseCache(tmp_path / "cache")
    cache.put("a prompt", "a-model", "a completion")
    entry = next(iter(cache.directory.iterdir()))
    entry.write_text("{ this is not json", encoding="utf-8")

    assert cache.get("a prompt", "a-model") is None
    assert not entry.exists()


def test_the_extractive_generator_needs_no_provider_at_all(retriever):
    # This is the reason an outage degrades rather than stops: the fallback is a
    # real generator with real citations, not an apology.
    passages = retriever.search(TICKET.text)
    draft = ExtractiveGenerator().draft(TICKET, passages)

    assert draft.answered
    assert draft.citations
    assert all(c["doc_id"] in {p.doc_id for p in passages} for c in draft.citations)


def test_the_model_generator_declines_when_nothing_was_retrieved(retriever):
    # A11's no-retrieval-hit case. Answering from nothing is the failure mode
    # the relevance floor exists to prevent, so the generator must not try.
    draft = ModelGenerator(UnavailableProvider()).draft(TICKET, [])

    assert not draft.answered
    assert draft.citations == []


def test_a_ticket_that_retrieves_nothing_escalates_without_error(make_pipeline):
    ticket = Ticket("PRV-2", "chat", "", "what is the weather in paris", "")

    response = make_pipeline(UnavailableProvider()).process(ticket)

    assert response.passages == []
    assert response.action == "escalate"
    assert response.escalation_note.strip()
    assert response.error == ""


def test_the_decision_log_still_records_every_stage_during_an_outage(make_pipeline, tmp_log):
    make_pipeline(UnavailableProvider()).process(TICKET)

    rows = tmp_log.for_ticket("PRV-1")
    assert [row["stage"] for row in rows] == [
        "classification",
        "retrieval",
        "routing",
        "generation",
        "validation",
    ]


def test_a_decision_log_written_during_an_outage_exports_to_jsonl(make_pipeline, tmp_log, tmp_path):
    make_pipeline(UnavailableProvider()).process(TICKET)
    destination = tmp_path / "exported.jsonl"

    written = tmp_log.export_jsonl(destination, run_id="test-run")

    assert written == 5
    lines = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert {line["ticket_id"] for line in lines} == {"PRV-1"}


# A11 again, and the case the circuit breaker was missing. A non-retryable
# status raises straight out of the loop, so it skipped the line at the bottom
# that opened the breaker: a provider answering 401 to everything was asked
# again on every ticket, 120 times over a 120 ticket run.
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_persistent_non_retryable_status_opens_the_circuit(provider_factory, monkeypatch, status):
    attempts = []

    def post(url, **kwargs):
        attempts.append(url)
        return FakeResponse(status)

    monkeypatch.setattr(requests, "post", post)
    provider = provider_factory(max_retries=3)

    with pytest.raises(ProviderUnavailable):
        provider.complete("the first ticket")

    assert len(attempts) == 1
    assert provider.available is False

    # The second ticket of the run must not reach the provider at all.
    with pytest.raises(ProviderUnavailable, match="circuit open"):
        provider.complete("the second ticket")

    assert len(attempts) == 1
    provider.reset_circuit()
    assert provider.available is True


def test_a_run_of_tickets_hits_a_broken_provider_once_not_once_each(provider_factory, monkeypatch):
    # The behaviour the breaker exists for, stated as the thing that was wrong.
    attempts = []
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: attempts.append(url) or FakeResponse(401))
    provider = provider_factory(max_retries=3)

    for index in range(20):
        with pytest.raises(ProviderUnavailable):
            provider.complete(f"ticket {index}")

    assert len(attempts) == 1


def test_the_backoff_is_not_spent_after_the_final_attempt(provider_factory, monkeypatch):
    # Sleeping after the last attempt delays a call that will never be made. With
    # three retries that was up to sixteen seconds per ticket buying nothing.
    slept = []
    monkeypatch.setattr(requests, "post", lambda url, **kwargs: FakeResponse(503))
    provider = provider_factory(max_retries=3)
    monkeypatch.setattr(provider, "_sleep", lambda *args, **kwargs: slept.append(args))

    with pytest.raises(ProviderUnavailable):
        provider.complete("a prompt")

    assert provider.calls == 3
    assert len(slept) == 2


def test_a_single_attempt_configuration_never_sleeps(provider_factory, monkeypatch):
    slept = []
    monkeypatch.setattr(
        requests, "post", lambda url, **kwargs: FakeResponse(429, headers={"Retry-After": "5"})
    )
    provider = provider_factory(max_retries=1)
    monkeypatch.setattr(provider, "_sleep", lambda *args, **kwargs: slept.append(args))

    with pytest.raises(ProviderUnavailable):
        provider.complete("a prompt")

    assert provider.calls == 1
    assert slept == []


def test_a_network_error_on_the_last_attempt_does_not_sleep_either(provider_factory, monkeypatch):
    slept = []

    def post(url, **kwargs):
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr(requests, "post", post)
    provider = provider_factory(max_retries=2)
    monkeypatch.setattr(provider, "_sleep", lambda *args, **kwargs: slept.append(args))

    with pytest.raises(ProviderUnavailable):
        provider.complete("a prompt")

    assert provider.calls == 2
    assert len(slept) == 1
