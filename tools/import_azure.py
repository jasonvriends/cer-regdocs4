#!/usr/bin/env python3
"""Import the reference extraction beside our own, in the same shape.

    python tools/import_azure.py [--apply] [doc_id ...]

Covers every PDF in source/, whether or not it has been extracted yet.

The reference extractions were made by a different pipeline, in another
repository, and are stored there split into 300-page parts. That is awkward to
compare against and easy to get wrong -- reading only the top-level file
silently truncates a long document to its first 300 pages.

This lands one merged, compressed document per filing in the same layout an
extraction run uses:

    output/<doc>/<run id>/<doc>.azure.json.gz     the merged analysis
    output/<doc>/<run id>/<doc>.azure.meta.json   what produced it

The run id is a hash of the code that produced the analysis -- the two Azure
stage scripts -- plus the analyzer and API version, on the same principle as an
extraction run id: the same configuration always lands in the same directory,
and a different one lands beside it rather than over it. It is not our code and
we cannot re-run it, which makes recording exactly which version produced these
bytes more important, not less.

Nothing here overwrites: a document already imported under the same id is
skipped. Reports what it would do unless --apply is given.
"""
import gzip, hashlib, json, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
SRC = ROOT.parent / "cer-regdocs2"
REF_ROOT = SRC / "workspace/3_analyze/content-understanding/raw"
STAGE_SCRIPTS = [
    SRC / "regdocs_atlas/stages/regdocs_3_azure_core.py",
    SRC / "regdocs_atlas/stages/regdocs_3_azure_worker.py",
]
ANALYZER_ID = "prebuilt-layout"
API_VERSION = "2025-11-01"
AZURE_SCHEMA = 1


def signature() -> dict:
    """Everything that decides what the reference extraction looks like."""
    scripts = {}
    for p in STAGE_SCRIPTS:
        scripts[p.name] = (hashlib.sha256(p.read_bytes()).hexdigest()
                           if p.exists() else None)
    return {"analyzer_id": ANALYZER_ID, "api_version": API_VERSION,
            "azure_schema": AZURE_SCHEMA, "scripts": scripts}


def run_id(sig: dict) -> str:
    canonical = json.dumps(sig, sort_keys=True, separators=(",", ":"))
    return "az" + hashlib.sha256(canonical.encode()).hexdigest()[:6]


def parts_for(doc_id: str) -> list[Path]:
    """Every file holding part of one document's analysis, in page order.

    A long document is split, and its top-level file then repeats only the
    first part -- so when a .parts directory exists it is the whole document
    and the top-level file is ignored. The .meta.json siblings describe the
    parts and hold no page content.
    """
    for d in REF_ROOT.glob(f"*/*/{doc_id}"):
        parts = sorted(f for pd in d.glob("*.parts")
                       for f in pd.glob("pages-*.json")
                       if not f.name.endswith(".meta.json"))
        if parts:
            return parts
        whole = [f for f in sorted(d.glob("*.json"))
                 if not f.name.endswith(".meta.json")]
        if whole:
            return whole
    return []


def merge(files: list[Path]) -> tuple[dict, list[dict]]:
    """One document from its parts, plus what each part recorded about itself.

    Page numbers in the parts are absolute, so the lists concatenate; nothing
    is renumbered and nothing is deduplicated.
    """
    merged, provenance = None, []
    for f in files:
        blob = json.loads(f.read_text())
        side = f.with_suffix(".meta.json")
        if side.exists():
            try:
                provenance.append(json.loads(side.read_text()))
            except Exception:
                pass
        if merged is None:
            merged = blob
            continue
        for i, content in enumerate(blob.get("contents") or []):
            if i >= len(merged["contents"]):
                merged["contents"].append(content)
                continue
            into = merged["contents"][i]
            for key in ("pages", "paragraphs", "sections", "tables",
                        "figures", "lines", "words"):
                if isinstance(content.get(key), list):
                    into.setdefault(key, []).extend(content[key])
            if content.get("markdown"):
                into["markdown"] = (into.get("markdown") or "") + content["markdown"]
            into["endPageNumber"] = content.get("endPageNumber", into.get("endPageNumber"))
    return merged, provenance


