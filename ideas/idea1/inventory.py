#!/usr/bin/env python3
"""Build an Obsidian-navigable entity inventory from a docling extraction.

Reads output/<id>/<id>.docling.json and writes inventory/<id>/ as a vault of
Markdown files, one per entity, linked with [[wikilinks]].

Two rules govern everything here:

1. Nothing is emitted without evidence. Every entity and every relationship
   carries the CER document id, the PDF page, the docling element reference and
   the supporting text it was read from.
2. Relationships come from structure (a table row that puts a station and a
   watercourse on the same line) or from an explicit cue phrase ("a wholly
   owned subsidiary of"). Co-occurrence on a page is not evidence of anything
   and never creates a relationship.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import collections
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# This lives in ideas/idea1/, so the corpus is two levels up. Resolving against
# the file rather than the working directory keeps the CLI usable from anywhere.
REPO_ROOT = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------
# categories -> vault subdirectory
# --------------------------------------------------------------------------

CATEGORIES = {
    "company": "companies",
    "pipeline": "pipelines",
    "station": "stations",
    "valve": "valves",
    "facility": "facilities",
    "equipment": "equipment",
    "report": "reports",
    "location": "locations",
    "environment": "environment",
    "document": "documents",
}

# Relationship types. The first six are the starting set; the rest are kept
# separate because the document states them explicitly and collapsing them into
# the six would assert more than the text does ("retained by" is not OWNS).
CORE_RELATIONS = ["OWNS", "OPERATES", "PART_OF", "CONTAINS", "LOCATED_AT", "MENTIONED_IN"]
EXTRA_RELATIONS = ["AFFILIATE_OF", "RETAINED_BY", "ANALYZED_BY", "SAMPLED_AT"]

INVERSE = {
    "OWNS": "OWNED_BY",
    "OPERATES": "OPERATED_BY",
    "PART_OF": "CONTAINS",
    "CONTAINS": "PART_OF",
    "LOCATED_AT": "LOCATION_OF",
    "MENTIONED_IN": "MENTIONS",
    "AFFILIATE_OF": "AFFILIATE_OF",
    "RETAINED_BY": "RETAINED",
    "ANALYZED_BY": "ANALYSED",
    "SAMPLED_AT": "SAMPLE_SITE_FOR",
}


# --------------------------------------------------------------------------
# core model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """A pointer back into the source PDF. Never constructed without a ref."""

    doc_id: str
    page: int | None
    ref: str
    text: str
    confidence: float
    note: str = ""

    def sort_key(self):
        return (self.page or 0, self.ref)


@dataclass
class Entity:
    key: str            # normalised identity, used for dedup
    name: str           # display name / filename
    category: str
    subtype: str = ""
    attrs: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    aliases: set = field(default_factory=set)

    def add_evidence(self, ev: Evidence):
        # keep evidence unique and bounded; a station appearing on 90 pages does
        # not need 90 identical quotes
        if ev not in self.evidence:
            self.evidence.append(ev)

    @property
    def confidence(self) -> float:
        return max((e.confidence for e in self.evidence), default=0.0)


@dataclass(frozen=True)
class Relation:
    src: str            # entity key
    rel: str
    dst: str            # entity key
    evidence: Evidence
    cue: str = ""


class Inventory:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self.entities: dict[str, Entity] = {}
        self.relations: list[Relation] = []
        self._rel_seen: set = set()

    def add(self, key, name, category, subtype="", attrs=None, aliases=None) -> Entity:
        if key not in self.entities:
            self.entities[key] = Entity(key, name, category, subtype)
        e = self.entities[key]
        if attrs:
            for k, v in attrs.items():
                if v not in (None, "") and k not in e.attrs:
                    e.attrs[k] = v
        if aliases:
            e.aliases.update(a for a in aliases if a and a != name)
        return e

    def relate(self, src, rel, dst, ev: Evidence, cue=""):
        if src == dst or src not in self.entities or dst not in self.entities:
            return
        # one relation per (src, rel, dst); keep the best-supported evidence
        sig = (src, rel, dst)
        if sig in self._rel_seen:
            return
        self._rel_seen.add(sig)
        self.relations.append(Relation(src, rel, dst, ev, cue))

    def out_relations(self, key):
        return [r for r in self.relations if r.src == key]

    def in_relations(self, key):
        return [r for r in self.relations if r.dst == key]


# --------------------------------------------------------------------------
# reading the docling document
# --------------------------------------------------------------------------


@dataclass
class TextEl:
    ref: str
    page: int | None
    label: str
    text: str


@dataclass
class TableEl:
    ref: str
    page: int | None
    grid: list           # list of rows of cell dicts


def load(path: Path):
    with path.open() as fh:
        doc = json.load(fh)

    def page_of(item):
        prov = item.get("prov") or []
        return prov[0]["page_no"] if prov else None

    texts = [
        TextEl(t["self_ref"], page_of(t), t.get("label", ""), (t.get("text") or "").strip())
        for t in doc.get("texts", [])
    ]
    tables = []
    for t in doc.get("tables", []):
        grid = (t.get("data") or {}).get("grid") or []
        tables.append(TableEl(t["self_ref"], page_of(t), grid))
    n_pages = len(doc.get("pages") or {})
    return texts, tables, n_pages


def cell_text(c) -> str:
    return re.sub(r"\s+", " ", (c.get("text") or "")).strip()


def row_text(row) -> str:
    return " | ".join(cell_text(c) for c in row).strip(" |")


def snippet(s: str, limit=400) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------
# identifier grammars, learned from the corpus (see README)
# --------------------------------------------------------------------------

RE_STATION = re.compile(
    r"\bELKO-(?:KP\d+(?:\+\d+)?|\d+(?:\.\d+)?)-(?:WC|WL|DR|SE|SP)(?:-(?:US|DS|SE))?\b"
)
RE_WATERCOURSE = re.compile(r"\b\d+(?:\.\d+)?-(?:WC|WL|DR|SE|SP)\b")
RE_KP = re.compile(r"\bKP\s?(\d{1,3})(?:\s?\+\s?(\d{3}))?\b")
RE_WORKORDER = re.compile(r"\b(?:CG|CA|CL|VA|EV)\d{7}\b")
# valve tags: supported so other filings populate the directory; this one has none
RE_VALVE = re.compile(r"\b(?:MLV|MLBV|BVS?|RTU)[- ]?\d{1,4}[A-Z]?\b")
RE_ORG = re.compile(
    r"\b[A-Z][A-Za-z&.'’-]*"
    r"(?:\s+(?:[A-Z][A-Za-z&.'’-]*|\([^)]{1,24}\)|of|and|for|the|de|des|du|von))"
    r"{0,7}\s+"
    r"(?:Ltd|Inc|Limited|Corporation|Corp|ULC|LLP|LP|Company|Partnership)\b\.?"
)
# role/column words that bleed into a name when a table cell abuts it
ORG_PREFIX_NOISE = {
    "client", "project", "sampler", "site", "sampler site", "laboratory",
    "false", "true", "to", "from", "attention", "contact", "company", "name",
    "report", "invoice", "recipient", "recipients", "p.biol", "p.geo",
    "ltd", "inc", "corp", "limited", "address", "submitted", "prepared",
    "by", "for", "and", "telephone", "date", "attn",
}
ORG_STOP = {"false canada ltd.", "canada ltd.", "ltd.", "inc."}


# A job title running into the company it belongs to: "Chief Financial Officer of
# Enron Corp." is a person's role, not a company name. Only a leading run that
# starts with a title word is stripped, because "of" is legitimately internal to
# names like "General Motors of Canada Ltd."
RE_ROLE_PREFIX = re.compile(
    r"^(?:Chief|Vice|Senior|Executive|Deputy|Assistant|President|Director|Manager|"
    r"Officer|Secretary|Treasurer|Counsel|Head|Chair(?:man|person)?|Partner)\b"
    r"[^,;]*?\bof\s+(?=[A-Z])"
)


def split_conjoined(raw: str):
    """'Kinder Morgan Canada Company and Kinder Morgan Cochin ULC' is two
    companies. Split only when both halves are complete names on their own, so
    'Amber Size and Chemical Co. Ltd.' stays whole."""
    for m in re.finditer(r"\s+and\s+", raw):
        left, right = raw[:m.start()], raw[m.end():]
        if RE_ORG.fullmatch(left.strip()) and RE_ORG.fullmatch(right.strip()):
            return [left.strip(), right.strip()]
    return [raw]


def norm_org(raw: str) -> str | None:
    """Trim a regex-captured organisation name down to the name itself."""
    s = re.sub(r"\s+", " ", raw).strip().strip(".,;:")
    s = RE_ROLE_PREFIX.sub("", s)
    if not s.endswith("."):
        s += "." if re.search(r"\b(Ltd|Inc|Corp)$", s) else ""
    words = s.split()
    # strip leading noise words one at a time
    while len(words) > 2 and words[0].lower().strip(".,:") in ORG_PREFIX_NOISE:
        words.pop(0)
    s = " ".join(words)
    # collapse an accidental doubling ("ALS Canada Ltd. ALS Canada Ltd.")
    half = len(s) // 2
    if len(s) % 2 == 1 and s[:half] == s[half + 1 :]:
        s = s[:half]
    if s.lower() in ORG_STOP or len(s) < 6:
        return None
    # the accepted suffixes must match RE_ORG's, or a split half is silently lost
    if not re.search(r"\b(Ltd|Inc|Limited|Corporation|Corp|ULC|LLP|LP|Company|"
                     r"Partnership)\b\.?$", s):
        return None
    # 'Company' and 'Partnership' are ordinary words as well as legal suffixes, so
    # two of them is not a name: this corpus has table headers reading "Contact
    # Company" (and, on its mojibake pages, "Centact Company"). A strong suffix
    # (Ltd, Inc, ULC, Corp) identifies an organisation on its own; these do not.
    if re.search(r"\b(Company|Partnership)\b\.?$", s) and len(s.split()) < 3:
        return None
    return s


def org_key(name: str) -> str:
    """Identity for a company, tolerant of the ways filings spell one.

    "Foothills Pipe Lines (South BC) Ltd." and "Foothills Pipe Lines
    (South B.C.) Ltd." are the same company; so are TransCanada Pipelines and
    TransCanada PipeLines.
    """
    s = name.lower()
    s = re.sub(r"\([^)]*\)", " ", s)                      # drop (South B.C.)
    s = re.sub(r"\b(ltd|inc|limited|corporation|corp|ulc|llp|lp)\b\.?", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def norm_pipeline(name: str) -> str:
    """One spelling for a pipeline. This corpus writes the same line as
    'BC Mainline Loop No. 2', 'British Columbia Mainline Loop No.2' and
    'British Columbia Mainline Loop No. 2'."""
    s = re.sub(r"\s+", " ", name).strip()
    s = re.sub(r"\bB\.?C\.?\b", "British Columbia", s)
    s = re.sub(r"\bNo\.\s*", "No. ", s)
    return s


def pipeline_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", norm_pipeline(name).lower())


def edit_distance_le(a: str, b: str, limit: int) -> bool:
    """True when a and b are within `limit` single-character edits."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


