#!/usr/bin/env python3
"""Load docling extractions into the regulatory asset ownership ledger.

    .venv/bin/python ledger.py init
    .venv/bin/python ledger.py load 4647200
    .venv/bin/python ledger.py holdings "Foothills"
    .venv/bin/python ledger.py history  "Elko Section"
    .venv/bin/python ledger.py review

The ledger is a single SQLite file (`ledger.db`). Everything is append-only:
corrections supersede rather than overwrite, so the record of what was believed,
and when, survives.

Company identity is anchored to REGDOCS metadata, not to the OCR'd text. The
filing company of a document is stated authoritatively by the regulator; the
text is used for assets and events, which is what the text is actually good for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]          # ideas/idea1/ -> repo root
DB = str(HERE / "ledger.db")

sys.path.insert(0, str(HERE))    # so `inventory` resolves whatever the CWD is
import inventory as ext          # noqa: E402 - needs HERE on the path first
NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

ASSET_KINDS = {"pipeline": "pipeline", "station": "station", "valve": "valve",
               "facility": "facility", "equipment": "equipment",
               "environment": None, "location": None, "company": None,
               "report": None, "document": None}


def connect(path=DB):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init(path=DB):
    c = connect(path)
    c.executescript((HERE / "schema.sql").read_text())
    seed_registry(c)
    c.commit()
    return c


# --------------------------------------------------------------------------
# entity resolution
# --------------------------------------------------------------------------


def norm_name(s: str) -> str:
    """Matching key for a company name. Deliberately aggressive: it drops legal
    suffixes and punctuation so that 'Foothills Pipe Lines (South B.C.) Ltd.',
    'Foothills Pipe Lines Ltd' and 'FOOTHILLS PIPE LINES (SOUTH BC) LTD.' all
    collapse to the same key. Aggressive normalisation is safe here only because
    a collision is resolved against the registry, not accepted blindly."""
    s = s.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\b(ltd|inc|limited|corporation|corp|ulc|llp|lp|company|"
               r"partnership|co)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


# The registry seed. In production this is loaded from the CER's regulated
# company list and Corporations Canada, which give a legal entity number and a
# name history for free. Inferring corporate identity from OCR'd prose when an
# authoritative register exists is doing hard work badly.
REGISTRY_SEED = [
    ("E-FOOTHILLS-SBC", "Foothills Pipe Lines (South B.C.) Ltd.", "cer", None),
    # TransCanada PipeLines Limited is the subsidiary, which kept its name. The
    # parent that renamed itself to TC Energy is E-TRANSCANADA-CORP below; the
    # two are different legal entities and must not be merged, however similar
    # the names look.
    ("E-TCPL",          "TransCanada PipeLines Limited",          "cer", None),
    ("E-BGC",           "BGC Engineering Inc.",                   "manual", None),
    ("E-WSP",           "WSP Canada Inc.",                        "manual", None),
    ("E-ALS",           "ALS Canada Ltd.",                        "manual", None),
    ("E-CALA",          "Canadian Association for Laboratory Accreditation Inc.",
     "manual", None),
]


def seed_registry(c):
    """Create the registry document and the seed entities."""
    c.execute("""INSERT OR IGNORE INTO document
                 (doc_id, title, filing_company, source_url, ingested_at)
                 VALUES ('REGISTRY', 'Regulator and corporate registry seed',
                         NULL, NULL, ?)""", (NOW(),))
    for eid, name, src, num in REGISTRY_SEED:
        c.execute("""INSERT OR IGNORE INTO legal_entity
                     (entity_id, registry_source, registry_number, jurisdiction,
                      created_at) VALUES (?,?,?,?,?)""",
                  (eid, src, num, "CA", NOW()))
        ev = add_evidence(c, "REGISTRY", None, "registry",
                          f"Registry seed: {name}", "registry", "ledger.py")
        c.execute("""INSERT INTO entity_name
                     (entity_id, name, name_norm, kind, valid_from, valid_to,
                      evidence_id, confidence, review_status)
                     VALUES (?,?,?,'legal',NULL,NULL,?,1.0,'confirmed')""",
                  (eid, name, norm_name(name), ev))

    # A real rename, recorded as a corporate event rather than as a change of
    # owner: the parent company renamed itself in 2019 and kept every asset.
    # This is the distinction the ledger exists to preserve.
    c.execute("""INSERT OR IGNORE INTO legal_entity
                 (entity_id, registry_source, jurisdiction, created_at)
                 VALUES ('E-TRANSCANADA-CORP','cer','CA',?)""", (NOW(),))
    ev = add_evidence(c, "REGISTRY", None, "registry",
                      "TransCanada Corporation changed its name to TC Energy "
                      "Corporation effective 2019-05-03.", "registry", "ledger.py")
    for nm, vf, vt in [("TransCanada Corporation", None, "2019-05-03"),
                       ("TC Energy Corporation", "2019-05-03", None)]:
        c.execute("""INSERT INTO entity_name (entity_id, name, name_norm, kind,
                     valid_from, valid_to, evidence_id, confidence, review_status)
                     VALUES ('E-TRANSCANADA-CORP',?,?,'legal',?,?,?,1.0,'confirmed')""",
                  (nm, norm_name(nm), vf, vt, ev))
    c.execute("""INSERT INTO corporate_event (kind, effective_date, predecessor_id,
                 successor_id, evidence_id, confidence, review_status)
                 VALUES ('rename','2019-05-03','E-TRANSCANADA-CORP',
                         'E-TRANSCANADA-CORP',?,1.0,'confirmed')""", (ev,))


def resolve_entity(c, name: str, evidence_id: int, confidence: float):
    """Map a name as written to a stable entity_id.

    Exact normalised match wins. A name that matches nothing becomes a new
    entity and is queued for review rather than being quietly attached to the
    nearest-looking company: a wrong merge is far more expensive to undo than a
    duplicate, because it silently moves assets between owners.
    """
    key = norm_name(name)
    rows = c.execute("""SELECT DISTINCT entity_id FROM entity_name
                        WHERE name_norm = ?""", (key,)).fetchall()
    if len(rows) == 1:
        eid = rows[0]["entity_id"]
        known = c.execute("""SELECT 1 FROM entity_name WHERE entity_id=? AND name=?""",
                          (eid, name)).fetchone()
        if not known:      # a new spelling of a known company
            c.execute("""INSERT INTO entity_name (entity_id, name, name_norm, kind,
                         evidence_id, confidence, review_status)
                         VALUES (?,?,?,'ocr_variant',?,?,'unreviewed')""",
                      (eid, name, key, evidence_id, confidence))
        return eid, "matched"
    if len(rows) > 1:
        queue(c, "entity_name", evidence_id,
              f"'{name}' normalises to a key held by {len(rows)} entities", 1)
        return rows[0]["entity_id"], "ambiguous"

    eid = "E-NEW-" + hashlib.sha1(key.encode()).hexdigest()[:10].upper()
    c.execute("""INSERT OR IGNORE INTO legal_entity (entity_id, registry_source,
                 jurisdiction, created_at) VALUES (?,'extracted','CA',?)""",
              (eid, NOW()))
    c.execute("""INSERT INTO entity_name (entity_id, name, name_norm, kind,
                 evidence_id, confidence, review_status)
                 VALUES (?,?,?,'legal',?,?,'unreviewed')""",
              (eid, name, key, evidence_id, confidence))
    queue(c, "legal_entity", evidence_id,
          f"'{name}' matched no registry entity; created {eid}", 2)
    return eid, "created"


def queue(c, table, row_id, reason, priority):
    c.execute("""INSERT INTO review_queue (table_name, row_id, reason, priority,
                 created_at) VALUES (?,?,?,?,?)""",
              (table, row_id, reason, priority, NOW()))


def add_evidence(c, doc_id, page, ref, quote, method, extractor):
    cur = c.execute("""INSERT INTO evidence (doc_id, page, element_ref, quote,
                       quote_sha256, method, extractor, extracted_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (doc_id, page, ref, quote,
                     hashlib.sha256(quote.encode()).hexdigest(),
                     method, extractor, NOW()))
    return cur.lastrowid


