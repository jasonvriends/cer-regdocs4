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
| `<id>.docling.json` | the document: structure, tables, provenance |
| `<id>.docling.meta.json` | settings, timing, per-page warnings, self-checks |

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

## Test document

Development and measurement used a single large filing, not committed here:

- **C38088-2 Foothills Zone 8 West Path Delivery 2023 Cond. 15 Acid Rock
  Drainage Mitigation Plan Reports Attach 1-2** — Foothills Pipe Lines (South
  BC) Ltd, filed 2026-01-30
- <https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4647200>
- 986 pages, 46.4 MB, sha256 `31a44f5b7e2435fac449842a42ec2dd56d6e98ae02dd460f506c6811b3e7e499`

PDFs and extraction output are deliberately kept out of git (`source/`,
`output*/`). Download the file above to reproduce.

## Documentation

- [docs/lessons_learned.md](docs/lessons_learned.md) — what was changed across
  five tuning runs, why, what it measured, what was tried and rejected, and
  where a commercial extractor is still ahead. Read this before changing
  defaults in `ingest.py`; several obvious-looking settings are wrong here for
  non-obvious reasons.
