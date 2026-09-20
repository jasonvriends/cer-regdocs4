#!/usr/bin/env python3
"""Write a filing sidecar next to each PDF, once, from a REGDOCS catalogue.

    python tools/write_filing_sidecars.py <catalogue.db> [source_dir]

The catalogue describes files already collected, so it can only answer for
documents already held -- which is why ingest.py does not consult it. Run this
once per batch of downloads and the identifiers travel with the PDF instead:
ingest.py reads `<pdf>.metadata.json` if it is there and records the filing's
title, company, filing number and date alongside the extraction.

The sidecar also carries the catalogue's sha256, which ingest checks against
the bytes it actually read.
"""
import json
import sqlite3
import sys
from pathlib import Path

FIELDS = ("id", "name", "url", "filing_date", "submitter", "company",
          "project", "filing_number", "item_kind", "hash")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    db = Path(sys.argv[1])
    src = Path(sys.argv[2] if len(sys.argv) > 2 else "source")
    if not db.exists():
        sys.exit(f"no catalogue at {db}")

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    written = missing = 0
    for pdf in sorted(src.glob("*.pdf")):
        if not pdf.stem.isdigit():
            continue
        row = con.execute(
            f"select {', '.join(FIELDS)} from documents where id = ?",
            (pdf.stem,)).fetchone()
        if not row:
            print(f"  {pdf.stem}: not in catalogue")
            missing += 1
            continue
        rec = dict(zip(FIELDS, row))
        out = {
            "document_id": rec["id"],
            "title": rec["name"],
            "source_url": rec["url"],
            "filing_number": rec["filing_number"],
            "filing_date": rec["filing_date"],
            "company": rec["company"],
            "submitter": rec["submitter"],
            "project": rec["project"],
            "item_kind": rec["item_kind"],
            "sha256": rec["hash"],
            "written_from": str(db),
        }
        out = {k: v for k, v in out.items() if v is not None}
        path = pdf.with_suffix(".metadata.json")
        path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"  {pdf.stem}: {rec['name'][:58]}")
        written += 1
    con.close()
    print(f"\n{written} sidecar(s) written, {missing} not found")


if __name__ == "__main__":
    main()