# --------------------------------------------------------------------------
# loading a document
# --------------------------------------------------------------------------


def corpus_metadata(doc_id: str) -> dict:
    """REGDOCS metadata for a document, from docs/test_corpus.md.

    In production this comes from the REGDOCS index directly. It matters because
    the filing company is stated by the regulator: it is the one company fact in
    the whole pipeline that does not depend on reading a PDF correctly.
    """
    md = REPO_ROOT / "docs" / "test_corpus.md"
    out = {"doc_id": doc_id}
    if not md.exists():
        return out
    for line in md.read_text().splitlines():
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(cells) >= 6 and doc_id in cells[0]:
            out.update(filing_id=cells[3], filed_date=cells[4],
                       filing_company=None if cells[5] == "—" else cells[5],
                       page_count=int(cells[1]) if cells[1].isdigit() else None)
        if len(cells) == 2 and cells[0] == doc_id:
            out["title"] = cells[1]
    return out


def load(doc_id: str, db=DB):
    src = REPO_ROOT / "output" / doc_id / f"{doc_id}.docling.json"
    if not src.exists():
        sys.exit(f"no docling extraction at {src}")
    c = connect(db) if Path(db).exists() else init(db)

    texts, tables, n_pages = ext.load(src)
    inv = run_extraction(inv_doc := doc_id, texts, tables, n_pages)

    meta = corpus_metadata(doc_id)
    c.execute("""INSERT OR REPLACE INTO document (doc_id, filing_id, title,
                 filed_date, filing_company, page_count, source_url, ingested_at)
                 VALUES (?,?,?,?,?,?,?,?)""",
              (doc_id, meta.get("filing_id"), meta.get("title"),
               meta.get("filed_date"), meta.get("filing_company"), n_pages,
               f"https://apps.cer-rec.gc.ca/REGDOCS/File/Download/{doc_id}", NOW()))

    extractor = f"inventory.py+ledger.py"
    stats = {"entities": 0, "assets": 0, "assertions": 0, "matched": 0,
             "created": 0, "ambiguous": 0}

    # 1. companies -> legal entities, resolved against the registry
    ent_map = {}
    for e in inv.entities.values():
        if e.category != "company":
            continue
        ev0 = e.evidence[0]
        ev = add_evidence(c, doc_id, ev0.page, ev0.ref, ev0.text,
                          "cue-phrase", extractor)
        eid, how = resolve_entity(c, e.name, ev, e.confidence)
        ent_map[e.key] = eid
        stats["entities"] += 1
        stats[how if how in stats else "matched"] += 1
        # every alias seen in this document, attached to the resolved entity
        for alias in sorted(e.aliases):
            if not c.execute("SELECT 1 FROM entity_name WHERE entity_id=? AND name=?",
                             (eid, alias)).fetchone():
                c.execute("""INSERT INTO entity_name (entity_id, name, name_norm,
                             kind, evidence_id, confidence, review_status)
                             VALUES (?,?,?,'abbreviation',?,?,'unreviewed')""",
                          (eid, alias, norm_name(alias), ev, 0.7))

    # 2. physical things -> assets, with their names and locations
    asset_map = {}
    for e in inv.entities.values():
        kind = ASSET_KINDS.get(e.category)
        if not kind:
            continue
        aid = f"A-{e.category.upper()[:4]}-{hashlib.sha1(e.key.encode()).hexdigest()[:10].upper()}"
        c.execute("""INSERT OR IGNORE INTO asset (asset_id, kind, created_at)
                     VALUES (?,?,?)""", (aid, kind, NOW()))
        asset_map[e.key] = aid
        stats["assets"] += 1
        ev0 = e.evidence[0]
        ev = add_evidence(c, doc_id, ev0.page, ev0.ref, ev0.text,
                          "table-row" if "tables" in ev0.ref else "cue-phrase",
                          extractor)
        if not c.execute("SELECT 1 FROM asset_name WHERE asset_id=? AND name=?",
                         (aid, e.name)).fetchone():
            c.execute("""INSERT INTO asset_name (asset_id, name, name_norm, kind,
                         evidence_id, confidence, review_status)
                         VALUES (?,?,?,'designation',?,?,'unreviewed')""",
                      (aid, e.name, norm_name(e.name), ev, e.confidence))
        for alias in sorted(e.aliases):
            if not c.execute("SELECT 1 FROM asset_name WHERE asset_id=? AND name=?",
                             (aid, alias)).fetchone():
                c.execute("""INSERT INTO asset_name (asset_id, name, name_norm,
                             kind, evidence_id, confidence, review_status)
                             VALUES (?,?,?,'ocr_variant',?,0.7,'unreviewed')""",
                          (aid, alias, norm_name(alias), ev))
        if e.attrs.get("Easting") or e.attrs.get("Length"):
            c.execute("""INSERT INTO asset_location (asset_id, easting, northing,
                         place, evidence_id, confidence) VALUES (?,?,?,?,?,?)""",
                      (aid, _f(e.attrs.get("Easting")), _f(e.attrs.get("Northing")),
                       None, ev, e.confidence))

    # 3. parent/child structure
    for r in inv.relations:
        if r.rel != "PART_OF":
            continue
        child, parent = asset_map.get(r.src), asset_map.get(r.dst)
        if child and parent:
            c.execute("UPDATE asset SET parent_asset_id=? WHERE asset_id=? "
                      "AND parent_asset_id IS NULL", (parent, child))

    # 4. the ledger rows: who holds what
    #    OPERATES is an operatorship assertion. The document's own filing company
    #    is recorded too, at lower confidence, as an applicant-role assertion over
    #    the assets the filing is about -- it is a weaker claim than a sentence
    #    that says who built the thing, and is marked as such.
    for r in inv.relations:
        if r.rel != "OPERATES":
            continue
        eid, aid = ent_map.get(r.src), asset_map.get(r.dst)
        if not (eid and aid):
            continue
        ev = add_evidence(c, doc_id, r.evidence.page, r.evidence.ref,
                          r.evidence.text, "cue-phrase", extractor)
        c.execute("""INSERT INTO assertion (asset_id, entity_id, role, valid_from,
                     valid_to, asserted_at, evidence_id, confidence, method,
                     review_status, note)
                     VALUES (?,?,'operator',NULL,NULL,?,?,?,'cue-phrase',
                             'unreviewed',?)""",
                  (aid, eid, NOW(), ev, r.evidence.confidence, r.cue))
        stats["assertions"] += 1
        queue(c, "assertion", c.execute("SELECT last_insert_rowid() AS i").fetchone()["i"],
              "machine-asserted operatorship; confirm against the filing", 1)

    c.commit()
    return stats, inv


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def run_extraction(doc_id, texts, tables, n_pages):
    """Run every extraction pass, as inventory.py's main() does."""
    inv = ext.Inventory(doc_id)
    ext.pass_document(inv, texts, n_pages, doc_id)
    wo = ext.pass_workorders(inv, texts, tables, doc_id)
    ext.pass_companies(inv, texts, tables, doc_id)
    pairs = ext.pass_company_relations(inv, texts, doc_id)
    ext.pass_pipeline(inv, texts, doc_id, pairs)
    ext.pass_stations(inv, texts, tables, doc_id)
    ext.pass_environment(inv, texts, doc_id)
    ext.pass_locations(inv, texts, doc_id, pairs)
    ext.pass_equipment(inv, texts, doc_id)
    ext.pass_facilities(inv, texts, tables, doc_id, pairs)
    ext.pass_valves(inv, texts, tables, doc_id)
    ext.pass_lab_relations(inv, texts, wo, pairs, doc_id)
    ext.pass_station_sampling(inv, tables, doc_id)
    ext.merge_truncated(inv, "station")
    return inv


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------


