# Lessons learned: getting docling to Azure-level accuracy

Working notes from tuning `ingest.py` against CER REGDOCS filings. Runs 1-6
used a single 986-page document,
[4647200](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4647200)
(*Foothills Zone 8 West Path Delivery 2023 — Condition 15 Acid Rock Drainage
Mitigation Plan Reports*, 46.4 MB); §6 onwards covers fifteen more. All sixteen
are listed with download links in [test_corpus.md](test_corpus.md).

The goal is to replace Azure Content Understanding with a local docling
pipeline. Azure is used here only as a yardstick during the learning phase, not
as a runtime dependency. Everything the pipeline does to judge its own output
works without it.

---

## 1. The short version

| run | change | mean coverage | median | pages ≥95% | pages <50% | time |
|-----|--------|--------|--------|-----|-----|------|
| 1 | trust the PDF text layer (as originally written) | 70.2% | 99.9% | 640 | **316** | 2071s |
| 2 | rasterize broken pages, `FULL_PAGE` OCR everywhere | 96.9% | 99.9% | 781 | 7 | 2093s |
| 3 | pick OCR mode per page | 98.7% | 99.9% | 884 | 2 | 2119s |
| 4 | TableFormer `FAST` instead of `ACCURATE` | 98.8% | 99.9% | 882 | **0** | 1574s |
| 5 | run both table modes, keep the better | 98.8% | **100.0%** | 885 | **0** | 1921s |
| 6 | PP-OCRv6 `medium` models instead of `small` | **99.0%** | **100.0%** | **895** | **0** | 1781s |

Coverage = share of the reference extraction's characters (normalised to
`[0-9a-z]`) that also appear in docling's output for the same page. 972 of 986
pages carry enough text to compare.

Cells discarded by the table matcher fell **783 → 24** across the same runs.

Run 6 swapped RapidOCR's PP-OCRv6 `small` detection and recognition models for
`medium` — same family, one size up, no slower (1781s vs 1921s). 42 pages
improved by more than 2 points, 21 slipped, and `ocr_empty` warnings halved
(12 → 6).

**The single most valuable finding:** the 316 badly-broken pages in run 1
produced **no warnings at all**. docling thought it had succeeded. They were
found only by comparing against a second extraction.

---

## 2. Root causes, in the order they were found

### 2.1 The PDF text layer cannot be trusted (315 of 986 pages)

`ingest.py` originally used `OcrMode.PDF_AWARE_LAYOUT_REGIONS`, which runs OCR
only where there is no text layer, so born-digital pages keep their exact text
and cost nothing. That is the right instinct and it was wrong here.

315 pages carry a text layer that is raw glyph codes — a font with no
`ToUnicode` map. Page 662's text layer literally begins:

```
'\x01\x04 \x05 \n\x06 \x08\t \n\x0b\r\n%\x08\x0e@\x078\x0e!\x04\x0e ...'
```

docling faithfully published that as text: `%  @ 8 !`, `&&*  & 2# *0`. The page
itself renders perfectly — a clean analytical-methods table.

Detection is cheap and needs no reference: measure the share of control
characters in each page's text layer. A threshold of 0.2 matched **314 of the
316** pages that turned out to be badly broken.

The fix is to destroy the bad text layer — render those pages to images so OCR
is the only source. See §3.1 for why no OCR setting alone can do this.

### 2.2 TableFormer reads the text layer directly, bypassing OCR

This is the key structural fact about docling, and it cost several wrong turns.

`table_structure_model_v2.py:510` calls `page._backend.get_text_in_rect(bbox)`.
Table cell text comes **straight from the PDF backend**, not from the OCR
pipeline. `OcrMode.FULL_PAGE` does discard PDF cells
(`base_ocr_model.py:458`), but that filtering never reaches table text.

Consequence: on a page with a corrupt text layer, every table cell is mojibake
*no matter which `OcrMode` is set*. Measured on page 662: `FULL_PAGE` lifted the
body text but left the table at 5.6% of the reference. Rasterizing the same page
took it to **91.4%**.

Tried and ruled out before finding this: `do_cell_matching=False` (no change),
`generate_parsed_pages=True` (no change), `images_scale=4.0` (no change).

