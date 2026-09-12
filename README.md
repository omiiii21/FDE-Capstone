# CloudServe support triage

Om Mengshetti · Forward Deployed AI Engineering capstone · September 2026

CloudServe Solutions asked for a chatbot. This is not one.

It is a triage pipeline. Every incoming ticket is normalised, classified,
matched against CloudServe's own documentation, and then either answered with
citations or handed to a support engineer with a summary, the relevant articles
and a plain statement of what the system was unsure about. Roughly four in five
tickets get an answer in under a second. The rest reach a human in better shape
than they do today.

The reasoning behind that framing is in `docs/decisions/ADR-001-triage-not-chatbot.md`.
The short version: a chatbot is a delivery mechanism. It says nothing about
where the answer comes from, whether it is right, or who is accountable when it
is not, and those were the three things wrong with CloudServe's support
function.

---

## Running it

You need Python 3.10 or later and about 200MB of disk. You do not need an API
key and you do not need a network connection. That is deliberate and section
["Running without a model provider"](#running-without-a-model-provider) explains why.

```bash
git clone git@github.com:omiiii21/FDE-Capstone.git
cd FDE-Capstone

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

cp .env.example .env               # works as-is; edit only to add a model key
```

That is the whole setup. Confirm it worked:

```bash
python -m pytest tests/ -q
```

### The full evaluation run

This is the command the project is assessed on. It takes an input path and an
output path, so it can be pointed at any ticket file using the standard schema,
including one this repository has never seen.

```bash
python -m evaluation.harness \
    --input data/validation_tickets.json \
    --output evaluation/results/latest
```

It runs unattended, processes every ticket in the file, and writes four things
into the output directory:

| File | What it is |
| --- | --- |
| `metrics.json` | Every figure, grouped as volume, business, technical, governance and fairness |
| `summary.md` | The same numbers as a table you can read |
| `responses.jsonl` | One line per ticket: what came in, what the system did, what it sent |
| `decision_log.jsonl` | Every decision at every stage, exported from the SQLite log |

On 80 validation tickets it finishes in well under a second. Exit code 0 means
the run completed; 2 means it completed but the decision log did not reconcile
against the tickets processed, which is a defect worth knowing about
immediately rather than discovering in a report.

### The API

```bash
uvicorn src.api:app --port 8000
```

Then `http://127.0.0.1:8000/docs` for the generated documentation. The
interesting endpoints:

```bash
# process one ticket
curl -X POST localhost:8000/tickets -H 'content-type: application/json' \
  -d '{"ticket_id":"DEMO-1","channel":"chat","subject":"","body":"my deployment keeps dying and I cannot work out why","customer_tier":"business"}'

# reconstruct why a decision was taken
curl localhost:8000/decisions/DEMO-1

# health, including whether the model provider is reachable
curl localhost:8000/healthz
```

The API is the demonstration and integration surface. The harness is the thing
the acceptance criteria are actually tested against.

### The kill switch

Automatic responses can be stopped without a deployment, in the time it takes
to write a file:

```bash
export ADMIN_TOKEN=some-secret-value          # unset means the endpoint refuses, not that it opens
curl -X POST localhost:8000/admin/halt -H "X-Admin-Token: $ADMIN_TOKEN"
```

Or, if the API is not running, `touch storage/HALT`. Either way the next ticket
routes on rule `R-00-kill-switch` and goes to a human with its draft attached.
`POST /admin/resume` or deleting the file turns it back on. Nothing in flight is
lost. This is covered in `docs/incident_response.md`.

---

## What it does, in order

```
ticket ──▶ ingest ──▶ classify ──▶ retrieve ──▶ route ──┬─▶ generate ──▶ validate ──▶ send
                                                        │                   │
                                                        └─▶ escalate ◀──────┘
                                                              (blocked, or policy)
                          every stage writes to the decision log
```

| Stage | File | What it is responsible for |
| --- | --- | --- |
| Ingest | `src/ingest.py` | Four channels into one representation. Strips email quote chains, repairs malformed records rather than dropping them. |
| Classify | `src/classify.py` | Intent from a calibrated linear model; urgency from a documented policy table. Confidence means P(this prediction is correct). |
| Retrieve | `src/retrieve.py` | BM25 over section-aware chunks, with a vocabulary bridge between customer words and documentation words. Returns nothing when nothing is relevant. |
| Route | `src/route.py` | Six ordered rules, first match wins. Deterministic. Every decision carries a reason a support manager could read. |
| Generate | `src/generate.py` | An answer grounded in the retrieved passages, with citations. Two implementations; see below. |
| Validate | `src/guardrails.py` | Five checks that can block. None of them call a model. |

Supporting pieces: `src/provider.py` (model access and what happens without
it), `src/logging_store.py` (the decision log), `src/pipeline.py` (the
orchestration), `src/api.py`, `src/metrics.py` (Prometheus).

### Running without a model provider

Two generators sit behind one interface. `ModelGenerator` asks the language
model to draft from the retrieved passages using `prompts/build/PR-01_answer_draft.v1.3.txt`.
`ExtractiveGenerator` composes the reply out of sentences that already exist in
the corpus; it cannot invent anything, because it cannot write a sentence the
documentation does not contain.

When no key is configured, or the provider times out, rate-limits or goes down
entirely, the system uses the extractive path. The answer is duller and it is
still cited and still correct. This is why the whole evaluation set processes
with no key at all, and it is the answer to acceptance criterion A11.

To use a model, put a key in `.env`:

```
OPENROUTER_API_KEY=sk-or-...
MODEL_NAME=meta-llama/llama-3.1-8b-instruct
```

Free tiers are sufficient. Responses are cached in `storage/provider_cache`, so
a repeated run costs nothing and gives the same output.

---

## Reproducing the numbers

Every figure in the report comes from one of these. They all write to
`evaluation/results/`.

```bash
python -m scripts.train_classifier      # retrains; the artefact is committed, so this is optional
python -m scripts.tune_retrieval --full # the top_k and relevance floor sweep
python -m scripts.tune_threshold        # the routing threshold cost model
python -m scripts.fairness_audit        # segment comparison and the matched-pair test
python -m scripts.documentation_gap     # where the knowledge base does not cover the queue
python -m scripts.answerability_probe   # the negative result that removed a component
```

`scripts/documentation_gap.py` is the one to run if you only run one. It is the
most useful output in this repository and it is not code.

### Seeing the guardrails block something

There is an awkward property of this system worth being upfront about. Across
the 500 development tickets, not one response was blocked. That is not because
the checks are weak: routing diverts the policy classes and the high-cost
classes before anything is drafted, and the extractive generator can only emit
sentences that exist in the corpus, which contains no customer data. Between
them, nothing unsafe ever reaches the guardrails.

Good operationally, useless as evidence. So there is a fault-injection provider
that returns what a language model having a bad day returns:

```bash
PROVIDER=unsafe_demo python -m evaluation.harness \
    --input data/guardrail_probe_tickets.json \
    --output evaluation/results/guardrail_demo
```

`data/guardrail_probe_tickets.json` holds nine tickets written to attack the
system: private data in the body, two prompt injections, a demand for a refund
and a fix date, a security incident, a question the corpus cannot answer, an
empty body, one carrying full-width characters, zero-width joiners, HTML
entities and control bytes, and one with a twenty thousand character log
pasted into it.

Run normally, all nine are handled without incident and seven never reach
generation. Run with `PROVIDER=unsafe_demo`, the simulated model promises a
refund, invents a delivery date and quotes the customer's own words back, and
the response is blocked on grounding and commitments and escalated instead,
with the reason recorded in the decision log.

---

## Layout

```
src/                    the system
prompts/build/          prompts that run inside it, versioned
prompts/evaluation/     prompts used to judge its output
evaluation/harness.py   the unattended run
evaluation/metrics.py   every metric calculation
evaluation/results/     dated output from each run
scripts/                the analyses behind the design decisions
models/                 the trained classifier, committed as JSON
tests/                  run with: python -m pytest tests/ -q
docs/architecture.md    how it fits together and why
docs/decisions/         seven architecture decision records
data/                   the four supplied data files
```

`data/` holds the files as supplied, not samples. They total under a megabyte
and the system cannot run without the documentation corpus, so committing them
keeps the repository self-contained. Nothing generated at runtime is committed:
`storage/` is ignored.

---

## Things worth knowing before you read the numbers

**The development set has 500 tickets and 215 distinct ticket bodies.** A
random train/test split puts the same text on both sides of the line and
reports 99.2% classification accuracy. Split by distinct body, the same model
scores 92.2%. Every figure reported anywhere in this project uses the second
one. 42 of the 60 distinct validation bodies also appear in the development
set, so validation accuracy is inflated too, and it is labelled as such
wherever it appears.

**The confidence threshold is nearly inert on this data.** The cost model in
`scripts/tune_threshold.py` is flat between 0.30 and 0.85. The threshold sits
at 0.62 because that is where observed accuracy collapses on held-out data, not
because the sweep chose it. What actually decides routing here is the policy
classes and the grounding guardrail.

**The ceiling is the documentation, not the model.** 28.6% of tickets are not
answerable from the existing 29 articles, and the system cannot reliably tell
which ones before it answers them — `scripts/answerability_probe.py` is the
failed attempt, kept because it changed the design. Writing about fourteen
articles would move the automation ceiling from 71.4% to 88.8%. No amount of
retrieval tuning will.

---

## Attribution

The BM25 implementation in `src/retrieve.py` follows Robertson and Zaragoza,
*The Probabilistic Relevance Framework: BM25 and Beyond* (2009). The confidence
calibration approach in `src/linear.py` is a two-feature variant of Platt
scaling; the temperature-scaling method it replaced is from Guo et al.,
*On Calibration of Modern Neural Networks* (2017). Everything else is written
for this project.

AI assistance was used and is declared in section 11 of the report. In short: I
used Claude for drafting code, for reviewing my own prompt text, and for
explaining library behaviour. The discovery findings, the problem statement, the
evaluation interpretation and the reflection are mine, and the three findings
that changed the design — the duplicate-body leakage, the inert threshold and
the documentation ceiling — came out of running the numbers rather than out of a
model.
