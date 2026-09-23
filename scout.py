#!/usr/bin/env python3
"""Find REGDOCS filings, record what REGDOCS says about them, and fetch them.

    python scout.py scout --from 2026-01-01 --to 2026-07-31 [--dry-run]
    python scout.py doc 4647200 [4692360 ...] [--dry-run]
    python scout.py missing [--dry-run]
    python scout.py download [--limit N]
    python scout.py seed ../cer-regdocs2/workspace/2_download/files

State is files, not a database. For every PDF document:

    output/<id>/<id>.cer.meta.json   what REGDOCS says about it, when it said it,
                                     and what changed since
    source/<id>.pdf                  the file itself, once downloaded

That record is the only one: the scout writes it, the download step adds the
file's hash to it, and nothing else holds a copy. A record with no PDF in
source/ is a document still to download; a document whose PDF exists is never
downloaded again. Scouting a range that has been scouted before re-reads
REGDOCS's metadata and updates each record where it has changed.

Only PDF documents are recorded. REGDOCS also lists HTML documents, Folders
and Compound Documents; the containers are walked to learn which filings a PDF
belongs to, but none of them gets a record, because nothing downstream reads
them.

This is a port of the REGDOCS logic in cer-regdocs2's stage 1 and 2 scripts,
without their SQLite state. Three things were deliberately left behind:

- **Detail pages.** One request per document, about seven hours for eight
  thousand filings, and they yield a title the search row already gives and a
  `language` field that reads "en" on every filing, French ones included.
- **Raw HTML snapshots.** The records here say what was observed; the pages it
  was observed on are not kept.
- **Concurrency.** Requests are paced at one every 2-4 seconds regardless, the
  same courtesy the original extended to REGDOCS, so parallelism would only
  queue behind the pacer.

The record lives in output/<id>/ rather than beside the PDF so that source/
holds nothing but the documents themselves, and so ingest.py -- which reads a
`<pdf>.metadata.json` sidecar automatically -- never picks it up by accident.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import random
import re
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

CER_SCHEMA = 4
ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "source"
OUT = ROOT / "output"

DOMAIN = "https://apps.cer-rec.gc.ca"
ADVANCED_URL = f"{DOMAIN}/REGDOCS/Search/Advanced"
RESULTS_URL = f"{DOMAIN}/REGDOCS/Search/SearchAdvancedResults"
VIEW_URL = f"{DOMAIN}/REGDOCS/Item/View/{{id}}"
PAGE_SIZE = 200
SORT_OLDEST_FIRST = 21

# Courtesy to a public government service, carried over unchanged.
MIN_DELAY, MAX_DELAY = 2.0, 4.0
MAX_RETRIES, BACKOFF = 4, 2.0
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
CONTAINER_MAX_DEPTH = 20
CONTAINER_MAX_ITEMS = 10_000

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-CA,en;q=0.9,fr-CA;q=0.7,fr;q=0.6",
}
AJAX_HEADERS = {**HEADERS, "X-Requested-With": "XMLHttpRequest"}

ITEM_HREF_RE = re.compile(r"/REGDOCS/(Item/View|File/Download)/(\d+)", re.I)
TOTAL_RE = re.compile(r"Item\(s\)\s*-\s*[\d,]+\s*to\s*[\d,]+\s*out of about\s*([\d,]+)", re.I)
FILING_RE = re.compile(r"Filing:\s*(\S+)", re.I)
WS_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^a-z0-9]+")
DATE_TOKEN_RE = re.compile(r"\b((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
FILING_TITLE_RE = re.compile(r"\b(C\d{4,})(?:-(\d{1,4}))?\b", re.I)
EXHIBIT_RE = re.compile(r"\b(A(?=[0-9A-Z]{5,}\b)(?=[0-9A-Z]*\d)[0-9A-Z]{5,})\b", re.I)
ACTIVITY_RE = re.compile(r"\b([A-Z]{2,8}(?:-[A-Z]{2,8})*[- ]?\d{2,4}(?:-\d{1,4})+)\b", re.I)
REGULATORY_ID_RE = re.compile(
    r"\b((?:AO|GO|GPSO|EPR|MO|RO|XG|GC|GH|OH|MH|RH|OF|OM)[A-Z0-9]*(?:-[A-Z0-9]+)+)\b", re.I)
NO_RESULTS_RE = re.compile(
    r"(?:no\s+(?:items?|results?|records?|documents?)\s+(?:(?:were|are)\s+)?(?:found|available)|"
    r"there\s+(?:are|were)\s+no\s+(?:items?|results?|records?|documents?)|"
    r"no\s+(?:items?|results?|records?|documents?)\s+to\s+display|"
    r"aucun(?:e)?\s+(?:élément|résultat|document)(?:\s+n['’]a\s+été\s+trouvé)?)", re.I)


# --------------------------------------------------------------------------
# small helpers

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(v) -> str:
    return "" if v is None else WS_RE.sub(" ", str(v)).strip()


def slug(v: str) -> str:
    return NON_WORD_RE.sub("-", clean(v).casefold()).strip("-")


def norm_label(label: str) -> str:
    t = clean(label).casefold().rstrip(":")
    t = re.sub(r"[\[\](){}]", " ", t)
    return clean(NON_WORD_RE.sub(" ", t))


def normalize_date(value: str) -> str | None:
    text = clean(value)
    if not text:
        return None
    cand = text.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d/%m/%Y", "%m/%d/%Y",
                "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(cand, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = DATE_TOKEN_RE.search(cand)
    if m:
        try:
            return date(*map(int, m.groups())).isoformat()
        except ValueError:
            return None
    return None


def parse_day(value: str) -> str:
    """A YYYY-MM-DD date, with a day past the end of its month clamped to the last.

    REGDOCS does not reject an impossible date; it silently drops it. An end
    date of 2026-09-31 becomes "until today", and a start date of 2026-02-30
    becomes "since 2002" -- about 550,000 items. So a date is made valid here,
    before it can reach a request, and anything that cannot be made valid is
    refused rather than guessed at.
    """
    parts = clean(value).split("-")
    if (len(parts) != 3 or not all(p.isdigit() for p in parts)
            or len(parts[0]) != 4 or not 1 <= len(parts[1]) <= 2 or not 1 <= len(parts[2]) <= 2):
        raise ValueError(f"{value!r} is not a YYYY-MM-DD date")
    year, month, day = map(int, parts)
    # A short or mistyped year is still a date REGDOCS accepts, and every
    # filing falls after it, so the row-date check below cannot catch it.
    if not 1990 <= year <= date.today().year + 1:
        raise ValueError(f"{value!r}: year {year} is outside REGDOCS's range")
    if not 1 <= month <= 12:
        raise ValueError(f"{value!r}: month must be 1-12")
    if day < 1:
        raise ValueError(f"{value!r}: day must be at least 1")
    return f"{year:04d}-{month:02d}-{min(day, calendar.monthrange(year, month)[1]):02d}"


class RangeIgnored(RuntimeError):
    """REGDOCS returned rows outside the dates it was asked for."""


def is_container_kind(v) -> bool:
    return clean(v).casefold() in {"compound document", "folder"}


def is_paper_only(v) -> bool:
    t = clean(v).casefold()
    return "paper only" in t or "papier seulement" in t


def write_json(path: Path, value) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def title_identifiers(title: str) -> dict[str, list[str]]:
    """Identifiers REGDOCS prints in a title, grouped by type.

    Same patterns and precedence as cer-regdocs2, so records seeded from it and
    records scouted here agree. These come from the title text, not from a
    structured field, and are only as reliable as the filer's naming.
    """
    text = clean(title)
    out: dict[str, list[str]] = defaultdict(list)

    def add(kind: str, value: str) -> None:
        if value not in out[kind]:
            out[kind].append(value)

    f = FILING_TITLE_RE.search(text)
    if f:
        add("filing_number", f.group(1).upper())
        if f.group(2):
            add("filing_sequence", f.group(2))
    for m in EXHIBIT_RE.finditer(text):
        add("exhibit_number", m.group(1).upper())
    seen = {v for vs in out.values() for v in vs}
    for m in REGULATORY_ID_RE.finditer(text):
        v = m.group(1).upper()
        kind = ("regulatory_instrument_number"
                if v.startswith(("AO-", "GO-", "GPSO-", "EPR-", "MO-", "RO-")) else "activity_number")
        if v not in seen:
            seen.add(v)
            add(kind, v)
    for m in ACTIVITY_RE.finditer(text):
        v = clean(m.group(1)).upper().replace(" ", "-")
        if v.startswith("C") and v[1:].replace("-", "").isdigit():
            continue
        kind = ("regulatory_instrument_number"
                if any(p in v for p in ("ORDER", "GPSO", "AO-", "GO-")) else "activity_number")
        if v not in seen:
            seen.add(v)
            add(kind, v)
    padded = f" {text} "
    if re.search(r"(?:^|\s|[-–—_/])(FR|FRENCH|FRANÇAIS)(?:\s|[-–—_/]|$)", padded, re.I):
        add("language_marker", "fr")
    elif re.search(r"(?:^|\s|[-–—_/])(EN|ENGLISH|ANGLAIS)(?:\s|[-–—_/]|$)", padded, re.I):
        add("language_marker", "en")
    return dict(out)


# --------------------------------------------------------------------------
# HTTP

class Http:
    """One request at a time, 2-4 s apart, retried, honouring Retry-After."""

    def __init__(self) -> None:
        self.client = httpx.Client(cookies={"RDI-NumberOfRecords": str(PAGE_SIZE)},
                                   follow_redirects=True,
                                   timeout=httpx.Timeout(60.0, connect=30.0))
        self.next_at = 0.0
        self.requests = 0

    def _pace(self) -> None:
        wait = self.next_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.next_at = time.monotonic() + random.uniform(MIN_DELAY, MAX_DELAY)

    @staticmethod
    def _retry_after(headers) -> float | None:
        raw = clean(headers.get("retry-after"))
        return float(raw) if raw.isdigit() else None

    def get(self, url: str, *, params=None, ajax=False, referer=None):
        """-> (ok, text, final_url, error)"""
        headers = dict(AJAX_HEADERS if ajax else HEADERS)
        if referer:
            headers["Referer"] = referer
        error = "unknown"
        for attempt in range(1, MAX_RETRIES + 2):
            self._pace()
            self.requests += 1
            retryable = True
            try:
                r = self.client.get(url, params=params or {}, headers=headers)
                if r.status_code == 200:
                    return True, r.text, str(r.url), None
                error = f"HTTP {r.status_code}"
                retryable = r.status_code in RETRYABLE
                ra = self._retry_after(r.headers)
                if ra:
                    self.next_at = max(self.next_at, time.monotonic() + ra)
            except httpx.RequestError as exc:
                error = f"{type(exc).__name__}: {exc}"
            if not retryable or attempt > MAX_RETRIES:
                break
            time.sleep(BACKOFF ** attempt + random.uniform(0, 1))
        return False, None, None, error

    def close(self) -> None:
        self.client.close()


# --------------------------------------------------------------------------
# REGDOCS page parsing (ported from cer-regdocs2 stage 1)

@dataclass
class Row:
    document_id: str
    name: str
    url: str
    is_file: bool
    date: str | None
    submitter: str
    kind: str | None = None
    filing_number: str | None = None
    filing_id: str | None = None
    company: str | None = None
    company_id: str | None = None
    project: str | None = None
    project_id: str | None = None
    synthetic: bool = False


def parse_total(html: str) -> int | None:
    m = TOTAL_RE.search(html)
    return int(m.group(1).replace(",", "")) if m else None


def recognized(html: str) -> bool:
    """Reject HTTP-200 maintenance or error pages posing as results."""
    if not clean(html):
        return False
    soup = BeautifulSoup(html, "lxml")
    if soup.find("tbody") is not None or parse_total(html) is not None:
        return True
    return bool(NO_RESULTS_RE.search(clean(soup.get_text(" ", strip=True))))


def explicit_empty(html: str) -> bool:
    return bool(clean(html)) and bool(
        NO_RESULTS_RE.search(clean(BeautifulSoup(html, "lxml").get_text(" ", strip=True))))


def _nearby_label(anchor: Tag) -> str:
    parent = anchor.parent
    if not isinstance(parent, Tag):
        return ""
    prev = parent.find_previous_sibling()
    if isinstance(prev, Tag):
        t = clean(prev.get_text(" ", strip=True)).rstrip(":")
        if 0 < len(t) <= 80:
            return t
    row = parent.find_parent(class_=re.compile(r"\brow\b", re.I))
    if isinstance(row, Tag):
        for c in row.find_all(class_=re.compile(r"label|field|name|header", re.I), limit=3):
            t = clean(c.get_text(" ", strip=True)).rstrip(":")
            if t and anchor not in c.descendants and len(t) <= 80:
                return t
    return ""


def parse_rows(html: str, *, container_id: str | None = None) -> list[Row]:
    """Result rows, keyed by REGDOCS item id.

    Inside a container, paper-only rows and rows sharing a placeholder id get a
    synthetic id instead, as in the original, so distinct rows cannot collapse
    onto one document. Synthetic rows are never files and are never written
    to source/.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[Row] = []
    for tr in (tr for tb in soup.find_all("tbody") for tr in tb.find_all("tr")):
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        summary = cells[0].find("summary")
        primary = summary if isinstance(summary, Tag) else cells[0]
        link = match = None
        for a in primary.find_all("a", href=True):
            m = ITEM_HREF_RE.search(a.get("href", ""))
            if m:
                link, match = a, m
                break
        if link is None:
            continue
        row = Row(document_id=match.group(2),
                  name=clean(link.get_text(" ", strip=True)),
                  url=urljoin(DOMAIN, link["href"]),
                  is_file=match.group(1).casefold() == "file/download",
                  date=normalize_date(cells[1].get_text(" ", strip=True)),
                  submitter=clean(cells[2].get_text(" ", strip=True)))
        for icon in primary.find_all(["i", "span", "img"]):
            t = clean(icon.get("title") or icon.get("alt"))
            if t and ("document" in t.casefold() or t.casefold() in {"folder", "compound document"}):
                row.kind = t
                break
        if not row.kind and re.search(r"(?:^|[-–— ])HTML(?:[-–— ]|$)", row.name, re.I):
            row.kind = "Html Document"
        details = cells[0].find("details")
        if isinstance(details, Tag):
            for a in details.find_all("a", href=True):
                if a is link:
                    continue
                ref = ITEM_HREF_RE.search(a["href"])
                if not ref:
                    continue
                text = clean(a.get_text(" ", strip=True))
                fm = FILING_RE.search(text)
                label = norm_label(_nearby_label(a))
                if fm:
                    row.filing_number, row.filing_id = fm.group(1), ref.group(2)
                elif label == "company":
                    row.company, row.company_id = text, ref.group(2)
                elif label == "project":
                    row.project, row.project_id = text, ref.group(2)
        out.append(row)

    if container_id and out:
        counts: dict[str, int] = defaultdict(int)
        for r in out:
            counts[r.document_id] += 1
        for r in out:
            if counts[r.document_id] > 1 or is_paper_only(f"{r.name} {r.kind or ''}"):
                r.synthetic, r.is_file = True, False
                r.document_id = f"row:{container_id}:{hashlib.sha256(r.name.encode()).hexdigest()[:12]}"
    return out


