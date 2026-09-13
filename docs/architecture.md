# Architecture

CloudServe asked for a support chatbot. By the end of the discovery interviews I was fairly sure that building one would have been the wrong answer to the problem they actually have, and everything else in this document follows from that judgement, so it is worth setting out first.

## The reframing

A chatbot is a delivery mechanism. It describes the surface a customer types into and it says nothing at all about the three questions that decide whether support automation helps or hurts: where the answer comes from, which tickets should get one, and who is accountable when the answer is wrong. You can build a chatbot on top of a system that has good answers to those questions, and you can build one on top of a system that has none, and from the outside on a good day the two look identical.

Three things in the data settled it. Only 71.4% of tickets are labelled answerable from the existing documentation, so roughly three in ten have no grounded answer available to write, whatever the interface. Eighty-seven of the 500 development tickets carry `must_not_auto_respond` (security incidents, compliance requests, feature requests, unclear requests), where the right behaviour is a fast handover rather than a good reply. And the retrieval score does not separate the answerable tickets from the unanswerable ones: median top-1 score is 22.8 for the first group and 20.9 for the second, and at the floor I ship, 95.1% of unanswerable tickets still get a passage back. The system cannot reliably know in advance whether it is about to answer from evidence or from the nearest-looking thing in the corpus.

Put those together and the hard part is not writing prose. It is deciding: this one I can answer from a document I can name, that one goes to a person now with the working shown. So what I built is a triage system with assisted escalation. Every ticket gets classified, grounded if possible, routed by rule, and logged; the answer, when there is one, is the easy half. A chat surface could sit on top of this later and would change nothing underneath it. Nothing underneath it could be bolted onto a chat surface afterwards.

## The six components

The pipeline in `src/pipeline.py` composes six things, and each of them owns exactly one question.

Ingest (`src/ingest.py`) turns a record from any of the four channels into one `Ticket`. It folds unicode, strips quoted reply chains and signatures from email, keeps the original payload verbatim on `raw`, and repairs rather than rejects: the only thing it refuses is a record with no usable identifier, because a malformed ticket is still a customer. Channel survives normalisation because it changes the latency budget and the tone of a good reply, but nothing downstream has to branch on it to work.

Classification (`src/classify.py`, `src/linear.py`) answers what this ticket is about and how much that answer should be trusted. It is tf-idf over word unigrams and bigrams feeding a multinomial logistic regression, written in numpy, serialised to JSON in `models/intent_classifier.json`. Twenty-two intents. Body-disjoint accuracy is 92.2%, and the confidence it reports is not the softmax mass on the winning class but a calibrated estimate of P(correct), which is the only version of a confidence number a threshold can safely be set against. Urgency comes from a policy table in the same module rather than a model, for reasons in ADR-006.

Retrieval (`src/retrieve.py`, `src/text.py`) answers what the documentation says about it. BM25 with k1=1.4 and b=0.72 over section-aware chunks, with title and category boosts of 2.6 and 1.4. The 29 articles become 69 passages, split on their own headings rather than on a character count. Before the query is scored it passes through a vocabulary bridge that adds documentation words alongside the customer's own. Recall@5 is 96.4% and precision@1 87.7%, at a mean of 4.81 passages returned.

Routing answers whether this ticket gets an automatic reply. Eight ordered rules, first match wins, no clock and no randomness, so the same ticket produces the same decision every time. Six of them are in `src/route.py`: R-00 kill switch, R-01 policy class, R-02 no grounding, R-03 high-cost class, R-04 below threshold, R-05 answer. The other two fire in `src/pipeline.py`, because each depends on something the router is never handed: R-01b downgrades an approved ticket to an escalation when the injection check has flagged the incoming text, and R-06 does the same when the generator declines to write an answer it can stand behind. The docstring in `src/route.py` says six and is right about its own file. The path a ticket takes has eight. Every branch returns a reason written for Marcus rather than for me, because "confidence 0.58 against the 0.62 we require" is something he can repeat in a compliance review and "routing_rule_4" is not.

Generation (`src/generate.py`) writes the customer-facing draft or the engineer's handover note. Two implementations: `ExtractiveGenerator`, which composes the reply out of sentences that already exist in the corpus and therefore cannot invent one, and `ModelGenerator`, which drafts from the same passages using `prompts/build/PR-01_answer_draft.v1.3.txt`.

Validation (`src/guardrails.py`) answers whether what was written is safe to send. Five deterministic checks cover private data, injection, grounding, commitments and the confidence floor. All of them run every time, and none of them calls the model.

## Three layers

The six components sort into three layers, and the boundary that matters is the second one.

The decision layer is ingest, classification, retrieval, routing and the guardrails. It is deterministic, local, and makes no network calls. Every decision that has a consequence for a customer is taken here, which is why the whole 80-ticket validation file runs in about a tenth of a second on the default path. The run recorded in `evaluation/results/validation_run/metrics.json` took 0.11 seconds for 80 tickets. That figure moves by a few hundredths of a second between runs on the same machine, so it is worth reading as an order of magnitude rather than as a constant.