### 2.3 `FULL_PAGE` OCR is harmful on pages that are fine

Run 2 applied `FULL_PAGE` to every page. That fixed the mojibake and quietly
damaged good pages: it throws away exact PDF text and re-reads it with OCR,
which struggles on small print.

| page | `FULL_PAGE` | `PDF_AWARE_LAYOUT_REGIONS` |
|------|-----------|---------------------------|
| 89 | 16.4% | **100.0%** |
| 290 | 16.3% | **100.0%** |
| 315 | 23.3% | **100.0%** |
| 379 | 32.4% | **100.0%** |
| 41 | 31.2% | **98.1%** |

Page 89 is a pristine page of small blue type with a perfect text layer, which
docling rendered as `0854145 CC: LGO0M`.

The fix is per-page mode selection: rasterized pages get `FULL_PAGE`, everything
else keeps `PDF_AWARE_LAYOUT_REGIONS`. This is only possible because conversion
runs one page at a time (§4.1).

### 2.4 TableFormer `ACCURATE` is not more accurate here

`ACCURATE` collapsed large forms to a degenerate grid and discarded everything
that did not fit:

| page | `ACCURATE` | `FAST` | reference shape |
|------|-----------|--------|-----------------|
| 376 | 1x1, 8.0% | 36x12, **90.3%** | 3 tables |
| 833 | 1x2, 26.5% | 25x10, **100%** | 25x10 (exact match) |
| 377 | 1x1, 96.2% | 35x13, **100%** | — |

On ten table-heavy pages that were already correct the two modes scored
identically (100.0%), and `FAST` is about twice as quick.

But `FAST` is not strictly better. It trims small two-column tables that
`ACCURATE` keeps whole — pages 538, 612 and 821 went 100% → ~94%, losing ~20%
of their cells.

### 2.5 Neither table mode wins everywhere, so run both

Run 5 runs `FAST` first and, **only when it reports dropped cells**, converts
the page again with `ACCURATE` and keeps the better result. "Better" is fewer
dropped cells, tie-broken on more table text.

The rule was validated against the reference on nine pages before being wired
in: **9/9 agreement** with whichever output was actually closer.

Because the second conversion is conditional it fired on only 27 of 986 pages,
costing ~6 minutes rather than doubling the run. Result: pages 538/612/821 back
to 100%, and no regressions anywhere.

---

## 3. Things that were tried and rejected

### 3.1 A vision model for OCR (DeepSeek-OCR 3B via ollama)

Tested directly on page 376, the worst page in the document. Through docling it
produced nothing usable (`<!-- image -->`). Called directly, its output opened
with ~75 lines of degenerate repetition, and where it did produce the table it
**fabricated data**:

| field | reference | DeepSeek |
|-------|-----------|----------|
| Company | BGC Engineering Inc. | BGC En**q**ineering Inc. |
| Street | **980** Howe Street | **680** Howe Street |
| Postal code | V6Z **0C8** | V5Z **OCB** |
| Email 1 | **sbrackmore**@bgcengineering.ca | **storage**@bgcengineering.ca |
| Email 2 | **mpbrien**@bgcengineering.ca | **motherboard**@bgcengineering.ca |
| Email 3 | *(empty)* | **mailbox**@bgcengineering.ca |

It did not misread those addresses — it invented fluent English substitutes and
added a recipient that does not exist.

This is the decisive argument against VLM-based extraction for this corpus. The
filing is full of values like `<0.010` (laboratory detection limits). A dropped
cell is loud and recorded; a plausible fabricated number is neither. The same
reasoning rules out granite-docling, Gemma and similar without a grounding
check that rejects any output token absent from the page's OCR text.

### 3.2 Rasterizing pages with a stub text layer — REVERSED, see §6.1

*This decision was wrong and has been undone. It is kept here because the
reasoning looked sound and the measurement was real.*

14 pages of the tuning document carry a 12-character text layer for a page
holding ~1,000 words — scans with a stamp. Rasterizing them was a wash (84.8%
vs 85.5% over six samples) and made page 376 worse, so the rule was removed.

On a genuinely scanned filing the same rule is the difference between 13% and
99% coverage. See §6.1.