def consolidate(cands: dict) -> dict:
    """Drop names that are only a fragment of a longer discovered name.

    Two failure modes this fixes, both from the same cause (a name abutting a
    table cell or running past the end of a regex window):
      'Canada Ltd.'  is a trailing fragment of 'WSP Canada Inc.'-style names
      'ALS Ltd.'     is the shorthand the text uses for 'ALS Canada Ltd.'
    A fragment is only merged when exactly one longer name can absorb it, so an
    ambiguous fragment is kept rather than attached to the wrong company.

    A third pass merges near-identical keys, which is how an OCR slip shows up:
    'Foothill Pipe Lines' against 'Foothills Pipe Lines'. The rarer spelling is
    folded into the commoner one, since a company named on eighty page headers
    and once as 'Foothill' was misread once, not incorporated twice.
    """
    keys = list(cands)
    drop = {}
    for k in keys:
        absorbers = [o for o in keys
                     if o != k and len(o) > len(k)
                     and (o.endswith(k) or o.startswith(k))]
        if len(absorbers) == 1:
            drop[k] = absorbers[0]

    count = {k: len(v) for k, v in cands.items()}
    for k in keys:
        if k in drop or len(k) < 12:
            continue
        near = [o for o in keys
                if o != k and o not in drop and len(o) >= 12
                and count[o] > count[k] and edit_distance_le(k, o, 2)]
        if len(near) == 1:
            drop[k] = near[0]

    out = {}
    for k, v in cands.items():
        tgt = k
        seen = set()
        while tgt in drop and tgt not in seen:
            seen.add(tgt)
            tgt = drop[tgt]
        out.setdefault(tgt, []).extend(v if isinstance(v, list) else [v])
    return out


def normalize_station_id(sid: str) -> str:
    """Repair a station ID against its own grammar: ELKO-<chainage>-<type>-<pos>,
    where a non-KP chainage is written to two decimals.

    'ELKO-15400-WC-US' is 'ELKO-154.00-WC-US' with the decimal point dropped.
    Note that fuzzy matching must never be used on these IDs: '-DS' and '-US'
    differ by one character, so an edit-distance merge would silently fuse the
    downstream and upstream stations of the same watercourse.
    """
    if m := re.fullmatch(r"(ELKO-)(\d{4,6})(-(?:WC|WL|DR|SE|SP)(?:-(?:US|DS|SE))?)", sid):
        digits = m.group(2)
        return f"{m.group(1)}{digits[:-2]}.{digits[-2:]}{m.group(3)}"
    return sid