`src/provider.py`, `src/generate.py` and the prompt files make up the language layer. It is the only place a model is involved, it is reached through one interface with four implementations, and it is fenced on both sides: the router has already decided this ticket may be answered before the layer is entered, and the grounding guardrail checks what comes out before it is released. The model is allowed to improve the prose. It is not allowed to decide anything.

Persistence and observation is `src/logging_store.py`, `src/metrics.py` and `src/config.py`, with `evaluation/harness.py` and `evaluation/metrics.py` reading from it. Every tunable value lives in config rather than scattered through the modules, because thresholds get argued about and there should be one place to look when the question is what it was set to on a given run.

## One ticket, end to end

```
   raw record (email | chat | docs_comment | forum)
        |
        v
  +------------------+
  | src/ingest.py    |  one Ticket; channel kept, raw payload preserved
  +------------------+
        |
        v
  +------------------+       +--------------------------------+
  | src/classify.py  |<------| models/intent_classifier.json  |
  | tf-idf + logreg  |       +--------------------------------+
  +------------------+
        |  intent, P(correct), urgency (policy)
        v
  +------------------+       +--------------------------------+
  | src/retrieve.py  |<------| data/documentation.json        |
  | BM25 + bridge    |       | 29 articles -> 69 passages     |
  +------------------+       +--------------------------------+
        |  0..5 passages above floor 4.2
        v
  +------------------+
  | check_injection  |  on the incoming ticket text
  +------------------+
        |
        v
  +------------------+
  | src/route.py     |  R-00 .. R-05, first match wins
  +------------------+  R-01b and R-06 fire in pipeline.py
      |           |
   escalate    auto_respond
      |           |
      v           v
  +--------------------------------+
  | src/generate.py                |
  | PR-02 note     |  PR-01 draft  |
  | extractive behind both         |
  +--------------------------------+
        |
        v
  +--------------------------------+
  | src/guardrails.py  run_all()   |
  | pii | injection | grounding    |
  | commitments | confidence floor |
  +--------------------------------+
        |  blocked -> escalate, reason attached
        v
     Response ------>  +----------------------+
                       | src/logging_store.py |  5 rows per ticket
                       +----------------------+
```

Concretely, DEV-0025, a chat ticket from an enterprise customer with no subject line: their deployment reaches running and then rolls back after about a minute, and the logs mention a health check timeout. Ingest normalises it and notes there is no subject. The classifier returns `deployment_failure` at a calibrated confidence of 0.9999, and the urgency table gives high, because that intent carries a high prior and the chat floor only ever lifts a low-urgency ticket to medium. Retrieval tokenises, and the bridge in `src/text.py` puts documentation words alongside the customer's own, which is the whole reason an article titled around container health check failures is reachable from a customer's phrasing at all. That expansion exists because of one sentence in Ines Varga's interview: "Someone writes my deployment keeps dying and my article is called resolving container health check failures. There is no path between those two phrases in a keyword search." Five passages come back above the 4.2 floor, never more than two from one article, led by the symptoms chunk of DOC-DEPLOY-001 at 29.75 and the resolution chunk of the same article at 16.68.

Then `check_injection` runs on the ticket text, before routing rather than after generation, because a ticket that tries to reprogram the system is worth recording whether or not it would have been answered. Nothing matches here. Routing evaluates in order: R-00 kill switch, R-01 policy class, R-02 no grounding, R-03 high-cost class below the 0.85 bar, R-04 below the 0.62 threshold, R-05 answer. Deployment failure is not a policy class and not high-cost, the ticket is grounded, and the confidence clears the threshold, so it lands on `R-05-auto-respond` with a reason that reads "Recognised as a deployment failure with 100% confidence, and the knowledge base has an article that covers it".

No provider is configured on the default path, so the extractive generator writes the draft, and this is where the behaviour described under the provider section shows plainly. The passage that ranked top is the symptoms section, and the reply quotes the resolution section instead: chunk `DOC-DEPLOY-001#2`, carrying the retrieved passage's score of 29.75 so that the citation still points at something genuinely retrieved. The customer gets the first two numbered recovery steps and the note about local state, one citation marked [1] resolving to DOC-DEPLOY-001, and the paragraph saying a machine drafted it. All five guardrails pass, grounding reporting one citation and all of them resolving. The whole ticket takes 1.2 milliseconds. R-01b and R-06 are never reached, because there is no injection to find and the generator did not decline.

The harder version of the same ticket is worth running too. Ines's phrase on its own, "my deployment keeps dying" on chat and nothing else, still puts DOC-DEPLOY-001 at the top of the ranking, so the bridge does its job on five words. The classifier does not: it reaches 0.4422 on that much text, below the 0.62 threshold, and the ticket escalates on `R-04-below-threshold` with the retrieved articles attached. Retrieval found the answer and the system still would not send it, which is the intended shape of the thing.