### 3.3 Bigger OCR models as a fix for misread characters

The `<3hT` → `<31T` error (§5.1) was *not* a model-size problem. `tiny`, `small`
and `medium` all read the isolated cell correctly at every render scale from
1.5x to 8x, and all three produced the same wrong answer in full-page context.
The error comes from text-line detection on the whole page, not recognition.

Corollary: **running two OCR model sizes and comparing would not have caught
it.** That idea was abandoned on this evidence.

Larger models are still worth using for a different reason — see §6.1.

### 3.4 Cross-extraction agreement as a runtime check

Comparing docling against Azure per page is the strongest quality signal
available, but it defeats the purpose: the goal is to stop paying for Azure.
Kept as a learning-phase tool only. Everything shipped in the pipeline judges
the output on its own terms.

### 3.5 180° rotation in the figure pass

Worth +0.4 to +1.7 points against +2 to +4 for 90°/270°. Dropped.

---

## 4. Design decisions worth keeping

### 4.1 One page per conversion pass

`CHUNK_PAGES = 1`. Benchmarked against 25-page passes on two real ranges:
**0.95–0.98x the wall time** — marginally faster, no batching penalty.

It buys three things:
- every warning names an exact page instead of a 25-page range
- OCR mode and rasterization can be chosen per page (§2.3)
- an interrupted run loses at most one page of work

### 4.2 Mixed staging, not blanket rasterization

Each page is staged as either a rendered image (broken text layer) or an
untouched copy (good text layer). Blanket rasterization also works but costs
~0.3% on pages that were already perfect (100% → 99.7% over six samples).
Selective staging costs nothing.

Rasterizing has one real casualty: the staged PDF carries no outline, so
`heading_hierarchy_options.use_bookmarks` is dead. That is acceptable here — 39
of this filing's 61 bookmarks are `Slide Number N`, and in the one run where
bookmarks were live they changed nothing measurable (480 level-1 headings, 17
level-2, all of the latter numbered and therefore recoverable from
`use_numbering`). The two attachment boundaries are preserved separately in
`meta.outline`.

### 4.3 Refuse to re-run across a settings change

Every meaningful setting is a module constant, so two runs can differ while the
`settings` block looks identical. `run_signature` records the script's sha256
plus chunk size, raster scale, threshold and table mode. On a mismatch the run
stops (exit 2) and prints what changed; `--force` overrides.

Partial chunks from a different signature are discarded rather than reused —
otherwise a run interrupted, edited and resumed would silently merge pages built
two different ways.

---

## 5. What the pipeline now checks about itself

All of this works without a reference extraction.

### 5.1 Suspect cells (`suspect_cells` in the meta)

A column of lab results is highly repetitive, so a value that differs from a
sibling in the same column by **one letter-to-digit substitution** is suspect on
its own terms.

The motivating case, page 673: docling read `<31T` where the page says `<3hT`.
A naive parser reads that as a detection limit of **31 instead of 3** — an
order-of-magnitude error in a regulatory water-quality result, from one
character, with no warning attached.

Rules that did *not* work: column-frequency outliers and magnitude outliers
produced 10,697 flags and **missed this cell entirely** (its column contained
the value only twice, so there was no "norm"). Digit-vs-digit differences are
deliberately ignored — those are usually two genuinely different measurements.

Current output: **782 flags over 986 pages** (~0.8/page), 501 letter/digit
confusions and 281 stray non-numeric cells in numeric columns.

### 5.2 Structure disagreement (`structure_disagreements`)

When both table modes run on a page, their table shapes are compared. Agreement
is weak evidence the grid is right; disagreement means at least one of them
misread it — something no cell-level check can detect. Example: page 538, `fast`
9x2 vs `accurate` 9x3.

### 5.3 Confidence, warnings and provenance

- `confidence_mean` / `low_confidence_pages` — docling's own parse/layout/table/OCR
  scores per page, which the script previously discarded
- `warning_counts`, `table_losses`, `severe_table_losses` — with drop ratios, so a
  99% wipeout does not read like a 1% trim
- `table_mode_overrides` — pages where the fallback changed the answer, recorded
  because a silently repaired page is still worth knowing about