def merge_truncated(inv: Inventory, category: str):
    """Fold a truncated entity name into its only possible completion.

    A table cell that wraps prints 'ELKO-155.60-WC' on one line and 'US' on the
    next, so the reader sees a station ID with no position suffix. Where exactly
    one full ID extends it, they are the same station. Where two do (both a US
    and a DS station exist), the reading is genuinely ambiguous and is kept as
    its own note, flagged, rather than guessed at.
    """
    keys = [k for k, e in inv.entities.items() if e.category == category]
    merge = {}
    for k in keys:
        longer = [o for o in keys if o != k and o.startswith(k)]
        if len(longer) == 1:
            merge[k] = longer[0]
        elif len(longer) > 1:
            inv.entities[k].attrs["Incomplete reading"] = (
                "no position suffix in the source; could be "
                + " or ".join(sorted(inv.entities[o].name for o in longer))
            )
    for src, dst in merge.items():
        a, b = inv.entities.pop(src), inv.entities[dst]
        b.aliases.add(a.name)
        for ev in a.evidence:
            b.add_evidence(ev)
        for k, v in a.attrs.items():
            b.attrs.setdefault(k, v)
    if merge:
        seen, rels = set(), []
        for r in inv.relations:
            s, d = merge.get(r.src, r.src), merge.get(r.dst, r.dst)
            if s == d or (sig := (s, r.rel, d)) in seen:
                continue
            seen.add(sig)
            rels.append(Relation(s, r.rel, d, r.evidence, r.cue))
        inv.relations[:] = rels
        inv._rel_seen = seen
    return len(merge)


def kp_label(m: re.Match) -> str:
    """Normalise KP 16+450 / KP16450 / 16+450 to one label."""
    km, metres = m.group(1), m.group(2)
    if metres is None:
        # KP23250 style: trailing 3 digits are metres when the number is long
        raw = m.group(1)
        if len(raw) > 2:
            km, metres = raw[:-3] or "0", raw[-3:]
        else:
            return f"KP {int(km)}"
    return f"KP {int(km)}+{metres}"


# --------------------------------------------------------------------------
# extraction passes
# --------------------------------------------------------------------------


def pass_document(inv: Inventory, texts, n_pages, doc_id):
    """The filing itself, plus the named reports inside it."""
    ev = Evidence(doc_id, 1, "#/body", f"CER REGDOCS filing {doc_id}", 1.0,
                  "the source PDF")
    d = inv.add(f"doc:{doc_id}", doc_id, "document", "CER filing",
                attrs={"CER document ID": doc_id, "Pages": n_pages,
                       "Source": f"https://apps.cer-rec.gc.ca/REGDOCS/File/Download/{doc_id}"})
    d.add_evidence(ev)

    # report titles: a title-cased line that names a monitoring report / memo
    title_re = re.compile(
        r"^(?:Annual\s+)?[A-Z][^|]{12,120}?"
        r"(?:Monitoring Report(?:\s*[-–]\s*\d{4})?|TECHNICAL MEMORANDUM|"
        r"CERTIFICATE OF ANALYSIS(?:\s*\(GUIDELINE\s+EVALUATION\))?|"
        r"QUALITY CONTROL (?:INTERPRETIVE )?REPORT)\s*$"
    )
    for t in texts:
        if t.label not in ("section_header", "text", "page_header"):
            continue
        if not (m := title_re.match(t.text)):
            continue
        name = re.sub(r"\s+", " ", m.group(0)).strip()
        if len(name) < 14 or re.match(r"APPENDIX\b", name, re.I):
            continue
        key = f"report:{name.lower()}"
        r = inv.add(key, name, "report", "report")
        r.add_evidence(Evidence(doc_id, t.page, t.ref, name, 0.85,
                                f"{t.label} reading as a report title"))
        inv.relate(key, "PART_OF", f"doc:{doc_id}",
                   Evidence(doc_id, t.page, t.ref, name, 0.9,
                            "appears as a titled report inside the filing"),
                   cue="titled section of the filing")

    # the BGC memo carries an explicit document number
    for t in texts:
        if m := re.fullmatch(r"\d{5}-[A-Z]{2,4}-[A-Z]-[A-Z]{2}-\d{4}_\d", t.text):
            r = inv.add(f"report:docnum:{t.text}", t.text, "report", "controlled document",
                        attrs={"Document number": t.text})
            r.add_evidence(Evidence(doc_id, t.page, t.ref, t.text, 0.9,
                                    "consultant document number"))
            inv.relate(r.key, "PART_OF", f"doc:{doc_id}",
                       Evidence(doc_id, t.page, t.ref, t.text, 0.9,
                                "document number printed in the filing"),
                       cue="document number in filing")


def pass_workorders(inv: Inventory, texts, tables, doc_id):
    """ALS laboratory work orders -> reports, with the lab as the analyst."""
    first: dict[str, tuple] = {}
    for t in texts:
        for wo in RE_WORKORDER.findall(t.text):
            first.setdefault(wo, (t.page, t.ref, t.text))
    for tb in tables:
        for ri, row in enumerate(tb.grid):
            rt = row_text(row)
            for wo in RE_WORKORDER.findall(rt):
                first.setdefault(wo, (tb.page, f"{tb.ref} row {ri}", rt))
    for wo, (page, ref, text) in sorted(first.items()):
        e = inv.add(f"report:wo:{wo}", wo, "report", "laboratory work order",
                    attrs={"Work order": wo})
        e.add_evidence(Evidence(doc_id, page, ref, snippet(text), 0.9,
                                "ALS work order number"))
        inv.relate(e.key, "PART_OF", f"doc:{doc_id}",
                   Evidence(doc_id, page, ref, snippet(text), 0.9,
                            "work order reported within the filing"),
                   cue="work order appears in filing")
    return first


