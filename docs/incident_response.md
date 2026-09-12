# Incident response

What to do when this system has answered a customer badly, or might be about to.

Written to be followed by someone who has not read the source, at two in the
morning, with no one else awake. Commands are copy-pasteable. Every query has
been run against a real decision log and returns what it says it returns.

Companion documents: `docs/architecture.md` for how the pipeline fits together,
`docs/monitoring/README.md` for the dashboard, `docs/fairness_audit.md` for the
known gaps by segment.

---

## 0. If you only read one section

Stop automatic answering first, understand it afterwards. Halting is cheap:
every ticket routes to a human with the drafted answer and the retrieved
passages attached, which is where every ticket was before this system existed.
Nothing is lost and no customer stops being served.

```bash
# Preferred. Needs the API up and ADMIN_TOKEN set in its environment.
curl -sS -X POST http://localhost:8000/admin/halt -H "X-Admin-Token: $ADMIN_TOKEN"

# If the API is not answering, or you do not have the token, and you have a
# shell on the host. Identical effect.
touch /path/to/cloudserve-triage/storage/HALT
```

Confirm it took:

```bash
curl -sS http://localhost:8000/healthz | python3 -m json.tool
# look for: "kill_switch": {"engaged": true, ...}
# and:      "degraded_because": ["automatic responses halted by an operator"]
```

Effective on the next ticket. No deployment, no restart, no config reload.
Routing reads the file once per ticket as rule `R-00`, ahead of every other
rule. A ticket already mid-pipeline finishes under the previous setting, which
takes well under a millisecond, so the in-flight window is one ticket.

Authorised to do this without asking anyone: the Head of Support and the
engineer on call. Do it on suspicion. Getting it wrong costs one operator five
minutes; not doing it costs a customer.

To undo, once the fix is in and a full harness run is green:

```bash
curl -sS -X POST http://localhost:8000/admin/resume -H "X-Admin-Token: $ADMIN_TOKEN"
# or
rm /path/to/cloudserve-triage/storage/HALT
```

---

## 1. Detection

Three things start an incident: a report from a customer or an agent, a shape
change on the dashboard, or a guardrail firing on something that has never fired
before.

### From Prometheus

Metrics are served on `METRICS_PORT` (8001 by default), on their own port so
that scraping does not depend on the application being healthy. The five series
are defined in `src/metrics.py`.

| Signal | Query | What makes it an incident |
|---|---|---|
| Private data blocked | `increase(guardrail_blocks_total{guardrail="pii"}[1h])` | Any value above zero. This counter has never incremented in 580 evaluated tickets. One increment is an incident, not a trend. |
| Automation share moved | `sum(rate(tickets_processed_total{outcome="auto_respond"}[15m])) / sum(rate(tickets_processed_total[15m]))` | The measured baseline is 0.814. Above roughly 0.88 the system is answering things it used to escalate. Below roughly 0.70 retrieval or the classifier has degraded. A move of ten points inside an hour matters more than the absolute value. |
| Confidence sliding | `histogram_quantile(0.5, sum by (le) (rate(classification_confidence_bucket[1h])))` | The baseline puts 478 of 500 predictions in the 0.8 to 1.0 band. A median dropping below 0.8, or mass piling up just under the 0.62 threshold, is drift. This moves weeks before anything else does. |
| Latency tail | `histogram_quantile(0.95, sum by (le) (rate(response_seconds_bucket[5m])))` | Measured p95 is 0.4 ms without a provider. A p95 sitting at or near 25 seconds means the provider is timing out and the retries are being paid for in full. |
| Guardrail step change | `sum by (guardrail) (increase(guardrail_blocks_total[1h]))` | Any guardrail that starts firing at a rate it was not firing at yesterday. A step in `grounding` usually means the corpus changed underneath the system. |
| Kill switch state | `kill_switch_engaged` | Should be 0 in normal operation. A 1 that nobody present can account for is itself an incident: somebody halted the system and did not say so. |

One caveat that will otherwise waste your night. `guardrail_blocks_total`
counts blocks, not activations, and it only increments when a check both fails
and is blocking. On the escalation path only the private-data check blocks, so
grounding and confidence-floor activations on handover notes do not appear here
at all. A flat counter does not mean the guardrails are idle. For activations,
go to the decision log.

### From the decision log

The live log is SQLite at `storage/decisions.db` (`DATABASE_URL`). It is
append-only in practice, nothing updates or deletes a row, and it is in WAL
mode, so you can read it with the system running.

