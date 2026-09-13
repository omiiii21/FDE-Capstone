"""The checks that run on every response and can stop it being sent.

Two things about this module are deliberate.

First, none of these checks calls the language model. A guardrail that depends
on an external provider fails open during exactly the outage it exists to
protect against, and "the model usually catches it" is a hope rather than a
control. Everything here is deterministic and local.

Second, they block rather than redact. The private data check in particular
does not strip an email address and send the rest - if a response contains
something identifying, the safe assumption is that we do not understand what
else it contains, so it goes to a human.

The checks run in order of severity and all of them run every time, because the
decision log should record what each one found whether or not an earlier one
already blocked (governance framework, section 4).

Serves FR-10, NFR-04. Acceptance criterion A7.
"""

from __future__ import annotations

import re
from typing import Sequence

from .schema import GuardrailResult, Passage, Ticket

# --- private data ------------------------------------------------------
# Tuned against the corpus and the development set. The account number pattern
# is intentionally narrow: CloudServe identifiers are CUST-nnnn, and a broad
# "any long digit string" rule flagged port numbers and byte counts in every
# second deployment answer, which is how a guardrail gets switched off.
PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email_address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")),
    ("customer_id", re.compile(r"\bCUST-\d{3,}\b")),
    ("api_key", re.compile(r"\b(?:sk|pk|api|key|tok|ghp|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b", re.I)),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}", re.I)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)

# --- commitments the system is not entitled to make --------------------
# Straight from Daniel: "Billing disputes, because those become contractual
# quickly and nothing automated should be making commitments about money."
COMMITMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Two patterns rather than one, and both of them need CloudServe to be the
    # party doing something. The version this replaces fired on the bare phrase
    # "a refund" or "a credit", which is the topic rather than a commitment:
    # DOC-BILL-002 says "where a refund rather than a credit is required, an
    # account owner should raise the request within thirty days", and a reply
    # that correctly quotes the refund policy was blocked for explaining it.
    # What matters is the system saying the money has already moved.
    (
        "refund_issued",
        re.compile(
            r"\b(?:i|we)(?:\s+have|\s+had|'ve)?\s+(?:already\s+|now\s+|just\s+)?"
            r"(?:issued|processed|approved|applied|arranged|actioned|authorised|authorized)\b"
            r"[^.\n]{0,40}\b(?:refund|credit|rebate|reimbursement)\b",
            re.I,
        ),
    ),
    (
        "refund_confirmed",
        re.compile(
            r"\b(?:a|an|the|your|this)\s+(?:\w+\s+){0,2}?(?:refund|credit|rebate|reimbursement)\b"
            r"[^.\n]{0,30}\b(?:has|have)\s+been\s+"
            r"(?:issued|processed|approved|applied|arranged|actioned|authorised|authorized)\b",
            re.I,
        ),
    ),
    (
        "refund_promise",
        re.compile(r"\b(?:will|shall|going to)\s+(?:be\s+)?(?:refund|credit|reimburse|waive)", re.I),
    ),
    (
        "fix_promise",
        re.compile(
            r"\b(?:we (?:have|'ve)\s+)?(?:fixed|resolved|corrected)\s+(?:this|it|the (?:issue|problem|bug))\b",
            re.I,
        ),
    ),
    (
        "fix_eta",
        re.compile(
            r"\b(?:will be (?:fixed|resolved|available|released|deployed)|expect(?:ed)? (?:a )?(?:fix|resolution|release))\b",
            re.I,
        ),
    ),
    (
        "delivery_date",
        re.compile(
            r"\b(?:by|before|within)\s+(?:the\s+)?(?:end of\s+)?(?:next|this|the)?\s*(?:week|month|quarter|sprint|monday|tuesday|wednesday|thursday|friday|q[1-4]|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))",
            re.I,
        ),
    ),
    (
        "roadmap",
        re.compile(r"\b(?:on (?:our|the) roadmap|planned for|scheduled for|in the next release)\b", re.I),
    ),
    ("guarantee", re.compile(r"\b(?:we guarantee|guaranteed to|we promise|rest assured)\b", re.I)),
)