def pass_companies(inv: Inventory, texts, tables, doc_id):
    """Companies, with aliases learned from '<Full Name> (ABBR)' in the text.

    Discovery is deliberately separated from entity creation: fragments are only
    recognisable as fragments once the whole set of names is known.
    """
    cands: dict[str, list] = {}

    def note(name, page, ref, text, conf):
        cands.setdefault(org_key(name), []).append((name, page, ref, text, conf))

    for t in texts:
        for m in RE_ORG.finditer(t.text):
            for part in split_conjoined(m.group(0)):
                if name := norm_org(part):
                    note(name, t.page, t.ref, t.text, 0.85)
    for tb in tables:
        for ri, row in enumerate(tb.grid):
            rt = row_text(row)
            for m in RE_ORG.finditer(rt):
                for part in split_conjoined(m.group(0)):
                    if name := norm_org(part):
                        note(name, tb.page, f"{tb.ref} row {ri}", rt, 0.8)

    for key, mentions in consolidate(cands).items():
        # the commonest spelling is the display name, longest breaking a tie; the
        # rest become aliases. Frequency beats length because the odd spelling is
        # usually the misread one ("(South B.C-)" for "(South B.C.)").
        freq = collections.Counter(m[0] for m in mentions)
        names = set(freq)
        name = max(names, key=lambda n: (freq[n], len(n)))
        e = inv.add(f"company:{key}", name, "company", "company",
                    aliases=names - {name})
        # a handful of mentions is enough to trace the name; prose before tables
        for nm, page, ref, text, conf in sorted(
                mentions, key=lambda m: (m[2].startswith("#/tables"), m[1] or 0))[:4]:
            e.add_evidence(Evidence(doc_id, page, ref, snippet(text), conf,
                                    "organisation name with a legal suffix"))
        first = e.evidence[0]
        inv.relate(e.key, "MENTIONED_IN", f"doc:{doc_id}",
                   Evidence(doc_id, first.page, first.ref, first.text, first.confidence,
                            "named in the filing"), cue="named in filing")

    # aliases: "BGC Engineering Inc. (BGC)", "TC Energy Corporation (TC Energy)"
    alias_re = re.compile(RE_ORG.pattern + r"\s*\(([A-Z][A-Za-z .]{1,24})\)")
    for t in texts:
        for m in alias_re.finditer(t.text):
            if not (name := norm_org(m.group(0).split("(")[0])):
                continue
            if e := inv.entities.get(f"company:{org_key(name)}"):
                e.aliases.add(m.group(1).strip())
    # a bare-surname alias is how the rest of the document refers to the company
    for e in inv.entities.values():
        if e.category == "company":
            e.aliases.add(e.name.split()[0])
            e.aliases.discard(e.name)
    return cands


def company_lookup(inv: Inventory):
    """alias/name -> company key, longest string first so 'TC Energy Corporation'
    wins over 'TC Energy'."""
    pairs = []
    for e in inv.entities.values():
        if e.category != "company":
            continue
        for s in {e.name, *e.aliases}:
            pairs.append((s, e.key))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def find_companies(text: str, pairs) -> list:
    """Company keys mentioned in a passage, in order of appearance."""
    hits, taken = [], []
    for s, key in pairs:
        for m in re.finditer(re.escape(s), text):
            if any(m.start() < e and m.end() > b for b, e in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append((m.start(), key))
    return [k for _, k in sorted(hits)]


# A coordinated appositive shares one subject across both clauses:
#   "Foothills, a wholly owned subsidiary of TCPL and affiliate of TC Energy"
# Both relations belong to Foothills. Reading the nearest company to the left of
# each cue instead gives "TCPL affiliate of TC Energy", which the document does
# not say, so this structure is matched as a whole before any single cue is tried.
APPOSITIVE_CUES = [
    (re.compile(
        r"(?P<subject>[^,;]+?),\s*(?:an?\s+)?(?:wholly[- ]owned\s+)?subsidiary of\s+"
        r"(?P<parent>.+?)\s*,?\s+and\s+(?:an?\s+)?affiliate of\s+(?P<affil>.+)", re.I),
     [("subject", "PART_OF", "parent"), ("subject", "AFFILIATE_OF", "affil")],
     "'<company>, a wholly owned subsidiary of <A> and affiliate of <B>'"),
]

# Single cues. Each maps one passage to one directed relation; the subject is the
# company nearest the cue on its left, the object the first one on its right.
CUES = [
    (re.compile(r"(?P<a>.+?)\bis a wholly owned subsidiary of\b(?P<b>.+)", re.I),
     "PART_OF", "'is a wholly owned subsidiary of'"),
    (re.compile(r"(?P<a>[^,;]+?),\s*(?:an?\s+)?(?:wholly[- ]owned\s+)?subsidiary of\s*(?P<b>.+)",
                re.I), "PART_OF", "'<company>, a wholly owned subsidiary of'"),
    (re.compile(r"(?P<a>[^,;]+?),?\s+(?:an?\s+)?affiliate of\b(?P<b>.+)", re.I),
     "AFFILIATE_OF", "'affiliate of'"),
    (re.compile(r"(?P<a>.+?)\b(?:was|were) retained by\b(?P<b>.+)", re.I),
     "RETAINED_BY", "'was retained by'"),
    (re.compile(r"(?P<a>.+?)\bretained by\b(?P<b>.+)", re.I),
     "RETAINED_BY", "'retained by'"),
]


def pass_company_relations(inv: Inventory, texts, doc_id):
    """Company-to-company relations, only from an explicit cue phrase.

    A sentence yields at most one reading: the first appositive structure that
    matches, else the first single cue. Running every cue over the same sentence
    is what produced the wrong affiliate subject.
    """
    pairs = company_lookup(inv)
    for t in texts:
        if len(t.text) < 30:
            continue
        for sent in re.split(r"(?<=[.;])\s+", t.text):
            if apply_appositive(inv, sent, t, pairs, doc_id):
                continue
            for cue, rel, label in CUES:
                if not (m := cue.search(sent)):
                    continue
                left = find_companies(m.group("a"), pairs)
                right = find_companies(m.group("b"), pairs)
                if not left or not right:
                    continue
                inv.relate(left[-1], rel, right[0],
                           Evidence(doc_id, t.page, t.ref, snippet(sent), 0.9,
                                    f"cue phrase {label}"),
                           cue=snippet(sent, 200))
                break
    return pairs


def apply_appositive(inv, sent, t, pairs, doc_id) -> bool:
    """Emit both relations of a coordinated appositive. True if one matched."""
    for cue, triples, label in APPOSITIVE_CUES:
        if not (m := cue.search(sent)):
            continue
        groups = {}
        for g in ("subject", "parent", "affil"):
            hits = find_companies(m.group(g), pairs)
            if not hits:
                break
            # the subject is the company nearest the comma; objects are the
            # first named after their cue
            groups[g] = hits[-1] if g == "subject" else hits[0]
        if len(groups) != 3:
            continue
        for src, rel, dst in triples:
            inv.relate(groups[src], rel, groups[dst],
                       Evidence(doc_id, t.page, t.ref, snippet(sent), 0.9,
                                f"cue phrase {label}"),
                       cue=snippet(sent, 200))
        return True
    return False


def pass_pipeline(inv: Inventory, texts, doc_id, pairs):
    """The pipeline, its named sections, and who built or operates it."""
    sec_re = re.compile(r"\b([A-Z][a-z]+)\s+Section\b")
    SEC_STOP = {"Reclamation", "This", "The", "Following", "Previous"}
    parent_re = re.compile(
        r"\b((?:British Columbia|BC|B\.C\.)\s+Mainline(?:\s+Loop\s+No\.\s*\d+)?)\b"
    )

    for t in texts:
        for m in sec_re.finditer(t.text):
            if m.group(1) in SEC_STOP:
                continue
            name = f"{m.group(1)} Section"
            e = inv.add(f"pipeline:{name.lower()}", name, "pipeline", "pipeline section")
            e.add_evidence(Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.9,
                                    "named pipeline section"))
            inv.relate(e.key, "MENTIONED_IN", f"doc:{doc_id}",
                       Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.9,
                                "named in the filing"), cue="named in filing")

    # discover every spelling of the parent pipeline, then collapse them
    pcands: dict[str, list] = {}
    for t in texts:
        for m in parent_re.finditer(t.text):
            pcands.setdefault(pipeline_key(m.group(1)), []).append(
                (norm_pipeline(m.group(1)), t.page, t.ref, t.text))
    parent_of_key = {}
    for key, mentions in consolidate(pcands).items():
        name = max({m[0] for m in mentions}, key=len)
        p = inv.add(f"pipeline:{key}", name, "pipeline", "pipeline",
                    aliases={m[0] for m in mentions} - {name})
        for nm, page, ref, text in mentions[:3]:
            p.add_evidence(Evidence(doc_id, page, ref, snippet(text), 0.9,
                                    "named pipeline"))
        inv.relate(p.key, "MENTIONED_IN", f"doc:{doc_id}",
                   Evidence(doc_id, mentions[0][1], mentions[0][2],
                            snippet(mentions[0][3]), 0.9, "named in the filing"),
                   cue="named in filing")
        for nm, page, ref, text in mentions:
            parent_of_key[nm] = p.key

    # "the Elko Section of the BC Mainline Loop No. 2 pipeline"
    # and "British Columbia Mainline Loop No.2 NPS 48 - Elko Section"
    for t in texts:
        for m in parent_re.finditer(t.text):
            pk = parent_of_key.get(norm_pipeline(m.group(1)))
            if not pk:
                continue
            tail = t.text[m.end():m.end() + 80]
            head = t.text[max(0, m.start() - 80):m.start()]
            for frag in (tail, head):
                if sm := re.search(r"\b([A-Z][a-z]+ Section)\b", frag):
                    if sm.group(1) in SEC_STOP:
                        continue
                    child = f"pipeline:{sm.group(1).lower()}"
                    if child in inv.entities and child != pk:
                        inv.relate(child, "PART_OF", pk,
                                   Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.95,
                                            "section named together with its pipeline"),
                                   cue=f"{sm.group(1)} of {norm_pipeline(m.group(1))}")

    # physical attributes of the section
    for t in texts:
        if m := re.search(r"([A-Z][a-z]+ Section) is an approximately ([\d.]+\s*km) long,?\s*"
                          r"(Nominal Pipe Size \d+)?", t.text):
            if e := inv.entities.get(f"pipeline:{m.group(1).lower()}"):
                e.attrs.setdefault("Length", m.group(2))
                if m.group(3):
                    e.attrs.setdefault("Diameter", m.group(3))
                e.add_evidence(Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.95,
                                        "stated length and pipe size"))
    for t in texts:
        if m := re.search(r"\bNPS\s*(\d+)\b.{0,40}?([A-Z][a-z]+ Section)", t.text):
            if e := inv.entities.get(f"pipeline:{m.group(2).lower()}"):
                e.attrs.setdefault("Diameter", f"NPS {m.group(1)}")

    # constructed / operated by
    for t in texts:
        for sent in re.split(r"(?<=[.;])\s+", t.text):
            m = re.search(r"\b([A-Z][a-z]+ Section)\b.*?\bwas (constructed|operated) by\b(.+)",
                          sent)
            if not m:
                continue
            pk = f"pipeline:{m.group(1).lower()}"
            for ck in find_companies(m.group(3), pairs)[:1]:
                inv.relate(ck, "OPERATES", pk,
                           Evidence(doc_id, t.page, t.ref, snippet(sent), 0.9,
                                    f"'was {m.group(2)} by'"),
                           cue=snippet(sent, 200))