One thing to know before you query: the table accumulates runs, and every row
carries a `run_id`. Filter by it or you will count the same ticket three times.

```bash
sqlite3 storage/decisions.db
```

```sql
-- The run you are looking at, almost always the most recent.
SELECT run_id, COUNT(*) AS decisions, MIN(timestamp) AS started, MAX(timestamp) AS ended
FROM decisions
GROUP BY run_id
ORDER BY started DESC
LIMIT 5;
```

```sql
-- Anything a guardrail flagged in the last day, newest first.
-- Read action_taken, not prediction: a row can show "released" and still have
-- escalated, because on an escalation only the private-data check blocks.
SELECT ticket_id, timestamp, action_taken, confidence, guardrail_results
FROM decisions
WHERE stage = 'validation'
  AND guardrail_results LIKE '%"fail"%'
  AND timestamp > datetime('now', '-1 day')
ORDER BY timestamp DESC;
```

```sql
-- Private data, which should return nothing. If it returns anything, stop
-- reading this file and go to section 5, step 4.
SELECT ticket_id, timestamp, action_taken, guardrail_results
FROM decisions
WHERE stage = 'validation'
  AND guardrail_results LIKE '%"pii": "fail"%';
```

```sql
-- What the router actually did, by rule. Baseline on 500 development tickets:
-- R-05-auto-respond 407, R-01-policy-class 82, R-04-below-threshold 6,
-- R-03-high-cost-class 5. A rule that has stopped firing is as informative as
-- one that has started.
SELECT model_version AS rule, COUNT(*) AS tickets
FROM decisions
WHERE stage = 'routing' AND run_id = :run_id
GROUP BY rule
ORDER BY tickets DESC;
```

```sql
-- Which intents are being answered automatically, and how many.
-- The intent lives on the classification row; the outcome lives on the
-- validation row, so this joins them on ticket_id within one run.
SELECT c.prediction AS intent, COUNT(*) AS answered
FROM decisions c
JOIN decisions v
  ON v.ticket_id = c.ticket_id AND v.run_id = c.run_id AND v.stage = 'validation'
WHERE c.stage = 'classification'
  AND c.run_id = :run_id
  AND v.action_taken = 'auto_respond'
GROUP BY intent
ORDER BY answered DESC;
```

```sql
-- Confidence below the floor but answered anyway. Should be empty. If it is
-- not, the threshold is not being applied and that is a system fault, not a
-- content fault.
SELECT ticket_id, confidence, threshold_applied, reason
FROM decisions
WHERE stage = 'validation'
  AND action_taken = 'auto_respond'
  AND confidence < threshold_applied;
```

---

## 2. Blast radius: how many customers got this

The question after "is it real" is always "how many". Because every answer
records the document it came from, that question is one query. The `sources_used`
column is JSON, so use `json_each`.

```sql
-- Every article the system has cited in this run, and how many customers saw
-- an answer built on it.
SELECT json_extract(s.value, '$.doc_id') AS doc_id,
       COUNT(DISTINCT d.ticket_id)       AS tickets
FROM decisions d, json_each(d.sources_used) s
WHERE d.stage = 'validation'
  AND d.action_taken = 'auto_respond'
  AND d.run_id = :run_id
GROUP BY doc_id
ORDER BY tickets DESC;
```

```sql
-- Everyone who received an answer citing one specific article, with the ticket
-- text, so you can read what they actually asked.
SELECT d.ticket_id, d.timestamp, d.input_summary, d.confidence
FROM decisions d, json_each(d.sources_used) s
WHERE d.stage = 'validation'
  AND d.action_taken = 'auto_respond'
  AND json_extract(s.value, '$.doc_id') = 'DOC-DATA-003'
ORDER BY d.timestamp DESC;
```

That list is the notification list. Hand it to the Head of Support, who decides
what those customers are told. A correction goes out from a named human, never
from the system.

---

## 3. Reconstructing what the system did for one customer

Two routes to the same five rows. Use the API if it is up, because it decodes
the JSON columns for you.

```bash
curl -sS http://localhost:8000/decisions/TCK-4821 | python3 -m json.tool
```

Returns the stages in order and every field of every decision: what the
classifier predicted and what else it considered, which passages were retrieved
and with what scores, which routing rule fired and the reason in plain English,
which prompt version drafted the answer, and what each of the five guardrails
found.

If the API is down, the same thing from SQLite:

```sql
SELECT stage, timestamp, action_taken, prediction, confidence,
       threshold_applied, model_version, reason
FROM decisions
WHERE ticket_id = 'TCK-4821'
ORDER BY timestamp, rowid;
```

