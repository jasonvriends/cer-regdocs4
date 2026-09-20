# cer-regdocs4

A tailored document extractor for Canada Energy Regulator (CER) REGDOCS
filings, built for **accuracy above all else**.

These are regulatory documents: water-quality results, laboratory certificates,
chain-of-custody forms, survey drawings. A value like `<0.010` is a reported
detection limit, and a single misread character can change it by an order of
magnitude. The pipeline is therefore built on two principles:

1. **Never silently lose or invent content.** Everything docling reports as
   lost or degraded is recorded per page, with the page number and the scale of
   the loss, so odd output can be traced back to a cause.
2. **Prefer a loud failure to a plausible one.** Generative (vision-model)
   extraction was tested and rejected: it fabricated email addresses and
   altered a postal code while looking entirely clean. See the lessons learned.

It is not a general-purpose PDF converter. Its defaults are tuned against real
CER filings and are documented where they are non-obvious.

## Usage

```bash
./setup.sh                              # create .venv, install docling
.venv/bin/python ingest.py <pdf-or-url> # ingest one filing
.venv/bin/python ingest.py <pdf> --force  # re-ingest after changing settings
```

Output lands in `output/<id>/`:

| file | contents |
|------|----------|
| `<id>.docling.json.gz` | the document: structure, tables, provenance |
| `<id>.docling.meta.json` | provenance, settings, timing, per-page quality and doubts |
| `<id>.docling.index.json` | headings and tables, to query a corpus without opening documents |

Markdown is a lossy projection and is not written; export it from the JSON when
needed (`doc.export_to_markdown()`).

## What it does per page

**Every page is converted several ways and the best result kept.** The variants
are declared in one list (`PAGE_VARIANTS`) — the page as it comes, the page
rendered so OCR is the only source, a smaller OCR model, a higher render
scale — and each result is scored on characters produced weighted by the share
that are letters or digits. That separates real text from mojibake without
anything to compare against.

This exists because fixed settings kept being right for one filing and wrong
for the next. Thresholds now only decide which variant is *tried first*; they
no longer decide the outcome, so a threshold tuned on the wrong corpus costs
time rather than content.

The selector was validated against an independent extraction on 47 page
comparisons and picked the closer output every time. With the input detection
disabled entirely, both of this pipeline's historical failures self-healed.

Also per page:

- **Both TableFormer modes are run when a table loses cells**, keeping whichever
  discards fewer — neither mode wins everywhere.
- **Conversion is one page at a time**, so every warning names an exact page and
  an interrupted run resumes from the last finished one.
- **A failed page is retried once** before the run is abandoned.

Adding a new strategy means adding a line to `PAGE_VARIANTS`. Removing variants
trades accuracy for speed; the list is the knob.

## What it records about its own output

`<id>.docling.meta.json` carries, beyond the usual provenance:

- `suspect_cells` — table cells that look misread judged against their own
  column (a one-character letter/digit substitution against an otherwise
  identical sibling). This is what catches `<3hT` being read as `<31T`.
- `table_losses` / `severe_table_losses` — dropped cells with drop ratios, so a
  99% wipeout does not read like a 1% trim
- `structure_disagreements` — pages where the two table modes disagreed about
  the grid itself
- `low_confidence_pages`, `confidence_mean` — docling's own per-page scores
- `rasterized_page_list`, `retried_pages`, `table_mode_overrides`
- `run_signature` — hash of `ingest.py` plus the settings that change output; a
  re-run under different settings stops rather than silently overwriting
- `pages` — **one row per page, for every page**: characters extracted, how
  character-like they were, which variant won, and how far the variants
  disagreed. Several ways of reading a page landing in the same place is the
  closest thing to a confidence measure available without a second extractor.
- `doubts` / `reprocess` — pages grouped by the *kind* of doubt
  (`thin_for_this_document`, `table_mostly_dropped`, `table_grid_disputed`,
  `almost_no_text`, …) and the flat list. This is the handle for a later pass:
  when a new model or docling release fixes one of these, the affected pages
  can be selected across the corpus and re-run without re-converting everything

### The index

`<id>.docling.index.json` lists the document's headings, one row per table —
page, dimensions, column names, caption, and how many body cells are numbers —
and one row per picture with its caption. The numeric count separates a dataset
from a table used for layout: water-quality results in this corpus run 66–72%
numeric, an address block runs 0%. So a table with a `Detection Limit` column is
findable across a corpus without opening a single 250 MB document.

