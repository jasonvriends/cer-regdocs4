# Regulatory asset ownership ledger

The goal is an inventory that answers, for any asset: **who holds it, who held
it before, and what document proves each step.** Obsidian could not do this —
a vault shows one static snapshot with no time dimension, no way to record that
an asset changed hands, and no way to hold two conflicting readings while a
human decides between them.

```bash
.venv/bin/python ideas/idea1/ledger.py init            # create ledger.db from schema.sql
.venv/bin/python ideas/idea1/ledger.py load 4647200    # extract and load one filing
.venv/bin/python ideas/idea1/ledger.py holdings "Foothills"
.venv/bin/python ideas/idea1/ledger.py history  "Elko Section"
.venv/bin/python ideas/idea1/ledger.py review          # what a human needs to confirm
```

## Three rules the schema enforces

**Nothing is ever updated or deleted.** A correction is a new row that
supersedes an old one (`assertion.superseded_by`). A regulatory record that can
be silently edited is not a record.

**Every claim points at immutable evidence** — document, page, docling element,
and the exact quote, hashed. If a re-extraction changes the quote, the hash
breaks and the drift is visible instead of silent.

**A name change is not an ownership change.** This is the distinction the whole
design turns on, and the easiest one to get wrong. `entity_name` carries names
with their own validity periods; `assertion` carries who held what. TransCanada
Corporation becoming TC Energy Corporation in 2019 is a row in the first and a
`corporate_event`, not a transfer — the company kept every asset. Foothills
selling a pipeline would be a row in `transfer`. Collapsing these into one
"current owner" field is how an inventory quietly loses its history.

Ownership is also **bitemporal**: `valid_from`/`valid_to` is when the fact was
true in the world, `asserted_at` is when we learned it. Both are needed, because
defending a past decision means answering "what did the record say on the day we
acted", which is a different question from "what was true".

## What loading 4647200 produces

```
document 2   evidence 106   legal_entity 7   entity_name 23
asset 90     asset_name 93  asset_location 73
assertion 2  transfer 0     corporate_event 1   review_queue 2
```

All seven companies resolved to registry entities, zero created, zero ambiguous.
Every spelling seen in the document — including the OCR damage — is attached to
the right entity, so a future filing that writes the name differently still
resolves:

```
E-FOOTHILLS-SBC  Foothills Pipe Lines (South B.C.) Ltd.
  known as: Foothills Pipe Lines (South B.C.) Ltd.; Foothills Pipe Lines Ltd.;
            Foothills Pipe Lines (South BC) Ltd.; Foothills [abbreviation];
            Foothill Pipe Lines (South BC) Ltd.;        <- OCR: missing 's'
            Foothills Pipe Lines (South B.C>) Ltd.      <- OCR: '>' for '.'
  OPERATOR pipeline Elko Section  [unreviewed, conf 0.90]
    source: CER 4647200 p425 #/texts/9134
    quote : "The Elko Section is an approximately 31.9 km long, Nominal Pipe
             Size 48 pipeline and was constructed by Foothills Pipe Lines…"
```

**Coverage is honest and low: 2 of 90 assets carry an ownership assertion, and
there are no transfers.** That is not an extraction failure — this filing is an
acid rock drainage monitoring report. It says who built the pipeline and nothing
about who owns the 83 monitoring stations, so the ledger records nothing about
them. Which leads to the main finding below.

## Entity resolution: registry first, text last

Company identity is anchored to **REGDOCS metadata and corporate registries, not
to OCR'd prose.** The regulator states the filing company of every document
authoritatively; Corporations Canada gives a corporate number and a name
history. That is the one company fact in the pipeline that does not depend on
reading a PDF correctly, and it should carry the identity.

Resolution order:

1. Normalise the mention (drop legal suffixes, parentheticals, punctuation, case).
2. Exact match on the normalised key against `entity_name`.
3. One hit → resolved; record any new spelling as an `ocr_variant` of that entity.
4. Several hits → **queue for review, do not guess.**
5. No hits → create a new entity, flag it, and queue it.

Rule 4 earned its place immediately. The first load flagged
`'TC Energy Corporation' normalises to a key held by 2 entities` — which was a
real duplicate in the registry seed, where TC Energy had been entered both as
its own entity and as the post-2019 name of TransCanada Corporation. The system
refused to pick one and a human fixed the seed. Had it silently chosen, assets
would have been split across two halves of the same company.

**A wrong merge is much more expensive than a duplicate**, because it moves
assets between owners invisibly, while a duplicate is obvious and easy to
collapse later. The resolver is biased accordingly.

## Do you run an LLM over every document?

