#!/usr/bin/env python3
"""Ingest a PDF into docling JSON, one page at a time.

    ingest.py <pdf-path-or-url> [--force]

--force re-ingests a document that was already done under different settings,
overwriting its output; without it such a run stops and prints what changed.

Everything is fixed: a CUDA GPU with 16 GB or more is used when present and the
CPU (8 threads) otherwise, RapidOCR (full-page on rasterized pages, pdf-aware
elsewhere), TableFormer for tables in fast mode with an accurate-mode fallback
(see the chunk loop), heading levels from legal numbering.

Each page is staged before conversion. Pages whose text layer is unmapped glyph
codes, or that carry no usable text layer at all, are rasterized, because
docling's table
stage reads the text layer directly and would otherwise emit mojibake in every
table cell whatever the OCR settings. Every other page is passed through
untouched and keeps its exact text. On the first 986-page filing that was 315
rasterized against 671 left alone.

Pages are converted one at a time and freed as they go, so memory stays flat on
a 1000-page filing and every warning names the page it came from. Results are
written to output/<id>/chunks/ as they finish, then merged into one document
and removed; an interrupted run resumes from the last finished page.

Content docling reports as lost or degraded (dropped table cells, empty OCR
regions, clamped bboxes, timeouts) is recorded per page and merged into the
meta, with drop ratios, so odd text in the output can be traced to a cause.
Pages where the table-mode fallback changed the answer are listed too, since a
silently repaired page is still one worth knowing about.

An empty warning list means docling reported nothing, not that nothing was
lost. The mojibake above was silent: 315 pages of it, found only by comparing
against a second extraction of the same filing.

Writes to output/<id>/:
    <id>.docling.json.gz    the document: structure, tables, provenance
    <id>.docling.meta.json  source, pages, settings, timing, per-page warnings

Markdown is a lossy projection and is not written here; export it from the JSON
when it is needed (doc.export_to_markdown()).
"""

from __future__ import annotations

import datetime as _dt
import gc
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

CHUNK_PAGES = 1    # one page per conversion pass: measured at 0.95-0.98x the
                   # wall time of 25-page passes (no batching penalty), and it
                   # pins every warning to an exact page instead of a range
THREADS = 8        # i9-14900KF: beats 16 by ~10% and 32 by ~45%; models are small
MIN_VRAM_GB = 16   # below this, a GPU run risks OOM on 2x-scale page images
TIMEOUT_S = 3600   # per page
MAX_WARNINGS_PER_CHUNK = 500  # cap so one pathological chunk cannot bloat the sidecar

# Loggers whose WARNING+ output is about this process's configuration, not about
# the document being converted. Recording them would put an identical line in
# every sidecar and say nothing about the content.
WARNING_LOGGER_DENYLIST = ("docling.datamodel.stage_model_specs",)

# Warnings that are identical on every run of every document: deprecation
# notices about how this script calls docling, and one torch note about a conv
# layer inside a vendored model. Capturing warnings.warn to catch the bbox
# clamps sweeps these in too, so they are dropped by message -- they describe
# the code, not the filing.
WARNING_MESSAGE_DENYLIST = (
    "DeprecationWarning",
    "FutureWarning",
    "PendingDeprecationWarning",
    "Using padding='same' with even kernel lengths",
)

# Substrings that classify a captured warning, so a sidecar can be grepped by
# failure kind instead of by prose. First match wins; anything else is "other".
WARNING_KINDS = (
    ("table_cells_dropped", "matched neither a row nor a column band"),
    ("ocr_empty", "returned empty result"),
    ("timeout", "processing timeout"),
    ("bbox_clamped", "is outside page bounds"),
)


def log(msg: str) -> None:
    print(f"[ingest] {msg}", flush=True)


def pick_device() -> str:
    """CUDA when a big enough GPU is present, else CPU. ~4x faster on a 4090."""
    try:
        import torch

        if not torch.cuda.is_available():
            return "cpu"
        vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
        name = torch.cuda.get_device_name(0)
        if vram_gb >= MIN_VRAM_GB:
            log(f"gpu: {name} ({vram_gb:.0f} GB) -> cuda")
            return "cuda"
        log(f"gpu: {name} ({vram_gb:.0f} GB) under {MIN_VRAM_GB} GB -> cpu")
    except Exception as exc:  # torch missing or driver problem
        log(f"gpu probe failed ({exc}) -> cpu")
    return "cpu"