# station registry tables: map a header cell to a column role
COL_ROLES = [
    ("station", re.compile(r"\b(?:Sample ID|Station ID|Monitoring (?:Station )?ID)\b", re.I)),
    ("watercourse", re.compile(r"Water\s*-?\s*course ID|\bDrainage\b|Feature", re.I)),
    ("kp", re.compile(r"\bKP\b", re.I)),
    ("ard", re.compile(r"ARD/ML Concern|Geochemical Hazard", re.I)),
    ("easting", re.compile(r"\bEasting\b", re.I)),
    ("northing", re.compile(r"\bNorthing\b", re.I)),
    ("status", re.compile(r"Station Status", re.I)),
    ("comments", re.compile(r"Comments", re.I)),
    ("relative", re.compile(r"Relative Location|Sample Location Relative", re.I)),
]


def table_roles(grid):
    """Column index -> role, read from the first row that looks like a header."""
    for hi, row in enumerate(grid[:3]):
        roles = {}
        for ci, c in enumerate(row):
            txt = cell_text(c)
            for role, rx in COL_ROLES:
                if rx.search(txt) and role not in roles.values():
                    roles[ci] = role
                    break
        if "station" in roles.values() or (
            "watercourse" in roles.values() and "kp" in roles.values()
        ):
            return hi, roles
    return None, {}