Conversion runs one page at a time, so **a table continued across pages arrives
as one table per page**. A consumer stitching a dataset back together matches
identical columns on consecutive pages; the index flags this with
`tables_are_per_page` rather than pretending otherwise.

It carries the `run_signature` it was built from. An index derived later, from
whatever document happens to be on disk, can silently describe a different
extraction — these outputs changed between runs while the pipeline was tuned.

What a document *is* — a monitoring report, an order — is deliberately absent.
Those rules change, and a judgement written at extraction time can only be
corrected by extracting again.

**An empty warning list means nothing was reported, not that nothing was lost.**
The worst failure found so far — 315 pages of mojibake — was completely silent.

## Accuracy

Measured against an independent extraction of the same filings.

Per-page variant selection, on the two documents where it has been measured
end to end:

| document | before | after |
|---|---|---|
| 4664850 (481 pages) | 97.8% mean | **98.6%** mean, 421 of 481 pages ≥95% |
| 4710294 (424 pages, scanned) | 13.2% mean, 368 pages <50% | **96.0%** mean, 11 pages <50% |

On the tuning document (4647200, 972 comparable pages), before variant
selection:

| | |
|---|---|
| mean character coverage | 99.0% (median 100.0%) |
| pages ≥95% | 895 of 972 |
| pages <50% | **0** |
| numeric tokens in tables agreeing | **96.9%** |
| detection limits (`<x`) found | **19,831 vs 19,831** |

Coverage counts characters, so it weighs a missing "the" the same as a wrong
digit. Numeric agreement is the more meaningful figure for this corpus.

## Asset ownership ledger

The extraction feeds a regulatory asset inventory: an append-only, bitemporal
record of **who holds which asset, who held it before, and the document that
proves each step**.

```bash
.venv/bin/python ideas/idea1/ledger.py init
.venv/bin/python ideas/idea1/ledger.py load 4647200
.venv/bin/python ideas/idea1/ledger.py holdings "Foothills"
.venv/bin/python ideas/idea1/ledger.py review    # what a human still needs to confirm
```

Nothing is ever updated or deleted — corrections supersede. Every row points at
immutable evidence (document, page, docling element, exact quote, hashed). A
name change is recorded separately from an ownership change, because
TransCanada Corporation becoming TC Energy Corporation kept every asset, while
a sale does not.

Company identity is anchored to REGDOCS metadata and corporate registries, not
to OCR'd prose — the regulator states the filing company of every document, and
that is the one company fact that does not depend on reading a PDF correctly.
Every spelling seen, including OCR damage, is attached to the resolved entity.
Ambiguous matches are queued for a human rather than guessed, because a wrong
merge moves assets between owners invisibly while a duplicate is obvious.

This is built on top of the extractor and lives in
[ideas/idea1/](ideas/idea1/), which holds everything specific to it:
`inventory.py` (extraction), `schema.sql` (the ledger), `ledger.py` (loader and
queries) and the design notes. Nothing in it changes `ingest.py`.

See [ideas/idea1/ledger.md](ideas/idea1/ledger.md) for the schema, the
entity-resolution rules, and the corpus strategy — including the measurement
that only **0.26%** of extracted text contains any ownership language, which is
why an LLM belongs on retrieved candidate passages rather than on every page.

## Test corpus

Sixteen CER REGDOCS filings, 9,294 pages, spanning born-digital reports,
scanned filings, French-language documents, drawing sets and laboratory
certificate bundles. The PDFs are not committed; every document is listed with
its REGDOCS download link in [docs/test_corpus.md](docs/test_corpus.md).

The two that shaped the pipeline:

- [4647200](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4647200) — 986
  pages; 315 of them carry a text layer of unmapped glyph codes. Everything was
  originally tuned against this one.
- [4710294](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4710294) — 463
  pages, scanned, 399 with no text layer at all. It scored 13.2% under settings
  tuned on 4647200, and is why extraction is now chosen per page by result.

PDFs and extraction output are deliberately kept out of git (`source/`,
`output*/`).

## Documentation

- [docs/test_corpus.md](docs/test_corpus.md) — the sixteen filings used for
  development and measurement, with download links.
- [ideas/idea1/](ideas/idea1/) — asset ownership ledger: schema, entity
  resolution, extraction stage, and how to scale across the corpus.
- [docs/lessons_learned.md](docs/lessons_learned.md) — what was changed across
  five tuning runs, why, what it measured, what was tried and rejected, and
  where a commercial extractor is still ahead. Read this before changing
  defaults in `ingest.py`; several obvious-looking settings are wrong here for
  non-obvious reasons.
