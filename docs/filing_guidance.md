# What filers could do to make filings machine-readable

Evidence for changes to the filing manual, from extracting 4,296 REGDOCS
filings (16,085 pages) and comparing every page against a second, independent
commercial extraction.

This is not a wish list. Each item below is a defect that was **measured**, with
the rate it occurs at, an acceptance test a filer or an intake system can run,
and an honest note on how strong the evidence is. A section at the end lists
things that look like they should be requirements and are not, because the
measurement says they do not matter.

The framing that matters: none of this asks filers to change what they submit,
only how the PDF is produced. Every defect below is introduced at export time,
by the tool, usually without the filer knowing.

---

## R1. The text layer must reproduce the text on the page

**The single largest quality problem in the corpus.**

On **14.7% of pages (2,371 of 16,085), across 17.9% of documents**, the text
embedded in the PDF was worse than running OCR on a picture of the page. The
pipeline recovers these by rasterising and reading them optically, which works
but is slow, lossy on small type, and unnecessary: the correct text existed in
the authoring tool and was discarded on export.

The extreme case in the corpus is a filing where 399 of 463 pages carried no
text layer at all. Extracted as filed, it yielded **13.2%** of its content.
After rasterising and OCR-ing every page, **96.0%**.

Rates vary enormously by how the PDF was made:

| how the PDF was produced | pages | needed OCR rescue |
|---|---:|---:|
| Adobe PDF Library | 5,817 | 5.1% |
| Microsoft Word (direct export) | 1,177 | 5.9% |
| Chrome / Skia print-to-PDF | 2,515 | 13.7% |
| Microsoft Print To PDF | 189 | 14.8% |
| *no producer recorded* | 1,738 | **28.1%** |
| one PDF-XChange Viewer filing | 474 | **91.1%** |

**Acceptance test.** Open the PDF, select all, copy, paste into a text editor.
If the page's visible text does not appear, the page has no usable text layer.
Automatable as: extracted characters per page against characters found by OCR
of the same page.

**Requirement worth adding:** a filing must contain a text layer that
reproduces its visible text. Where the source is genuinely paper, it must be
OCR'd before filing — the filer knows what the document is and can check the
result; a downstream extractor is guessing.

**Evidence strength: strong.** 16,085 pages, independently scored.

---

## R2. Embedded text must decode to the characters it displays

A PDF can contain text that looks perfect on screen and extracts as nonsense,
because the embedded font subset carries no `ToUnicode` table. One filing in
the corpus has **315 of 986 pages** in this state. Its text layer begins:

```
33 39 38 39 43 39 i255 196 34 44 43 41 44 i255 37 41 44 45 36 33 41 37
```

while the page displays an ordinary page of English.

This is the most dangerous defect here, because **it is silent**. The file
opens correctly, prints correctly, and passes visual review. Nothing signals a
problem until a machine reads it. Our own quality check missed a variant of it
for months, because the garbage decoded to digits and digits looked like
legitimate data.

**Acceptance test.** The same copy-paste test as R1, but read what you pasted.
Text that pastes as digit sequences, or as characters that are not the ones on
screen, fails.

**Requirement worth adding:** exported text must be extractable as the
characters displayed. In practice this means not re-distilling a finished PDF
through PostScript, and not stripping font metadata to reduce file size.

**Evidence strength: strong for the defect, weak for the cause.** The defect is
unambiguous and well documented. Six of the nine surviving cases came from 88
pages produced by two versions of Acrobat Distiller — about a hundred times the
corpus baseline — but six pages is far too small a sample to name a product in
a filing manual.

---

## R3. Individual pages must not be unboundedly large

Thirteen pages across five filings cannot be processed at all. They are plan
sheets: one is **182.5 × 34.5 inches**, twelve are **92 × 92 inches**. Rendered
at normal working resolution these are 294 and 395 megapixels, past the point
where standard image libraries refuse to decode them. Every extraction strategy
fails on them, and they produce no output rather than degraded output.

This is 0.06% of documents, so it is a small problem — but it is a **total**
loss on the pages it touches, and those pages are alignment sheets and site
plans, which carry exactly the spatial information hardest to recover any other
way.

**Acceptance test.** Page box dimensions, readable from the file without
rendering it.

**Requirement worth adding:** a cap on page dimensions, with oversized drawings
filed as separate sheets at a standard size, or tiled. A 15-foot alignment sheet
is not readable by a person on a screen either.

**Evidence strength: strong but narrow.** The failure is absolute and exactly
attributable. It affects very few filings.

---

## R4. Scanned filings should be OCR'd by the filer, not the regulator

R1 covers this mechanically, but it is worth stating separately because the
responsibility question is different. When a filer OCRs their own document,
they can see the source, know the terminology, and check the result. When an
extractor OCRs it years later, it is reading a picture with no idea what the
words should be — and on this corpus, OCR of laboratory certificates is where
single-character errors turn `<0.010` into a different measurement.

**Evidence strength: strong on the mechanism, unmeasured on the difference.**
We have not compared filer-OCR'd documents against our own OCR, because the
corpus does not label which is which.

---

## What is *not* worth requiring

Rules cost filers effort and cost the regulator enforcement. These were checked
and do not earn their place:

- **Page rotation.** Pages with `/Rotate 90` are common in drawing sets and
  look like they would cause trouble. Measured: 0.713 mean table-extraction
  quality on unrotated pages against **0.734** on rotated ones. No effect. (A
  rotation bug did appear during this work — in our own analysis code, not in
  the filings.)
- **Naming a required PDF producer.** The evidence is a hundredfold difference
  on a six-page sample. Not enough to put a vendor's name in a manual.
- **Table formatting rules.** Tables are where extraction is weakest — our
  best result recovers 87% of the reference's cells, and the two leading
  extractors disagree about the grid on pages where both look confident. We
  cannot specify what filers should do differently, because we cannot yet say
  what makes one table easier to read than another.
- **Document length.** 986-page filings extract as well as 2-page ones. Length
  is not a quality problem.

---

## The recommendation that matters most

Every test above runs on the file alone, in under a second, without knowing
anything about the contents:

| check | reads |
|---|---|
| R1 — text layer present and better than OCR | text layer + one rendered page |
| R2 — text decodes to displayed characters | share of letters vs digits, presence of words |
| R3 — page dimensions | page boxes only |

**A rule in a manual is advice; a check at intake is a rule.** These defects are
all introduced silently by export tooling, so a filer who is told to "ensure
text is extractable" has no way to know they have failed. An automated check at
submission that says *page 47 of this filing has no usable text layer* is
actionable in a way the manual text is not, and it moves the fix to the only
point where the correct text still exists.

The three checks above are already implemented in this repository as part of
extraction quality reporting; they would need repackaging as a pre-submission
tool, not writing from scratch.

---

*Measurements and their limits are documented in
[lessons_learned.md](lessons_learned.md) §14. Several of the conclusions there
were wrong on first analysis and are recorded with their corrections; any
number above that matters to a policy decision should be re-derived before it
is relied on.*
