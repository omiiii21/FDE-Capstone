"""Tokenisation and the vocabulary bridge between customers and documentation.

The second half of this file exists because of one thing Ines said in her
interview: "Someone writes my deployment keeps dying and my article is called
resolving container health check failures. There is no path between those two
phrases in a keyword search." That is a vocabulary mismatch, and it is the
single biggest reason the existing internal search does not get used.

Every entry in EXPANSIONS was derived by taking a customer phrase that appears
in development_tickets.json and the article that labels.expected_doc_ids says
should answer it, then finding the term the article uses instead. The list is
built from the development set only; scripts/build_expansions.py regenerates
the candidates and the curation is mine.
"""

from __future__ import annotations

import re
from typing import Iterable

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_'\-]*")

# Words too common in this corpus to carry signal. Deliberately short - an
# aggressive stop list removes the "how do I" phrasing that distinguishes a
# question from an incident report.
STOPWORDS = frozenset(
    """
    a about after all also am an and any are as at be been being but by can cant could
    did do does doing done dont for from get gets getting had has have having he her
    here hers him his how i if in into is it its just like me more most my no not now
    of on once one only or other our ours out over own please really same she should
    so some such than that the their them then there these they this those to too
    up us very was we were what when where which while who why will with would you
    your yours
    """.split()
)

# Customer vocabulary on the left, documentation vocabulary on the right.
# Applied additively: the original terms are always kept, so an expansion can
# only ever add a way of matching, never remove one.
EXPANSIONS: dict[str, tuple[str, ...]] = {
    # deployment
    "dying": ("health", "check", "failure"),
    "died": ("health", "check", "failure"),
    "crashing": ("health", "check", "failure", "restart"),
    "stuck": ("timeout", "pending", "failure"),
    "hanging": ("timeout", "pending"),
    "rollback": ("revision", "previous", "release"),
    "revert": ("revision", "rollback", "previous"),
    "undo": ("rollback", "revision"),
    "redeploy": ("deployment", "release"),
    "pipeline": ("build", "deployment"),
    "build": ("dependency", "resolution", "deployment"),
    # authentication and access
    "login": ("credential", "authentication", "sign"),
    "signin": ("credential", "authentication", "login"),
    "locked": ("lockout", "locked", "failed", "attempts"),
    "lockout": ("locked", "failed", "attempts"),
    "password": ("credential", "authentication"),
    "2fa": ("mfa", "factor", "authentication"),
    "mfa": ("factor", "authentication"),
    "sso": ("saml", "identity", "provider", "single"),
    "saml": ("sso", "identity", "provider"),
    "permissions": ("role", "effective", "access"),
    "permission": ("role", "effective", "access"),
    "role": ("permission", "effective", "group"),
    "invite": ("invitation", "pending", "accepted"),
    "revoke": ("rotate", "key", "disable"),
    "rotate": ("key", "revoke", "credential"),
    # api
    "throttled": ("rate", "limit", "quota"),
    "throttling": ("rate", "limit", "quota"),
    "429": ("rate", "limit", "too", "many", "requests"),
    "401": ("unauthorised", "credential", "authentication", "key"),
    "403": ("permission", "forbidden", "role"),
    "500": ("error", "server"),
    "pagination": ("cursor", "page", "size"),
    "paging": ("cursor", "page"),
    "cursor": ("pagination", "page", "expiry"),
    "webhook": ("delivery", "signature", "retry", "endpoint"),
    "callback": ("webhook", "delivery", "endpoint"),
    "retries": ("retry", "backoff", "delivery"),
    # billing
    "invoice": ("billing", "charge", "period"),
    "bill": ("billing", "invoice", "charge"),
    "charged": ("billing", "charge", "usage"),
    "overage": ("quota", "usage", "limit", "plan"),
    "spend": ("usage", "billing", "cap"),
    "refund": ("billing", "credit", "charge"),
    # data
    "export": ("download", "extract", "archive"),
    "download": ("export", "link", "archive"),
    "backup": ("restore", "snapshot", "export"),
    "restore": ("backup", "snapshot", "recovery"),
    "residency": ("region", "location", "stored"),
    "gdpr": ("compliance", "residency", "retention", "region"),
    "retention": ("period", "deletion", "plan"),
    # performance
    "slow": ("latency", "degradation", "performance", "response"),
    "slowness": ("latency", "degradation", "performance"),
    "lag": ("latency", "delay"),
    "timeout": ("timeout", "deadline", "latency"),
    "p95": ("percentile", "latency"),
    "spike": ("peak", "load", "scaling"),
    "scaling": ("autoscaling", "capacity", "load"),
    "pool": ("connection", "pool", "exhaustion"),
    # general trouble words - these carry almost no signal on their own but
    # they do tell you it is an incident rather than a question
    "broken": ("failure", "error"),
    "failing": ("failure", "error"),
    "error": ("failure", "error"),
}

# Contractions and misspellings that appear in the non-fluent segment. These
# matter more than they look: 24% of the development set is marked non_fluent
# and the retrieval step is where that segment loses ground (see the fairness
# audit in docs/fairness_audit.md).
NORMALISATIONS = {
    "cant": "cannot",
    "wont": "will not",
    "dont": "do not",
    "isnt": "is not",
    "doesnt": "does not",
    "didnt": "did not",
    "im": "i am",
    "ive": "i have",
    "its": "it is",
    "pls": "please",
    "plz": "please",
    "thx": "thanks",
    "acct": "account",
    "auth": "authentication",
    "config": "configuration",
    "db": "database",
    "env": "environment",
    "prod": "production",
    "repo": "repository",
    "deploys": "deployment",
    "deploying": "deployment",
    "deployed": "deployment",
    "deploy": "deployment",
    "logs": "log",
    "keys": "key",
    "requests": "request",
    "limits": "limit",
    "errors": "error",
    "failures": "failure",
    "users": "user",
    "accounts": "account",
}


def _fold(token: str) -> str:
    token = NORMALISATIONS.get(token, token)
    # Crude plural stripping. A real stemmer would be better but pulling in
    # nltk for this corpus is not a trade I would make; measured difference on
    # the development set was under a point (scripts/tune_retrieval.py).
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenise(text: str, *, drop_stopwords: bool = True) -> list[str]:
    tokens = []
    for raw in _TOKEN.findall(text.lower()):
        folded = _fold(raw)
        for part in folded.split():
            if drop_stopwords and part in STOPWORDS:
                continue
            if len(part) < 2 and not part.isdigit():
                continue
            tokens.append(part)
    return tokens


def expand_query(tokens: Iterable[str]) -> list[str]:
    """Add documentation vocabulary alongside the customer's own words."""
    out = list(tokens)
    seen = set(out)
    for token in list(out):
        for extra in EXPANSIONS.get(token, ()):  # noqa: B007
            if extra not in seen:
                seen.add(extra)
                out.append(extra)
    return out
