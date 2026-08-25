"""Configuration, read once from the environment at import time.

Everything tunable lives here rather than being scattered through the modules,
because the threshold values in particular get argued about and I wanted a
single place to look when the answer to "what was it set to on that run" matters.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is in requirements, but the system should not need it
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - only hit on a broken install
    pass


REPO_ROOT = Path(__file__).resolve().parent.parent


def _path(name: str, default: str) -> Path:
    value = os.getenv(name, default)
    p = Path(value)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # --- model provider -------------------------------------------------
    openrouter_api_key: str | None = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY") or None)
    model_name: str = field(
        default_factory=lambda: os.getenv("MODEL_NAME", "meta-llama/llama-3.1-8b-instruct")
    )
    provider_timeout: float = field(default_factory=lambda: _float("PROVIDER_TIMEOUT", 25.0))
    provider_max_retries: int = field(default_factory=lambda: _int("PROVIDER_MAX_RETRIES", 3))
    provider_cache: bool = field(
        default_factory=lambda: os.getenv("PROVIDER_CACHE", "1") not in ("0", "false", "False")
    )

    # --- retrieval ------------------------------------------------------
    retrieval_top_k: int = field(default_factory=lambda: _int("RETRIEVAL_TOP_K", 5))
    # Below this BM25 score a passage is treated as noise and dropped. Set from
    # scripts/tune_retrieval.py against the development set; see ADR-002.
    retrieval_floor: float = field(default_factory=lambda: _float("RETRIEVAL_FLOOR", 4.2))
    retrieval_backend: str = field(default_factory=lambda: os.getenv("RETRIEVAL_BACKEND", "lexical"))

    # --- routing --------------------------------------------------------
    # Chosen from the cost curve in scripts/tune_threshold.py, not by eye.
    confidence_threshold: float = field(default_factory=lambda: _float("CONFIDENCE_THRESHOLD", 0.62))
    # A separate, higher bar for the intents where a wrong answer is expensive.
    high_cost_threshold: float = field(default_factory=lambda: _float("HIGH_COST_THRESHOLD", 0.85))

    # --- paths ----------------------------------------------------------
    corpus_path: Path = field(default_factory=lambda: _path("CORPUS_PATH", "data/documentation.json"))
    model_path: Path = field(default_factory=lambda: _path("MODEL_PATH", "models/intent_classifier.json"))
    storage_path: Path = field(default_factory=lambda: _path("STORAGE_PATH", "storage"))
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./storage/decisions.db")
    )

    # --- operations -----------------------------------------------------
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    metrics_port: int = field(default_factory=lambda: _int("METRICS_PORT", 8001))
    # The kill switch. When this file exists the system stops auto-responding
    # and routes everything to a human. See docs/incident_response.md.
    kill_switch_path: Path = field(default_factory=lambda: _path("KILL_SWITCH_PATH", "storage/HALT"))

    @property
    def sqlite_path(self) -> Path:
        """Filesystem path behind database_url, for the sqlite case."""
        url = self.database_url
        if not url.startswith("sqlite"):
            raise ValueError(f"only sqlite is supported for the decision log, got {url!r}")
        raw = url.split("///", 1)[1]
        p = Path(raw)
        return p if p.is_absolute() else (REPO_ROOT / p)

    def auto_responses_halted(self) -> bool:
        return self.kill_switch_path.exists()


settings = Settings()

# 22 intent classes, as they appear in the labels block of the ticket schema.
INTENTS = (
    "account_access",
    "api_key_issue",
    "api_usage_question",
    "authentication_failure",
    "billing_query",
    "compliance_request",
    "configuration_help",
    "data_export",
    "data_residency",
    "database_issue",
    "deployment_failure",
    "feature_request",
    "integration_help",
    "onboarding",
    "performance_degradation",
    "quota_or_overage",
    "rate_limit",
    "rollback_request",
    "security_incident",
    "sso_configuration",
    "unclear_request",
    "webhook_issue",
)

URGENCIES = ("low", "medium", "high")

CHANNELS = ("email", "chat", "docs_comment", "forum")

# Classes that never auto-respond whatever the confidence says. Three of these
# come straight from Daniel's interview (security, billing disputes, data
# location); feature_request and unclear_request are here because there is
# nothing in the corpus to ground an answer in. Matches the
# labels.must_not_auto_respond flag in the data - verified in
# scripts/check_policy_classes.py.
NEVER_AUTO_RESPOND = frozenset(
    {
        "security_incident",
        "compliance_request",
        "feature_request",
        "unclear_request",
    }
)

# Classes where we answer, but only on a higher confidence bar, because a wrong
# answer costs more than a slow one. Marcus: "I would rather it said nothing
# than said something wrong."
HIGH_COST_INTENTS = frozenset(
    {
        "billing_query",
        "quota_or_overage",
        "data_residency",
        "api_key_issue",
        "database_issue",
    }
)
