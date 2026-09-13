#!/usr/bin/env python3
"""Candidate terms for the vocabulary bridge in src/text.py.

Run:  python -m scripts.build_expansions

Ines described the problem: "Someone writes my deployment keeps dying and my
article is called resolving container health check failures. There is no path
between those two phrases in a keyword search." EXPANSIONS in src/text.py is the
path. Its comment said this script regenerates the candidates, and the script
did not exist, so the provenance was a claim rather than something anyone could
re-run. This is the generator. The curation on the other side of it is still
mine and still by hand: the script proposes, it does not decide.

What it does. For every development ticket the labels name an expected article
for, it takes the words the customer used that the documentation never uses
anywhere. Those are the words with no path into the corpus. Each one is then
paired with the terms that are common to every article it points at and rare in
the corpus as a whole, which is what separates "health, check, timeout" from
"symptoms, resolution, note" - the second set appears in all twenty-nine
articles and bridges nothing. Candidates are ranked by how few articles they
point at, because a word pointing at one article is a bridge and a word pointing
at ten is a stopword nobody added to the list.

Two views are printed. The default is every candidate. --misses-only narrows to
the tickets where retrieval currently fails to put the expected article in the
top k, which is the shorter list and the one worth reading first: those are the
gaps that are costing recall today, including the three intents the fairness
audit found the non-fluent gap concentrated in.

Nothing here writes to src/text.py. The output is a JSON file and a table.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.retrieve import Retriever
from src.text import EXPANSIONS, tokenise

# A candidate has to appear in this many distinct tickets before it is worth
# looking at. Below three it is one customer's phrasing rather than a pattern,
# and the list is already long enough to read.
MIN_TICKETS = 3
# How many documentation terms to suggest per customer word. EXPANSIONS entries
# are two to four terms, so offering more than this is offering noise.
TOP_TERMS = 6
# A term present in more than this share of the corpus is house style rather
# than subject matter. At 29 articles this drops "symptoms", "resolution" and
# "applies", which is the whole point of the filter.
MAX_CORPUS_SHARE = 0.34


def article_tokens(doc: dict) -> set[str]:
    return set(tokenise(f"{doc.get('title', '')} {doc.get('category', '')} {doc.get('content', '')}"))


def heading_tokens(doc: dict) -> set[str]:
    """Title and category only.

    Ines's example is about a title: "my article is called resolving container
    health check failures". A term in the title is the term the article is
    filed under, so it is worth more as a bridge than a word that happens to be
    rare in the body.
    """
    return set(tokenise(f"{doc.get('title', '')} {doc.get('category', '').replace('_', ' ')}"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", default="data/development_tickets.json")
    parser.add_argument("--output", default="evaluation/results/expansion_candidates.json")
    parser.add_argument(
        "--misses-only",
        action="store_true",
        help="only tickets whose expected article is not retrieved in the top k today",
    )
    parser.add_argument("--limit", type=int, default=30, help="rows to print")
    args = parser.parse_args(argv)

    from src.ingest import load_tickets

    tickets = load_tickets(args.input)
    retriever = Retriever()
    vocabulary = {doc_id: article_tokens(doc) for doc_id, doc in retriever.documents.items()}
    headings = {doc_id: heading_tokens(doc) for doc_id, doc in retriever.documents.items()}
    # Every word the documentation uses anywhere. A customer word inside it is
    # already reachable and needs no bridge.
    documented = set().union(*vocabulary.values()) if vocabulary else set()

    # How many articles use each term. A term in most of them is house style.
    corpus_df: Counter[str] = Counter()
    for terms in vocabulary.values():
        corpus_df.update(terms)
    article_count = len(vocabulary) or 1

    seen_tickets: dict[str, set[str]] = defaultdict(set)
    expected_docs: dict[str, set[str]] = defaultdict(set)
    considered = 0

    for ticket in tickets:
        expected = (ticket.labels or {}).get("expected_doc_ids") or []
        expected = [d for d in expected if d in vocabulary]
        if not expected:
            continue
        if args.misses_only:
            retrieved = {p.doc_id for p in retriever.search(ticket.text)}
            if retrieved & set(expected):
                continue
        considered += 1
        words = set(tokenise(ticket.text))
        # The customer's words with no path into the corpus at all. A word the
        # documentation uses somewhere already retrieves on its own.
        for word in words - documented:
            seen_tickets[word].add(ticket.ticket_id)
            expected_docs[word].update(expected)

    rows = []
    for word, tickets_seen in seen_tickets.items():
        support = len(tickets_seen)
        if support < MIN_TICKETS:
            continue
        targets = sorted(expected_docs[word])
        # Terms every one of the articles this word points at uses, ranked by
        # how rare they are elsewhere. Common to the targets and rare in the
        # corpus is exactly the shape of a bridge term.
        shared = set.intersection(*(vocabulary[d] for d in targets))
        in_headings = set.intersection(*(headings[d] for d in targets))
        scored = [
            (0 if term in in_headings else 1, corpus_df[term], term)
            for term in shared
            if corpus_df[term] / article_count <= MAX_CORPUS_SHARE and len(term) > 2
        ]
        suggested = [term for _, _, term in sorted(scored)[:TOP_TERMS]]
        rows.append(
            {
                "customer_word": word,
                "tickets": support,
                "expected_articles": targets,
                "suggested_terms": suggested,
                "already_in_expansions": word in EXPANSIONS,
                "current_expansion": list(EXPANSIONS.get(word, ())),
            }
        )
    # Concentrated first: a word pointing at one or two articles is a bridge, a
    # word pointing at ten is a word everybody uses.
    rows.sort(key=lambda r: (len(r["expected_articles"]), -r["tickets"], r["customer_word"]))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "input": args.input,
        "tickets_considered": considered,
        "misses_only": args.misses_only,
        "minimum_tickets_per_candidate": MIN_TICKETS,
        "candidates": len(rows),
        "candidates_already_curated": sum(1 for r in rows if r["already_in_expansions"]),
        "rows": rows,
    }
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    scope = "tickets whose expected article is missed today" if args.misses_only else "labelled tickets"
    print(f"{considered} {scope}; {len(rows)} candidate word(s) seen in {MIN_TICKETS} or more of them.")
    print(f"{summary['candidates_already_curated']} of them are already in EXPANSIONS.\n")
    print(f"{'':1s}{'customer word':18s} {'tickets':>7s} {'articles':>8s}  suggested documentation terms")
    for row in rows[: args.limit]:
        marker = "*" if row["already_in_expansions"] else " "
        print(
            f"{marker}{row['customer_word']:18s} {row['tickets']:>7d} "
            f"{len(row['expected_articles']):>8d}  {', '.join(row['suggested_terms']) or '(none)'}"
        )
    print("\n* already curated into EXPANSIONS. The rest are proposals, not decisions.")
    print(f"\nwritten to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
