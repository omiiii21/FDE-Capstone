# Fairness audit

Who this system serves worse, by how much, and whether the gap is real.

Reproduce with `python -m scripts.fairness_audit`, which writes
`evaluation/results/fairness_audit.json`. The per-segment resolution figures
come from the full run report, `evaluation/results/dev_run/metrics.json`. Both
were produced from the 500 development tickets on a clean checkout with no API
key and no network.

---

## The question, and why the obvious answer to it is weak

Sofia, a tier one agent, said in discovery that tickets written in non-fluent
English "have our worst satisfaction scores". That is a specific, falsifiable
claim about a group of customers, and it is the reason this audit exists.

The historical data contradicts her. Non-fluent tickets have a mean CSAT of
3.04 against 2.95 for fluent ones, and a first contact resolution rate of 45.8%
against 43.2%. On the record CloudServe already holds, non-fluent tickets do
slightly better, not worse.

Her instinct was still worth taking seriously, because the mechanism she
described is real and the system I was building would introduce it for the
first time. A human agent reads around a customer's phrasing. A BM25 retriever
does not: it matches terms, and if a customer does not use the words the article
uses, the article is not returned. So the risk was prospective rather than
historical, and the place to look for it was retrieval rather than satisfaction.
That is where the audit found it.

---

## Method

Two analyses, reported together, because they answer different questions and
one of them is considerably stronger than the other.

**Measures.** Three, all defined against labels already in the ticket data.

- *Retrieval recall*, the share of answerable tickets where at least one of the
  documents in `labels.expected_doc_ids` appears in the five passages returned.
  Measured only on tickets that have an expected document, so the basis is
  smaller than the segment.
- *Verified resolution*, the share of automatic answers whose cited document
  matches an expected document. This is the closest thing available to "the
  customer got the right answer", and it is a proxy, not an outcome.
- *Automation rate*, the share of tickets in the segment answered automatically
  rather than escalated. Included because a segment can look fine on quality
  while quietly being escalated twice as often, and that is also a fairness
  finding.

**Segments.** Customer tier, language fluency, region, channel and ticket
length, each derived from a field already on the ticket. A segment's retrieval
recall enters the spread calculation only when it has at least 25 answerable
tickets, so a thin slice cannot produce a dramatic headline.

**The target.** No dimension should vary by more than five points. Three of the
five fail it. Those failures are reported as failures.

---

## Segment results

### Language fluency

| Segment | Tickets | Answerable basis | Automation | Intent accuracy | Retrieval recall |
|---|---|---|---|---|---|
| Fluent | 380 | 270 | 81.3% | 97.1% | 97.8% |
| Non-fluent | 120 | 87 | 81.7% | 100% | 92.0% |

A 5.8 point gap in retrieval recall, and it is the largest single quality gap in
the audit. Everything else about this comparison is even or better for
non-fluent tickets: they are automated at the same rate, and the classifier gets
their intent right on every one of the 120. On verified resolution the gap is
1.94 points, 90.8% against 88.9%, which passes the five-point target.

So the harm, where there is harm, is entirely in retrieval. The system
understands what non-fluent customers are asking. It is less good at finding the
article that answers them. Sofia was wrong about the history and right about the
mechanism.

### Customer tier

| Segment | Tickets | Verified basis | Automation | Verified resolution | Retrieval recall |
|---|---|---|---|---|---|
| Enterprise | 83 | 55 | 84.3% | 94.5% | 96.6% |
| Business | 164 | 111 | 81.1% | 90.1% | 96.8% |
| Standard | 253 | 155 | 80.6% | 89.0% | 96.0% |

Spread of 5.52 points, which fails. Enterprise is best served on every measure
that matters and is also the largest segment by ticket value. Retrieval recall
is flat across the three within 0.8 points, so this is not a retrieval story:
enterprise tickets are longer and more specific, and length is the variable
doing the work, as the length segment below shows.