def url_stem(source: str) -> str:
    """A filename for a downloaded PDF, from the URL.

    REGDOCS download links end in the document id, which is the name this
    corpus uses everywhere else. Anything else falls back to the last path
    segment, reduced to characters that are safe in a filename.
    """
    from urllib.parse import unquote, urlparse

    tail = unquote(urlparse(source).path.rstrip("/").split("/")[-1])
    tail = re.sub(r"\.pdf$", "", tail, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", tail).strip("._-")
    return cleaned[:80] or "download"


def fetch_pdf(source: str, into: Path) -> Path:
    """Return a local path for the source, downloading it if it is a URL.

    Downloads land in the source directory under a name taken from the URL,
    beside every other PDF in the corpus, so a file fetched once is a file
    already present the next time and is not downloaded again.
    """
    if not source.lower().startswith(("http://", "https://")):
        return Path(source)
    into.mkdir(parents=True, exist_ok=True)
    dest = into / f"{url_stem(source)}.pdf"
    if dest.exists():
        log(f"already downloaded -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest
    tmp = dest.with_suffix(".pdf.part")
    log(f"downloading {source}")
    subprocess.run([
        "curl", "-fSL", "--retry", "3", "--retry-delay", "5",
        "--max-time", "3600", "-C", "-", "-o", str(tmp), source,
    ], check=True)
    tmp.replace(dest)
    log(f"downloaded {dest.stat().st_size / 1e6:.1f} MB -> {dest}")
    return dest


def build_converter(device: str, ocr_mode=None, table_mode=None, ocr_model="medium"):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        OcrMode,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.accelerator_options.device = device
    opts.accelerator_options.num_threads = THREADS
    opts.generate_parsed_pages = False  # purge page objects from RAM as they go
    opts.images_scale = 2.0             # sharper crops for OCR and table structure

    # RapidOCR, torch backend (ships with docling, no onnxruntime needed).
    # The mode is chosen per page by the caller, because neither setting is
    # right for the whole document: FULL_PAGE is the only thing that reads a
    # rasterized page, but on a page with a good text layer it throws that
    # exact text away and re-reads it with OCR -- measured at 16-32% of the
    # reference on small print, against 98-100% for PDF_AWARE_LAYOUT_REGIONS.
    opts.do_ocr = True
    opts.ocr_options = RapidOcrOptions(backend="torch")
    # Inert with the multi-language PP-OCRv6 models selected below: en, fr and
    # la score identically on a scanned French filing in this corpus. Left
    # explicit anyway, because RapidOcrOptions defaults to Chinese, and a
    # future change of model family would otherwise inherit that silently.
    opts.ocr_options.lang = ["en"]
    opts.ocr_options.mode = ocr_mode or OcrMode.PDF_AWARE_LAYOUT_REGIONS
    # RapidOCR ships PP-OCRv6 "small" by default; "medium" is the same family
    # one size up. On seven pages where this pipeline trailed an independent
    # extraction it averaged 86.0% against 82.7%, took no longer (17s vs 16s
    # for the batch), and left an already-perfect control page at 100%. The
    # "tiny" variant was worse than both, at 79.8%.
    from rapidocr import ModelType as _ModelType

    _size = {"medium": _ModelType.MEDIUM, "small": _ModelType.SMALL,
             "tiny": _ModelType.TINY}[ocr_model]
    opts.ocr_options.rapidocr_params = {
        "Det.model_type": _size, "Rec.model_type": _size,
    }

    # Table mode is chosen per page by the caller, defaulting to FAST. Neither
    # mode is right everywhere: ACCURATE collapses large forms to a 1x1 grid
    # (page 376 lost 297 of 299 cells, page 833 came out 1x2 where the page
    # holds 25x10), while FAST trims small two-column tables that ACCURATE
    # keeps whole. FAST is also about twice as quick, so it goes first.
    opts.do_table_structure = True
    opts.table_structure_options.mode = table_mode or TableFormerMode.FAST
    opts.do_code_enrichment = False
    opts.do_formula_enrichment = False

    # Heading levels from legal numbering. Font-style inference is off: it
    # needs the parsed pages purged above.
    opts.heading_hierarchy_options.enabled = True
    # Bookmarks are off: pages reach docling as rasterized images or imported
    # copies, neither of which carries the outline. In the one run where they
    # were live they changed nothing measurable -- 39 of this filing's 61
    # bookmarks are "Slide Number N" and the level-2 headings that appeared
    # were all numbered ones, which use_numbering recovers anyway.
    opts.heading_hierarchy_options.use_bookmarks = False
    opts.heading_hierarchy_options.use_numbering = True
    opts.heading_hierarchy_options.use_style = False

    opts.document_timeout = TIMEOUT_S
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )


class WarningCapture(logging.Handler):
    """Collect WARNING+ records emitted while one chunk converts.

    docling reports most content loss only by logging it -- the table matcher
    drops a cell, says so at WARNING, and returns a result that still claims
    success. Attaching this to the root logger for the duration of a convert is
    the only way to get those events into the sidecar.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[dict] = []
        self.dropped = 0  # over the cap; counted so the sidecar cannot lie by omission

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(WARNING_LOGGER_DENYLIST):
            return
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must not kill the ingest
            message = repr(record.msg)
        if any(d in message for d in WARNING_MESSAGE_DENYLIST):
            return
        if len(self.records) >= MAX_WARNINGS_PER_CHUNK:
            self.dropped += 1
            return
        kind = classify_warning(message)
        self.records.append({
            "kind": kind,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            **warning_detail(kind, message),
        })

    def __enter__(self) -> "WarningCapture":
        # docling_core reports out-of-bounds bboxes through warnings.warn, not
        # logging, and the warnings module shows each location only once per
        # process. Both have to be overridden or those events never arrive.
        self._warn_ctx = warnings.catch_warnings()
        self._warn_ctx.__enter__()
        warnings.simplefilter("always")
        logging.captureWarnings(True)
        logging.getLogger().addHandler(self)
        self._root_level = logging.getLogger().level
        if not logging.getLogger().isEnabledFor(logging.WARNING):
            logging.getLogger().setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc) -> None:
        logging.getLogger().removeHandler(self)
        logging.getLogger().setLevel(self._root_level)
        logging.captureWarnings(False)
        self._warn_ctx.__exit__(*exc)


def classify_warning(message: str) -> str:
    for kind, needle in WARNING_KINDS:
        if needle in message:
            return kind
    return "other"


# "300 of 302 pdf cells ... of the 2x11 grid were dropped"
_DROPPED = re.compile(r"(\d+) of (\d+) pdf cells .*? of the (\d+x\d+) grid")


def warning_detail(kind: str, message: str) -> dict:
    """Pull the numbers out of a warning so severity is sortable.

    A count alone cannot be triaged: losing 300 of 302 cells destroys a table,
    losing 2 of 1014 trims a stray. Both are one line of prose, so the ratio is
    parsed out here rather than left for a reader to eyeball.
    """
    detail: dict = {}
    if kind == "table_cells_dropped":
        m = _DROPPED.search(message)
        if m:
            dropped, total = int(m.group(1)), int(m.group(2))
            detail["cells_dropped"] = dropped
            detail["cells_total"] = total
            detail["drop_ratio"] = round(dropped / total, 4) if total else None
            detail["grid"] = m.group(3)
    elif kind == "bbox_clamped":
        m = re.search(r"on page (\d+)", message)
        if m:
            detail["page_no"] = int(m.group(1))
    return detail


LOW_CONFIDENCE = 0.5  # below this, docling itself is unsure about the page


def confidence_scores(result) -> dict:
    """docling's own per-page quality scores, which this script used to drop.

    parse/layout/table/ocr each land in 0..1. They are the closest thing to
    Azure's per-word confidence, and unlike the warning log they say something
    about every page, including the ones that failed silently.
    """
    conf = getattr(result, "confidence", None)
    if conf is None:
        return {}
    out = {}
    for name in ("parse_score", "layout_score", "table_score", "ocr_score"):
        v = getattr(conf, name, None)
        if v is None or (isinstance(v, float) and v != v):  # drop NaN
            continue
        out[name.replace("_score", "")] = round(float(v), 4)
    return out


NUMERIC_CELL = re.compile(r"^[<>]?\s*-?\d+(?:[.,]\d+)?$")
MAX_SUSPECT_CELLS = 2000   # a reviewable list, not a second copy of the tables


def _confusion_pair(a: str, b: str):
    """Two cell values differing at one position, letter on one side, digit on
    the other -- the signature of an OCR substitution inside an otherwise
    identical value, e.g. '<3hT' read as '<31T'.

    A digit-vs-digit difference is ignored: those are usually two genuinely
    different measurements. Values under three characters are ignored too,
    since any single digit then "matches" any single letter.
    """
    if len(a) != len(b) or a == b or len(a) < 3:
        return None
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(diff) != 1:
        return None
    x, y = a[diff[0]], b[diff[0]]
    if x.isdigit() and y.isalpha():
        return a, b
    if y.isdigit() and x.isalpha():
        return b, a
    return None


def suspect_cells(doc) -> list[dict]:
    """Table cells that look misread, judged against their own column.

    No reference extraction is involved: a column of lab results is highly
    repetitive, so a value that differs from a sibling by one letter-to-digit
    substitution is suspect on its own terms. This is the check that catches
    the class of error where one character moves a regulatory number by an
    order of magnitude -- '<3hT' becoming '<31T' reads as a limit of 31
    instead of 3.
    """
    out = []
    for table in doc.tables:
        pages = [pr.page_no for pr in table.prov]
        page = pages[0] if pages else None
        cols: dict = {}
        boxes: dict = {}
        for c in table.data.table_cells:
            if c.column_header or c.row_header:
                continue
            text = (c.text or "").strip()
            if text:
                cols.setdefault(c.start_col_offset_idx, []).append(
                    (c.start_row_offset_idx, text))
                # Keep the cell's box: a later pass re-reads the crop directly,
                # and that is the one remedy already shown to fix this class --
                # the cell that came out as "<31T" reads correctly in isolation.
                if c.bbox is not None:
                    boxes[(c.start_col_offset_idx, c.start_row_offset_idx)] = [
                        round(c.bbox.l, 1), round(c.bbox.t, 1),
                        round(c.bbox.r, 1), round(c.bbox.b, 1)]
        for col, cells in cols.items():
            values = sorted({t for _, t in cells})
            for i, a in enumerate(values):
                for b in values[i + 1:]:
                    pair = _confusion_pair(a, b)
                    if not pair:
                        continue
                    suspect, sibling = pair
                    rows = [r for r, t in cells if t == suspect]
                    out.append({
                        "page": page, "column": col, "rows": rows,
                        "bboxes": [boxes[(col, r)] for r in rows if (col, r) in boxes],
                        "value": suspect, "sibling": sibling,
                        "rule": "letter_digit_confusion",
                    })
            # One stray non-numeric value in a long numeric column usually
            # means a cell was misread or the grid slipped a row.
            if len(cells) >= 10:
                odd = [(r, t) for r, t in cells if not NUMERIC_CELL.match(t)]
                if len(odd) == 1:
                    key = (col, odd[0][0])
                    out.append({"page": page, "column": col,
                                "rows": [odd[0][0]], "value": odd[0][1],
                                "bboxes": [boxes[key]] if key in boxes else [],
                                "rule": "non_numeric_in_numeric_column"})
    out.sort(key=lambda f: (f["page"] is None, f["page"], f["column"]))
    return out


# Off by default. The pass works -- 44 words recovered from one map page --
# but the run then hung, GPU idle at 3% and the process at 0% CPU, with a
# second RapidOCR instance live alongside docling's own. Until that is run out
# of process, or made to reuse docling's engine, this stays off.
FIGURE_PASS = False              # read text inside drawings; see figure_text()
FIGURE_ROTATIONS = (0, 90, 270)  # 180 was measured at +0.4 to +1.7 pts: skipped
_ocr_engine = None


def _figure_ocr():
    """One RapidOCR instance for the figure pass, built on first use."""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr import EngineType, ModelType, RapidOCR

        _ocr_engine = RapidOCR(params={
            "Det.engine_type": EngineType.TORCH, "Rec.engine_type": EngineType.TORCH,
            "Cls.engine_type": EngineType.TORCH,
            "Det.model_type": ModelType.MEDIUM, "Rec.model_type": ModelType.MEDIUM,
        })
    return _ocr_engine


def figure_text(pdf: Path, page_no: int, already: set[str]) -> list[str]:
    """Text inside drawings, which the layout stage does not read.

    Maps and plan drawings carry labels and grid coordinates over the image
    itself, and docling treats that region as one picture. Reading the page
    directly recovers them; reading it rotated recovers the ones printed up
    the margins, which no amount of OCR at 0 degrees will find. Measured on
    six such pages this lifted them from ~63% to ~74% of a reference
    extraction, most of it from reading the figure at all and the rest from
    the rotations.

    Returns only words not already in the page's extracted text. Positions are
    not recovered: this is searchable text, not document structure.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    image = doc[page_no - 1].render(scale=RASTER_SCALE).to_pil().convert("RGB")
    doc.close()
    engine = _figure_ocr()
    found: list[str] = []
    seen = set(already)
    for angle in FIGURE_ROTATIONS:
        try:
            result = engine(image.rotate(angle, expand=True) if angle else image)
        except Exception as exc:  # a failed rotation must not fail the page
            log(f"figure ocr failed on page {page_no} at {angle} deg: {exc}")
            continue
        for line in (result.txts or ()) if result else ():
            for word in str(line).split():
                key = re.sub(r"[^0-9a-z]", "", word.lower())
                if key and key not in seen:
                    seen.add(key)
                    found.append(word)
    return found


# Each page is converted under every one of these and the best output kept.
# Adding a strategy is adding a line here: the selector decides per page, so a
# variant that helps one filing and hurts another can simply be listed. Every
# entry below was validated against an independent extraction before being
# added -- across 47 page comparisons the score picked the closer output every
# time, including between variants that both looked reasonable.
#
#   as-is      keeps the page's exact embedded text, best when it is sound
#   raster     renders the page so OCR is the only source; rescues pages whose
#              text layer is unmapped glyph codes or missing entirely
#   raster-sml the smaller OCR model, which wins on roughly 3 pages in 10
#   raster-hi  a higher render scale, which wins on roughly 3 pages in 10
#
# Cost is one conversion per variant per page. Trim the list for speed.
PAGE_VARIANTS = [
    {"name": "as-is",      "raster": False, "scale": None, "ocr_model": "medium"},
    {"name": "raster",     "raster": True,  "scale": 3.0,  "ocr_model": "medium"},
    {"name": "raster-sml", "raster": True,  "scale": 3.0,  "ocr_model": "small"},
    {"name": "raster-hi",  "raster": True,  "scale": 4.5,  "ocr_model": "medium"},
]
# A variant must beat the incumbent by this much before it is taken. Scores
# differ by a fraction of a percent between variants that are all reading the
# page correctly, and at that resolution the score is noise: on a clean page
# argmax would swap exact embedded text for an OCR pass scoring 0.4% higher
# and lose a little accuracy. Calibrated over 24 pages from three filings --
# never switching scored 98.0%, pure argmax 98.5%, a 1-3% margin 98.55%.
SWITCH_MARGIN = 0.02
MIN_PAGE_CHARS = 50       # below this a page produced essentially nothing
MIN_ALNUM_RATIO = 0.35    # below this the "text" is not made of characters


def page_quality(result) -> dict:
    """Score a converted page on its own terms, with nothing to compare against.

    Both failures this pipeline has had were plain in its own output. A page
    whose text layer was unmapped glyph codes came out as '%  @ 8 ! C *&00 *':
    992 characters, only 11% of them letters or digits. A scanned page left
    unrasterized came out empty. Neither needed a second extraction to spot.

    The measure is the share of characters that are letters or digits, not the
    share that are unprintable -- mojibake arrives as ordinary punctuation. A
    dense numeric table still scores about 0.68, well clear of the threshold.
    """
    parts = [item.text or "" for item in result.document.texts]
    parts += [c.text or "" for t in result.document.tables for c in t.data.table_cells]
    text = " ".join(parts)
    n = len(text)
    if n == 0:
        return {"chars": 0, "alnum_ratio": 0.0, "score": 0.0}
    alnum = sum(1 for ch in text if ch.isalnum()) / n
    return {"chars": n, "alnum_ratio": round(alnum, 4),
            "score": round(n * alnum, 1)}


def looks_broken(quality: dict) -> bool:
    """Gross failure only: nothing extracted, or output that is not characters.

    Deliberately not a quality bar. A genuinely blank or sparse page trips this
    and costs one extra conversion, which is cheaper than shipping a page of
    mojibake.
    """
    return (quality["chars"] < MIN_PAGE_CHARS
            or quality["alnum_ratio"] < MIN_ALNUM_RATIO)


def convert_staged(pdf: Path, start: int, end: int, raster: bool,
                   converters: dict, staged: Path, label: str,
                   scale: float = None, ocr_model: str = "medium",
                   table_mode: str = None):
    """Stage these pages one way and convert them, retrying once on failure.

    A single page failing must not end a 986-page run: conversion has been seen
    to fail once and then succeed on identical input, so a failure is retried
    before it is believed.
    """
    pages = set(range(start, end + 1)) if raster else set()
    result = retries = None
    for attempt in (1, 2):
        with WarningCapture() as cap:
            n_raster = build_chunk_pdf(pdf, start, end, pages, staged, scale)
            try:
                key = (n_raster > 0, ocr_model, table_mode or TABLE_MODE)
                result = converters[key].convert(str(staged))
            except Exception as exc:
                if attempt == 2:
                    raise
                retries = f"{type(exc).__name__}: {exc}"
                log(f"{label}: {retries} -- retrying")
                gc.collect()
                continue
        if result.status.value == "success":
            return result, cap, n_raster, retries
        if attempt == 2:
            raise RuntimeError(f"{label} failed: {result.status}")
        retries = f"status={result.status.value}"
        log(f"{label}: {retries} -- retrying")
        gc.collect()
    raise RuntimeError(f"{label}: conversion did not succeed")


AGREEMENT_SPREAD = 0.05   # variants within this of each other agree
LOW_YIELD_FRACTION = 0.3  # a page under this share of the document's median
                          # yield is thin enough to be worth revisiting


def page_report(report: dict) -> dict:
    """One row per page, for every page, whether or not anything went wrong.

    The meta used to record events -- a warning fired, cells were dropped --
    which says nothing about a page that quietly under-extracts. Measured
    against an independent extraction on one filing, event records caught 35
    of 67 weak pages and raised 134 false alarms. A page that produced no
    event is not a page that came out well; it is a page nothing was said
    about.

    So every page gets a row carrying what the pipeline actually knows: how
    much it extracted, how character-like it was, and how far the variants
    disagreed. Variant spread is the closest thing to a confidence measure
    available without a second extractor: when several ways of reading a page
    land in the same place, the page is probably read; when they scatter,
    something about it is ambiguous.
    """
    scores = {k: v.get("score", 0.0) for k, v in (report.get("staging_scores") or {}).items()}
    ordered = sorted(scores.values(), reverse=True)
    top = ordered[0] if ordered else 0.0
    low = ordered[-1] if ordered else 0.0
    spread = round((top - low) / top, 4) if top > 0 else 0.0
    # How far the winner beat the next best. Without this, "this variant won
    # 65 pages" cannot be told apart from "it won 65 coin flips", and there is
    # no basis for dropping a variant to halve the run.
    runner_up = ordered[1] if len(ordered) > 1 else None
    margin = round((top - runner_up) / top, 4) if runner_up is not None and top > 0 else None
    q = report.get("quality") or {}
    row = {
        "page": report["page_start"],
        "staging": report.get("staging"),
        "chars": q.get("chars"),
        "alnum_ratio": q.get("alnum_ratio"),
        "seconds": report.get("elapsed_seconds"),
        "variant_spread": spread,
        "win_margin": margin,
        "variant_scores": {k: round(v, 1) for k, v in scores.items()},
        "variants_agree": spread <= AGREEMENT_SPREAD,
        "doubts": doubts_for(report, q, spread),
    }
    if report.get("table_mode") and report["table_mode"] != TABLE_MODE:
        row["table_mode"] = report["table_mode"]
    return row


def doubts_for(report: dict, quality: dict, spread: float) -> list[str]:
    """Named, machine-readable reasons a page may not be fully extracted.

    These are the handles a later re-run needs: not "something was odd" but
    which kind of odd, so a future pass can select the pages a new model or a
    new docling release might actually improve.
    """
    out = []
    if (quality.get("chars") or 0) < MIN_PAGE_CHARS:
        out.append("almost_no_text")
    if (quality.get("alnum_ratio") or 1.0) < MIN_ALNUM_RATIO:
        out.append("not_character_like")
    # Variant disagreement alone is not a doubt. A page rescued from a broken
    # text layer disagrees enormously and is then fine; flagging those would
    # fill the queue with pages that came out well. The spread is kept on the
    # row, and whether a page is actually thin is judged at merge, against the
    # document's own median.
    for w in report.get("warnings", []) + report.get("errors", []):
        kind = w.get("kind")
        if kind == "table_cells_dropped" and (w.get("drop_ratio") or 0) > 0.5:
            out.append("table_mostly_dropped")
        elif kind in ("ocr_empty", "bbox_clamped", "timeout"):
            out.append(kind)
    alt = report.get("table_mode_alternate") or {}
    if alt.get("shapes_agree") is False:
        out.append("table_grid_disputed")
    if report.get("retried"):
        out.append("needed_retry")
    return sorted(set(out))


def _median(values: list) -> float:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    return float(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2)


MAX_INDEX_ENTRIES = 3000   # keeps the index a summary, not a second copy


def content_index(doc) -> dict:
    """What is in the document, without opening the document.

    The document JSON is gzipped and can run to hundreds of megabytes, so a
    question asked across a corpus -- which filings contain water-quality
    result tables, what sections does this one have -- should not require
    reading every one of them. The headings are the filing's structure as
    extracted; the table headers are the handle for finding structured data,
    since a table whose columns read "Parameter | Result | Detection Limit" is
    a dataset whatever the document is called.
    """
    labels: dict = {}
    for item in doc.texts:
        labels[item.label] = labels.get(item.label, 0) + 1

    headings = []
    for item in doc.texts:
        if item.label != "section_header":
            continue
        pages = [pr.page_no for pr in item.prov]
        headings.append({"page": pages[0] if pages else None,
                         "level": getattr(item, "level", None),
                         "text": (item.text or "")[:160]})

    tables = []
    for t in doc.tables:
        pages = [pr.page_no for pr in t.prov]
        cells = t.data.table_cells
        header = " | ".join((c.text or "").strip() for c in cells if c.column_header)
        tables.append({"page": pages[0] if pages else None,
                       "rows": t.data.num_rows, "cols": t.data.num_cols,
                       "header": header[:160]})

    return {
        "labels": dict(sorted(labels.items(), key=lambda kv: -kv[1])),
        "tables": len(tables), "pictures": len(doc.pictures),
        "headings": headings[:MAX_INDEX_ENTRIES],
        "headings_truncated": max(0, len(headings) - MAX_INDEX_ENTRIES),
        "table_index": tables[:MAX_INDEX_ENTRIES],
        "table_index_truncated": max(0, len(tables) - MAX_INDEX_ENTRIES),
    }


def dropped_cells(records: list[dict]) -> int:
    """Total PDF cells the table matcher could not place, across a page."""
    return sum(r.get("cells_dropped", 0) for r in records
               if r.get("kind") == "table_cells_dropped")


def table_text(result) -> int:
    """Characters captured inside tables; the tie-break between table modes."""
    return sum(len(c.text or "")
               for t in result.document.tables for c in t.data.table_cells)


def table_shapes(result) -> list[list[int]]:
    """Row/column counts of each table, in order."""
    return [[t.data.num_rows, t.data.num_cols] for t in result.document.tables]


def result_errors(result) -> list[dict]:
    """docling's own structured defects, which carry an exact page number."""
    out = []
    for err in getattr(result, "errors", None) or []:
        try:
            item = err.model_dump(mode="json")
        except AttributeError:
            item = {"error_message": str(err)}
        item["kind"] = classify_warning(str(item.get("error_message", "")))
        out.append(item)
    return out


# Every package that can change what comes out, and so can change the warning
# counts between runs. docling alone is not enough: the dropped-cell warnings
# come from docling-ibm-models and the empty-OCR ones from rapidocr, both of
# which version independently of docling.
COMPONENT_PACKAGES = (
    "docling", "docling-core", "docling-ibm-models", "docling-parse",
    "rapidocr", "torch", "transformers",
)


def component_versions() -> dict:
    from importlib.metadata import PackageNotFoundError, version

    out = {}
    for pkg in COMPONENT_PACKAGES:
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = None  # absent is itself worth recording
    return out


def ocr_model_files() -> dict:
    """The RapidOCR weights actually on disk.

    These decide whether a region OCRs to text or to nothing, and they can be
    swapped without the rapidocr version moving -- so a version string alone
    does not pin the behaviour behind an ocr_empty warning.
    """
    try:
        import rapidocr

        models = Path(rapidocr.__file__).parent / "models"
        return {f.name: f.stat().st_size for f in sorted(models.glob("*.pth"))}
    except Exception as exc:
        return {"error": f"could not read model dir: {exc}"}


RASTER_SCALE = 3.0    # ~216 dpi: enough for 8pt table text, ~0.5 MB/page
UNMAPPED_RATIO = 0.2  # share of control chars above which a text layer is junk
MIN_TEXT_CHARS = 100  # below this a "text layer" is a scan's stub, not content
TABLE_MODE = "fast"   # tried first; the other mode is used when this one drops
                      # cells. Over 986 pages the fallback fired on 27 and took
                      # cells dropped from 153 to 24


def unmapped_pages(pdf: Path, total: int) -> set[int]:
    """Pages whose text layer is unmapped glyph codes rather than characters.

    A font with no ToUnicode map yields control bytes where letters should be.
    docling cannot tell the difference -- it publishes them as text, and its
    table stage reads the text layer directly, so no OCR setting recovers
    them. Those pages get rasterized; the rest keep their exact text.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    bad = set()
    for i in range(total):
        text = doc[i].get_textpage().get_text_range() or ""
        # A page with no text layer, or only a stub of one, must be rasterized
        # too. Left alone it goes to pdf-aware OCR and yields nothing at all:
        # on a scanned filing three sampled pages scored 0.0% against the
        # reference, and 100%, 99.5% and 88.0% once rasterized. An earlier
        # version skipped these because on a mostly-digital filing the change
        # looked like a wash (84.8% vs 85.5% over six pages); that document
        # had 14 such pages, the scanned one has 399.
        if len(text) < MIN_TEXT_CHARS:
            bad.add(i + 1)
            continue
        ctrl = sum(1 for ch in text if ord(ch) < 32 and ch not in "\r\n\t")
        if ctrl / len(text) > UNMAPPED_RATIO:
            bad.add(i + 1)
    doc.close()
    return bad


def read_outline(pdf: Path) -> list[dict]:
    """The filing's top-level boundaries, e.g. where Attachment 2 starts.

    Kept because page numbering cannot express it and nothing else in the
    output records where one attachment ends and the next begins. Deeper
    levels are dropped: they are mostly "Slide Number N".
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    out = []
    for bm in doc.get_toc():
        if bm.level != 0:
            continue
        try:
            page = bm.get_dest().get_index() + 1
        except Exception:
            page = None
        out.append({"title": bm.get_title(), "page": page})
    doc.close()
    return out


def build_chunk_pdf(pdf: Path, start: int, end: int, bad: set[int], dest: Path,
                    scale: float = None) -> int:
    """One PDF for this chunk: broken pages as images, good pages untouched.

    Rasterizing everything would cost the exact text on pages that were already
    correct; rasterizing nothing leaves a third of this filing as mojibake.
    Mixing them per page keeps both, in a single conversion pass.
    """
    import pypdfium2 as pdfium

    src = pdfium.PdfDocument(str(pdf))
    pages = list(range(start, end + 1))
    to_raster = [n for n in pages if n in bad]

    raster_doc = None
    if to_raster:
        images = [src[n - 1].render(scale=scale or RASTER_SCALE).to_pil().convert("RGB")
                  for n in to_raster]
        buf = io.BytesIO()
        images[0].save(buf, "PDF", resolution=72 * (scale or RASTER_SCALE),
                       save_all=True, append_images=images[1:])
        buf.seek(0)
        raster_doc = pdfium.PdfDocument(buf)
        del images

    out = pdfium.PdfDocument.new()
    for n in pages:  # page order must match the original exactly
        if n in bad:
            out.import_pages(raster_doc, [to_raster.index(n)])
        else:
            out.import_pages(src, [n - 1])
    tmp = dest.with_name(dest.name + ".part")
    out.save(str(tmp))
    tmp.replace(dest)
    return len(to_raster)


def renumber_pages(doc, offset: int) -> None:
    """Shift a chunk's page numbers back to their place in the whole filing.

    The chunk was converted from a rasterized copy holding only its own pages,
    so everything in it is numbered from 1. Without this the merged document
    would report every chunk as starting at page 1.
    """
    if not offset:
        return
    for prov in (pr for item in doc.texts for pr in item.prov):
        prov.page_no += offset
    for group in (doc.tables, doc.pictures):
        for item in group:
            for pr in item.prov:
                pr.page_no += offset
    doc.pages = {n + offset: pg for n, pg in sorted(doc.pages.items())}
    for n, pg in doc.pages.items():
        pg.page_no = n


REGDOCS_URL = "https://apps.cer-rec.gc.ca/REGDOCS/File/Download/{}"


def file_digest(path: Path) -> str:
    """sha256 of the source PDF, so a run can be tied to exact bytes.

    Size alone cannot tell a re-issued filing from the one that was read.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def pdf_provenance(pdf: Path) -> dict:
    """Where the file came from and what produced it.

    The producer matters more than it looks: the failures in this corpus are
    not spread evenly across tools. Recording it per document makes the
    question answerable across a whole corpus -- which producers make files
    whose text layer cannot be trusted -- instead of one filing at a time.

    """
    import pypdfium2 as pdfium

    info: dict = {"sha256": file_digest(pdf), "bytes": pdf.stat().st_size}
    try:
        doc = pdfium.PdfDocument(str(pdf))
        meta = doc.get_metadata_dict() or {}
        info["pdf"] = {
            "version": doc.get_version(),
            "producer": meta.get("Producer") or None,
            "creator": meta.get("Creator") or None,
            "author": meta.get("Author") or None,
            "title": meta.get("Title") or None,
            "created": meta.get("CreationDate") or None,
            "modified": meta.get("ModDate") or None,
        }
        doc.close()
    except Exception as exc:
        info["pdf"] = {"error": str(exc)}

    # A sidecar beside the PDF, written by whatever fetched it. File-local, so
    # it travels with the document; absent for a PDF handed over on its own.
    sidecar = pdf.with_suffix(".metadata.json")
    if sidecar.exists():
        try:
            ext = json.loads(sidecar.read_text())
            filing = {k: ext.get(k) for k in
                      ("document_id", "title", "company", "submitter", "project",
                       "filing_number", "filing_date", "source_url", "sha256")
                      if ext.get(k) is not None}
            if filing:
                info["filing"] = filing
                known = filing.get("sha256")
                if known:
                    info["sha256_matches_sidecar"] = (known == info["sha256"])
        except Exception as exc:
            info["filing"] = {"error": f"unreadable sidecar: {exc}"}
    return info


def verify_regdocs_url(doc_id: str, given: str | None) -> dict:
    """Where this file can be fetched again.

    If the ingest was handed a URL, that is the answer. Otherwise the REGDOCS
    download link is derived from the filename and then checked, because a
    derived link that has never been tried is a guess written down as a fact.

    The check is a one-kilobyte ranged request following redirects. A real
    document ends at a PDF byte range whose redirect carries the filing's
    filename; an unknown id lands on an HTML page instead. Network trouble
    leaves the link recorded as underived rather than failing the ingest.
    """
    from_arg = bool(given and given.lower().startswith(("http://", "https://")))
    if not from_arg and (not doc_id.isdigit() or os.environ.get("NO_URL_CHECK")):
        return {}
    url = given if from_arg else REGDOCS_URL.format(doc_id)
    if os.environ.get("NO_URL_CHECK"):
        return {"source_url": url, "source_url_from": "argument"}
    try:
        out = subprocess.run(
            ["curl", "-sL", "-o", "/dev/null", "-r", "0-999", "--max-time", "45",
             # content_type contains spaces, so the fields need a delimiter
             "-w", "%{http_code}|%{content_type}|%{url_effective}", url],
            capture_output=True, text=True, timeout=60)
        code, ctype, effective = (out.stdout.split("|", 2) + ["", "", ""])[:3]
        resolved = code in ("200", "206") and "text/html" not in ctype
        info = {"source_url": url,
                "source_url_from": "argument" if from_arg else "filename pattern",
                "source_url_verified": resolved}
        if resolved and effective:
            info["source_url_resolved_to"] = effective.strip()
        return info
    except Exception as exc:
        return {"source_url": url,
                "source_url_from": "argument" if from_arg else "filename pattern",
                "source_url_verified": None,
                "source_url_check_error": str(exc)}


META_SCHEMA = 1   # bumped when the shape of the meta changes
# The document JSON is mostly repeated field names and coordinates and gzips to
# about a tenth of its size; the PDF beside it is already compressed and gains
# nothing from it. The meta stays plain text -- it is the file people open, and
# it is small. Set false to write the document uncompressed.
COMPRESS_DOCUMENT = True


def host_environment(device: str) -> dict:
    """The machine and libraries that produced this run.

    OCR is not bit-identical across GPUs, drivers and library versions. When a
    run is compared against one made years later, the first question is what
    else changed, and nothing else in the output answers it.
    """
    import platform

    env = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "device": device,
        "threads": THREADS,
    }
    try:
        import torch

        env["torch"] = torch.__version__
        if torch.cuda.is_available():
            env["gpu"] = torch.cuda.get_device_name(0)
            env["cuda"] = torch.version.cuda
            env["gpu_memory_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / 1024**3, 1)
    except Exception as exc:
        env["torch_probe_error"] = str(exc)
    return env


def page_geometry(pdf: Path, total: int) -> dict:
    """Page sizes and rotations, summarised.

    A page that extracts oddly is often a page that is rotated, or a different
    size from the rest of the filing -- a drawing sheet among letter pages.
    Recording it costs nothing and answers that question without the PDF.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    sizes: dict = {}
    rotations: dict = {}
    for i in range(total):
        page = doc[i]
        w, h = page.get_size()
        key = f"{round(w)}x{round(h)}"
        sizes[key] = sizes.get(key, 0) + 1
        rot = page.get_rotation()
        rotations[str(rot)] = rotations.get(str(rot), 0) + 1
    doc.close()
    return {"sizes": dict(sorted(sizes.items(), key=lambda kv: -kv[1])),
            "rotations": rotations}


def ingest_fingerprint() -> dict:
    """Identify the script that produced a run.

    Every setting here -- raster threshold, OCR mode, chunk size, table mode --
    changes the output, and most of them are constants rather than arguments.
    Recording the file's hash means two runs of the same document can be told
    apart even when the settings block looks the same, and a later reader can
    check whether the script has moved since.
    """
    src = Path(__file__).resolve()
    return {
        "file": src.name,
        "sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "mtime": _dt.datetime.fromtimestamp(
            src.stat().st_mtime, _dt.timezone.utc).isoformat(),
    }