def holdings(term: str, db=DB):
    c = connect(db)
    ents = c.execute("""SELECT DISTINCT entity_id FROM entity_name
                        WHERE name_norm LIKE ?""", (f"%{norm_name(term)}%",)).fetchall()
    if not ents:
        print(f"no entity matching {term!r}")
        return
    for row in ents:
        eid = row["entity_id"]
        cur = c.execute("""SELECT name, kind, valid_from, valid_to FROM entity_name
                           WHERE entity_id=? ORDER BY kind, name""", (eid,)).fetchall()
        current = [r["name"] for r in cur if r["valid_to"] is None and r["kind"] == "legal"]
        print(f"\n=== {eid}  {current[0] if current else '?'}")
        print(f"    known as: " + "; ".join(
            f"{r['name']}" + (f" [{r['kind']}]" if r["kind"] != "legal" else "")
            + (f" until {r['valid_to']}" if r["valid_to"] else "")
            for r in cur))
        held = c.execute("""SELECT * FROM v_current_holdings WHERE entity_id=?
                            ORDER BY asset_kind, asset_name""", (eid,)).fetchall()
        if not held:
            print("    holds: (no asset assertions)")
        for h in held:
            print(f"    {h['role'].upper():9s} {h['asset_kind']:9s} {h['asset_name']}"
                  f"   [{h['review_status']}, conf {h['confidence']:.2f}]")
            print(f"      source: CER {h['doc_id']} p{h['page']} {h['element_ref']}")
            print(f"      quote : \"{(h['quote'] or '')[:150]}\"")
            kids = c.execute("""SELECT COUNT(*) n FROM asset WHERE parent_asset_id=?""",
                             (h["asset_id"],)).fetchone()["n"]
            if kids:
                print(f"      contains {kids} sub-assets")