No. Measured over the 14 extractions currently in `output/` — 150,384 text
elements across roughly 9,300 pages — **0.26% of elements contain any
ownership-transfer language at all**:

| | elements | cue hits | share |
|---|---:|---:|---:|
| 14 filings | 150,384 | 386 | 0.26% |

Running a language model over all 150,384 elements to find 386 is the wrong
shape, on cost and on reproducibility. The right shape is three stages with very
different costs:

**1. Deterministic extraction (script, every page).** Asset IDs, chainage,
coordinates, table structure. These have grammars — `ELKO-154.00-WC-US` is
parseable, and the station registry table is a grid, not prose. Cheap,
exhaustive, reproducible, and already built.

**2. Deterministic retrieval (regex, every page).** Find the candidate passages
with cue phrases: "leave to transfer", "amalgamated", "wholly owned subsidiary
of", "formerly known as", "acquired". This stage is tuned for **recall, not
precision** — it is allowed to be wrong most of the time.

**3. LLM adjudication (only the candidates).** ~400 passages per 9,300 pages,
not 150,000. At that volume a strong model is affordable per passage, and this
is where an LLM genuinely beats a regex: reading "X, a wholly owned subsidiary
of Y, acquired Z's interest in the W system effective January 2019" into
structured roles and dates.

Sampling stage 2's hits shows its precision is low — maybe one in five is about
corporate ownership, and the rest are about *land* acquisition from landowners,
which uses the same verbs. That is the intended division of labour: the regex
does recall cheaply, the LLM does precision expensively, and the two are only
affordable together in that order.

### Constraining the LLM so it cannot fabricate

This repo already rejected generative extraction once: a vision model invented
email addresses and altered a postal code while looking clean
([`docs/lessons_learned.md`](../../docs/lessons_learned.md)). That finding stands, and the design here respects it
rather than quietly reversing it. Two differences make this use safe where that
one was not:

- **The model never transcribes.** It reads text docling already extracted and
  classifies relationships in it. It is never the thing that turns pixels into
  characters, which is where the fabrication happened.
- **Every output is span-verified.** The model must return the exact supporting
  quote; the loader checks that quote appears **verbatim** in the cited element
  and rejects the extraction if it does not. A hallucinated relationship cannot
  produce a quote that matches the source, so it fails closed.

Anything that survives both still lands as `review_status='unreviewed'` and goes
in the review queue. No machine assertion is treated as fact.

## The finding that matters most for scale

**Document selection beats document processing.**

These 16 filings were chosen for *size*, to stress the extractor — and it
worked, they exposed every failure in [`docs/lessons_learned.md`](../../docs/lessons_learned.md). But they are
environmental assessments, letters of comment and books of authorities. They
contain almost no ownership content, which is why loading a 986-page filing
yields two assertions and zero transfers.

Ownership changes live in a different and much smaller document class: transfer
applications and the Commission's own orders. You do not find them by OCR'ing
everything and searching; you find them by **filtering REGDOCS metadata first**
— filing type, company, date — and only then ingesting.

That matters because docling costs ~1.8 s/page on this GPU (986 pages in
30 minutes). Ingesting REGDOCS indiscriminately is years of compute. Ingesting
the filings that record transfers is plausibly days.

The corpus metadata already captured in [`docs/test_corpus.md`](../../docs/test_corpus.md) — document id,
filing id, filed date, filing company — is the beginning of that index. Building
it out for the whole of REGDOCS is cheap, needs no OCR, and should come before
any more page processing.

## Suggested order of work

1. **Harvest the REGDOCS index** (id, filing, type, company, date, title) for
   the whole corpus. No OCR. This is the targeting layer and everything else
   depends on it.
2. **Seed the registry** from the CER regulated company list and Corporations
   Canada, so entity identity starts from an authority rather than from prose.
3. **Select the ownership-bearing document classes** from the index and ingest
   only those.
4. **Add stages 2 and 3** (cue retrieval, then span-verified LLM adjudication)
   to populate `transfer` and `corporate_event`.
5. **Work the review queue.** The ledger's value is the confirmed rows; the
   machine's job is to make human attention efficient, not to replace it.

## Status

Built and working: schema, loader, entity resolution against a registry seed,
the review queue, and the holdings/history queries. Stage 1 extraction is
[`inventory.py`](inventory.py), reused as a library — see
[inventory_prototype.md](inventory_prototype.md).

Not built: the REGDOCS index harvester, the registry import, and the LLM
adjudication stage. `transfer` and `corporate_event` are therefore nearly empty
— the schema supports chain-of-custody, but no document loaded so far records a
transfer.