def run_signature() -> dict:
    """Everything that decides what the output looks like.

    Compared against a previous run's meta so a changed setting can never be
    mistaken for a finished one. The device is left out on purpose: cuda and
    cpu produce the same document, only slower.
    """
    return {
        "ingest_sha256": ingest_fingerprint()["sha256"],
        "chunk_pages": CHUNK_PAGES,
        "raster_scale": RASTER_SCALE,
        "unmapped_ratio": UNMAPPED_RATIO,
        "min_text_chars": MIN_TEXT_CHARS,
        "variants": [v["name"] for v in PAGE_VARIANTS],
        "switch_margin": SWITCH_MARGIN,
        "min_page_chars": MIN_PAGE_CHARS,
        "min_alnum_ratio": MIN_ALNUM_RATIO,
        "figure_pass": FIGURE_PASS,
        "table_mode": TABLE_MODE,
        "table_mode_fallback": True,
        "ocr_model_type": "medium",
    }


def describe_drift(old: dict, new: dict) -> list[str]:
    lines = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a != b:
            if key == "ingest_sha256":
                a, b = (str(a)[:8] if a else "?"), (str(b)[:8] if b else "?")
            lines.append(f"  {key}: {a} -> {b}")
    return lines


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    import docling
    import pypdfium2 as pdfium
    from docling_core.types.doc.base import ImageRefMode
    from docling_core.types.doc.document import DoclingDocument

    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    source = args[0] if args else ""
    if not source or source in ("-h", "--help"):
        sys.exit(__doc__)

    pdf = fetch_pdf(source, Path("source"))
    doc_id = pdf.stem[:80].replace(" ", "_")
    out = Path("output") / doc_id
    chunks = out / "chunks"
    chunks.mkdir(parents=True, exist_ok=True)
    final = out / (f"{doc_id}.docling.json.gz" if COMPRESS_DOCUMENT
                   else f"{doc_id}.docling.json")
    signature = run_signature()
    meta_path = out / f"{doc_id}.docling.meta.json"
    if final.exists():
        previous = {}
        if meta_path.exists():
            try:
                previous = json.loads(meta_path.read_text()).get("run_signature") or {}
            except Exception:
                previous = {}
        drift = describe_drift(previous, signature)
        if not drift:
            log(f"{doc_id}: already ingested with these settings -> {final}")
            return
        if not force:
            log(f"{doc_id}: already ingested, but the settings changed:")
            for line in drift:
                log(line)
            log("re-run with --force to overwrite this output")
            sys.exit(2)
        log(f"{doc_id}: settings changed and --force given; overwriting")
        for line in drift:
            log(line)
    total = len(pdfium.PdfDocument(str(pdf)))

    device = pick_device()
    ranges = [(s, min(s + CHUNK_PAGES - 1, total))
              for s in range(1, total + 1, CHUNK_PAGES)]
    log(f"{doc_id}: {total} pages, {len(ranges)} chunks of {CHUNK_PAGES}, device={device}")

    # Pages left by a run with different settings must not be folded into this
    # one: the merged document would mix two configurations with no record.
    stamp = chunks / "run_signature.json"
    if stamp.exists():
        try:
            stale = json.loads(stamp.read_text()) != signature
        except Exception:
            stale = True
    else:
        stale = any(chunks.glob("*.docling.json"))
    if stale:
        dropped = sorted(chunks.glob("*.docling.json"))
        for f in chunks.iterdir():
            f.unlink()
        if dropped:
            log(f"{doc_id}: discarded {len(dropped)} page(s) converted under other "
                f"settings; they will be reconverted")
    write_atomic(stamp, json.dumps(signature, indent=2) + "\n")

    provenance = pdf_provenance(pdf)
    geometry = page_geometry(pdf, total)
    provenance.update(verify_regdocs_url(doc_id, source))
    bad_pages = unmapped_pages(pdf, total)
    outline = read_outline(pdf)
    log(f"{doc_id}: {len(bad_pages)} of {total} pages have an unusable text layer "
        f"-> rasterized; the rest keep their exact text")

    # Two converters, picked per page: rasterized pages have no text layer and
    # need full-page OCR; the rest keep their exact text.
    from docling.datamodel.pipeline_options import OcrMode as _OcrMode
    from docling.datamodel.pipeline_options import TableFormerMode as _TFMode
    # One converter per (staging, ocr model, table mode) actually used. Built
    # once: model loading dominates, conversion does not.
    models = {v["ocr_model"] for v in PAGE_VARIANTS}
    converters = {
        (raster, model, tname): build_converter(
            device,
            _OcrMode.FULL_PAGE if raster else _OcrMode.PDF_AWARE_LAYOUT_REGIONS,
            tmode, model)
        for raster in (True, False)
        for model in models
        for tname, tmode in (("fast", _TFMode.FAST), ("accurate", _TFMode.ACCURATE))
    }
    t0 = time.time()
    for i, (start, end) in enumerate(ranges, 1):
        json_out = chunks / f"pg-{start:04d}-{end:04d}.docling.json"
        warn_out = chunks / f"pg-{start:04d}-{end:04d}.warnings.json"
        if json_out.exists():
            log(f"{i}/{len(ranges)} pages {start}-{end}: already done")
            continue
        t1 = time.time()
        # Staging is not guessed at, it is decided by result. Every variant in
        # PAGE_VARIANTS is run and the best output kept, scored by how many
        # characters were produced weighted by the share that are letters or
        # digits -- which separates real text from mojibake.
        #
        # Validated against an independent extraction on 47 page comparisons:
        # the score picked the closer output every time, including between
        # variants that both looked reasonable. One page scored 77.6% under
        # the first variant and 84.1% under another, a difference no failure
        # check would have noticed.
        #
        # The text-layer thresholds now only order the attempts, so the usual
        # winner is tried first and the log reads sensibly. A threshold tuned
        # on the wrong corpus costs time here, not content.
        first_raster = any(n in bad_pages for n in range(start, end + 1))
        variants = sorted(PAGE_VARIANTS, key=lambda v: v["raster"] != first_raster)
        label = f"{i}/{len(ranges)} pages {start}-{end}"
        attempts, staged_paths = [], {}
        for v in variants:
            path = chunks / f"pg-{start:04d}-{end:04d}.{v['name']}.pdf"
            try:
                res, res_cap, res_n, res_retry = convert_staged(
                    pdf, start, end, v["raster"], converters, path, label,
                    scale=v.get("scale"), ocr_model=v.get("ocr_model", "medium"))
            except Exception as exc:
                log(f"{label}: variant {v['name']} failed ({exc})")
                path.unlink(missing_ok=True)
                continue
            staged_paths[v["name"]] = path
            attempts.append({"variant": v, "result": res, "cap": res_cap,
                             "n_raster": res_n, "retry": res_retry,
                             "quality": page_quality(res)})
        if not attempts:
            raise RuntimeError(f"{label}: every variant failed")

        # The first variant is the incumbent; another takes over only by a
        # clear margin, so near-ties keep the text layer rather than trading it
        # for an OCR pass that scored a fraction higher.
        best = attempts[0]
        for a in attempts[1:]:
            if a["quality"]["score"] > best["quality"]["score"] * (1 + SWITCH_MARGIN):
                best = a
        result, cap = best["result"], best["cap"]
        n_raster, retries = best["n_raster"], best["retry"]
        quality = best["quality"]
        staging_used = best["variant"]["name"]
        staged = staged_paths[staging_used]
        for name, path in staged_paths.items():
            if name != staging_used:
                path.unlink(missing_ok=True)

        staging_scores = {a["variant"]["name"]: a["quality"] for a in attempts}
        restaged = None
        if len(attempts) > 1 and staging_used != variants[0]["name"]:
            firstq = staging_scores.get(variants[0]["name"], {})
            restaged = {"from": variants[0]["name"], "to": staging_used,
                        "scores": staging_scores, "kept": True}
            log(f"{label}: kept variant {staging_used} "
                f"({firstq.get('chars', 0)} chars -> {quality['chars']})")
        for a in attempts:
            a.pop("result", None); a.pop("cap", None)

        # Neither table mode wins everywhere: fast rescues the large forms that
        # accurate collapses to a 1x1 grid, accurate keeps small two-column
        # tables that fast trims. When the first mode reports dropped cells the
        # other one is tried and the better result kept -- "better" being fewer
        # dropped cells, then more text. Checked against an independent
        # extraction on nine pages, that rule picked the closer output every
        # time. Pages that drop nothing are not re-converted.
        table_mode_used, alternate = TABLE_MODE, None
        if dropped_cells(cap.records):
            other = "accurate" if TABLE_MODE == "fast" else "fast"
            with WarningCapture() as cap2:
                result2 = converters[
                    (n_raster > 0, best["variant"].get("ocr_model", "medium"), other)
                ].convert(str(staged))
            if result2.status.value == "success":
                a, b = dropped_cells(cap.records), dropped_cells(cap2.records)
                better = b < a or (b == a and table_text(result2) > table_text(result))
                # Both modes ran on this page, so their table shapes can be
                # compared for free. Agreement is weak evidence the grid is
                # right; disagreement means at least one of them misread it,
                # which no cell-level check can tell you on its own.
                shapes_first, shapes_other = table_shapes(result), table_shapes(result2)
                alternate = {"mode": other, "cells_dropped": b,
                             "kept": bool(better), "first_mode_dropped": a,
                             "shapes": {TABLE_MODE: shapes_first, other: shapes_other},
                             "shapes_agree": shapes_first == shapes_other}
                if better:
                    result, cap, table_mode_used = result2, cap2, other
                    log(f"{i}/{len(ranges)} pages {start}-{end}: {other} kept "
                        f"({b} cells dropped vs {a})")
        renumber_pages(result.document, start - 1)

        # A page that is all drawing and no table has its labels inside the
        # picture, where the layout stage never looks. Only those pages pay
        # for the extra OCR passes.
        figure_words: list[str] = []
        if (FIGURE_PASS and CHUNK_PAGES == 1
                and not result.document.tables and result.document.pictures):
            have = {re.sub(r"[^0-9a-z]", "", w.lower())
                    for item in result.document.texts
                    for w in (item.text or "").split()}
            figure_words = figure_text(pdf, start, have)
            if figure_words:
                log(f"{i}/{len(ranges)} pages {start}-{end}: "
                    f"{len(figure_words)} extra word(s) recovered from figures")
        tmp = json_out.with_name(json_out.name + ".part")
        result.document.save_as_json(tmp, image_mode=ImageRefMode.EMBEDDED)
        tmp.replace(json_out)
        staged.unlink(missing_ok=True)  # one staged page; never accumulate them
        # Sidecar beside the chunk, written after the chunk it describes: a
        # resumed run skips finished chunks, so warnings kept only in memory
        # would vanish and the final meta would under-report the damage.
        write_atomic(warn_out, json.dumps({
            "page_start": start,
            "page_end": end,
            "status": result.status.value,
            "rasterized_pages": n_raster,
            "table_mode": table_mode_used,
            "staging": staging_used,
            "quality": quality,
            "staging_scores": staging_scores,
            "restaged": restaged,
            "figure_text": figure_words,
            "retried": retries,
            "table_mode_alternate": alternate,
            "confidence": confidence_scores(result),
            "elapsed_seconds": round(time.time() - t1, 1),
            "warnings": cap.records,
            "warnings_over_cap": cap.dropped,
            "errors": result_errors(result),
        }, indent=2) + "\n")
        n_warn = len(cap.records) + len(result_errors(result))
        del result
        gc.collect()  # drop this chunk's document before starting the next
        log(f"{i}/{len(ranges)} pages {start}-{end}: {time.time() - t1:.0f}s"
            + (f", {n_warn} warning(s)" if n_warn else ""))

    # Merge the chunks into one document, then drop the per-chunk files.
    log(f"merging {len(ranges)} chunks")
    # The merge has warnings of its own: docling_core clamps out-of-page bboxes
    # while loading and saving, and those fire here rather than in any chunk.
    with WarningCapture() as merge_cap:
        merged = DoclingDocument.concatenate([
            DoclingDocument.load_from_json(chunks / f"pg-{s:04d}-{e:04d}.docling.json")
            for s, e in ranges
        ])
        merged.name = doc_id
        tmp = out / f"{doc_id}.docling.json.part"
        merged.save_as_json(tmp, image_mode=ImageRefMode.EMBEDDED)
    if COMPRESS_DOCUMENT:
        packed = tmp.with_name(tmp.name + ".gz")
        with tmp.open("rb") as src_f, gzip.open(packed, "wb", compresslevel=6) as dst_f:
            shutil.copyfileobj(src_f, dst_f, length=1 << 20)
        tmp.unlink()
        tmp = packed
    tmp.replace(final)
    # A run that switches between compressed and plain output would otherwise
    # leave the previous form sitting beside the new one, and nothing says
    # which is current.
    stale_twin = out / (f"{doc_id}.docling.json" if COMPRESS_DOCUMENT
                        else f"{doc_id}.docling.json.gz")
    if stale_twin.exists():
        stale_twin.unlink()
        log(f"{doc_id}: removed the previous {stale_twin.suffix} output")

    # Gather the per-chunk sidecars before the chunk directory goes away.
    chunk_reports, kind_counts = [], {}
    conf_totals, low_confidence, table_mode_overrides = {}, [], []
    structure_disagreements, figure_pages, retried_pages = [], [], []
    restaged_pages, near_empty_pages, variant_wins = [], [], {}
    pages_report = []
    for start, end in ranges:
        warn_file = chunks / f"pg-{start:04d}-{end:04d}.warnings.json"
        if not warn_file.exists():
            # Chunk finished under an older run that predates this sidecar.
            chunk_reports.append({
                "page_start": start, "page_end": end, "status": "unknown",
                "note": "converted before warning capture existed; not a clean result",
            })
            kind_counts["unrecorded_chunk"] = kind_counts.get("unrecorded_chunk", 0) + 1
            continue
        report = json.loads(warn_file.read_text(encoding="utf-8"))
        # A page converted with the non-default table mode leaves no warning
        # once the better result is kept, so record the swap here or the meta
        # would not show that the page was handled differently.
        pages_report.append(page_report(report))
        won = report.get("staging")
        if won:
            variant_wins[won] = variant_wins.get(won, 0) + 1
        if report.get("restaged"):
            restaged_pages.append({"page_start": report["page_start"],
                                   "page_end": report["page_end"],
                                   **report["restaged"]})
        q = report.get("quality") or {}
        if q and q.get("chars", 0) < MIN_PAGE_CHARS:
            near_empty_pages.append({"page_start": report["page_start"],
                                     "chars": q.get("chars"),
                                     "alnum_ratio": q.get("alnum_ratio")})
        if report.get("retried"):
            retried_pages.append({"page_start": report["page_start"],
                                  "page_end": report["page_end"],
                                  "first_attempt": report["retried"]})
        if report.get("figure_text"):
            figure_pages.append({"page_start": report["page_start"],
                                 "page_end": report["page_end"],
                                 "words": report["figure_text"]})
        alt = report.get("table_mode_alternate") or {}
        if alt and alt.get("shapes_agree") is False:
            structure_disagreements.append({
                "page_start": report["page_start"], "page_end": report["page_end"],
                "kept_mode": report.get("table_mode"),
                "shapes": alt.get("shapes"),
            })
        if report.get("table_mode") and report["table_mode"] != TABLE_MODE:
            alt = report.get("table_mode_alternate") or {}
            table_mode_overrides.append({
                "page_start": report["page_start"], "page_end": report["page_end"],
                "mode": report["table_mode"],
                "cells_dropped": alt.get("cells_dropped"),
                "instead_of": TABLE_MODE,
                "which_dropped": alt.get("first_mode_dropped"),
            })
        conf = report.get("confidence") or {}
        for name, v in conf.items():
            conf_totals.setdefault(name, []).append(v)
        low = {k: v for k, v in conf.items() if v < LOW_CONFIDENCE}
        if low:
            low_confidence.append({"page_start": report["page_start"],
                                   "page_end": report["page_end"], **low})
        for entry in report.get("warnings", []) + report.get("errors", []):
            kind = entry.get("kind", "other")
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if report.get("warnings") or report.get("errors") or report.get("warnings_over_cap"):
            chunk_reports.append(report)  # clean chunks are omitted; counts stay exact

    contents = content_index(merged)
    suspects = suspect_cells(merged)
    suspect_counts = {}
    for f in suspects:
        suspect_counts[f["rule"]] = suspect_counts.get(f["rule"], 0) + 1
    if suspects:
        log(f"{doc_id}: {len(suspects)} suspect table cell(s) flagged for review")

    for entry in merge_cap.records:
        kind_counts[entry["kind"]] = kind_counts.get(entry["kind"], 0) + 1

    # Worst table losses first, so the meta answers "what broke" without a
    # reader having to scan every chunk report.
    losses = []
    for report in chunk_reports:
        for w in report.get("warnings", []):
            if w.get("drop_ratio") is not None:
                losses.append({
                    "drop_ratio": w["drop_ratio"],
                    "cells_dropped": w["cells_dropped"],
                    "cells_total": w["cells_total"],
                    "grid": w.get("grid"),
                    "page_start": report["page_start"],
                    "page_end": report["page_end"],
                })
    losses.sort(key=lambda x: x["drop_ratio"], reverse=True)

    for f in chunks.iterdir():
        f.unlink()
    chunks.rmdir()
    # A page is thin relative to this document, not to a constant: a filing of
    # dense tables and one of sparse cover letters have nothing in common in
    # absolute terms. Pages well under the document's own median yield are
    # worth revisiting even when nothing went visibly wrong.
    median_chars = _median([r["chars"] or 0 for r in pages_report])
    for row in pages_report:
        if median_chars and (row["chars"] or 0) < median_chars * LOW_YIELD_FRACTION:
            row["doubts"] = sorted(set(row["doubts"] + ["thin_for_this_document"]))
    doubt_index: dict = {}
    for row in pages_report:
        for d in row["doubts"]:
            doubt_index.setdefault(d, []).append(row["page"])
    reprocess = {pg for pages in doubt_index.values() for pg in pages}

    page_seconds = sorted(r["seconds"] for r in pages_report if r.get("seconds"))
    write_atomic(out / f"{doc_id}.docling.meta.json", json.dumps({
        "meta_schema": META_SCHEMA,
        "doc_id": doc_id,
        "source": source,
        "pdf_bytes": pdf.stat().st_size,
        "provenance": provenance,
        "page_count": total,
        "chunk_pages": CHUNK_PAGES,
        "docling_version": docling.__version__,
        "ingest": ingest_fingerprint(),
        "host": host_environment(device),
        "argv": sys.argv[1:],
        "geometry": geometry,
        # An index of the document's own contents, so a corpus-wide question
        # does not require opening every document.
        "contents": contents,
        "page_seconds": {
            "min": page_seconds[0] if page_seconds else None,
            "median": _median(page_seconds),
            "max": page_seconds[-1] if page_seconds else None,
        },
        "run_signature": signature,
        # Pin the whole stack, not just docling: warning counts are only
        # comparable across runs when these match.
        "components": component_versions(),
        "ocr_models": ocr_model_files(),
        "ingested_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "elapsed_seconds": round(time.time() - t0, 1),
        # docling's own view of how well each page went. Unlike the warnings,
        # this covers every page, including ones that failed without saying so.
        "confidence_mean": {k: round(sum(v) / len(v), 4)
                            for k, v in sorted(conf_totals.items()) if v},
        "low_confidence_pages": low_confidence,
        # One row per page: what was extracted, how character-like it was, and
        # how far the variants disagreed. Present for every page, so a weak
        # page can be found without a reference extraction to compare against.
        "pages": pages_report,
        # Pages grouped by the kind of doubt, and the same list flattened.
        # This is the handle for a later pass: a new docling release or OCR
        # model can be pointed at exactly the pages a given problem affects,
        # instead of re-converting the corpus.
        "doubts": doubt_index,
        "reprocess": sorted(reprocess),
        "reprocess_pages": len(reprocess),
        "quality": {
            "pages": len(pages_report),
            "variants_agree": sum(1 for r in pages_report if r["variants_agree"]),
            "median_chars": _median([r["chars"] or 0 for r in pages_report]),
            "median_alnum_ratio": _median([r["alnum_ratio"] or 0 for r in pages_report]),
        },
        # Cells that look misread judged against their own column. Not errors
        # by themselves: a review list, shortest-odds first.
        "suspect_cell_counts": suspect_counts,
        "suspect_cell_total": len(suspects),
        "suspect_cells": suspects[:MAX_SUSPECT_CELLS],
        "suspect_cells_truncated": max(0, len(suspects) - MAX_SUSPECT_CELLS),
        # Pages whose first staging produced grossly broken output and were
        # converted again the other way. The thresholds that pick the first
        # staging are a guess; this is the check on that guess.
        "restaged_pages": restaged_pages,
        "variant_wins": variant_wins,
        # Pages that still came out with almost no text after all of the above.
        # Usually genuinely blank; worth a look when they are not.
        "near_empty_pages": near_empty_pages,
        # Pages that failed once and succeeded on a retry. A page here came
        # out fine, but a pipeline that fails intermittently is worth watching.
        "retried_pages": retried_pages,
        # Words read out of drawings, including rotated marginalia. Searchable
        # text with no position: it is not part of the document structure.
        "figure_text": figure_pages,
        "figure_text_pages": len(figure_pages),
        "figure_text_words": sum(len(f["words"]) for f in figure_pages),
        # Pages where the two table modes disagreed about the grid itself.
        # One of them read the table wrongly; the cell text alone cannot say
        # which, so these are worth a look even though a winner was picked.
        "structure_disagreements": structure_disagreements,
        # Pages where the other table mode beat the default and was kept.
        "table_mode_overrides": table_mode_overrides,
        # Where each attachment begins; not recoverable from the pages.
        "outline": outline,
        "rasterized_page_list": sorted(bad_pages),
        "warning_counts": kind_counts,
        # Tables that lost more than half their cells: these are destroyed,
        # not trimmed, and are worth re-extracting by other means.
        "severe_table_losses": [x for x in losses if x["drop_ratio"] > 0.5],
        "table_losses": losses,
        "cells_dropped_total": sum(x["cells_dropped"] for x in losses),
        "warning_total": sum(kind_counts.values()),
        # Only chunks with something to report are listed. An empty list means
        # docling reported nothing -- not that nothing was lost.
        "chunks_with_warnings": chunk_reports,
        # Raised while concatenating and saving, so they belong to no chunk.
        "merge_warnings": merge_cap.records,
        "merge_warnings_over_cap": merge_cap.dropped,
        "settings": {
            "device": device,
            "threads": THREADS,
            "ocr": "rapidocr-torch pp-ocrv6-medium, en, full_page on "
                   "rasterized pages, pdf_aware_layout_regions on the rest",
            "tables": f"tableformer-{TABLE_MODE} first, other mode when cells drop",
            "images_scale": 2.0,
            "headings": "numbering (bookmarks unavailable after staging)",
            "rasterized_pages": len(bad_pages),
            "raster_scale": RASTER_SCALE,
            "raster_policy": "pages whose text layer is unmapped glyph codes",
        },
    }, indent=2) + "\n")
    log(f"INGESTED {doc_id}: {len(ranges)} chunks in {time.time() - t0:.0f}s -> {out}")


if __name__ == "__main__":
    main()