def container_endpoint(html: str, page_url: str, item_id: str) -> str | None:
    """The member-list URL a container page declares, and nothing guessed."""
    soup = BeautifulSoup(html, "lxml")
    cands = []
    sec = soup.find("section", id="section-items")
    if isinstance(sec, Tag):
        cands += [clean(sec.get(a)) for a in ("data-ajax-replace", "data-ajax-append", "data-url")
                  if clean(sec.get(a))]
    if not cands:
        cands += [clean(e.get("data-ajax-replace")) for e in soup.find_all(attrs={"data-ajax-replace": True})
                  if "/REGDOCS/Item/LoadResult/" in clean(e.get("data-ajax-replace"))]
    want = f"/REGDOCS/Item/LoadResult/{item_id}".casefold()
    for c in cands:
        u = urljoin(page_url, c)
        if want in u.casefold():
            return u
    return None


def next_container_page(html: str, current: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a"):
        rel = " ".join(a.get("rel", [])).casefold() if a.get("rel") else ""
        text = clean(a.get_text(" ", strip=True)).casefold()
        title = clean(a.get("title") or a.get("aria-label")).casefold()
        cls = " ".join(a.get("class", [])).casefold()
        if not ("next" in rel or "next" in title or "next" in cls
                or text in {"next", "next page", ">", "»", "›"}):
            continue
        for attr in ("href", "data-ajax-url", "data-url"):
            t = clean(a.get(attr))
            if t and t != "#":
                return urljoin(current, t)
        m = re.search(r"['\"]([^'\"]*?/REGDOCS/Item/LoadResult/[^'\"]+)['\"]", clean(a.get("onclick")))
        if m:
            return urljoin(current, m.group(1))
    return None


def facet_catalog(html: str) -> dict[str, dict[str, str]]:
    """Every facet category Advanced Search offers, with its values."""
    soup = BeautifulSoup(html, "lxml")
    cat: dict[str, dict[str, str]] = {}
    for lab in soup.find_all("label", attrs={"for": re.compile(r"^selectFilter\d+$")}):
        name = clean(lab.get_text(" ", strip=True))
        sel = soup.find("select", id=lab.get("for"))
        if not name or not isinstance(sel, Tag):
            continue
        opts = {clean(o.get("value")): clean(o.get_text(" ", strip=True))
                for o in sel.find_all("option")
                if clean(o.get("value")) and clean(o.get_text(" ", strip=True))}
        if opts:
            cat[name] = opts
    return cat


# --------------------------------------------------------------------------
# crawling

def crawl_search(http: Http, params: dict) -> tuple[dict[str, Row], bool]:
    """Every row for one search. Complete only if no page failed.

    REGDOCS labels its total "about", so it is used to plan pages, not to judge
    completeness; a final probe past the last full page catches any overrun.

    Every row's date is checked against the range asked for. A row outside it
    means REGDOCS ignored the range -- which it does silently for a date it
    cannot parse -- and the search stops at once rather than walking the whole
    archive.
    """
    base = {**params, "srt": SORT_OLDEST_FIRST}
    lo, hi = params.get("sd"), params.get("ed")
    rows: dict[str, Row] = {}

    def page(offset: int):
        ok, html, _, _ = http.get(RESULTS_URL, params={**base, "sr": offset}, ajax=True)
        if ok and not recognized(html):
            ok = False
        got = parse_rows(html) if ok else []
        for r in got:
            if lo and hi and r.date and not lo <= r.date <= hi:
                raise RangeIgnored(f"asked for {lo}..{hi}, REGDOCS returned a row dated "
                                   f"{r.date} (total about {parse_total(html or '')}); "
                                   f"it did not apply the date range")
        return ok, got, html

    ok, first, html = page(1)
    if not ok:
        return {}, False
    for r in first:
        rows.setdefault(r.document_id, r)
    total = parse_total(html or "")
    if not first or len(first) < PAGE_SIZE:
        return rows, True
    last_count, offset, failed = len(first), 1, False
    for offset in range(1 + PAGE_SIZE, (total or 0) + 1, PAGE_SIZE):
        ok, got, _ = page(offset)
        if not ok:
            failed = True
            continue
        last_count = len(got)
        for r in got:
            rows.setdefault(r.document_id, r)
    nxt = offset + PAGE_SIZE
    while not failed and last_count == PAGE_SIZE:
        ok, got, _ = page(nxt)
        if not ok:
            failed = True
            break
        before = len(rows)
        for r in got:
            rows.setdefault(r.document_id, r)
        last_count = len(got)
        if not got or len(rows) == before:
            break
        nxt += PAGE_SIZE
    return rows, not failed


def read_container(http: Http, box_id: str) -> tuple[dict[str, Row], bool, str | None]:
    """One container's members. -> (members, complete, error)

    Membership counts as complete only when the parsed rows reach REGDOCS's own
    total or it explicitly says the container is empty; anything less is
    incomplete, so it cannot be used to remove a membership.
    """
    shell_url = VIEW_URL.format(id=box_id)
    ok, shell, final, err = http.get(shell_url)
    if not ok:
        return {}, False, f"shell failed ({err})"
    endpoint = container_endpoint(shell, final or shell_url, box_id)
    if not endpoint:
        return {}, False, "no member-list endpoint declared"
    members: dict[str, Row] = {}
    total, empty, failed, url, visited = None, False, False, endpoint, set()
    while url and url not in visited:
        visited.add(url)
        ok, frag, ffinal, err = http.get(url, ajax=True, referer=final or shell_url)
        if not ok or not recognized(frag):
            failed = True
            break
        t = parse_total(frag)
        if t is not None:
            total = max(total or 0, t)
        got = parse_rows(frag, container_id=box_id)
        if not got and explicit_empty(frag):
            empty = True
        for m in got:
            if m.document_id != box_id:
                members.setdefault(m.document_id, m)
        nxt = next_container_page(frag, ffinal or url)
        url = nxt if nxt and nxt not in visited else None
    if total is None and not members and empty:
        total = 0
    return members, (not failed and total is not None and len(members) >= total), None


def expand_containers(http: Http, rows: dict[str, Row], log) -> dict[str, tuple[Row, set[str], bool]]:
    """Walk Compound Documents and Folders to their members, nested ones included.

    Returns container id -> (container row, member ids, complete).
    """
    seeds = [r for r in rows.values() if is_container_kind(r.kind)]
    queue = deque((r, 0) for r in seeds)
    queued = {r.document_id for r in seeds}
    seen: set[str] = set()
    result: dict[str, tuple[Row, set[str], bool]] = {}
    while queue and len(seen) < CONTAINER_MAX_ITEMS:
        box, depth = queue.popleft()
        if box.document_id in seen:
            continue
        seen.add(box.document_id)
        members, complete, err = read_container(http, box.document_id)
        result[box.document_id] = (box, set(members), complete)
        if err:
            log(f"  container {box.document_id}: {err}")
            continue
        for m in members.values():
            rows.setdefault(m.document_id, m)
            if (is_container_kind(m.kind) and m.document_id not in queued
                    and depth + 1 <= CONTAINER_MAX_DEPTH):
                queue.append((m, depth + 1))
                queued.add(m.document_id)
        log(f"  container {box.document_id}: {len(members)} member(s), "
            f"{'complete' if complete else 'INCOMPLETE'}")
    return result


def breadcrumb_parent(html: str, doc_id: str) -> str | None:
    """The item a document's page names as its immediate parent -- its filing.

    A REGDOCS page's breadcrumb runs from the site root through commodity,
    company and project down to the filing the document sits in, each an
    Item/View link. The last of those that is not the document itself is the
    filing.
    """
    crumb = BeautifulSoup(html, "lxml").select_one("ol.breadcrumb")
    if not isinstance(crumb, Tag):
        return None
    ids = [m.group(2) for a in crumb.find_all("a", href=True)
           if (m := ITEM_HREF_RE.search(a["href"])) and m.group(2) != doc_id]
    return ids[-1] if ids else None


def find_date(http: Http, doc_id: str) -> tuple[str | None, str]:
    """A document's filing date, found without knowing it in advance.

    The scout searches by date, and a document's own page does not state its
    date -- the only date on it is the project's. So the date is read from the
    document's row in its filing's member list, found through the breadcrumb.
    -> (date or None, how it was found or why not)
    """
    ok, html, _, err = http.get(VIEW_URL.format(id=doc_id))
    if not ok:
        return None, f"its page is unavailable ({err})"
    parent = breadcrumb_parent(html, doc_id)
    if not parent:
        return None, "its page names no filing"
    members, _, err = read_container(http, parent)
    if err:
        return None, f"its filing {parent}: {err}"
    row = members.get(doc_id)
    if row is None or not row.date:
        return None, f"its filing {parent} does not list it with a date"
    if not is_pdf_kind(row.kind):
        return None, f"it is {row.kind or 'of unknown kind'}, not a PDF"
    return row.date, f"from filing {parent}"


def scout_facets(http: Http, start: str, end: str, log):
    """doc id -> {category: [labels]}, plus which categories finished cleanly."""
    ok, html, _, err = http.get(ADVANCED_URL)
    if not ok:
        log(f"facet catalog unavailable ({err}); facets left unchanged")
        return {}, {}
    catalog = facet_catalog(html)
    log(f"facets: {len(catalog)} categories, {sum(map(len, catalog.values()))} values")
    found: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    complete: dict[str, bool] = {}
    for category, options in catalog.items():
        complete[category] = True
        for filter_id, label in options.items():
            got, ok = crawl_search(http, {"sd": start, "ed": end, "rds": filter_id})
            if not ok:
                complete[category] = False
                log(f"  facet {category} = {label}: incomplete")
            for doc_id in got:
                if label not in found[doc_id][category]:
                    found[doc_id][category].append(label)
    return found, complete


# --------------------------------------------------------------------------
# records

def record_from_row(row: Row) -> dict:
    return {
        "title": row.name,
        "date": row.date,
        "submitter": row.submitter or None,
        "kind": row.kind,
        "url": row.url,
        "view_url": VIEW_URL.format(id=row.document_id),
        "filing": {"number": row.filing_number,
                   "id": int(row.filing_id) if row.filing_id else None},
        "company": {"name": row.company,
                    "id": int(row.company_id) if row.company_id else None},
        "project": {"name": row.project,
                    "id": int(row.project_id) if row.project_id else None},
        "identifiers": title_identifiers(row.name),
    }


def merge(old: dict | None, doc_id: str, obs: dict, facets: dict, facets_done: dict[str, bool],
          in_range: bool, containers_seen: list[dict], containers_cleared: set[str],
          when: str) -> tuple[dict, list[dict]]:
    """Fold one observation into a record. -> (record, changes)

    The rules exist so that a scrape that partly failed cannot erase anything:

    - A field is only ever replaced by a non-empty value. A row missing its
      company does not blank a company recorded earlier.
    - A facet category is replaced outright only if every search in it
      finished AND the document was inside the scouted date range -- facet
      searches are date-bounded, so a document reached only through a
      container would otherwise lose every facet. Otherwise values only grow.
    - A container membership is dropped only when that container was fully
      traversed this time and the document was not in it.
    """
    rec = dict(old) if old else {
        "cer_schema": CER_SCHEMA, "doc_id": doc_id,
        "first_seen_at": when, "download": None, "changes": [],
    }
    changes: list[dict] = []

    def note(field: str, before, after) -> None:
        if before != after:
            changes.append({"field": field, "old": before, "new": after, "observed_at": when})

    for key, value in obs.items():
        if isinstance(value, dict) and key != "identifiers":
            cur = dict(rec.get(key) or {})
            for sub, v in value.items():
                if v not in (None, "", [], {}):
                    note(f"{key}.{sub}", cur.get(sub), v)
                    cur[sub] = v
            rec[key] = cur
        elif value not in (None, "", [], {}):
            note(key, rec.get(key), value)
            rec[key] = value

    cur_f = {k: list(v) for k, v in (rec.get("facets") or {}).items()}
    done_f = dict(rec.get("facets_complete") or {})
    for category, finished in facets_done.items():
        new = list(facets.get(category, []))
        before = cur_f.get(category, [])
        if finished and in_range:
            after = new
        else:
            after = before + [v for v in new if v not in before]
        note(f"facets.{category}", before, after)
        cur_f[category] = after
        done_f[category] = bool(finished and in_range) or done_f.get(category, False)
    if facets_done:
        rec["facets"], rec["facets_complete"] = cur_f, done_f

    cur_c = [c for c in (rec.get("containers") or []) if c.get("id") not in containers_cleared]
    have = {c.get("id") for c in cur_c}
    cur_c += [c for c in containers_seen if c["id"] not in have]
    note("containers", rec.get("containers") or [], cur_c)
    rec["containers"] = cur_c

    rec["last_seen_at"] = when
    if old is None:
        # A first sighting is a creation, not a change.
        changes = []
    rec["changes"] = (rec.get("changes") or []) + changes
    return rec, changes


def record_path(doc_id: str) -> Path:
    return OUT / doc_id / f"{doc_id}.cer.meta.json"


def read_record(doc_id: str) -> dict | None:
    """The existing record, or None. A file in an older layout does not count:
    it carries no history, so it is replaced rather than merged into."""
    path = record_path(doc_id)
    if not path.exists():
        return None
    rec = json.loads(path.read_text())
    return rec if rec.get("cer_schema", 0) >= CER_SCHEMA else None


def is_pdf_kind(kind) -> bool:
    return "pdf" in clean(kind).casefold()


# --------------------------------------------------------------------------
# commands

def cmd_scout(args) -> int:
    try:
        start, end = parse_day(args.date_from), parse_day(args.date_to)
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2
    for given, used, flag in ((args.date_from, start, "--from"), (args.date_to, end, "--to")):
        if clean(given) != used:
            print(f"{flag} {given} adjusted to {used} (the last day of that month)")
    if start > end:
        print(f"refusing: --from {start} is after --to {end}")
        return 2
    http = Http()
    try:
        code, _ = scout_range(http, start, end, only=None, dry_run=args.dry_run, log=print)
    finally:
        http.close()
    return code


def scout_range(http: Http, start: str, end: str, *, only: set[str] | None,
                dry_run: bool, log) -> tuple[int, set[str]]:
    """Scout one date range and fold what it finds into the records.

    With `only`, the whole range is still read -- facets and containers can
    only be learned that way -- but records are written for those documents
    alone. -> (exit code, ids recorded)
    """
    when = now()
    written: set[str] = set()
    try:
        log(f"scouting {start} .. {end}")
        base, base_ok = crawl_search(http, {"sd": start, "ed": end})
        log(f"base search: {len(base)} item(s){'' if base_ok else ' -- INCOMPLETE'}")
        in_range = set(base)
        rows = dict(base)
        boxes = expand_containers(http, rows, log) if any(
            is_container_kind(r.kind) for r in base.values()) else {}
        facets, facets_done = scout_facets(http, start, end, log)
    except RangeIgnored as exc:
        # Nothing has been written yet; stopping here leaves every record as it was.
        log(f"STOPPED: {exc}")
        log("nothing written")
        return 2, written
    if not base_ok:
        # An incomplete base search is still worth recording, but it says less.
        log("base search incomplete: records are updated, nothing is removed")

    member_of: dict[str, list[dict]] = defaultdict(list)
    cleared_for: dict[str, set[str]] = defaultdict(set)
    for box_id, (box, members, complete) in boxes.items():
        ref = {"id": box_id, "kind": box.kind, "title": box.name}
        for m in members:
            member_of[m].append(ref)
        if complete:
            for doc_id in rows:
                if doc_id not in members:
                    cleared_for[doc_id].add(box_id)

    tally = defaultdict(int)
    changed_fields = defaultdict(int)
    for doc_id, row in sorted(rows.items()):
        if not row.is_file or row.synthetic or not doc_id.isdigit():
            tally["skipped: container or paper-only"] += 1
            continue
        if not is_pdf_kind(row.kind):
            tally[f"skipped: {row.kind or 'unknown kind'}"] += 1
            continue
        if only is not None and doc_id not in only:
            tally["skipped: not requested"] += 1
            continue
        path = record_path(doc_id)
        old = read_record(doc_id)
        rec, changes = merge(old, doc_id, record_from_row(row), facets.get(doc_id, {}),
                             facets_done, doc_id in in_range, member_of.get(doc_id, []),
                             cleared_for.get(doc_id, set()), when)
        if old is None:
            tally["new"] += 1
        elif changes:
            tally["updated"] += 1
            for c in changes:
                changed_fields[c["field"]] += 1
        else:
            tally["unchanged"] += 1
        written.add(doc_id)
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, rec)
    log(f"\n{http.requests} request(s) so far")
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        log(f"  {k:>44}: {v}")
    if changed_fields:
        log("  fields that changed:")
        for k, v in sorted(changed_fields.items(), key=lambda kv: -kv[1]):
            log(f"    {k:>40}: {v}")
    if dry_run:
        log("\ndry run; nothing written")
    return (0 if base_ok else 1), written


