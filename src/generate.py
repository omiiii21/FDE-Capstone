"""Drafting an answer from retrieved passages, with citations attached.

There are two generators and the choice between them is not a fallback bolted
on at the end - it is the design.

  ExtractiveGenerator  composes the reply out of sentences that exist in the
                       corpus. It cannot hallucinate, because it cannot write
                       a sentence the documentation does not contain. It needs
                       no provider and no network.

  ModelGenerator       asks the language model to draft from the same passages
                       using PR-01. Better prose, and it can be wrong, which is
                       why everything it produces goes through the grounding
                       guardrail before release.

The model path is preferred when a provider is configured and reachable. When
it is not, the system does not stop and it does not send something unchecked -
it sends the extractive draft, which is duller and safe. That is what A11 means
by degrading rather than crashing.

Serves FR-06, FR-07, FR-08, FR-09, FR-14, FR-15. Acceptance criteria A6, A11.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import REPO_ROOT
from .provider import Provider, ProviderUnavailable
from .schema import Passage, Ticket

PROMPT_DIR = REPO_ROOT / "prompts" / "build"
ANSWER_PROMPT = "PR-01_answer_draft.v1.3.txt"
ESCALATION_PROMPT = "PR-02_escalation_summary.v1.1.txt"

# Sentence splitter. Deliberately simple: the corpus is written by one technical
# writer in a consistent house style, so the hard cases a general splitter exists
# to handle (initials, abbreviations mid-sentence) do not occur here. If the
# corpus opened up to arbitrary sources this would need replacing.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`'\"(])")
_STEP = re.compile(r"^\s*\d+\.\s+")
_HEADING_LINE = re.compile(r"^\s*#{1,6}\s+")
_BULLET = re.compile(r"^\s*[-*]\s+")
# Article furniture. These lines are structure, not prose, and a reply that
# quotes them reads like a paste of the page rather than an answer.
_METADATA = re.compile(r"^\s*(\*\*[A-Za-z ]+:\*\*|Applies to:)", re.IGNORECASE)

# A supporting article has to score at least this share of the top result to be
# cited alongside it. Set from the development set: below about 0.6 the second
# citation was a different topic more often than not.
SECONDARY_SOURCE_RATIO = 0.6

NO_ANSWER = (
    "I do not have documentation covering this, so I am passing it to a support "
    "engineer rather than guessing."
)

# FR-15. Ravi Menon, the customer, asked for this directly and gave the reason:
# "Not because I object, but because I calibrate how much I trust it. If I know a
# person wrote it I will act without checking. If I know a machine drafted it I
# will verify first. Hiding that would be the thing that annoys me."
#
# It sits in the reply rather than in an email footer because a footer is the
# part nobody reads, and the whole point is that it changes what the customer
# does next.
DISCLOSURE = (
    "This reply was drafted automatically from CloudServe's support documentation, and the "
    "articles it came from are listed above. If it does not answer your question, reply to this "
    "message and a support engineer will pick it up."
)


def finalise(body: str) -> str:
    """Add the greeting and the disclosure to a drafted answer.

    Both generators route through here so the disclosure cannot be attached to
    one path and forgotten on the other. PR-01 tells the model not to write a
    greeting or a sign-off for the same reason: the delivery layer owns them, so
    there is one place to change them and one place to check them.
    """
    body = body.strip()
    if not body:
        return body
    return f"Thanks for getting in touch.\n\n{body}\n\n{DISCLOSURE}"


@dataclass
class Draft:
    body: str
    citations: list[dict]
    answered: bool
    uncertain_about: str
    method: str


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def build_sources_block(passages: list[Passage], *, max_chars: int = 900) -> str:
    """Number the passages so citations have something to refer to."""
    parts = []
    for index, passage in enumerate(passages, start=1):
        text = passage.text.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rsplit(" ", 1)[0] + " ..."
        parts.append(f"[{index}] ({passage.doc_id}) {passage.title}\n{text}")
    return "\n\n".join(parts)


def render(template: str, **fields: str) -> str:
    out = template
    for key, value in fields.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


def _useful_sentences(passage: Passage, query_terms: set[str]) -> list[str]:
    """Sentences from a passage that bear on the question.

    Symptom lists are skipped. They match the customer's own words better than
    anything else in the article and they tell the customer only what they
    already told us, which reads as confident and useless.
    """
    section = ""
    sentences: list[tuple[str, str]] = []
    title_line = passage.title.strip().lower()
    for line in passage.text.splitlines():
        if _HEADING_LINE.match(line):
            section = _HEADING_LINE.sub("", line).strip().lower()
            continue
        line = line.strip()
        if not line or _METADATA.match(line):
            continue
        # The chunk repeats the article title at the top so a quoted passage
        # says what it is about. That is useful for retrieval and is not a
        # sentence, so it does not belong in the reply.
        if line.lower() == title_line:
            continue
        line = _BULLET.sub("", line)
        for sentence in _SENTENCE.split(line):
            sentence = sentence.strip()
            if len(sentence) > 25:
                sentences.append((section, sentence))

    resolution = [s for sec, s in sentences if "resolution" in sec or _STEP.match(s)]
    causes = [s for sec, s in sentences if "cause" in sec]
    notes = [s for sec, s in sentences if "note" in sec]
    other = [
        s for sec, s in sentences if not any(k in sec for k in ("symptom", "resolution", "cause", "note"))
    ]

    chosen = resolution[:4] or other[:3]
    # One cause sentence gives the reply a "why", which is what stops it reading
    # like a list of instructions with no reasoning behind it.
    if causes and len(chosen) < 5:
        best = max(causes, key=lambda s: len(query_terms & set(re.findall(r"[a-z]+", s.lower()))))
        chosen = [best] + chosen
    if notes and len(chosen) < 5:
        chosen = chosen + notes[:1]
    return chosen[:5]


class ExtractiveGenerator:
    """Builds the reply from sentences that are already in the corpus."""

    name = "extractive"

    def __init__(self, retriever=None):
        # Optional. Without it the generator quotes whichever chunk ranked
        # best, which is frequently the symptoms section - the part describing
        # the problem the customer already told us about. With it, the reply
        # comes from the same article's resolution steps instead.
        self.retriever = retriever

    def _best_chunk_for(self, passage: Passage) -> Passage:
        if self.retriever is None:
            return passage
        siblings = self.retriever.chunks_for(passage.doc_id)
        for chunk in siblings:
            if "resolution" in chunk.section.lower():
                if chunk.chunk_id == passage.chunk_id:
                    return passage
                return Passage(
                    doc_id=chunk.doc_id,
                    title=chunk.title,
                    category=chunk.category,
                    chunk_id=chunk.chunk_id,
                    text=chunk.text,
                    # The score belongs to the passage that was retrieved. The
                    # quoted text comes from a sibling, and the citation still
                    # resolves to the same article, which is what A6 checks.
                    score=passage.score,
                )
        return passage

    def draft(self, ticket: Ticket, passages: list[Passage]) -> Draft:
        if not passages:
            return Draft(NO_ANSWER, [], False, "nothing in the knowledge base matched this ticket", self.name)

        query_terms = set(re.findall(r"[a-z]+", ticket.text.lower()))
        paragraphs: list[str] = []
        citations: list[dict] = []

        # One passage per article. Two chunks of the same document cited as [1]
        # and [2] read as two sources agreeing with each other, which is not
        # what happened.
        distinct: list[Passage] = []
        seen: set[str] = set()
        for passage in passages:
            if passage.doc_id in seen:
                continue
            # A second article only earns a citation if it scored within
            # touching distance of the first. Below that it is the tail of the
            # ranking rather than a corroborating source, and citing it makes
            # the answer look better evidenced than it is.
            if distinct and passage.score < SECONDARY_SOURCE_RATIO * distinct[0].score:
                continue
            seen.add(passage.doc_id)
            distinct.append(passage)

        for index, passage in enumerate(distinct[:3], start=1):
            passage = self._best_chunk_for(passage)
            sentences = _useful_sentences(passage, query_terms)
            if not sentences:
                continue
            text = " ".join(_STEP.sub("", s) for s in sentences)
            text = re.sub(r"\s+", " ", text).strip()
            if not text.endswith((".", "!", "?")):
                text += "."
            paragraphs.append(f"{text} [{index}]")
            citations.append(
                {
                    "marker": index,
                    "doc_id": passage.doc_id,
                    "title": passage.title,
                    "chunk_id": passage.chunk_id,
                    "score": passage.score,
                }
            )
            if len(paragraphs) >= 2:
                break

        if not paragraphs:
            return Draft(NO_ANSWER, [], False, "retrieved passages contained no actionable steps", self.name)

        return Draft(finalise("\n\n".join(paragraphs)), citations, True, "", self.name)


class ModelGenerator:
    """PR-01 through the configured provider, with the extractive draft behind it."""

    name = "model"

    def __init__(self, provider: Provider, fallback: ExtractiveGenerator | None = None, retriever=None):
        self.provider = provider
        self.fallback = fallback or ExtractiveGenerator(retriever)
        self._template = load_prompt(ANSWER_PROMPT)
        self._escalation_template = load_prompt(ESCALATION_PROMPT)

    def draft(
        self, ticket: Ticket, passages: list[Passage], *, confidence: float = 0.0, intent: str = ""
    ) -> Draft:
        if not passages:
            return Draft(NO_ANSWER, [], False, "nothing in the knowledge base matched this ticket", "none")
        if not self.provider.available:
            return self.fallback.draft(ticket, passages)

        prompt = render(
            self._template,
            sources_block=build_sources_block(passages),
            channel=ticket.channel,
            subject=ticket.subject or "(none)",
            body=ticket.body,
            intent=intent,
            confidence=f"{confidence:.2f}",
        )
        try:
            raw = self.provider.complete(prompt, max_tokens=700, temperature=0.0)
        except ProviderUnavailable:
            # Not an error worth propagating. The extractive draft is a real
            # answer with real citations; it just reads more stiffly.
            draft = self.fallback.draft(ticket, passages)
            draft.method = "extractive:provider-unavailable"
            return draft

        parsed = _parse_json_object(raw)
        if parsed is None:
            draft = self.fallback.draft(ticket, passages)
            draft.method = "extractive:unparseable-model-output"
            return draft

        answer = str(parsed.get("answer", "")).strip()
        # A response with no answer field at all is a malformed payload, not a
        # considered refusal, and it should degrade like every other provider
        # fault rather than costing the ticket an answer the extractive
        # generator could have produced from the same passages.
        if "answer" not in parsed:
            draft = self.fallback.draft(ticket, passages)
            draft.method = "extractive:model-returned-no-answer-field"
            return draft

        answered = bool(parsed.get("answered", True)) and bool(answer)
        if not answered:
            return Draft(
                NO_ANSWER,
                [],
                False,
                str(parsed.get("uncertain_about", "") or "the model declined to answer"),
                "model:declined",
            )

        # Trust the markers in the text over the citations array. The array is
        # what the model says it used; the markers are what it actually wrote.
        markers = sorted({int(m) for m in re.findall(r"\[(\d+)\]", answer)})
        citations = [
            {
                "marker": m,
                "doc_id": passages[m - 1].doc_id,
                "title": passages[m - 1].title,
                "chunk_id": passages[m - 1].chunk_id,
                "score": passages[m - 1].score,
            }
            for m in markers
            if 1 <= m <= len(passages)
        ]
        return Draft(finalise(answer), citations, True, str(parsed.get("uncertain_about", "")), "model")

    def escalation_note(
        self, ticket: Ticket, passages: list[Passage], *, intent: str, confidence: float, routing_reason: str
    ) -> str:
        """The handover note. Daniel asked for the system to show its working;
        this is that. It is produced for every escalation, including the ones
        where retrieval found nothing."""
        deterministic = _deterministic_escalation_note(
            ticket, passages, intent=intent, confidence=confidence, routing_reason=routing_reason
        )
        if not self.provider.available:
            return deterministic
        prompt = render(
            self._escalation_template,
            sources_block=build_sources_block(passages) or "(nothing retrieved above the relevance floor)",
            channel=ticket.channel,
            subject=ticket.subject or "(none)",
            body=ticket.body,
            intent=intent,
            confidence=f"{confidence:.2f}",
            routing_reason=routing_reason,
        )
        try:
            raw = self.provider.complete(prompt, max_tokens=450, temperature=0.0)
        except ProviderUnavailable:
            return deterministic
        parsed = _parse_json_object(raw)
        if not parsed or not parsed.get("summary"):
            return deterministic
        return str(parsed["summary"]).strip()


def _deterministic_escalation_note(
    ticket: Ticket, passages: list[Passage], *, intent: str, confidence: float, routing_reason: str
) -> str:
    """The same three paragraphs Daniel asked for, without a model.

    Every escalation gets one of these even during an outage, because an
    escalation arriving as a bare forwarded ticket is the thing he said wastes
    the most of his time.
    """
    first_line = ticket.body.strip().split("\n")[0]
    if len(first_line) > 180:
        first_line = first_line[:177].rsplit(" ", 1)[0] + "..."

    what = (
        f"Customer is on the {ticket.customer_tier} plan, contacting us via {ticket.channel}. "
        f"Classified as {intent.replace('_', ' ')} at confidence {confidence:.2f}. "
        f'They wrote: "{first_line}"'
    )
    if passages:
        docs = ", ".join(f"{p.doc_id} ({p.title})" for p in passages[:3])
        covers = f"Documentation that looked relevant: {docs}. These were retrieved but not sent."
    else:
        covers = (
            "Nothing in the knowledge base scored above the relevance floor for this ticket, "
            "which usually means it is not covered by an existing article."
        )
    uncertain = f"Not answered automatically because: {routing_reason}"
    return "\n\n".join([what, covers, uncertain])


def _parse_json_object(raw: str) -> dict | None:
    """Pull the JSON object out of a model response.

    Small models wrap JSON in prose and in code fences however firmly you ask
    them not to, so this looks for the object rather than trusting the response
    to be one. Returns None rather than raising - an unparseable response is a
    reason to fall back, not a reason to lose the ticket.
    """
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def get_generator(provider: Provider, retriever=None) -> ModelGenerator:
    return ModelGenerator(provider, retriever=retriever)