This has a commercial edge worth naming. Ravi, a customer on the business plan,
said he has a colleague on enterprise who gets answers in about an hour and that
"if this change makes that gap bigger, we will notice, and it will come up at
renewal". Historically he was wrong about the direction: enterprise has the
slowest human median resolution at 369 minutes against business at 141. This
system reverses that and widens it in enterprise's favour.

### Region

| Segment | Tickets | Verified basis | Automation | Verified resolution | Retrieval recall |
|---|---|---|---|---|---|
| Latin America | 60 | 40 | 88.3% | 95.0% | 97.7% |
| Asia Pacific | 119 | 69 | 77.3% | 92.8% | 94.7% |
| North America | 170 | 110 | 81.8% | 91.8% | 96.8% |
| Europe | 151 | 102 | 81.5% | 85.3% | 96.5% |

Spread of 9.71 points, the widest in the audit, and the one I understand least.
Europe is 9.7 points below Latin America on verified resolution while sitting
mid-table on retrieval recall, so the right article is being found and the wrong
one is being cited. My working hypothesis is composition: Europe carries a
heavier share of `data_residency` and `compliance_request` traffic, where
neighbouring articles are easy to confuse and the corpus is thinnest. I have not
proved that on 102 verifiable tickets and I am not going to claim it. It is the
first thing I would test with more data.

Asia Pacific is the quieter finding. It has the lowest automation rate of the
four at 77.3% and the lowest retrieval recall, and its verified resolution is
second best. It escalates more and is right more often when it does answer,
which is the system failing in the safe direction.

### Channel

| Segment | Tickets | Verified basis | Automation | Verified resolution | Retrieval recall |
|---|---|---|---|---|---|
| Forum | 55 | 35 | 83.6% | 94.3% | 97.3% |
| Email | 212 | 137 | 80.7% | 92.0% | 98.0% |
| Documentation comment | 78 | 42 | 79.5% | 90.5% | 95.8% |
| Chat | 155 | 107 | 82.6% | 86.9% | 94.1% |

Spread of 7.37 points, which fails. Chat is worst on both quality measures and
the cause is structural rather than mysterious: chat tickets have no subject
line, and the retriever applies a title boost of 2.6 to subject terms, so a chat
ticket hands the retriever less to work with. Chat messages are also shorter,
which compounds with the length effect below. Email, which has a subject line
and is the longest channel on average, has the best retrieval recall of the
four.

The documentation comment channel deserves a line of its own. Nobody mentioned
it in five interviews, and it is the worst channel in the current human process:
34.6% first contact resolution, a 466 minute median, 33.3% repeat contacts, and
only 61.5% of its tickets answerable from the docs at all. The system does
noticeably better on it than the process it replaces, which is the one place in
this audit where a below-average segment is still an improvement.

### Ticket length

| Segment | Tickets | Verified basis | Automation | Verified resolution | Retrieval recall |
|---|---|---|---|---|---|
| Long | 390 | 256 | 85.1% | 91.0% | 96.8% |
| Short | 110 | 65 | 68.2% | 87.7% | 94.7% |

Spread of 3.33 points on resolution, which passes, and 17 points on automation,
which is the number that actually matters. My first explanation for it was that
a short ticket gives BM25 fewer terms to match, so retrieval returns less or
nothing and `R-02-no-grounding` escalates. The decision log says otherwise.
`R-02-no-grounding` did not fire once on the 500-ticket run. Of the 35
escalations among short tickets, 34 are `R-01-policy-class` and one is
`R-04-below-threshold`, and 15 of those 34 are `unclear_request`. Seven short
tickets did retrieve nothing at all, and all seven were unclear requests that
R-01 had already removed before the no-grounding rule was reached.

The real mechanism is composition rather than retrieval. A ticket short enough
to fall under 120 characters is disproportionately a ticket that does not say
what it wants, and that is a class the policy rule takes out of automation on
sight. So the system is not answering short tickets badly, and it is not
declining them for want of a passage either. It is declining them because they
are the kind of ticket a person has to read. That is the correct behaviour and
still a difference in service: a customer who writes two lines waits for a
human, and a customer who writes two paragraphs gets a reply in under a
millisecond.