def record_documents(ids: list[str], dry_run: bool) -> int:
    """Give specific documents a record: find each one's date, scout those days.

    Documents filed on the same day share one scout. A document whose date
    cannot be found is reported, not guessed at.
    """
    bad = [i for i in ids if not i.isdigit()]
    if bad:
        print(f"refusing: not REGDOCS document ids: {', '.join(bad)}")
        return 2
    http = Http()
    by_date: dict[str, list[str]] = defaultdict(list)
    unplaced: dict[str, str] = {}
    recorded: set[str] = set()
    code = 0
    try:
        for doc_id in ids:
            day, how = find_date(http, doc_id)
            print(f"  {doc_id}: {day or 'NOT PLACED'} -- {how}")
            if day:
                by_date[day].append(doc_id)
            else:
                unplaced[doc_id] = how
        for day, group in sorted(by_date.items()):
            print(f"\n{len(group)} document(s) filed {day}")
            c, got = scout_range(http, day, day, only=set(group), dry_run=dry_run, log=print)
            recorded |= got
            code = max(code, c)
    finally:
        http.close()
    missed = [i for g in by_date.values() for i in g if i not in recorded]
    print(f"\nrecorded {len(recorded)} of {len(ids)}")
    for doc_id, why in unplaced.items():
        print(f"  {doc_id}: not placed -- {why}; scout a date range that includes it")
    for doc_id in missed:
        print(f"  {doc_id}: not in its day's search results")
    return max(code, 1 if (unplaced or missed) else 0)