def history(term: str, db=DB):
    c = connect(db)
    rows = c.execute("""SELECT DISTINCT asset_id FROM asset_name
                        WHERE name_norm LIKE ?""", (f"%{norm_name(term)}%",)).fetchall()
    for r in rows:
        aid = r["asset_id"]
        names = c.execute("""SELECT name, kind FROM asset_name WHERE asset_id=?""",
                          (aid,)).fetchall()
        print(f"\n=== {aid}   {names[0]['name']}")
        print("    known as: " + "; ".join(f"{n['name']} [{n['kind']}]" for n in names))
        tr = c.execute("SELECT * FROM v_chain_of_custody WHERE asset_id=?",
                       (aid,)).fetchall()
        if not tr:
            print("    no recorded transfers "
                  "(no transfer instrument has been loaded for this asset)")
        for t in tr:
            print(f"    {t['effective_date']}  {t['from_entity_id']} -> "
                  f"{t['to_entity_id']}  ({t['instrument']})")
        for a in c.execute("""SELECT * FROM v_current_holdings WHERE asset_id=?""",
                           (aid,)).fetchall():
            print(f"    held as {a['role']} by {a['entity_name']} "
                  f"[{a['review_status']}] — CER {a['doc_id']} p{a['page']}")


def review(db=DB, limit=20):
    c = connect(db)
    rows = c.execute("""SELECT * FROM review_queue WHERE resolved_at IS NULL
                        ORDER BY priority, item_id LIMIT ?""", (limit,)).fetchall()
    total = c.execute("SELECT COUNT(*) n FROM review_queue WHERE resolved_at IS NULL"
                      ).fetchone()["n"]
    print(f"{total} open review items; showing {len(rows)}\n")
    for r in rows:
        print(f"  P{r['priority']}  {r['table_name']}#{r['row_id']}: {r['reason']}")


def stats(db=DB):
    c = connect(db)
    for t in ("document", "evidence", "legal_entity", "entity_name", "asset",
              "asset_name", "asset_location", "assertion", "transfer",
              "corporate_event", "review_queue"):
        n = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
        print(f"  {t:16s} {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("load");     p.add_argument("doc_id")
    p = sub.add_parser("holdings"); p.add_argument("term")
    p = sub.add_parser("history");  p.add_argument("term")
    sub.add_parser("review")
    sub.add_parser("stats")
    a = ap.parse_args()

    if a.cmd == "init":
        Path(DB).unlink(missing_ok=True)
        init(); print(f"created {DB}")
    elif a.cmd == "load":
        s, _ = load(a.doc_id)
        print(f"loaded {a.doc_id}: " + ", ".join(f"{k}={v}" for k, v in s.items()))
    elif a.cmd == "holdings":
        holdings(a.term)
    elif a.cmd == "history":
        history(a.term)
    elif a.cmd == "review":
        review()
    elif a.cmd == "stats":
        stats()


if __name__ == "__main__":
    main()