I expected the opposite result before measuring, on the reasoning that a long
ticket contains more to be confused by. For a lexical retriever, more text is
more signal.

---

## The matched-pair analysis

### Why segment averages are not enough

The 5.8 point fluency gap compares two groups that differ in more than the thing
being tested. Non-fluent tickets are not a random sample of fluent ones. They
are shorter, they skew towards different channels, and they skew towards
different intents. Any of those could produce a retrieval gap on their own, and
the segment average cannot tell you which.

Put concretely: if non-fluent customers happen to ask more about onboarding, and
the onboarding article is hard to retrieve for everyone, the segment average
reports a fluency gap that is really a topic gap. The remedy for a fluency gap
is expansion terms. The remedy for a topic gap is a better article. Those are
different fixes, owned by different people, and averaging the two together tells
you to do neither.

### The design

The development set makes a better test possible, more or less by accident. Most
questions appear twice, once in fluent English and once in non-fluent English,
with the same labelled intent and the same expected documents. Grouping tickets
by `(intent, expected_doc_ids)` and keeping only the groups that contain both
phrasings gives near-matched pairs: same question, same right answer, different
words. Comparing retrieval within a pair isolates phrasing from topic, from
length and from everything else.

29 groups carry both phrasings. Within each group, tickets are deduplicated by
normalised body text before anything is averaged, so a phrasing that appears
eleven times in the data counts once. That matters more here than it would in
most datasets, because these 500 tickets contain only 215 distinct bodies, and
without the deduplication the comparison would mostly be measuring which
duplicate got copied more often.

### What it found

| Result | Value |
|---|---|
| Groups compared | 29 |
| Mean retrieval hit gap | 2.8 points against non-fluent |
| Groups with no gap | 24 |
| Groups worse for non-fluent | 3 |
| Groups better for non-fluent | 2 |
| Mean top-1 score gap | 1.22 |

The mean gap is less than half the raw segment gap, and 24 of 29 groups show no
gap at all. Two groups are actually better for the non-fluent phrasing, which is
the sort of result that tells you the measurement is not simply confirming what
you expected.

So most of the headline 5.8 points is compositional. It is a statement about
what non-fluent customers happen to ask about, not about how the retriever
treats their words. Reporting the 5.8 alone would have been true and misleading
at once.

### The three that are real

| Intent | Expected document | Fluent | Non-fluent | Gap | Top-1 score gap |
|---|---|---|---|---|---|
| deployment_failure | DOC-DEPLOY-001, "Container deployments failing during the health check phase" | 100% (5 phrasings) | 50% (2) | 50.0 points | -3.20 |
| onboarding | DOC-ONB-001, "First deployment: a walkthrough" | 66.7% (3) | 20% (5) | 46.7 points | 5.37 |
| database_issue | DOC-PERF-003, "Database connection pool exhaustion" | 66.7% (3) | 33.3% (3) | 33.3 points | -1.04 |

These are not small gaps, and concentrating the damage is worse for the
customers inside them than spreading it thinly would be. A non-fluent customer
whose deployment is failing has a one in two chance of the retriever finding the
article that would fix it, against a certainty for a fluent customer asking the
same question.

The deployment_failure case is exactly the one Ines described in her interview,
almost word for word: "Someone writes my deployment keeps dying and my article is
called resolving container health check failures. There is no path between those
two phrases in a keyword search." The failure surviving in the audit is the same
failure, a level down: the obvious phrasings are bridged and a less obvious one
is not.

Note that two of the three gaps have a negative score gap, meaning the
non-fluent phrasings actually scored higher on their top passage. They retrieved
something confidently. It was the wrong thing. That rules out the simple
explanation, that short or broken phrasing produces weak matches, and points at
a specific vocabulary collision instead.

---

## What was done about it, and what was not