# --- prompt injection --------------------------------------------------
# These are checked against the customer's text, not the response. A ticket
# that tries this is recorded for review whether or not it worked, because the
# second attempt is usually better than the first.
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|your)\b[^.\n]{0,30}\b(?:instruction|prompt|rule|direction|system)",
            re.I,
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"\b(?:you are now|from now on you|act as (?:if|a)|pretend (?:to be|you)|new (?:role|persona))\b",
            re.I,
        ),
    ),
    (
        "prompt_exfiltration",
        re.compile(
            r"\b(?:repeat|print|show|reveal|output|tell me)\b[^.\n]{0,30}\b(?:your |the )?(?:system )?(?:prompt|instruction|rule)s?\b",
            re.I,
        ),
    ),
    (
        "delimiter_escape",
        re.compile(r"(?:<<<\s*TICKET_(?:START|END)\s*>>>|\bSYSTEM\s*:|\bASSISTANT\s*:)", re.I),
    ),
    (
        "policy_bypass",
        re.compile(
            r"\b(?:developer mode|jailbreak|without (?:any )?(?:restriction|filter|guardrail)|bypass(?:ing)? (?:the )?(?:check|rule|policy))\b",
            re.I,
        ),
    ),
)

# Whether a sentence asserts something about the product.
#
# This used to be a list of about twenty verbs, and the list was the bug. English
# has an open verb class, so "Deleting the pod triggers a fresh rollout" and
# "Rotating the key invalidates every existing session" both went out uncited:
# neither uses a verb anyone thought to enumerate, and no enumeration ever
# closes. The test therefore runs the other way round now. A sentence is a claim
# unless it is conversation.
#
# Standing in for "has a finite verb" without a parser takes two tests. The first
# is the closed class of auxiliaries, modals and the copula, which genuinely is
# closed and can be written out. The second is inflection: a third person
# singular present ends in -s and a regular past in -ed. Plural nouns end in -s
# as well, so the second test over-fires, and over-firing is the side to be on
# here. This check only ever reads a paragraph that cites nothing at all, so a
# false positive costs an escalation and a false negative puts an uncited claim
# in a customer's inbox.
_FINITE_AUXILIARY = re.compile(
    r"\b(?:is|are|was|were|am|be|been|being|has|have|had|do|does|did|will|would|shall|should|"
    r"can|could|may|might|must|ought|cannot|isn't|aren't|wasn't|weren't|hasn't|haven't|hadn't|"
    r"doesn't|don't|didn't|won't|wouldn't|shouldn't|can't|couldn't|mustn't)\b",
    re.I,
)
_INFLECTED_VERB = re.compile(r"\b[a-z]{2,}(?:ed|es|s)\b", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Sentences that are conversation rather than claims. Without this the opening
# and closing lines of every reply are flagged as ungrounded. Everything the
# delivery layer adds around an answer belongs here, and nothing about
# CloudServe's behaviour does.
_BOILERPLATE = re.compile(
    r"^(?:thanks|thank you|hello|hi\b|i do not have documentation|"
    r"if (?:that|it) does not (?:resolve|answer)|"
    r"reply to this message|a support engineer|apologies|sorry|"
    r"let (?:me|us) know|feel free|do not hesitate|"
    r"this reply was drafted automatically)",
    re.I,
)

# Line breaks inside a ticket are wrapping, not punctuation. An injection
# pattern that forbids a newline inside the match is defeated by a mail client
# folding the line, which is how "Ignore all previous\ninstructions" walked past
# the check. Runs of blank lines are left alone so that two unrelated paragraphs
# are not joined into a phrase neither of them contains.
_SOFT_WRAP = re.compile(r"[ \t]*\n(?![ \t]*\n)[ \t]*")


def _unwrap(text: str) -> str:
    """Join soft-wrapped lines, keeping paragraph breaks as boundaries."""
    return _SOFT_WRAP.sub(" ", text)


def _is_claim(sentence: str) -> bool:
    """Whether this sentence asserts something that would need a source."""
    if _BOILERPLATE.match(sentence):
        return False
    # A question asks for a fact rather than stating one, so it has nothing to
    # cite. The generators do not write them; a model draft might.
    if sentence.rstrip().endswith("?"):
        return False
    return bool(_FINITE_AUXILIARY.search(sentence) or _INFLECTED_VERB.search(sentence))


def _find(patterns: Sequence[tuple[str, re.Pattern[str]]], text: str) -> list[str]:
    return [name for name, pattern in patterns if pattern.search(text)]


def check_private_data(response: str, ticket: Ticket | None = None) -> GuardrailResult:
    """Zero occurrences is the requirement, so this blocks rather than redacts.

    The customer's own email address in their own ticket is not a leak, but it
    becomes one the moment it is quoted back into a reply that a different
    process might forward, so no exception is made for it.
    """
    found = _find(PII_PATTERNS, response)

    # Customer names are not a fixed pattern, so they are checked by value.
    if ticket and ticket.customer_name:
        parts = [p for p in ticket.customer_name.split() if len(p) > 2]
        for part in parts:
            if re.search(rf"\b{re.escape(part)}\b", response):
                found.append("customer_name")
                break
    if ticket and ticket.customer_id and ticket.customer_id in response:
        if "customer_id" not in found:
            found.append("customer_id")

    if found:
        return GuardrailResult("pii", False, f"matched: {', '.join(sorted(set(found)))}")
    return GuardrailResult("pii", True, "no identifying data found")


def check_grounding(response: str, passages: Sequence[Passage], citations: Sequence[dict]) -> GuardrailResult:
    """Every factual sentence carries a citation, and every citation resolves.

    This is the check that stands in for hallucination detection at runtime. It
    is not a semantic entailment check - it cannot tell you that a cited passage
    fails to support the sentence attached to it, only that the sentence claims
    something and points at nothing. The semantic version is PR-03 and it runs
    in evaluation, where being slow and needing a provider is acceptable.
    """
    if not response.strip():
        return GuardrailResult("grounding", False, "empty response")

    valid_doc_ids = {p.doc_id for p in passages}
    markers_available = set(range(1, len(passages) + 1))

    cited_markers = {int(m) for m in re.findall(r"\[(\d+)\]", response)}
    dangling = cited_markers - markers_available
    if dangling:
        return GuardrailResult(
            "grounding", False, f"citation markers {sorted(dangling)} do not resolve to a retrieved passage"
        )

    for citation in citations:
        if citation.get("doc_id") not in valid_doc_ids:
            return GuardrailResult(
                "grounding", False, f"cited {citation.get('doc_id')} which was not retrieved"
            )

    # A citation covers the paragraph it sits in, not only the sentence it
    # terminates. Support replies are written with the marker at the end of the
    # passage it supports, so checking sentence by sentence flags the very claim
    # the citation was attached to. The unit that has to carry a citation is
    # therefore the paragraph.
    uncited: list[str] = []
    for paragraph in re.split(r"\n\s*\n", response.strip()):
        paragraph = paragraph.strip()
        if not paragraph or re.search(r"\[\d+\]", paragraph):
            continue
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            sentence = sentence.strip()
            # Below about thirty characters there is not room for a claim worth
            # sourcing, and the greeting is the case that keeps proving it.
            if len(sentence) < 30:
                continue
            if _is_claim(sentence):
                uncited.append(sentence)

    if uncited:
        return GuardrailResult(
            "grounding",
            False,
            f"{len(uncited)} factual sentence(s) in a paragraph with no citation, "
            f'first: "{uncited[0][:110]}"',
        )
    if not cited_markers:
        return GuardrailResult("grounding", False, "no citations at all")
    return GuardrailResult("grounding", True, f"{len(cited_markers)} citation(s), all resolving")


def check_commitments(response: str) -> GuardrailResult:
    """Scope and tone. Blocks anything that commits CloudServe to money, a fix
    or a date - none of which are the system's to promise."""
    found = _find(COMMITMENT_PATTERNS, response)
    if found:
        return GuardrailResult("commitments", False, f"matched: {', '.join(sorted(set(found)))}")
    return GuardrailResult("commitments", True, "no commitments made")


def check_injection(ticket: Ticket) -> GuardrailResult:
    """Runs on the incoming ticket, not the response.

    The structural defence is in PR-01 - customer text sits inside a delimiter
    block and the prompt says it is information rather than instruction. This is
    the detection layer on top: it does not assume the delimiters held.
    """
    found = _find(INJECTION_PATTERNS, _unwrap(f"{ticket.subject}\n\n{ticket.body}"))
    if found:
        return GuardrailResult("injection", False, f"ticket text matched: {', '.join(sorted(set(found)))}")
    return GuardrailResult("injection", True, "no instruction-like content in the ticket")


def check_confidence_floor(confidence: float, threshold: float) -> GuardrailResult:
    """The routing threshold was actually applied and a score was actually
    present. A missing confidence is not a high one."""
    if confidence is None:
        return GuardrailResult("confidence_floor", False, "no confidence score attached")
    if confidence < threshold:
        return GuardrailResult(
            "confidence_floor", False, f"confidence {confidence:.2f} below threshold {threshold:.2f}"
        )
    return GuardrailResult(
        "confidence_floor", True, f"confidence {confidence:.2f} at or above threshold {threshold:.2f}"
    )


def run_all(
    response: str,
    *,
    ticket: Ticket,
    passages: Sequence[Passage],
    citations: Sequence[dict],
    confidence: float,
    threshold: float,
) -> list[GuardrailResult]:
    """Every check, every time, in severity order.

    All of them run even once one has failed, because the log should say what
    each one found. Knowing that a blocked response also made a commitment is
    what tells you whether the block was lucky or intended.
    """
    return [
        check_private_data(response, ticket),
        check_injection(ticket),
        check_grounding(response, passages, citations),
        check_commitments(response),
        check_confidence_floor(confidence, threshold),
    ]