```sql
-- The passages that were in front of the generator, with their scores.
SELECT json_extract(s.value, '$.doc_id')  AS doc_id,
       json_extract(s.value, '$.chunk_id') AS chunk_id,
       json_extract(s.value, '$.title')    AS title,
       json_extract(s.value, '$.score')    AS score
FROM decisions d, json_each(d.sources_used) s
WHERE d.ticket_id = 'TCK-4821' AND d.stage = 'retrieval'
ORDER BY score DESC;
```

```sql
-- What the classifier nearly said instead. A confident call and a coin flip
-- look identical without this.
SELECT json_extract(a.value, '$.value')      AS alternative,
       json_extract(a.value, '$.confidence') AS confidence
FROM decisions d, json_each(d.alternatives) a
WHERE d.ticket_id = 'TCK-4821' AND d.stage = 'classification'
ORDER BY confidence DESC;
```

What a healthy trail looks like, from ticket DEV-0001 of the shipped
development run:

| Stage | action_taken | model_version | Reason |
|---|---|---|---|
| classification | classified | classifier v2.1 | Classified as deployment_failure at 1.00 confidence |
| retrieval | retrieved | top_k=5, floor=4.2 | Retrieved 5 passages above the relevance floor of 4.2 |
| routing | auto_respond | R-05-auto-respond | Recognised as a deployment failure with 100% confidence, DOC-DEPLOY-003 covers it |
| generation | drafted | extractive v1.2 | Drafted via extractive with 1 citation |
| validation | auto_respond | v2.1 | All 5 checks passed; response released |

Five rows per ticket, always. Four rows means something went wrong with the
logging and not just with the answer.

---

## 4. Is this a corpus problem or a system problem?

This is the fork that decides who fixes it, and getting it wrong wastes a day.
Ines Varga put the requirement plainly in her interview: "I would want to know
which article an answer came from, so that when an answer is wrong I can tell
whether the article is wrong or the system misread it. Those need fixing in
completely different places."

The decision log answers it in three steps.

**Step one. Find the article the answer came from.** The validation row's
`sources_used` holds the cited `doc_id`. Open that article in
`data/documentation.json`.

```bash
python3 - <<'PY'
import json
docs = {d["doc_id"]: d for d in json.load(open("data/documentation.json"))}
d = docs["DOC-DATA-003"]
print(d["title"], "|", d["category"], "| reviewed", d["last_reviewed_days_ago"], "days ago")
print(d["content"])
PY
```

**Step two. Read the article against the customer's question.** Three outcomes,
and only three.

| What you find | Diagnosis | Owner | Fix |
|---|---|---|---|
| The article says something that is no longer true, or was never true | Corpus problem | Technical Writer (Ines Varga) | Correct the article, reload the corpus, re-run the affected tickets. No code change. |
| The article is correct and the reply misstates it, or attaches the citation to a claim the passage does not make | System problem | The engineer on call | The generator or the grounding check. Add the failing case to `tests/` first, then fix. |
| The article is correct and simply does not cover what was asked, but the system answered anyway | Coverage problem | Head of Support, with Ines | The hardest of the three and the most common. See below. |

**Step three, for the coverage case.** Compare the retrieval scores against
what a good match looks like. `sources_used` on the retrieval row carries the
BM25 score per passage. A confident, well-covered answer on this corpus scores
in the low twenties on the top passage. What will not help you is treating a
low score as proof of a gap: on the development set the median top-1 score is
22.8 for answerable tickets and 20.9 for unanswerable ones, and at the chosen
floor of 4.2 fully 95.1% of unanswerable tickets still get a passage back. The
system genuinely cannot tell the two apart, which is documented in
`evaluation/results/answerability_probe.json` and is why no answerability gate
was built. 143 of 500 development tickets (28.6%) are not answerable from the
current 29 articles, and 86 of the 101 false auto-responses are exactly those
tickets.

So in the coverage case the judgement is human and the remedy is either a new
article or removing that intent from automation. To do the second immediately,
add the intent to `NEVER_AUTO_RESPOND` in `src/config.py`. That takes the whole
class out of automation at rule `R-01`, at any confidence, and it is a
three-line change with a test already in place
(`tests/test_route.py::test_policy_classes_escalate_at_the_highest_confidence`).

---

## 5. Worked example: a customer says they got a wrong answer about data residency

It is 02:10. Marcus has forwarded an email from a customer on the business tier
saying the system told them their data is stored in their own region, and their
auditor has just told them otherwise.