- `rasterized_page_list`, `run_signature`, `components`, `ocr_models` — enough to
  tell why two runs of the same document differ

**`ocr_empty` is benign.** All 12 occurrences are on pages scoring 92.9–100%.
It fires when RapidOCR is handed a region with no text. It is not a loss signal.

### 5.4 The standing caveat

An empty warning list means docling reported nothing, **not** that nothing was
lost. The 315-page mojibake failure was completely silent. Every check in §5 was
built because of that, and none of them proves the absence of the next silent
failure.

---

## 6. What generalising to 15 more filings found

Runs 1-6 tuned everything against one document. Fifteen further filings
(437-799 pages, 8,200 pages in total) were then ingested with the run-6
settings and compared against the same reference extractor.

**Fourteen of sixteen documents came out at 97.8-100.0% mean coverage**, most
at 100.0% median with zero pages below 50%. Several needed no rasterization at
all. The pipeline generalised.

Two did not, and one of them failed badly:

| doc | pages | mean | pages <50% | rasterized |
|-----|------:|-----:|-----------:|-----------:|
| 4692624 | 446 | 91.6% | 3 | 0 |
| **4710294** | 424 | **13.2%** | **368** | **4** |

### 6.1 The bug: a page with no text layer was left alone

`unmapped_pages()` skipped any page whose text layer was empty, on the
reasoning that OCR would handle it:

```python
if not text:
    continue  # no text layer at all: OCR already handles these
```

4710294 is a scanned filing: **399 of its 463 pages have a completely empty
text layer**. Left unrasterized they go to pdf-aware OCR, which produced
*nothing*. Three sampled pages scored **0.0%** against the reference, and
**100%, 99.5% and 88.0%** once rasterized.

The rule that would have caught this — rasterize a page whose text layer is
shorter than 100 characters — **had been tried and removed in §3.2**, because
on the tuning document it looked like a wash (84.8% vs 85.5% over six pages).
That document had 14 such pages. This one has 399.

The measurement was not wrong; the sample was. A change that is neutral on the
document you are holding can be the difference between 13% and 99% on the next
one. `MIN_TEXT_CHARS = 100` is now in place, and §3.2 is superseded.

### 6.2 What this says about the rest of the tuning

Every default here was chosen against one filing: the 0.2 control-character
threshold, `FAST`-first, the validator's rules, `medium` OCR models. One of
them turned out to be catastrophically wrong on the second document type
encountered. The others have not yet been tested that way — 4710294 was a
scanned filing, and the corpus will contain other shapes that 4647200 does not
represent.

Treat the numbers in §1 as "this pipeline is good on filings like 4647200",
not as a general accuracy claim.

## 7. Choosing per page instead of configuring per corpus

§6.1 was the second time a fixed setting, correct on the document in front of
us, was wrong on the next one. The fix is not a better threshold. It is to stop
letting thresholds decide the outcome.

### 7.1 The principle

Every decision that was wrong had the same shape: **classify the input, then
trust the classification**. Every decision that held had the opposite shape:
**produce a result, measure it, keep the better one**. The table-mode fallback
(§2.5) was built that way and has never mispicked.

So each page is now converted under several variants and the best output kept.
The variants are declared in one list, `PAGE_VARIANTS`:

| variant | what it changes |
|---------|-----------------|
| `as-is` | the page's own text layer, when it is sound |
| `raster` | rendered so OCR is the only source |
| `raster-sml` | the smaller OCR model |
| `raster-hi` | a higher render scale |

Adding a strategy is adding a line. The selector decides per page, so a setting
that helps one filing and hurts another no longer needs a global answer.

### 7.2 The score, and why it is that score

Each result is scored as **characters produced, weighted by the share of them
that are letters or digits**.

The first version of this scored the share of characters that were *not
printable*, and it missed mojibake entirely: `%  @ 8 ! C *&00 *` is ordinary
punctuation. Letters-and-digits separates cleanly:

| page | alnum share |
|------|------------:|
| mojibake | 0.11 |
| recovered text | 0.81 |
| dense numeric table | 0.68 |

### 7.3 Validation