def pass_stations(inv: Inventory, texts, tables, doc_id):
    """Stations, watercourses and KP markers, read structurally from the
    registry and result tables. A row is the evidence for the relation."""
    for tb in tables:
        hi, roles = table_roles(tb.grid)
        if not roles:
            continue
        for ri, row in enumerate(tb.grid[hi + 1 :], start=hi + 1):
            cells = {roles[ci]: cell_text(row[ci])
                     for ci in roles if ci < len(row)}
            rt = row_text(row)
            if not rt or len(rt) < 6:
                continue
            ref = f"{tb.ref} row {ri}"

            st_ids = RE_STATION.findall(cells.get("station", "")) or \
                RE_STATION.findall(rt)
            if not st_ids:
                continue
            st_id = normalize_station_id(st_ids[0])
            ev = Evidence(doc_id, tb.page, ref, snippet(rt), 0.95,
                          "station registry / result table row")
            s = inv.add(f"station:{st_id}", st_id, "station", "monitoring station")
            s.add_evidence(ev)
            for label, role in (("ARD/ML concern", "ard"), ("Station status", "status"),
                                ("Easting", "easting"), ("Northing", "northing"),
                                ("Relative to RoW", "relative")):
                v = cells.get(role, "")
                if v and len(v) < 60:
                    s.attrs.setdefault(label, v)
            inv.relate(s.key, "MENTIONED_IN", f"doc:{doc_id}", ev, cue="tabulated in filing")

            # watercourse on the same row -> the station monitors it
            wc_src = cells.get("watercourse", "") or st_id
            for wc in RE_WATERCOURSE.findall(wc_src)[:1]:
                w = inv.add(f"env:{wc}", wc, "environment", "watercourse or drainage")
                w.add_evidence(Evidence(doc_id, tb.page, ref, snippet(rt), 0.95,
                                        "watercourse ID in a table row"))
                inv.relate(s.key, "PART_OF", w.key,
                           Evidence(doc_id, tb.page, ref, snippet(rt), 0.95,
                                    "station and watercourse on the same table row"),
                           cue=f"row lists station {st_id} against watercourse {wc}")
                inv.relate(w.key, "MENTIONED_IN", f"doc:{doc_id}",
                           Evidence(doc_id, tb.page, ref, snippet(rt), 0.9,
                                    "tabulated in filing"), cue="tabulated in filing")

            # KP on the same row -> the station sits there
            kp_src = cells.get("kp", "")
            kms = list(RE_KP.finditer(kp_src)) or list(
                RE_KP.finditer(st_id)) or []
            if not kms and re.fullmatch(r"\d{1,3}\+\d{3}", kp_src):
                kms = list(RE_KP.finditer("KP " + kp_src))
            for m in kms[:1]:
                label = kp_label(m)
                loc = inv.add(f"loc:{label}", label, "location", "pipeline chainage marker")
                loc.add_evidence(Evidence(doc_id, tb.page, ref, snippet(rt), 0.9,
                                          "KP value in a table row"))
                inv.relate(s.key, "LOCATED_AT", loc.key,
                           Evidence(doc_id, tb.page, ref, snippet(rt), 0.9,
                                    "station and KP on the same table row"),
                           cue=f"row lists station {st_id} at {label}")

    # stations named in prose only
    for t in texts:
        for st_id in {normalize_station_id(x) for x in RE_STATION.findall(t.text)}:
            s = inv.add(f"station:{st_id}", st_id, "station", "monitoring station")
            s.add_evidence(Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.85,
                                    "station ID in body text"))


def pass_environment(inv: Inventory, texts, doc_id):
    """Named watercourses and the guideline regime they are assessed against."""
    named = re.compile(r"\b([A-Z][A-Za-z']+)\s+(Creek|River|Lake|Brook)\b")
    for t in texts:
        for m in named.finditer(t.text):
            name = f"{m.group(1)} {m.group(2)}"
            if m.group(1) in ("Acid", "Rock", "Total"):
                continue
            e = inv.add(f"env:named:{name.lower()}", name, "environment", m.group(2).lower())
            e.add_evidence(Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.85,
                                    "named watercourse"))
            inv.relate(e.key, "MENTIONED_IN", f"doc:{doc_id}",
                       Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.85,
                                "named in the filing"), cue="named in filing")


def pass_locations(inv: Inventory, texts, doc_id, pairs):
    """Place names, restricted to a gazetteer so prose nouns are not promoted
    to locations."""
    gaz = {
        "Elko": "community", "Fernie": "community", "Calgary": "city",
        "British Columbia": "province", "Alberta": "province",
        "Burnaby": "city", "Vancouver": "city",
    }
    for t in texts:
        for place, kind in gaz.items():
            if not re.search(rf"\b{re.escape(place)}\b", t.text):
                continue
            if place == "Elko" and re.search(r"\bELKO-", t.text):
                continue
            e = inv.add(f"loc:{place}", place, "location", kind)
            e.add_evidence(Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.8,
                                    "place name from gazetteer"))
            inv.relate(e.key, "MENTIONED_IN", f"doc:{doc_id}",
                       Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.8,
                                "named in the filing"), cue="named in filing")

    # "near Fernie, BC" / "located in the province of British Columbia"
    for t in texts:
        for m in re.finditer(r"([A-Z][a-z]+ Section)[^.]{0,80}?\bnear\s+([A-Z][a-z]+)", t.text):
            pk, lk = f"pipeline:{m.group(1).lower()}", f"loc:{m.group(2)}"
            inv.relate(pk, "LOCATED_AT", lk,
                       Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.9, "'near <place>'"),
                       cue=snippet(m.group(0), 120))
        for m in re.finditer(r"([A-Z][a-z]+ Section) is located in the province of "
                             r"(British Columbia|Alberta)", t.text):
            inv.relate(f"pipeline:{m.group(1).lower()}", "LOCATED_AT", f"loc:{m.group(2)}",
                       Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.95,
                                "'is located in the province of'"),
                       cue=snippet(m.group(0), 120))


def pass_equipment(inv: Inventory, texts, doc_id):
    """Field instruments, named in the methods sections."""
    kinds = [
        (r"\bYSI ProQuatro\b|\bYellow Springs Instrument[a-z]* \(YSI\) ProQuatro\b",
         "YSI ProQuatro multimeter", "water quality multimeter"),
        (r"\bYSI Pro Plus\b|\(YSI\) Pro Plus\b", "YSI Pro Plus multimeter",
         "water quality multimeter"),
        (r"\bHanna\b", "Hanna multimeter", "water quality multimeter"),
    ]
    for t in texts:
        for rx, name, sub in kinds:
            if re.search(rx, t.text):
                e = inv.add(f"equip:{name.lower()}", name, "equipment", sub)
                e.add_evidence(Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.85,
                                        "instrument named in the methods"))
                inv.relate(e.key, "MENTIONED_IN", f"doc:{doc_id}",
                           Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.85,
                                    "named in the filing"), cue="named in filing")