Data residency is one of the five high-cost intents, held to a confidence bar of
0.85 rather than 0.62, precisely because Daniel said in discovery that data
location is a category "we get wrong occasionally even as humans and the
consequence is a compliance problem". So this is exactly the case the design
was worried about, and it is worth the full six steps.

**1. Detect (10 minutes).** Get the ticket id from the forwarded email. Pull
the trail.

```bash
curl -sS http://localhost:8000/decisions/TCK-7731 | python3 -m json.tool
```

Read four things and nothing else yet: the routing rule, the confidence, the
cited `doc_id`, and the guardrail block. Suppose the trail shows
`R-05-auto-respond`, confidence 0.91, cited `DOC-DATA-003`, all five guardrails
passing. The system was above the high-cost bar, it cited a real article, and
nothing was supposed to stop it. That is consistent with the customer's account,
so the report is real.

**2. Contain (5 minutes).**

```bash
curl -sS -X POST http://localhost:8000/admin/halt -H "X-Admin-Token: $ADMIN_TOKEN"
curl -sS http://localhost:8000/healthz | python3 -m json.tool   # confirm engaged: true
```

Do not narrow it to data residency first. Halt everything, then narrow. You do
not yet know whether DOC-DATA-003 is the only article affected.

**3. Assess (30 to 60 minutes).** Scope first:

```sql
SELECT d.ticket_id, d.timestamp, d.confidence, d.input_summary
FROM decisions d, json_each(d.sources_used) s
WHERE d.stage = 'validation'
  AND d.action_taken = 'auto_respond'
  AND json_extract(s.value, '$.doc_id') = 'DOC-DATA-003'
  AND d.timestamp > datetime('now', '-7 day')
ORDER BY d.timestamp;
```

On the development run DOC-DATA-003 was cited on 29 tickets, so expect this to
return tens rather than units. Every row is a customer who may have been told
the same thing.

Then cause. Open DOC-DATA-003, "Data residency and regional storage", and read
it against what the customer asked. In this scenario the article is accurate for
the standard regions and says nothing about the customer's specific
configuration, so the reply was drawn from an article that was adjacent rather
than right. That is the coverage case in section 4, and it is the residual risk
named in the governance framework: a partial answer that is grounded, cited and
false at the same time. The grounding guardrail cannot catch it, because every
sentence really did come from the passage.

**4. Notify (within the hour).** Marcus gets three facts and nothing else:
how many customers are on the list from the scope query, that DOC-DATA-003 is
the implicated article, and that automation is currently halted. He decides what
customers are told and who else at CloudServe hears about it. Corrections go out
from a named human. If the scope query had shown a private-data block instead,
CloudServe's data protection process starts here and this runbook stops being
the governing document.

**5. Remediate (hours).** Two changes, in this order. Immediately, take the
class out of automation so that resuming is safe:

```python
# src/config.py
NEVER_AUTO_RESPOND = frozenset({
    "security_incident",
    "compliance_request",
    "feature_request",
    "unclear_request",
    "data_residency",     # added <date>, incident <ref>, pending a corpus fix
})
```

Run the tests and the full harness before resuming:

```bash
python -m pytest tests/ -q
python -m evaluation.harness --input data/development_tickets.json --output /tmp/check
echo "exit code: $?"   # must be 0; 2 means the decision log did not reconcile
```

Then, on a normal working day rather than at 02:10, Ines extends DOC-DATA-003 to
cover the configuration the customer asked about. When the article lands, remove
the line from `NEVER_AUTO_RESPOND`, re-run the harness, and check that the data
residency tickets in the development set still route as expected. Daniel
verifies a sample of the corrected answers before the class goes back into
automation.

Resume:

```bash
curl -sS -X POST http://localhost:8000/admin/resume -H "X-Admin-Token: $ADMIN_TOKEN"
```

**6. Review (within five working days).** Marcus chairs, the engineer on call
writes it, and the incident is not closed until it has produced one of three
things: a test case, a guardrail pattern, or an amended row in the risk
register. In this example the honest output is an amended row: R-09, the risk
that the system answers a ticket the corpus does not cover, with this incident
attached as the first real-world instance. Filed alongside the ADRs in
`docs/decisions/`.

---

## What this runbook does not cover

Ingest rejections. A malformed payload is refused with a 400 before a `Ticket`
object exists, so it never reaches the decision log and none of the queries here
will find it. If a customer's ticket appears to have vanished rather than to
have been answered badly, check the application log rather than
`decisions.db`. Closing that gap, by writing an ingest-stage row from the
rejection handler, is the first change I would make to this system in
production.