A selector that picks the wrong variant is worse than no selector. It was
checked against the independent extraction on **47 page comparisons** spanning
two documents and four dimensions -- staging, render scale, OCR model size, and
a six-way sweep on known-weak pages. **It picked the closer output every
time**, including between variants that both looked reasonable:

- a page scoring 77.6% as-is and 84.1% rasterized, where neither looked broken
- a page at 94.7% that reached 98.1% at a higher render scale
- three weak pages where the best of six variants was chosen with zero loss

With the input detector disabled entirely, both historical failures self-healed:
mojibake 991 -> 3,022 characters, and two scanned pages 0 -> 1,588 and 1,052.

### 7.4 Argmax alone is harmful; the margin matters

Taking the highest score outright makes clean pages slightly worse. Four
variants reading a sound page correctly score within a fraction of a percent of
each other, and at that resolution the score is noise. On one page argmax swapped
the page's exact embedded text (3,483 characters) for an OCR pass that scored
0.4% higher on 3,316 characters, losing accuracy for nothing.

So the first variant is an incumbent and another takes over only by a clear
margin. Calibrated over 24 pages from three filings:

| margin | mean coverage | switches |
|--------|--------------:|---------:|
| never switch | 97.99% | 0 |
| 0% (pure argmax) | 98.52% | 6 |
| **1-3%** | **98.55%** | 2 |
| 5% | 98.22% | 1 |

`SWITCH_MARGIN = 0.02`. Near-ties keep the text layer; rescues still happen,
because a rescue is not a near-tie -- the mojibake page scored 114 against
2,472.

### 7.5 What each variant is worth

One filing (4664850, 481 comparable pages), same pages, variants added in turn:

