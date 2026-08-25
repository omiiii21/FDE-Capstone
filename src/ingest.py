"""Normalise tickets from the four channels into one internal representation.

The point of doing this properly is that nothing downstream should have to ask
which channel a ticket came from in order to work. Channel is still recorded,
because it changes the latency budget and the tone of a good answer, but it
never changes the shape of the object.

Serves FR-01. Acceptance criterion A2.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import CHANNELS
from .schema import Ticket

# Quoted reply chains and signatures on email tickets. Anything below one of
# these lines is almost always the previous message, and including it in the
# retrieval query pulls the search towards whatever was discussed last week.
_QUOTE_MARKERS = (
    re.compile(r"^\s*on .{0,80}\bwrote:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*_{5,}\s*$", re.MULTILINE),
    re.compile(r"^\s*--\s*$", re.MULTILINE),
)

_FORUM_PREFIX = re.compile(r"^\s*(re|fwd|fw)\s*:\s*", re.IGNORECASE)
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


class IngestError(ValueError):
    """Raised only when a record has no usable identifier. Everything else is
    repaired rather than rejected - a malformed ticket is still a customer."""


def clean_text(value: Any) -> str:
    """Make text safe to embed in a prompt and stable to tokenise.

    Deliberately conservative: it strips things that are noise in every channel
    and leaves everything else alone, including the customer's own formatting.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    # NFKC folds the full-width and ligature characters that turn up in
    # copy-pasted console output into their ASCII equivalents.
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(_ZERO_WIDTH)
    value = html.unescape(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    # Control characters other than newline and tab.
    value = "".join(ch for ch in value if ch == "\n" or ch == "\t" or unicodedata.category(ch)[0] != "C")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def strip_quoted_reply(body: str) -> str:
    """Drop the quoted history from an email body, keeping the top post."""
    cut = len(body)
    for marker in _QUOTE_MARKERS:
        match = marker.search(body)
        if match and match.start() < cut:
            cut = match.start()
    head = body[:cut].strip()
    # If the quote markers ate everything, the customer wrote only a reply
    # chain and the chain is all we have. Better a noisy ticket than an empty one.
    return head if head else body.strip()


def normalise_channel(raw: Any) -> str:
    """Map whatever the source system called it onto our four channels."""
    value = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if value in CHANNELS:
        return value
    aliases = {
        "e_mail": "email",
        "mail": "email",
        "live_chat": "chat",
        "livechat": "chat",
        "messenger": "chat",
        "docs": "docs_comment",
        "documentation": "docs_comment",
        "documentation_comment": "docs_comment",
        "doc_comment": "docs_comment",
        "community": "forum",
        "community_forum": "forum",
    }
    return aliases.get(value, "email")


def normalise_ticket(record: dict[str, Any]) -> Ticket:
    """One record from an input file to one Ticket. Never raises on content."""
    if not isinstance(record, dict):
        raise IngestError(f"expected an object, got {type(record).__name__}")

    ticket_id = str(record.get("ticket_id") or record.get("id") or "").strip()
    if not ticket_id:
        raise IngestError("record has no ticket_id")

    channel = normalise_channel(record.get("channel"))
    subject = clean_text(record.get("subject"))
    body = clean_text(record.get("body") or record.get("message") or record.get("text"))

    if channel == "email":
        body = strip_quoted_reply(body)
    if channel == "forum":
        subject = _FORUM_PREFIX.sub("", subject).strip()

    # An empty body is not an error. Chat tickets arrive with no subject by
    # design, and a docs comment can be a single sentence in the subject line.
    # If both are empty the ticket still flows through - it will classify as
    # unclear_request and escalate, which is the correct outcome.
    if not body and subject:
        # Ticket.text joins subject and body, so copying the subject into the
        # body would double those terms into retrieval and classification.
        # Leave the body empty; text falls back to the subject on its own.
        pass

    return Ticket(
        ticket_id=ticket_id,
        channel=channel,
        subject=subject,
        body=body,
        received_at=str(record.get("received_at") or "").strip(),
        customer_id=str(record.get("customer_id") or "").strip(),
        customer_name=clean_text(record.get("customer_name")),
        customer_tier=str(record.get("customer_tier") or "standard").strip().lower(),
        customer_region=str(record.get("customer_region") or "").strip().lower(),
        language_fluency=str(record.get("language_fluency") or "fluent").strip().lower(),
        raw=record,
        labels=record.get("labels") or {},
    )


def load_tickets(path: str | Path) -> list[Ticket]:
    """Read a ticket file. Accepts a JSON array, a {"tickets": [...]} wrapper or
    JSON Lines, because the harness will be pointed at a file I have not seen
    and I would rather guess the container than fail on it."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    records: Iterable[Any]
    stripped = text.lstrip()
    if stripped.startswith("["):
        records = json.loads(text)
    elif stripped.startswith("{"):
        payload = json.loads(text)
        for key in ("tickets", "data", "items", "records"):
            if isinstance(payload.get(key), list):
                records = payload[key]
                break
        else:
            records = [payload]
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]

    tickets: list[Ticket] = []
    skipped = 0
    for index, record in enumerate(records):
        try:
            tickets.append(normalise_ticket(record))
        except IngestError:
            # Give it a positional identifier rather than dropping it. A9 says
            # no ticket is silently skipped, and a record with no id is still a
            # record that has to appear in the output.
            if isinstance(record, dict):
                record = dict(record)
                record["ticket_id"] = f"UNIDENTIFIED-{index:04d}"
                tickets.append(normalise_ticket(record))
            else:
                skipped += 1
    if skipped:
        raise IngestError(f"{skipped} records in {path} were not objects and could not be repaired")
    return tickets


def iter_tickets(path: str | Path) -> Iterator[Ticket]:
    yield from load_tickets(path)