**Done.** `src/text.py` carries an expansion table that maps customer vocabulary
onto documentation vocabulary before retrieval runs. It is applied additively,
so the original terms are always kept and an expansion can only ever add a way
of matching, never remove one. Every entry was derived by taking a customer
phrase that appears in the development tickets and the article that
`labels.expected_doc_ids` says should answer it, then finding the word the
article used instead. `dying`, `died` and `crashing` all map onto
`health check failure`, which is Ines's example encoded.

This is the reason 24 of 29 matched groups show no gap. The bridge was built
before the audit ran, and the audit is what tells me it mostly worked.

**Not done.** I did not add expansion terms for the three remaining gaps. Doing
so would have closed the number without closing the problem, because the terms
would have been chosen by reading the same 500 tickets the audit measures, and I
would have had no way to tell a fix from a fit. The audit would then have
reported zero gaps and meant nothing.

What the fix actually requires is a set of real non-fluent phrasings from
tickets that were not used to build the expansion table, three sets of terms
derived from them, and a re-run of `scripts/fairness_audit.py` on the held-out
set. The script is committed and takes an `--input` argument for exactly that.
It is a day of work on data CloudServe already has and I did not have.

The more important thing this says is that the three gaps are three sets of
expansion terms and not an architecture problem. Nothing here argues for
replacing the retriever.

**A choice this audit argues against.** `ADR-002` selects lexical BM25 over
dense embeddings. Running both over the same 500 tickets
(`scripts/compare_retrieval.py`, all-MiniLM-L6-v2):

| Backend | recall@5 | precision@1 | Fluent | Non-fluent | Gap | p95 latency |
|---|---|---|---|---|---|---|
| Lexical | 96.4% | 87.7% | 97.8% | 92.0% | 5.8 points | 0.1 ms |
| Dense | 96.1% | 88.8% | 97.0% | 93.1% | 3.9 points | 3.6 ms |

Dense narrows the fluency gap by 1.9 points. That is a real fairness argument
for the option I did not take, and I am recording it as one rather than
listing only the reasons that suit the decision I made. What decided it was 800
MB of dependencies, a 36-fold latency increase and a 0.3 point loss on recall,
against a gap that the matched-pair analysis says is mostly compositional
anyway. If CloudServe deploys this and the three concentrated gaps turn out not
to close with expansion terms, revisiting ADR-002 is the next move and the
comparison script is there to re-run.

---

## Limits

This audit measures retrieval and citation correctness. It does not measure
whether the answer read well.

A non-native speaker could receive a perfectly grounded reply, citing exactly
the right article, written in a register they find hard to follow, containing an
idiom that does not travel. Every number in this document would score that reply
as a success. Finding out whether it was one requires human review by people who
are not me, ideally including reviewers who read English as a second language,
and that review has not been done.

Four further limits, all of which cap how much weight these numbers can carry.

The verified basis is small. Verified resolution is measured only where a
citation can be checked against an expected document, so the enterprise figure
rests on 55 tickets and the forum figure on 35. A handful of tickets moves those
percentages several points.

There are no live customers. CSAT appears in this document only as historical
context from the ticket history block; nothing in this system has been measured
against a real person's satisfaction, and the proxies used here are proxies.

The data is duplicated. 215 distinct bodies across 500 tickets means the
segments are less independent than their sizes suggest. The matched-pair
analysis deduplicates by body for exactly this reason; the segment tables do
not, because they are answering the framework's question about what the
population experiences.

Region is unexplained. The 9.71 point European gap is the largest finding in the
audit and I have a hypothesis rather than a cause. It should not be presented to
CloudServe as understood.

---

## Summary

| Dimension | Spread | Within five points | Assessment |
|---|---|---|---|
| Language fluency | 1.94 points resolution, 5.8 retrieval | Resolution yes, retrieval no | Real, concentrated in three intents, fixable with expansion terms |
| Ticket length | 3.33 points | Yes | Real but safe: short tickets escalate rather than get bad answers |
| Customer tier | 5.52 points | No | Real, driven by length, commercially sensitive |
| Channel | 7.37 points | No | Real and structural: chat has no subject line for the title boost |
| Region | 9.71 points | No | Real and not yet explained |
