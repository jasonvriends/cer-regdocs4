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
# change a setting and re-run: the new run lands beside the old
```

Output is written per **run of settings**, not per document:

```
output/<id>/
  <id>.cer.meta.json              what the regulator says: filing, parties, facets
  runs.json                       one line per run: settings in, results out
  <run>/<id>.docling.json.gz      the document: structure, tables, provenance
  <run>/<id>.docling.meta.json    provenance, settings, per-page quality, doubts
  az<hash>/<id>.azure.json.gz     the reference extraction, merged from parts
  az<hash>/<id>.azure.meta.json   which code and analyzer version produced it
```

The script behind each run is kept under `runs/<run id>`. `cer.meta.json`
sits in the document folder rather than a run folder because nothing ran to
produce it: it is a fact about the filing, written by `tools/import_cer.py`.
The reference extraction is written by `tools/import_azure.py`.

`runs.json` is derived and safe to delete — the next ingest rebuilds it from
whatever run directories exist. Each run keeps a copy of `ingest.py`: the meta
records which code ran, and the copy records what that code *was*, which a hash
cannot if the run was made from an edited working copy.

Every setting that changes the output goes into a run signature, and its short
hash names the directory. The same settings land in the same place and are not
redone; change one and the next run lands **beside** the old rather than over
it. Comparing two parameter choices is then reading `runs.json`:

```
310a9d38  table_mode=fast       28s  suspect_cells=0
c885606d  table_mode=accurate   44s  suspect_cells=1
```

Nothing overwrites a finished run. To redo one, delete its directory — an
explicit removal rather than a flag that quietly destroys the output you wanted
to compare against.

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

### What the meta does not cover

The meta answers two questions: what produced this output, and what went well or
badly in it. What the document *contains* — its headings, tables and the
datasets inside them — is a separate pass over the finished extraction. Those
rules change on their own schedule, and correcting them should not mean
re-extracting a corpus.

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

## Adding a document

Copy the PDF into `source/`, with its metadata sidecar if the download pipeline
wrote one:

```bash
cp ../cer-regdocs2/workspace/2_download/files/4647200.{pdf,metadata.json} source/
```

`ingest.py` reads `<pdf>.metadata.json` if it is there and records the filing's
title, company, filing number and date in the meta, and checks the sidecar's
sha256 against the bytes it actually read. Without a sidecar everything still
works — the meta keeps the file hash and a REGDOCS download link derived from
the filename and verified over the network — it just has no filing identifiers.

A URL works too, and lands in `source/` under the document id:

```bash
.venv/bin/python ingest.py https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4647200
```

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
