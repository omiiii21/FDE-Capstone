# Monitoring

Prometheus scrape config and a Grafana dashboard for the triage system, plus
the part that matters more than either: what a bad shape looks like on each
panel.

A dashboard that only tells you the system is alive is not worth the screen it
occupies. The counters here tell you volume and outcome, which you would notice
anyway. The confidence histogram is the reason this directory exists.

---

## Bringing it up

**1. The application.** Metrics are served on their own port so that scraping
does not depend on the application being healthy and the endpoint does not sit
next to the customer-facing routes.

```bash
pip install -r requirements.txt          # prometheus-client is already in it
export METRICS_PORT=8001                 # the default
uvicorn src.api:app --port 8000
```

Check the endpoint before going any further:

```bash
curl -s localhost:8001/metrics | grep -E '^(tickets_processed|response_seconds|guardrail_blocks|classification_confidence|kill_switch)'
```

Nothing there means one of two things, both visible in the application log:
`prometheus_client` is not installed, in which case every collector is a no-op
and the metrics are being discarded silently by design, or the port was already
in use, in which case the server logged a warning and carried on serving
tickets blind. Neither stops the system working. Both stop this dashboard
working.

**2. Prometheus.**

```bash
prometheus --config.file=docs/monitoring/prometheus.yml
```

Confirm the target is up at `http://localhost:9090/targets`. Edit the `targets`
and the `environment` label in `prometheus.yml` per deployment; running
Prometheus in Docker against an application on the host usually means
`host.docker.internal:8001` rather than `localhost:8001`.

**3. Grafana.** Add a Prometheus data source pointing at
`http://localhost:9090`, then Dashboards, New, Import, Upload JSON file, and
select `docs/monitoring/grafana_dashboard.json`. Pick the data source when
prompted; the dashboard takes it as a variable rather than hard-coding a uid,
so it imports into any instance. The dashboard uid is `cloudserve-triage`, so
re-importing updates it in place rather than creating a second copy.

**One thing to know before you wonder why the graphs are empty.** These metrics
are recorded on the API path only, in `record_response()`, which is called from
`POST /tickets`. The batch evaluation harness does not emit Prometheus metrics;
it writes `evaluation/results/<run>/metrics.json` instead, which is a different
artefact for a different purpose. A full harness run will not move any panel on
this dashboard.

---

## The panels

Every baseline quoted below comes from the 500-ticket development run in
`evaluation/results/dev_run/metrics.json`. They are what the system did on that
data, not a service level anyone has agreed to.

### 1. Tickets per hour by channel and outcome

`sum by (channel, outcome) (rate(tickets_processed_total[5m])) * 3600`

**For.** Knowing what arrived and what happened to it, split four ways by
channel and three ways by outcome (`auto_respond`, `escalate`, `blocked`). This
is the panel you open first during an incident, because it answers "is this
happening to everyone or to one channel".

**Baseline.** Development composition was email 212, chat 155, documentation
comment 78, forum 55.

**Bad shapes.** One channel flat at zero while the others continue: that channel
has stopped being ingested, and nobody will complain about it until the backlog
is a day old. A `blocked` band appearing at all: on the development run it was
zero, because routing diverted everything a guardrail would have caught, so any
visible `blocked` volume means responses are being stopped after being drafted
and is worth reading the decision log over. Documentation-comment volume rising
as a share: it is the worst channel in the current process, 34.6% first contact
resolution against a 43.8% average, and a rise there is a rise in the hardest
traffic. Total volume stepping up or down without a product event behind it:
suspect the source system before suspecting this one.

### 2. Automated versus escalated share

`sum by (outcome) (rate(tickets_processed_total[30m])) / scalar(sum(rate(tickets_processed_total[30m])))`

**For.** The single number the business case rests on. At the shipped operating
point the routing cost model puts a ticket at 3.12 against 4.00 for escalating
everything, a saving of 0.88 per ticket, and that saving is bought entirely with
this share.

**Baseline.** 81.4% automated, 18.6% escalated, 0% blocked. The current human
process resolves 43.8% at first contact.

**Bad shapes.** Automated share climbing above about 0.88 is the dangerous
direction, not the good one: this system's ceiling on the development data is
82.6% automation, and a sustained rise above it means tickets that used to
escalate are now being answered, which is what a classifier drifting confident
looks like from the outside. Falling below about 0.70 means retrieval or the
classifier has degraded and the pipeline is failing safe. A move of ten points
inside an hour matters more than either absolute number, because the traffic mix
does not change that fast and the system does. A step to 0% automated with no
alert anywhere else is almost always the kill switch: check panel 7 before
investigating anything.

### 3. Response latency, p50 and p95

`histogram_quantile(0.50, sum by (le) (rate(response_seconds_bucket[5m])))`
`histogram_quantile(0.95, sum by (le) (rate(response_seconds_bucket[5m])))`

**For.** Whether the thing is fast, and specifically whether the tail is
detaching from the middle. Chat customers leave if nothing happens, which is why
the histogram buckets are tight below one second and coarse above five.

**Baseline.** p50 0.3 ms, p95 0.4 ms, p99 0.6 ms, max 6.1 ms, measured with no
model provider configured. Nothing on the request path calls a network service
except the optional generator: retrieval is BM25 over 69 chunks, classification
is a numpy dot product, routing is eight comparisons.