def pass_facilities(inv: Inventory, texts, tables, doc_id, pairs):
    """Laboratories and coded facilities."""
    for t in texts:
        if m := re.search(r"EQ\w*S\s*[Ff]acility\s*[Cc]ode:?\s*=?\s*(\d{6,})", t.text):
            code = m.group(1)
            e = inv.add(f"fac:equis:{code}", f"WSP EQuIS Facility {code}", "facility",
                        "environmental data facility", attrs={"EQuIS facility code": code})
            e.add_evidence(Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.9,
                                    "EQuIS facility code"))
            inv.relate(e.key, "MENTIONED_IN", f"doc:{doc_id}",
                       Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.9,
                                "named in the filing"), cue="named in filing")

    # ALS laboratory addresses on certificates -> a facility per city
    lab_re = re.compile(r"\bALS\b[^.\n]{0,60}?\b(Calgary|Burnaby|Vancouver|Edmonton|Waterloo)\b")
    for t in texts:
        if m := lab_re.search(t.text):
            city = m.group(1)
            name = f"ALS Environmental Laboratory — {city}"
            e = inv.add(f"fac:als:{city.lower()}", name, "facility", "analytical laboratory")
            e.add_evidence(Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.8,
                                    "ALS laboratory location on a certificate"))
            inv.relate(e.key, "LOCATED_AT", f"loc:{city}",
                       Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.8,
                                "laboratory address"), cue=snippet(m.group(0), 120))
            for ck in find_companies("ALS Canada Ltd.", pairs)[:1]:
                inv.relate(ck, "OPERATES", e.key,
                           Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.75,
                                    "laboratory branded to the company on the certificate"),
                           cue=snippet(m.group(0), 120))


def pass_valves(inv: Inventory, texts, tables, doc_id):
    """Valve tags. This filing has none; the pass exists so the category is
    populated from filings that do, rather than being silently unsupported."""
    found = 0
    for t in texts:
        for tag in RE_VALVE.findall(t.text):
            # require the word valve nearby, or the tag is just a table code
            if not re.search(r"valve", t.text, re.I):
                continue
            e = inv.add(f"valve:{tag}", tag, "valve", "valve")
            e.add_evidence(Evidence(doc_id, t.page, t.ref, snippet(t.text), 0.8,
                                    "valve tag with 'valve' in the same element"))
            found += 1
    return found


def pass_lab_relations(inv: Inventory, texts, workorders, pairs, doc_id):
    """Work order -> the lab that issued it, where the certificate says so."""
    als = [k for k in inv.entities if k.startswith("company:") and "als" in k]
    if not als:
        return
    for wo, (page, ref, text) in workorders.items():
        inv.relate(f"report:wo:{wo}", "ANALYZED_BY", als[0],
                   Evidence(doc_id, page, ref, snippet(text), 0.8,
                            "work order appears on an ALS certificate of analysis"),
                   cue="ALS work order numbering (CG…/CA…) on ALS certificates")


def pass_station_sampling(inv: Inventory, tables, doc_id):
    """Station -> work order, where a results table puts a sample ID under a
    work-order column."""
    for tb in tables:
        for ri, row in enumerate(tb.grid):
            rt = row_text(row)
            wos = RE_WORKORDER.findall(rt)
            sts = RE_STATION.findall(rt)
            if not wos or not sts:
                continue
            ev = Evidence(doc_id, tb.page, f"{tb.ref} row {ri}", snippet(rt), 0.85,
                          "station and work order on the same table row")
            for st in {normalize_station_id(x) for x in sts}:
                for wo in set(wos):
                    inv.relate(f"report:wo:{wo}", "SAMPLED_AT", f"station:{st}", ev,
                               cue=f"row links {wo} to {st}")


# --------------------------------------------------------------------------
# vault writing
# --------------------------------------------------------------------------