def import_one(doc_id: str, sig: dict, rid: str, apply: bool) -> str:
    doc_root = OUT / doc_id
    files = parts_for(doc_id)
    if not files:
        return "no reference found"
    dest = doc_root / rid
    gz = dest / f"{doc_id}.azure.json.gz"
    if gz.exists():
        return "already imported"
    if not apply:
        return f"would import {len(files)} part(s)"
    # The reference does not depend on our extraction, so it is not gated on
    # one having run. A document we have not converted yet still gets its
    # reference; the extraction lands beside it later under its own run id.
    doc_root.mkdir(parents=True, exist_ok=True)
    merged, provenance = merge(files)
    if merged is None:
        return "reference unreadable"
    content = (merged.get("contents") or [{}])[0]
    pages = content.get("pages") or []
    dest.mkdir(parents=True, exist_ok=True)
    tmp = gz.with_name(gz.name + ".part")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(merged, fh)
    tmp.replace(gz)
    meta = {
        "azure_schema": AZURE_SCHEMA,
        "doc_id": doc_id,
        "run_id": rid,
        "kind": "reference extraction",
        # Not produced here and not reproducible here: these bytes came from a
        # paid API call made by another pipeline. The signature records which
        # code and which analyzer version made them.
        "produced_by": "cer-regdocs2 stage 3 (Azure Content Understanding)",
        "run_signature": sig,
        "analyzer_id": merged.get("analyzerId") or ANALYZER_ID,
        "api_version": merged.get("apiVersion") or API_VERSION,
        "created_at": merged.get("createdAt"),
        "page_count": len(pages),
        "page_range": [pages[0].get("pageNumber"), pages[-1].get("pageNumber")] if pages else None,
        "counts": {k: len(content.get(k) or [])
                   for k in ("pages", "paragraphs", "sections", "tables", "figures")},
        "warnings": merged.get("warnings") or [],
        "source_files": [str(f.relative_to(SRC)) for f in files],
        "source_parts": provenance,
        "compressed_bytes": gz.stat().st_size,
    }
    (dest / f"{doc_id}.azure.meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return f"imported {len(pages)} pages from {len(files)} part(s)"


def main() -> int:
    apply = "--apply" in sys.argv
    docs = [a for a in sys.argv[1:] if not a.startswith("-")]
    sig = signature()
    rid = run_id(sig)
    missing = [n for n, h in sig["scripts"].items() if h is None]
    if missing:
        print(f"warning: producing script(s) not found: {', '.join(missing)}")
        print("the run id cannot identify code it cannot read; fix this before importing")
        return 1
    if apply:
        keep = ROOT / "runs" / rid
        keep.mkdir(parents=True, exist_ok=True)
        for p in STAGE_SCRIPTS:
            shutil.copy2(p, keep / p.name)
        (keep / "README.md").write_text(
            f"# {rid}\n\nThe reference extraction, not ours. These are the "
            f"cer-regdocs2 stage scripts that called Azure Content "
            f"Understanding (`{ANALYZER_ID}`, API `{API_VERSION}`), copied "
            f"here because that repository is not a dependency of this one "
            f"and the analysis cannot be re-run from here.\n", encoding="utf-8")
    # Every document we hold a PDF for, not merely those already extracted.
    docs = docs or sorted(p.stem for p in (ROOT / "source").glob("*.pdf"))
    tally = {}
    for doc_id in docs:
        result = import_one(doc_id, sig, rid, apply)
        tally[result.split(" ")[0]] = tally.get(result.split(" ")[0], 0) + 1
        if len(docs) <= 20:
            print(f"{doc_id:>10}  {result}")
    print(f"\nrun id {rid}  ({len(docs)} documents)")
    for k, v in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {k:>16}: {v}")
    if not apply:
        print("\ndry run; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
