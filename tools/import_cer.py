#!/usr/bin/env python3
"""Record what the regulator says about each filing, beside our extraction.

    python tools/import_cer.py [--apply] [--refresh] [doc_id ...]

Writes output/<doc>/<doc>.cer.meta.json for every PDF in source/: the filing's
identity, parties and CER's own classification facets, taken from the
cer-regdocs2 download sidecar.

It sits in the document folder rather than a run folder because nothing ran
to produce it. It is a fact about the filing, not an output of a
configuration, and it does not change when the extraction does. The snapshot
it came from is recorded inside it.

What it deliberately leaves out:

- the REGDOCS detail page's `language`, which is the language of the web page
  and reads "en" on every filing, French ones included;
- the search `snippet`, which is text from inside the document -- content, and
  content is a separate pass;
- cer-regdocs2's own bookkeeping (collection runs, download attempts, pipeline
  status), which describes that tool's run rather than the filing.

An existing file is left alone unless --refresh is given. Reports what it would
do unless --apply is given.
"""
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
SIDECARS = ROOT.parent / "cer-regdocs2/workspace/2_download/files"
CER_SCHEMA = 1


def _one(v):
    """A single-valued identifier as a scalar; anything else as given."""
    return v[0] if isinstance(v, list) and len(v) == 1 else (v or None)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build(doc_id: str, side: dict) -> dict:
    m = side.get("metadata") or {}
    detail = m.get("detail_page") or {}
    facets = m.get("facets") or {}
    collected = (m.get("collection") or {}).get("facets") or {}
    container = (m.get("container_memberships") or [None])[0] or {}
    pdf = ROOT / "source" / f"{doc_id}.pdf"
    return {
        "cer_schema": CER_SCHEMA,
        "doc_id": doc_id,
        "document": {
            "title": side.get("title"),
            "kind": m.get("kind") or side.get("item_kind"),
            "url": side.get("source_url"),
            "view_url": detail.get("canonical_url"),
            "sha256": side.get("sha256"),
            "size_bytes": m.get("size_bytes"),
        },
        "filing": {
            "filing_number": side.get("filing_number"),
            "filing_id": m.get("filing_id"),
            "filing_date": side.get("filing_date"),
            "container": {
                "id": container.get("container_id"),
                "kind": container.get("container_kind"),
                "title": container.get("container_title"),
            } if container else None,
            "project": side.get("project"),
        },
        # company_id is the stable key. The two names often spell the same
        # company differently, and company is absent on about a quarter of
        # filings while submitter never is -- so both are kept verbatim.
        "parties": {
            "company": m.get("company"),
            "company_id": m.get("company_id"),
            "submitter": side.get("submitter"),
        },
        # CER's facets exactly as REGDOCS publishes them, under their own
        # names. Two of them answer different questions that are easy to
        # conflate: "Document Type": ["Application"] means this document IS
        # an application; a non-empty "Application Type" means it belongs to
        # an application proceeding, whatever kind of document it is.
        "facets": {name: list(values or []) for name, values in facets.items()},
        # Whether every facet lookup succeeded. When true, an empty list is
        # REGDOCS saying the filing is untagged, not a gap in collection.
        "facets_complete": bool(collected) and all(
            (v or {}).get("status") == "SUCCEEDED" for v in collected.values()),
        "identifiers": {k: _one(v) for k, v in (m.get("identifiers") or {}).items() if v},
        "provenance": {
            "from": "cer-regdocs2 download sidecar",
            "sidecar_schema": f"{side.get('schema')} v{side.get('schema_version')}",
            "catalogued_at": m.get("scraped_at"),
            "detail_fetched_at": detail.get("fetched_at"),
            "sha256_matches_source_pdf": (pdf.exists() and
                                          file_sha256(pdf) == side.get("sha256")),
        },
    }


def import_one(doc_id: str, apply: bool, refresh: bool) -> str:
    side_path = SIDECARS / f"{doc_id}.metadata.json"
    if not side_path.exists():
        return "no sidecar"
    dest = OUT / doc_id / f"{doc_id}.cer.meta.json"
    if dest.exists() and not refresh:
        return "already present"
    if not apply:
        return "would write"
    meta = build(doc_id, json.loads(side_path.read_text()))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(dest)
    return "written" if meta["provenance"]["sha256_matches_source_pdf"] else "written (sha256 MISMATCH)"


def main() -> int:
    apply, refresh = "--apply" in sys.argv, "--refresh" in sys.argv
    docs = [a for a in sys.argv[1:] if not a.startswith("-")] or \
        sorted(p.stem for p in (ROOT / "source").glob("*.pdf"))
    tally = {}
    for doc_id in docs:
        r = import_one(doc_id, apply, refresh)
        tally[r] = tally.get(r, 0) + 1
        if len(docs) <= 20:
            print(f"{doc_id:>10}  {r}")
    print(f"\n{len(docs)} documents")
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:>28}: {v}")
    if not apply:
        print("\ndry run; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