def cmd_doc(args) -> int:
    return record_documents(args.ids, args.dry_run)


def cmd_missing(args) -> int:
    """Every PDF in source/ that has no record, e.g. one fetched with ingest.py <url>."""
    ids = sorted(p.stem for p in SOURCE.glob("*.pdf") if read_record(p.stem) is None)
    print(f"{len(ids)} PDF(s) in source/ without a record")
    if not ids:
        return 0
    if args.limit:
        ids = ids[:args.limit]
    return record_documents(ids, args.dry_run)


def sniff(first: bytes) -> tuple[str, str]:
    if first.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    if first.startswith(b"PK\x03\x04"):
        return ".zip", "application/zip"
    if first.startswith(b"\x89PNG"):
        return ".png", "image/png"
    if first.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if first.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif", "image/tiff"
    low = first.lstrip()[:1024].lower()
    if low.startswith((b"<!doctype html", b"<html")) or b"<html" in low or b"<body" in low:
        return ".html", "text/html"
    return ".bin", "application/octet-stream"


def cmd_download(args) -> int:
    """Fetch every recorded PDF not already in source/. Existing files are left alone."""
    todo = []
    for path in sorted(OUT.glob("*/*.cer.meta.json")):
        rec = json.loads(path.read_text())
        doc_id = rec.get("doc_id")
        if (rec.get("cer_schema", 0) >= CER_SCHEMA and is_pdf_kind(rec.get("kind"))
                and "/File/Download/" in (rec.get("url") or "")
                and not (SOURCE / f"{doc_id}.pdf").exists()):
            todo.append((path, rec))
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} file(s) to download")
    http = Http()
    ok_n = fail_n = 0
    try:
        for path, rec in todo:
            doc_id = rec["doc_id"]
            SOURCE.mkdir(exist_ok=True)
            tmp = SOURCE / f"{doc_id}.pdf.part"
            error = None
            for attempt in range(1, MAX_RETRIES + 2):
                http._pace()
                http.requests += 1
                try:
                    h = hashlib.sha256()
                    first = b""
                    with http.client.stream("GET", rec["url"], headers=HEADERS) as r:
                        if r.status_code != 200:
                            raise httpx.HTTPStatusError(f"HTTP {r.status_code}",
                                                        request=r.request, response=r)
                        with open(tmp, "wb") as fh:
                            for chunk in r.iter_bytes(1 << 20):
                                if len(first) < 4096:
                                    first += chunk[:4096 - len(first)]
                                h.update(chunk)
                                fh.write(chunk)
                        final_url, ctype = str(r.url), r.headers.get("content-type", "")
                    ext, mime = sniff(first)
                    # REGDOCS sometimes answers a file link with an HTML page.
                    # For something catalogued as a PDF that is a failure to
                    # retry, not a document to keep.
                    if ext != ".pdf":
                        raise ValueError(f"expected a PDF, got {mime}")
                    SOURCE.mkdir(exist_ok=True)
                    dest = SOURCE / f"{doc_id}.pdf"
                    tmp.replace(dest)
                    rec["download"] = {"sha256": h.hexdigest(), "size_bytes": dest.stat().st_size,
                                       "content_type": mime, "extension": ext,
                                       "resolved_url": final_url, "downloaded_at": now(),
                                       "sha256_matches_file": True}
                    write_json(path, rec)
                    error = None
                    break
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    tmp.unlink(missing_ok=True)
                    if attempt > MAX_RETRIES:
                        break
                    time.sleep(BACKOFF ** attempt + random.uniform(0, 1))
            if error:
                fail_n += 1
                print(f"  {doc_id}: FAILED {error}")
            else:
                ok_n += 1
                print(f"  {doc_id}: {rec['download']['size_bytes']:,} bytes")
    finally:
        http.close()
    print(f"\ndownloaded {ok_n}, failed {fail_n}")
    return 1 if fail_n else 0


