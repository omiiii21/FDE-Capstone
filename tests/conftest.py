"""Fixtures shared across the suite.

Two rules are enforced here rather than remembered in each test file. The first
is that nothing reaches the network: the provider is forced offline for the
whole session and requests.post is replaced with something that refuses. The
second is that nothing writes into the repository - every path a test writes to
comes from tmp_path, including the decision log, so running the tests never
disturbs storage/ or evaluation/results/.

Run with: python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.classify import Classifier  # noqa: E402
from src.ingest import load_tickets  # noqa: E402
from src.logging_store import DecisionLog  # noqa: E402
from src.pipeline import Pipeline  # noqa: E402
from src.provider import OfflineProvider  # noqa: E402
from src.retrieve import Retriever  # noqa: E402


def _refuse_network(*args, **kwargs):
    raise AssertionError("a test tried to make a real HTTP call; patch requests.post in the test instead")


@pytest.fixture(autouse=True, scope="session")
def offline_only():
    """A12: the suite must pass with no API key and no network.

    PROVIDER is read by get_provider() at call time, so setting it in the
    environment is enough to force the degraded path everywhere. The key is
    removed as well, otherwise a developer with one exported in their shell
    would be running a different code path from CI.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PROVIDER", "offline")
        mp.delenv("OPENROUTER_API_KEY", raising=False)
        mp.setattr(requests, "post", _refuse_network)
        yield


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def corpus_path(repo_root: Path) -> Path:
    return repo_root / "data" / "documentation.json"


@pytest.fixture(scope="session")
def retriever(corpus_path: Path) -> Retriever:
    """Session-scoped because building the index chunks and tokenises all 29
    articles, and rebuilding it per test would dominate the runtime."""
    return Retriever(corpus_path)


@pytest.fixture(scope="session")
def classifier(repo_root: Path) -> Classifier:
    """Session-scoped for the same reason: the artefact is half a megabyte of
    JSON and loading it repeatedly proves nothing."""
    return Classifier(repo_root / "models" / "intent_classifier.json")


@pytest.fixture(scope="session")
def sample_tickets(repo_root: Path):
    return load_tickets(repo_root / "data" / "validation_tickets.json")


@pytest.fixture
def tmp_log(tmp_path: Path) -> Iterator[DecisionLog]:
    log = DecisionLog(tmp_path / "decisions.db", run_id="test-run")
    yield log
    log.close()


@pytest.fixture
def pipeline(classifier: Classifier, retriever: Retriever, tmp_log: DecisionLog) -> Pipeline:
    """The whole system, wired to an offline provider and a throwaway log.

    The provider is passed explicitly rather than left to get_provider() so the
    test is independent of the environment it happens to run in.
    """
    return Pipeline(
        classifier=classifier,
        retriever=retriever,
        provider=OfflineProvider(),
        decision_log=tmp_log,
    )
