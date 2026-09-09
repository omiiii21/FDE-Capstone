# ADR-002: Lexical BM25 as the default retrieval backend

## Status

Accepted, 27 August 2026.

## Context

Twenty-nine knowledge base articles, written by one technical writer in a consistent house style, chunked to 69 passages. That is a small corpus with a regular internal structure — symptoms, common causes, numbered resolution steps, notes — and the structure matters more than the size. Splitting on a character count cuts through numbered resolution sequences and produces passages that score well and read as half a thought, so `chunk_article` in `src/retrieve.py` splits on the article's own headings and folds sections under 200 characters into the one that follows.

The obvious alternative is dense retrieval: embed the chunks, embed the query, rank by cosine similarity. I built it. It sits behind `RETRIEVAL_BACKEND=dense` using Chroma and all-MiniLM-L6-v2, and `requirements-dense.txt` installs what it needs.

Discovery also told me what kind of problem this is, and it is vocabulary rather than semantics in the abstract. Ines Varga put it plainly: "Someone writes my deployment keeps dying and my article is called resolving container health check failures. There is no path between those two phrases in a keyword search." That is the diagnosis for why nobody uses the existing internal search, and it does not require an embedding model to fix. `src/text.py` holds a curated bridge that maps customer words onto documentation words and applies it additively, so an expansion can only ever add a way of matching and never remove one.

## Decision

Lexical BM25 (k1=1.4, b=0.72) over section-aware chunks, with a title boost of 2.6 and a category boost of 1.4, plus the vocabulary bridge on the query. Dense retrieval stays in the repository as an optional backend and falls back silently to lexical when the packages are absent.

I ran both over the same 500 development tickets. `scripts/compare_retrieval.py` is the measurement and `evaluation/results/retrieval_backend_comparison.json` is its output. Each backend was given the relevance floor that maximised its own recall, because the two score on different scales and a shared floor would have measured the floor rather than the ranking.

| | recall@5 | precision@1 | fluent | non-fluent | fluency gap | p95 per query |
| --- | --- | --- | --- | --- | --- | --- |
| lexical | 96.4% | 87.7% | 97.8% | 92.0% | 5.8 pts | 0.1 ms |
| dense | 96.1% | 88.8% | 97.0% | 93.1% | 3.9 pts | 3.6 ms |

Dense is 0.3 points worse on recall, 1.1 points better on precision@1, and about thirty-six times slower per query. On this corpus neither is meaningfully better at finding the right article, which is roughly what I expected: 29 articles of dense technical vocabulary is the case keyword matching is good at, and the vocabulary bridge already handles the paraphrase problem an embedding model would otherwise solve.

What decided it is that `requirements-dense.txt` adds roughly 800MB of dependencies, including compiled wheels and a model download, to a system whose first acceptance criterion is that it installs from a clean checkout on a machine I do not own and runs unattended. Paying that for minus 0.3 points of recall is not a trade I would defend.

## Consequences

Measured on the 500 development tickets: recall@5 of 96.4%, precision@1 of 87.7%, a mean of 4.81 passages returned, at a relevance floor of 4.2. The full sweep is in `evaluation/results/retrieval_sweep.json`.

The floor is where the honest caveat lives. I chose it hoping it would do two jobs — keep recall high and reject the tickets the corpus cannot answer — and it only does the first. Median top-1 score is 22.8 for answerable tickets and 20.9 for unanswerable ones; at 4.2, 95.1% of unanswerable tickets still get a passage back. No floor that preserves recall rejects them. The consequence for the architecture is that "can we ground this" is not decided by the retrieval score, and R-02 in `src/route.py` fires only on the genuinely empty case.

The fairness audit found the segment where this backend is weakest, and it is the one real argument against the decision above. Retrieval recall is 92.0% on tickets marked non-fluent against 97.8% on fluent ones, a 5.8 point gap. Dense retrieval narrows that to 3.9 points. So the backend I did not choose is measurably fairer on the axis the governance framework specifically warns about, and I am choosing 1.9 points of fairness against 800MB of dependencies and the risk to the gate.

I am not comfortable with that trade and I would not make it permanently. The matched-pair analysis in `scripts/fairness_audit.py` narrows the problem usefully: 24 of the 29 comparable (intent, expected-document) groups show no gap at all, and almost all of the damage is in three — deployment_failure, onboarding and database_issue. Those are three sets of expansion terms in `src/text.py`, not an architecture change, and that is the cheaper fix to try first. The vocabulary bridge absorbs most of the downstream effect already: the verified resolution spread across the two groups is 1.94 points.

## What would change my mind

Run both backends over the same 500 tickets and compare. If dense beats 96.4% recall@5 by more than three points, or closes the non-fluent gap below two points without losing recall elsewhere, the 800MB is worth paying and the default should flip.

The corpus size would also do it. BM25 with hand-curated expansions works because 29 articles can be read by one person; past a few hundred articles, or the moment CloudServe publish documentation in a second language, curation stops scaling and the embedding model earns its dependencies.