def cmd_seed(args) -> int:
    """Build scout records from cer-regdocs2's download sidecars.

    A one-off, so documents already held have a record to compare the next
    scout against. Only documents whose PDF is in source/ are seeded, and
    only fields a scout here can also observe are carried over -- a field
    the scout cannot see would otherwise read as removed on the first run.
    The PDF is hashed against the sidecar's sha256 as it is seeded.
    """
    src = Path(args.sidecars)
    tally = defaultdict(int)
    for pdf in sorted(SOURCE.glob("*.pdf")):
        doc_id = pdf.stem
        side_path = src / f"{doc_id}.metadata.json"
        dest = record_path(doc_id)
        if read_record(doc_id) is not None and not args.force:
            tally["already seeded"] += 1
            continue
        if not side_path.exists():
            tally["no sidecar"] += 1
            continue
        d = json.loads(side_path.read_text())
        m = d.get("metadata") or {}
        col = (m.get("collection") or {}).get("facets") or {}
        dl = (m.get("download") or {})
        num = lambda v: int(v) if isinstance(v, (int, str)) and str(v).isdigit() else None
        h = hashlib.sha256()
        with open(pdf, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        rec = {
            "cer_schema": CER_SCHEMA,
            "doc_id": doc_id,
            # The search row's name, not the detail page's title: this scout
            # never fetches detail pages, and seeding from them would make
            # every title look changed on the first run.
            "title": m.get("name") or d.get("title"),
            "date": normalize_date(m.get("date") or d.get("filing_date") or ""),
            "submitter": m.get("submitter") or d.get("submitter"),
            "kind": m.get("kind"),
            "url": m.get("url") or d.get("source_url"),
            "view_url": VIEW_URL.format(id=doc_id),
            "filing": {"number": m.get("filing_number"), "id": num(m.get("filing_id"))},
            "company": {"name": m.get("company"), "id": num(m.get("company_id"))},
            "project": {"name": m.get("project"), "id": num(m.get("project_id"))},
            "identifiers": title_identifiers(m.get("name") or d.get("title") or ""),
            "facets": {k: list(v or []) for k, v in (m.get("facets") or {}).items()},
            "facets_complete": {k: (v or {}).get("status") == "SUCCEEDED" for k, v in col.items()},
            "containers": [{"id": c.get("container_id"), "kind": c.get("container_kind"),
                            "title": c.get("container_title")}
                           for c in (m.get("container_memberships") or [])],
            "first_seen_at": (d.get("pipeline") or {}).get("first_seen_at"),
            "last_seen_at": m.get("scraped_at"),
            "download": {"sha256": d.get("sha256"), "size_bytes": m.get("size_bytes"),
                         "content_type": d.get("content_type"), "extension": d.get("extension"),
                         "resolved_url": dl.get("resolved_url") or m.get("resolved_url"),
                         "downloaded_at": dl.get("downloaded_at") or m.get("downloaded_at"),
                         "sha256_matches_file": h.hexdigest() == d.get("sha256")},
            "changes": [],
            "seeded_from": f"cer-regdocs2 sidecar ({d.get('schema')} v{d.get('schema_version')})",
        }
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_json(dest, rec)
        tally["seeded" if rec["download"]["sha256_matches_file"] else "seeded (sha256 MISMATCH)"] += 1
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:>16}: {v}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scout", help="find filings in a date range and update their records")
    s.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    s.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD")
    s.add_argument("--dry-run", action="store_true", help="report changes without writing")
    o = sub.add_parser("doc", help="record specific documents by id")
    o.add_argument("ids", nargs="+")
    o.add_argument("--dry-run", action="store_true")
    n = sub.add_parser("missing", help="record every PDF in source/ that has no record")
    n.add_argument("--limit", type=int)
    n.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("download", help="fetch recorded PDFs not already in source/")
    d.add_argument("--limit", type=int)
    e = sub.add_parser("seed", help="one-off: build records from cer-regdocs2 sidecars")
    e.add_argument("sidecars")
    e.add_argument("--force", action="store_true")
    args = p.parse_args()
    return {"scout": cmd_scout, "doc": cmd_doc, "missing": cmd_missing,
            "download": cmd_download, "seed": cmd_seed}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