**Bad shapes.** p95 detaching from p50 while p50 holds: a tail, and on this
architecture the tail is almost always the model provider. p95 pinned near 25
seconds, the `PROVIDER_TIMEOUT`: the provider is timing out and the retries are
being paid for in full, three attempts with backoff before the circuit opens for
sixty seconds. Both series collapsing to near zero while volume holds steady:
every ticket has fallen to the extractive path, which means the provider is
unreachable and the system is quietly degraded rather than broken. That last one
looks like an improvement on this panel and is not, which is exactly why it is
worth writing down.

### 4. Guardrail activations by type

`sum by (guardrail) (increase(guardrail_blocks_total[1h]))`

**For.** What the five checks are stopping, per guardrail rather than per
response, because a response stopped by two checks is two different things going
wrong.

**Baseline.** Zero blocks on the development run. All 407 automatic replies
passed all five checks.

**Read this before drawing a conclusion from a flat line.** This counter
increments only when a check both fails and is blocking. On the escalation path
only the private-data check blocks, because the checked text is an internal
handover note that deliberately carries no citations. So the 93 grounding, 14
confidence-floor and 5 commitment activations recorded in the decision log for
the development run appear nowhere on this panel. A flat counter does not mean
the guardrails are idle. For activations rather than blocks, query
`decisions.db`; `docs/incident_response.md` has the SQL.

**Bad shapes.** Any `pii` increment at all, which is coloured red for that
reason: this counter has never moved in 580 evaluated tickets, and one
increment is an incident rather than a trend. A step change in `grounding`,
which usually means the corpus changed underneath the system, an article was
edited or reindexed and answers have stopped resolving against it. `injection`
appearing repeatedly from what looks like one source: somebody is working on it,
and the second attempt is usually better than the first. A guardrail that used
to fire occasionally and has gone silent: patterns do not stop matching by
themselves, so suspect a deployment.

### 5. Classifier confidence distribution

`sum by (le) (increase(classification_confidence_bucket[1h]))`

**For.** Drift, earlier than anything else on this dashboard can show it. Every
prediction is observed here whatever the routing decision was. The distribution
slides left for a week or two before the escalation rate moves far enough for
anybody to notice it on panel 2, and by the time panel 2 moves the customers
have already had the answers.

**Baseline.** Heavily right-shifted and tight: 478 of 500 predictions in the 0.8
to 1.0 band, 13 below 0.6, expected calibration error 0.0094, and the top band
sits 0.1 points from perfect. The shape to compare against is a dense block at
the top of the heatmap with a thin scatter underneath.

**Bad shapes.** Mass leaving the top band, in any quantity: the model is
recognising less of the traffic and CloudServe's product has probably changed
under it. Weight accumulating immediately under 0.62, the routing threshold,
which is the specific shape that produces a surge of escalations a few days
later. The distribution becoming bimodal, a cluster high and a cluster low with
nothing between: that is usually two populations of traffic, one the model knows
and one it does not, and it is the signature of a new intent arriving that has
no class. A distribution that gets tighter and higher is not automatically good
news either: the calibrator was fitted on two features and a distribution that
saturates at 1.000 leaves the threshold nothing to separate, which is precisely
why temperature scaling was rejected during development.

### 6. Predictions below the routing threshold

`sum(increase(classification_confidence_bucket{le="0.6"}[6h])) / sum(increase(classification_confidence_count[6h]))`

**For.** Panel 5 as a single number, for when you want to know whether the shape
has moved without reading a heatmap. 0.6 is the nearest histogram bucket edge
below the 0.62 threshold, so this slightly understates the true share.

**Baseline.** 13 of 500, 2.6%. Fourteen tickets fell below the threshold itself.

**Bad shapes.** Anything above about 10% is a real change, since the observed
accuracy below 0.62 is around 21% against roughly 100% above it, so every point
here is a point of traffic the system genuinely should not be answering. A
reading of exactly zero over a long window is not reassuring: it means nothing
is landing in the low buckets at all, which on this calibrator is more likely to
be saturation than competence.

### 7. Kill switch

`max(kill_switch_engaged)`

**For.** Whether automation is currently on. The gauge is refreshed on every
processed ticket and on every health check, so it is current rather than a
startup value.

**Bad shapes.** A 1 that nobody present can account for. Somebody halted the
system and did not say so, which is a communication incident rather than a
technical one and is worth treating as an incident anyway. A 0 during an
incident that should have been contained is the other direction: the halt was
attempted and did not take, and section 0 of `docs/incident_response.md` has the
second path, `touch storage/HALT` on the host.

---

## What is deliberately not here

No alerting rules. Every threshold suggested above was derived from 500
development tickets, and committing them as alerts would give them an authority
they have not earned and train people to ignore them. They should be set after
a shadow-mode period, from what CloudServe's traffic actually does.

No per-intent or per-ticket labels. Both would be unbounded, and an unbounded
label is the usual way a Prometheus instance falls over. The bounded labels here
give at most a few dozen series in total. Per-intent analysis belongs in the
decision log, which is indexed for it.

No business metrics. First contact resolution, citation accuracy and the
fairness segments are not on this dashboard, because none of them can be
computed without labels the system does not have at runtime. They come from the
evaluation harness, against labelled data, and they are reported in
`evaluation/results/<run>/summary.md`.