def safe_name(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|#\[\]^]', "-", name).strip(" .")
    return re.sub(r"\s+", " ", s)[:120] or "unnamed"


def assign_filenames(inv: Inventory):
    used: dict[str, str] = {}
    for e in sorted(inv.entities.values(), key=lambda x: (x.category, x.name)):
        base = safe_name(e.name)
        cand, n = base, 2
        while used.get(f"{e.category}/{cand}") not in (None, e.key):
            cand, n = f"{base} ({n})", n + 1
        used[f"{e.category}/{cand}"] = e.key
        e.attrs["_file"] = cand
    # wikilinks must be unique vault-wide; warn if two categories collide
    by_link = defaultdict(list)
    for e in inv.entities.values():
        by_link[e.attrs["_file"]].append(e)
    for link, es in by_link.items():
        if len(es) > 1:
            for e in es:
                e.attrs["_file"] = safe_name(f"{e.name} ({CATEGORIES[e.category][:-1]})")


def link(inv: Inventory, key: str) -> str:
    e = inv.entities.get(key)
    return f"[[{e.attrs['_file']}]]" if e else f"`{key}`"


def fmt_evidence(inv: Inventory, evs: list, doc_file: str) -> list:
    lines = []
    for ev in sorted(set(evs), key=Evidence.sort_key)[:12]:
        lines.append(f"### CER {ev.doc_id} — page {ev.page}\n")
        lines.append(f"> {ev.text}\n")
        lines.append(f"- Docling reference: `{ev.ref}`")
        lines.append(f"- Document: [[{doc_file}]] (page {ev.page})")
        lines.append(f"- Confidence: {ev.confidence:.2f}"
                     + (f" — {ev.note}" if ev.note else ""))
        lines.append("")
    return lines


def write_vault(inv: Inventory, root: Path, doc_id: str, n_pages: int):
    if root.exists():
        shutil.rmtree(root)
    for sub in CATEGORIES.values():
        (root / sub).mkdir(parents=True, exist_ok=True)

    assign_filenames(inv)
    doc_e = inv.entities[f"doc:{doc_id}"]
    doc_file = doc_e.attrs["_file"]

    # group an entity's outgoing links by relation, and by the category of the
    # target, so a company page reads as an asset register
    for e in inv.entities.values():
        out = inv.out_relations(e.key)
        inc = inv.in_relations(e.key)
        L = [f"# {e.name}", ""]
        L.append(f"**Type:** {e.subtype or CATEGORIES[e.category][:-1]}")
        L.append(f"**Category:** {CATEGORIES[e.category]}")
        if e.aliases:
            L.append(f"**Also written as:** {', '.join(sorted(e.aliases))}")
        L.append(f"**Extraction confidence:** {e.confidence:.2f}")
        L.append(f"**Evidence count:** {len(e.evidence)}")
        for k, v in e.attrs.items():
            if not k.startswith("_"):
                L.append(f"**{k}:** {v}")
        L.append("")

        # an asset register, for entities that own or contain things
        held = defaultdict(list)
        for r in inc:
            if r.rel in ("PART_OF",) or (r.rel == "MENTIONED_IN" and e.category == "document"):
                held[inv.entities[r.src].category].append(r.src)
        for r in out:
            if r.rel in ("OPERATES", "OWNS", "CONTAINS"):
                held[inv.entities[r.dst].category].append(r.dst)
        if held:
            L += ["## Assets and contents", ""]
            for cat in CATEGORIES:
                keys = sorted(set(held.get(cat, [])),
                              key=lambda k: inv.entities[k].name)
                if not keys:
                    continue
                L.append(f"### {CATEGORIES[cat].title()} ({len(keys)})")
                for k in keys[:400]:
                    L.append(f"- {link(inv, k)}")
                if len(keys) > 400:
                    L.append(f"- …and {len(keys) - 400} more")
                L.append("")

        if out or inc:
            L += ["## Relationships", ""]
            for r in sorted(out, key=lambda r: (r.rel, inv.entities[r.dst].name)):
                cue = f" — *{r.cue}*" if r.cue else ""
                L.append(f"- **{r.rel}** {link(inv, r.dst)}{cue} "
                         f"`{r.evidence.ref}` p{r.evidence.page}")
            for r in sorted(inc, key=lambda r: (r.rel, inv.entities[r.src].name))[:200]:
                if r.rel == "MENTIONED_IN" and e.category == "document":
                    continue          # already listed under assets
                L.append(f"- **{INVERSE.get(r.rel, r.rel + ' (inbound)')}** "
                         f"{link(inv, r.src)} `{r.evidence.ref}` p{r.evidence.page}")
            L.append("")

        L += ["## Evidence", ""]
        L += fmt_evidence(inv, e.evidence, doc_file)

        path = root / CATEGORIES[e.category] / f"{e.attrs['_file']}.md"
        path.write_text("\n".join(L).rstrip() + "\n")

    # index
    counts = {c: sum(1 for e in inv.entities.values() if e.category == c) for c in CATEGORIES}
    rel_counts = defaultdict(int)
    for r in inv.relations:
        rel_counts[r.rel] += 1
    idx = [f"# Inventory — CER {doc_id}", "",
           f"Extracted from `output/{doc_id}/{doc_id}.docling.json` "
           f"({n_pages} pages).", "",
           f"Source PDF: <https://apps.cer-rec.gc.ca/REGDOCS/File/Download/{doc_id}>", "",
           f"Root document: [[{doc_file}]]", "",
           "## Entities", "", "| category | count |", "|---|---|"]
    for c, sub in CATEGORIES.items():
        idx.append(f"| {sub} | {counts[c]} |")
    idx += ["", f"**Total:** {len(inv.entities)} entities, "
                f"{len(inv.relations)} relationships", "",
            "## Relationships", "", "| type | count |", "|---|---|"]
    for r in CORE_RELATIONS + EXTRA_RELATIONS:
        idx.append(f"| {r} | {rel_counts.get(r, 0)} |")
    idx += ["",
            "Each relationship is stored once, in the direction the document "
            "states it, and rendered from both ends. `CONTAINS` therefore counts "
            "zero here: it is how a stored `PART_OF` reads on the parent's page. "
            "`OWNS` counts zero because this filing states corporate structure as "
            "\"a wholly owned subsidiary of\", which is recorded as `PART_OF`.", ""]
    empty = [CATEGORIES[c] for c in CATEGORIES if counts[c] == 0]
    if empty:
        idx += ["## Empty categories", "",
                "These are supported by the extractor but this filing contains no "
                "evidence for them, so no files were written: "
                + ", ".join(f"`{e}`" for e in empty) + ".", ""]
    (root / "Inventory.md").write_text("\n".join(idx) + "\n")
    return check_links(root)


def check_links(root: Path):
    """Every [[wikilink]] must resolve to a file, and no two files may share a
    basename. A dangling link is a silently broken graph, which is the failure
    this whole vault exists to avoid."""
    stems = defaultdict(list)
    for p in root.rglob("*.md"):
        stems[p.stem].append(p)
    dangling, total = defaultdict(int), 0
    for p in root.rglob("*.md"):
        for m in re.finditer(r"\[\[([^\]|]+)", p.read_text()):
            total += 1
            target = m.group(1).strip()
            if target not in stems:
                dangling[target] += 1
    ambiguous = {k: v for k, v in stems.items() if len(v) > 1}
    return {"links": total, "dangling": dict(dangling), "ambiguous": list(ambiguous)}


# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("doc_id", help="CER document id, e.g. 4647200")
    ap.add_argument("--output", default=str(REPO_ROOT / "output"),
                    help="directory holding the docling JSON")
    ap.add_argument("--vault", default=str(Path(__file__).resolve().parent / "inventory"),
                    help="directory to write the vault into")
    args = ap.parse_args()

    src = Path(args.output) / args.doc_id / f"{args.doc_id}.docling.json"
    if not src.exists():
        sys.exit(f"no docling extraction at {src}")

    print(f"reading {src} …", file=sys.stderr)
    texts, tables, n_pages = load(src)
    print(f"  {len(texts)} text elements, {len(tables)} tables, {n_pages} pages",
          file=sys.stderr)

    inv = Inventory(args.doc_id)
    pass_document(inv, texts, n_pages, args.doc_id)
    workorders = pass_workorders(inv, texts, tables, args.doc_id)
    pass_companies(inv, texts, tables, args.doc_id)
    pairs = pass_company_relations(inv, texts, args.doc_id)
    pass_pipeline(inv, texts, args.doc_id, pairs)
    pass_stations(inv, texts, tables, args.doc_id)
    pass_environment(inv, texts, args.doc_id)
    pass_locations(inv, texts, args.doc_id, pairs)
    pass_equipment(inv, texts, args.doc_id)
    pass_facilities(inv, texts, tables, args.doc_id, pairs)
    n_valves = pass_valves(inv, texts, tables, args.doc_id)
    pass_lab_relations(inv, texts, workorders, pairs, args.doc_id)
    pass_station_sampling(inv, tables, args.doc_id)
    n_merged = merge_truncated(inv, "station")

    root = Path(args.vault) / args.doc_id
    checks = write_vault(inv, root, args.doc_id, n_pages)

    counts = defaultdict(int)
    for e in inv.entities.values():
        counts[CATEGORIES[e.category]] += 1
    print(f"\nwrote {root}", file=sys.stderr)
    for sub in CATEGORIES.values():
        print(f"  {sub:14s} {counts[sub]}", file=sys.stderr)
    print(f"  {'relationships':14s} {len(inv.relations)}", file=sys.stderr)
    if not n_valves:
        print("  (no valve tags in this filing)", file=sys.stderr)
    if n_merged:
        print(f"  merged {n_merged} truncated station ID(s)", file=sys.stderr)

    print(f"\n  {'wikilinks':14s} {checks['links']}", file=sys.stderr)
    if checks["dangling"] or checks["ambiguous"]:
        print(f"  DANGLING LINKS: {checks['dangling']}", file=sys.stderr)
        print(f"  AMBIGUOUS NAMES: {checks['ambiguous']}", file=sys.stderr)
        sys.exit("vault is not internally consistent")
    print("  all wikilinks resolve; no duplicate note names", file=sys.stderr)


if __name__ == "__main__":
    main()