| variants | mean | pages >=95% | time |
|----------|-----:|------------:|-----:|
| 1 (detector's choice only) | 97.8% | 405 | ~400s |
| 2 (+ the other staging) | 98.2% | 410 | 843s |
| **4 (+ smaller model, higher scale)** | **98.6%** | **421** | 1603s |

Each doubling of cost bought about 0.4 points. Which variant won, per page:
`as-is` 410, `raster` 38, `raster-sml` 23, `raster-hi` 11 -- so the two extra
variants decided 7% of pages.

That is the trade to revisit for a very large corpus: dropping to two variants
halves the runtime and costs ~0.4 points.

### 7.6 The document that failed, re-run

[4710294](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4710294) is the
scanned filing that scored 13.2% in §6, with 368 of 424 pages
below half the reference. Under per-page variant selection, with no setting
specific to it:

| | before | after |
|---|------:|------:|
| mean coverage | 13.2% | **96.0%** |
| median | 0.0% | **99.8%** |
| pages >=95% | 55 | **376** |
| pages <50% | **368** | **11** |

Variant wins: `raster` 350, `as-is` 56, `raster-hi` 33, `raster-sml` 24. The
pipeline worked out for itself that this document needs rendering, page by
page, without being told.

Ten of the eleven remaining weak pages are drawings with no table on them --
the known gap where the layout stage treats a map as one picture and never
reads inside it (§8). That is a characterised limitation, not an unknown.

### 7.7 What it costs, and what it does not fix

One conversion per variant per page. The thresholds still order the attempts,
so the likely winner goes first and the logs stay readable, but they no longer
decide anything.

This catches gross failure and plausible-but-worse output. It does **not**
catch a confident wrong answer: `<3hT` read as `<31T` scores perfectly. That
class needs the column validator in §5.1, and ultimately sampling with human
review. At corpus scale, assume a residual silent-error rate and design
downstream for it.

## 8. The pipeline knew less about itself than it was worth

Measured on 4692624, comparing what the meta flagged against what was actually
weak:

| | |
|---|---|
| pages below 95% of the reference | 67 |
| **weak pages the meta flagged** | **35 of 67** |
| weak pages it said nothing about | **32** |
| pages it flagged that were fine | 134 |

A coin flip with a lot of false alarms, on a pipeline that was extracting at
96.9%. The extraction was better than its own account of itself.

### 8.1 Why

The meta recorded **events**: a warning fired, cells were dropped, a cell looked
misread. A page that quietly under-extracts produces no event, so nothing was
written about it. Silence was being produced by two different situations --
"this page is fine" and "nothing was noticed" -- and they were indistinguishable.

### 8.2 What replaced it

Every page now gets a row, whether or not anything went wrong, carrying what
the pipeline actually knows: characters extracted, how character-like they were,
which variant won, and **how far the variants disagreed**.

Variant spread is the useful part, and it was already being computed and thrown
away. Several independent ways of reading a page landing in the same place is
the closest thing to a confidence measure available without a second extractor.
A clean page scores a spread of 0.009; a page rescued from a broken text layer
scores 0.96.

### 8.3 Disagreement is not doubt

The first version treated variant disagreement as a problem and flagged every
rescued page -- on a scanned filing, nearly all of them. But a page whose
variants disagreed enormously and which then came out clean is a success, not a
concern.

So doubt is judged on the result, and thinness is judged **against the
document's own median yield** rather than a constant. A filing of dense tables
and one of sparse cover letters have nothing in common in absolute terms. Pages
under 30% of their document's median are worth revisiting even when nothing
visibly failed.

### 8.4 The re-run handle

Doubts are named rather than scored: `almost_no_text`, `not_character_like`,
`table_mostly_dropped`, `table_grid_disputed`, `thin_for_this_document`,
`ocr_empty`, `bbox_clamped`, `needed_retry`. The meta carries a `doubts` index
grouping pages by kind, and a flat `reprocess` list.

That is the handle a later pass needs. When a new docling release fixes table
grids, or a better OCR model appears, the pages affected by that specific
problem can be selected across the corpus and re-run -- without re-converting
everything, and without anyone having to remember what was wrong.

## 9. Azure against docling: what each is good at

Neither is uniformly better. The differences are systematic, and knowing which
is which decides what is worth fixing.

### 9.1 What Azure does better

**It never trusts the text layer.** Every page is OCR'd unconditionally, which
makes it immune to the entire class of failure in §2.1 and §6.1 -- mojibake and
empty text layers -- without needing to detect anything. This pipeline had to
reach the same immunity the long way round, by converting each page several ways
and judging the result.

**It reads inside drawings.** The largest remaining gap. A GIS location plan
carries labels and UTM grid coordinates printed over the imagery, some rotated
90 degrees up the margins; docling classifies the map as one picture and never
looks inside. On 4692624, ten of the eleven worst pages are drawings.

**It finds more tables.** 105 against 20 on 4692624. Some of that is convention
-- Azure splits a page header block and a main table where docling emits one --
but not all of it.

**It decodes barcodes** (`PDF417 -> CCG2414506`), **detects more checkboxes**
(341 vs 32 on 4647200), and reports **per-word confidence** and **page skew**,
none of which docling provides.

### 9.2 What docling does better

**It keeps exact text where the text layer is sound.** Azure OCRs regardless,
so it re-reads text it could have copied; docling passes the page through
untouched. On clean pages this pipeline scores 100.0% of Azure, and some of
Azure's "extra" content is its own OCR error -- on two weak pages of 4692624 the
tokens docling "missed" include `aposssusus`, `comonseu`, `nofdyesty` and
`rimgbohal`. Coverage against Azure is not coverage against truth.

**It says when it has failed.** TableFormer reports how many cells it discarded,
which is how the 99.3% collapse on page 376 was found. Azure reported zero
warnings on the entire 986-page filing -- including for content it got wrong.
For a regulatory corpus, an extractor that admits failure is worth a great deal.

**It is local, free, and inspectable.** Its failure modes can be traced to a
line of code, which is how every fix in this document was found.

**Its output is richer structurally**: footnotes, captions, list items, formulas
and code as distinct labels, and table cells carrying row and column spans and
header flags.

### 9.3 Azure's own defects, worth knowing

**Escaped markdown.** Its markdown HTML-escapes `<`, so a cell whose `content`
is `<0.010` appears as `&lt;0.010` and the span offsets count the escaped form.
On some filings a majority of paragraph spans do not match their own content.
Anything consuming Azure markdown by offset must unescape first, or it will
mangle every detection limit in the corpus.

**Parts are not concatenable as delivered.** Each analyzer result restarts span
offsets at 0 and indexes elements (`/paragraphs/49`) into its own arrays.
Merging requires rebasing both. Page numbers and `source` polygons are already
global.

**It cannot be interrogated.** When it is wrong there is no warning, no
confidence on the structure, and no way to find out why.

### 9.4 Raw comparison

Measured against the run-5 output of 4647200.

| | Azure | docling (run 5) |
|---|---|---|
| Tables | 1,718 | 887 |
| Figures / pictures | 956 | 1,078 |
| Checkboxes | 341 | 32 |
| Barcodes decoded | 6 (`PDF417 → CCG2414506`) | none |
| Per-word confidence | yes | per-page only |
| Page skew angle | yes | no |
| Text inside drawings | yes | no (see below) |

Table counts are not directly comparable — Azure splits a page into a header
table plus the main table where docling emits one — but checkboxes and barcodes
are real gaps.

**Text inside drawings is the largest remaining gap.** 24 of the 87 pages below
95% are figure/drawing pages. Page 453 is a GIS location plan whose UTM grid
coordinates are printed **rotated 90°** up the margins; docling classifies the
whole map as one picture and never reads inside it. Recovering that text by
OCR-ing the page at 0/90/270° was measured at **+10.8 points** on six such pages
(~63% → ~74%), most of it from reading the figure at all rather than from the
rotations. Even then those pages stay well short of Azure.

**Azure has its own defect worth knowing:** its markdown HTML-escapes `<`, so a
cell whose `content` is `<0.010` appears in the markdown as `&lt;0.010`, and the
span offsets count the escaped form. Anything consuming Azure markdown must
unescape or it will mangle every detection limit.

**Azure's parts are not concatenable as delivered.** The four 300-page parts
each restart span offsets at 0 and index elements (`/paragraphs/49`) into their
own arrays. Merging requires rebasing both; page numbers and `source` polygons
are already global.

---

## 10. Where the numbers actually stand

Character coverage overstates the problem: it counts a missing `the` the same as
a wrong digit. On the substance:

| | |
|---|---|
| numeric tokens in tables | Azure 158,631 · docling 154,574 |
| agreeing | **153,743 — 96.9% of Azure, 99.5% of docling** |
| detection limits (`<x`) | Azure **19,831** · docling **19,831** |
| detection-limit values differing | **2**, both traced to the single `<3hT` cell |

Most remaining "Azure-only" numbers are project and work-order IDs that docling
files under `texts` rather than table cells — placement, not loss.

---

## 11. Known intermittent failure

One run died at page 28 with `'builtin_function_or_method' object is not
subscriptable`, raised inside `DocumentConverter.convert`. The same page
converts cleanly on its own, and a 30-page run over the same range succeeded
twice afterwards, so it is not deterministic. Three other GPU jobs were running
against the same card at the time, which is the most likely cause.

`ingest.py` now retries a failed page once before giving up, and records any
page that needed it in `retried_pages`. A 986-page run should not end because
one page failed once — but a pipeline that fails intermittently is worth
watching, so the retries are recorded rather than swallowed.

## 12. Open items

- **Figure text pass — implemented but disabled (`FIGURE_PASS = False`).** It
  works: 44 words recovered from the page 453 map. But the run then hung after
  that page, GPU idle at 3% and the process at 0% CPU, with a second RapidOCR
  instance alive alongside docling's own. Two engines on the same GPU appear to
  deadlock. It needs to run out of process, or reuse docling's engine, before
  being turned back on. The measured value (+10.8 points on six figure pages,
  ~+0.3 document-wide) is small, so this is not urgent.
- **Checkboxes and barcodes** — untouched. Barcodes would need a decoder pass
  (`pyzbar`) over picture regions.
- **Table structure has never been measured properly.** Coverage and numeric
  agreement are both blind to a table whose values are all correct and all in
  the wrong rows. `docling-eval` is not directly usable (it needs ground-truth
  datasets), but its TEDS metric is the right idea.
- **The 0.2 control-character threshold** is tuned on one document. It matched
  314/316 here; it has not been tested on another filing.
- **Everything in this document is one filing.** The corpus is not.