If a guardrail does block, the action becomes `blocked`, a person gets the ticket with the block reason on the front of the handover note, and nothing redacted goes to the customer. That path is not exercised by either result set in `evaluation/results/`, because nothing unsafe reaches the guardrails on the default path. The place it can be watched is the fault-injection run described below.

## Why the decision log is in the persistence layer

Five decision records are written per ticket, one per stage whatever the outcome, into SQLite via `src/logging_store.py`. On the 500-ticket development run that is 2,500 rows reconciling exactly against 500 tickets.

It would have been less code to have the pipeline return a `Response` and let the caller log whatever it found interesting. I did not do that, because logging that a caller opts into is logging that a caller can skip, and the paths most likely to skip it are the error paths, precisely the ones where something will eventually go wrong unobserved. The log is a component with a schema, not a side effect of running the system, and A8 checks it by reconciling counts rather than by trusting that the writes happened.

The columns are columns, not one JSON blob, so that a support manager can answer "how many replies did we hold back for private data last week" with a query. The fields that matter most for that are `prompt_version` and `requirement_ids`: they are what lets you ask, months later, whether a behaviour was intended, rather than only whether it was possible.

## When the provider is gone

`src/provider.py` has four implementations behind one interface: OpenRouter with exponential backoff, `Retry-After` support, a 60-second circuit breaker after repeated failure and a response cache; Offline, which is unavailable on purpose; Null, which is selected automatically when no key is configured; and UnsafeDemo, a fault-injection provider reachable only through `PROVIDER=unsafe_demo`. The fourth exists because of an awkward property of the other three: on the default path the guardrails have nothing to catch, so the only way to demonstrate that they block is to hand them a draft that deserves blocking. Nothing above that module asks whether a provider is available. It asks for a completion and gets text or a `ProviderUnavailable`.

That is the evidence behind A7, so it is worth stating what the run produces rather than only that it exists:

```
PROVIDER=unsafe_demo python -m evaluation.harness \
    --input data/guardrail_probe_tickets.json \
    --output evaluation/results/guardrail_demo
```

Nine probe tickets, 45 decision records reconciling, and one response blocked. The blocked one is PROBE-UNICODE-01, a forum ticket written in full-width characters with zero-width joiners between the words and a payload appended. It classifies as `deployment_failure` at 0.93 and routes to `R-05-auto-respond`, so the draft is written, and then the fault-injection provider returns what a model having a bad day returns: the customer's own text quoted back as fact, a refund, a fix and a delivery date. Grounding fails it on three factual sentences in a paragraph carrying no citation, commitments fails it on `delivery_date`, `fix_eta` and `refund`, and the action comes out as `blocked` rather than `auto_respond`. The customer gets nothing. A CloudServe engineer gets the ticket with both block reasons at the top of the handover note. The other eight probes escalate before a draft exists, which is the point being made elsewhere in this document about why the default path blocks nothing.

The reason that is enough is that the extractive generator is a real answer path rather than a stub. It does something less obvious than quoting the passage that ranked highest. A customer's wording matches the symptoms section better than any other part of an article, and the symptoms section describes the problem they have just finished describing to us, so the best-ranked chunk is frequently the least useful one. `_best_chunk_for()` reaches across to the resolution section of the same article and quotes that instead, keeping the retrieved passage's citation and score so that A6 still resolves to something that was genuinely retrieved. It also drops the article furniture the chunker prepends: the title line and the applies-to metadata. Before that change, a rollback ticket was answered with "A defect reached production that testing did not catch Rolling back a failed release **Applies to:** Container Service, Functions." After it, the same ticket gets the four actual rollback steps. One passage per article is cited, and a second article only if it scored at least 0.6 of the top result. Both the 500-ticket and 80-ticket runs in `evaluation/results/` were produced with the provider unavailable, which is also why the timings are what they are.

Guardrails are in the deterministic layer for the same reason. A check that depends on an external provider fails open during exactly the outage it exists to protect against.

## What I would restructure

The routing rules should all live in `src/route.py`. R-01b and R-06 sit in `src/pipeline.py` because injection is checked after retrieval and the generator's refusal is only known after generation, and rather than restructure I patched the returned `RoutingDecision` in place. It works and it is tested, but "the router" is currently three files, which is the kind of thing that is obvious to me now and will not be obvious to whoever reads this next. Passing the injection result and a generator verdict into `route()` as inputs would fix it.

Decision records are appended inline in `_process`, which means adding a stage involves remembering to append one. Since A8 reconciliation depends on nobody forgetting, that dependency should be structural: each stage emits its record, and the pipeline collects them.

The guardrail blocking policy belongs to the guardrail, not to the caller. At the moment the pipeline rewrites the results for escalations so that nothing blocks on an internal note, using a list comprehension that is the ugliest thing in the repository.

Finally, `Retriever.__init__` loads the corpus and builds the index together, so a test that wants a two-article corpus has to write a JSON file first, and the shipped classifier artefact still carries the urgency model that only ADR-006's comparison uses. Neither is a bug. Both are the sort of shape that makes a component harder to reason about than the job it does.
