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
./setup.sh                                                        # create .venv, install docling
.venv/bin/python scout.py scout --from 2026-08-01 --to 2026-08-31 # find filings, record their metadata
.venv/bin/python scout.py download                                # fetch the PDFs into source/
.venv/bin/python ingest.py source/4647200.pdf                     # extract one filing
```

To extract everything in `source/`, smallest file first so results arrive
early and the long filings come last:

```bash
ls -Sr source/*.pdf | while read f; do
  .venv/bin/python ingest.py "$f"
done >> batch.log 2>&1
```

Finished documents are skipped, so the same command resumes after a crash.
Keep `batch.log`: a crash in native code (a segfault) leaves nothing in the
document's own `ingest.log`, and the batch's output is the only record of it.
`tools/status.py` lists anything that did not finish.

## Output

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
produce it: it is what REGDOCS says about the filing, with a history of what
changed, written by `scout.py`. The reference extraction is written by
`tools/import_azure.py`.

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

## Finding and fetching filings

`scout.py` finds filings on REGDOCS and keeps one record per document in
`output/<id>/<id>.cer.meta.json`. It searches by date, follows Compound
Documents and Folders to learn which filings each document belongs to, and
reads REGDOCS's five facet categories (Document Type, Application Type,
Commodity, Role, File Type). Only PDF documents get a record; HTML documents
and containers are skipped.

```bash
.venv/bin/python scout.py scout --from 2026-01-05 --to 2026-01-05 --dry-run
```

```
scouting 2026-01-05 .. 2026-01-05
base search: 30 item(s)
  container 4633939: 2 member(s), complete
  ...
facets: 5 categories, 156 values

180 request(s)
                                     unchanged: 17
              skipped: container or paper-only: 11
                        skipped: Html Document: 9
```

`--dry-run` reports what would change without writing. Without it, new
documents get a record, and existing records are compared with what REGDOCS
says now and updated where it differs. `scout.py download` then fetches every
recorded PDF that is not already in `source/`.

**A PDF already in `source/` is never downloaded again.** Only its record is
refreshed. Every change is kept, so re-tagging by REGDOCS is visible over time:

```json
"facets": { "Document Type": ["Application"], "Commodity": ["Gas"], ... },
"first_seen_at": "2026-08-07T01:33:56+00:00",
"last_seen_at":  "2026-10-01T00:00:00+00:00",
"changes": [
  { "field": "facets.Document Type",
    "old": ["Supplemental Information"], "new": ["Application"],
    "observed_at": "2026-10-01T00:00:00+00:00" }
]
```

A scrape that partly fails cannot erase anything: a field is only replaced by
a non-empty value, a facet is replaced outright only when every search for it
finished, and a filing membership is dropped only when that filing was read in
full without the document in it.

Dates are checked before any request. A day past the end of its month is
clamped (`--to 2026-09-31` runs to `2026-09-30`, and says so); anything else
malformed is refused. This matters more than it looks: **REGDOCS does not
reject an impossible date, it drops it.** An end date of `2026-09-31` silently
becomes "until today", and a start date of `2026-02-30` becomes "since 2002",
about 550,000 items. As a second guard, every result row's date is checked
against the range asked for, and the scout stops before writing anything if
REGDOCS returns a row outside it.

Requests are paced at one every 2-4 seconds, one at a time. The one-day scout
above took 180 requests, about ten minutes, most of them the 156 facet
searches. Longer ranges need more pages per search and more containers, so
they cost more than the day count alone suggests.

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

## Test corpus

The working corpus is whatever `scout.py` has found: 8,210 PDF filings as of
September 2026. Development and most of the measurement in the lessons learned
used a subset of sixteen filings, 9,294 pages, spanning born-digital reports,
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

Scout the date it was filed and download it:

```bash
.venv/bin/python scout.py scout --from 2026-01-30 --to 2026-01-30
.venv/bin/python scout.py download
```

That gives it a record as well as a file. A single URL also works, straight
into the extractor, landing in `source/` under the document id -- but without a
record, so the extraction has no filing number, company or facets beside it:

```bash
.venv/bin/python ingest.py https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4647200
```

## Tools

| | |
|---|---|
| `tools/status.py` | which documents did not finish, and where each one stopped |
| `tools/triage.py` | sorts pages into review tiers from the stored doubts; a policy, versioned separately from extraction |
| `tools/coverage.py` | page coverage against the reference extraction |
| `tools/import_azure.py` | lands the reference extraction beside ours, merged from its parts |

## Documentation

- [docs/test_corpus.md](docs/test_corpus.md) — the sixteen filings used for
  development and measurement, with download links.
- [docs/filing_guidance.md](docs/filing_guidance.md) — what filers could do
  differently, with the measured rate of each defect; evidence for the filing
  manual.
- [runs/README.md](runs/README.md) — what the run directories hold and why the
  code behind each run is kept.
- [docs/lessons_learned.md](docs/lessons_learned.md) — what was changed across
  five tuning runs, why, what it measured, what was tried and rejected, and
  where a commercial extractor is still ahead. Read this before changing
  defaults in `ingest.py`; several obvious-looking settings are wrong here for
  non-obvious reasons.
