#!/usr/bin/env python3
"""Coverage of each extraction against an independent reference.

    python tools/coverage.py [doc_id ...]

For every page, the share of the reference extraction's characters that also
appear in ours, normalised to letters and digits so whitespace and punctuation
do not count. Pages with almost no reference text are skipped.

This measures resemblance to the reference, not accuracy. The reference
invents text on scanned pages -- tokens like "aposssusus" and "nofdyesty" --
and we are penalised for not reproducing them. It is a broad completeness
check and nothing stronger.
"""
import collections, gzip, json, re, statistics, sys, gc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
# The reference extractions are not kept in this repo: they are large, they are
# not ours, and they are only needed when something is being measured. They are
# read in place from the pipeline that produced them.
REF_ROOT = ROOT.parent / "cer-regdocs2/workspace/3_analyze/content-understanding/raw"


def reference_for(doc_id: str) -> list[Path]:
    """Every file making up one document's reference extraction.

    Looks beside the output first so a copied-in reference still wins. In the
    source pipeline's own store a long document is split: the top-level file
    then covers only its first pages and the rest sit in a .parts directory,
    so taking the first file found silently truncates the comparison. The
    .meta.json siblings describe the parts and carry no page content.
    """
    # An imported reference (tools/import_azure.py) is one merged, compressed
    # file in its own run directory and is preferred: it is already whole,
    # whereas the source store must be reassembled from parts every time.
    imported = sorted((OUT / doc_id).glob(f"*/{doc_id}.azure.json.gz"))
    if imported:
        return [imported[-1]]
    local = OUT / doc_id / f"{doc_id}.azure.json"
    if local.exists():
        return [local]
    for d in REF_ROOT.glob(f"*/*/{doc_id}"):
        parts = sorted(pd.glob("pages-*.json") for pd in d.glob("*.parts"))
        flat = [f for group in parts for f in group if not f.name.endswith(".meta.json")]
        if flat:
            return flat
        files = [f for f in sorted(d.glob("*.json")) if not f.name.endswith(".meta.json")]
        if files:
            return files
    return []
norm = lambda s: re.sub(r"[^0-9a-z]", "", s.lower())


def page_coverage(doc_id: str) -> dict | None:
    d = OUT / doc_id
    runs = sorted(d.glob("*/*.docling.json.gz")) + sorted(d.glob("*/*.docling.json"))
    ref_files = reference_for(doc_id)
    if not runs or not ref_files:
        return None
    ref = {}
    for rf in ref_files:
        try:
            if rf.suffix == ".gz":
                with gzip.open(rf, "rt", encoding="utf-8") as fh:
                    blob = json.load(fh)
            else:
                blob = json.loads(rf.read_text())
        except Exception:
            continue
        for content in blob.get("contents") or []:
            for p in content.get("pages") or []:
                ref[p["pageNumber"]] = collections.Counter(
                    norm("".join(w["content"] for w in p.get("words", []))))
    gc.collect()
    run = runs[-1]
    opener = gzip.open if run.suffix == ".gz" else open
    with opener(run, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    got = collections.defaultdict(collections.Counter)
    for item in doc.get("texts", []):
        for prov in item.get("prov", []):
            got[prov.get("page_no")].update(norm(item.get("text") or "")); break
    for table in doc.get("tables", []):
        pages = [pr.get("page_no") for pr in table.get("prov", [])]
        if pages:
            for cell in (table.get("data") or {}).get("table_cells") or []:
                got[pages[0]].update(norm(cell.get("text") or ""))
    del doc; gc.collect()
    out = {}
    for page, want in ref.items():
        total = sum(want.values())
        if total < 100:
            continue
        have = got.get(page, collections.Counter())
        out[page] = sum(min(n, have[t]) for t, n in want.items()) / total
    del ref, got; gc.collect()
    return out


def main() -> int:
    docs = sys.argv[1:] or [d.name for d in sorted(OUT.iterdir()) if d.is_dir()]
    every = []
    for doc_id in docs:
        covs = page_coverage(doc_id)
        if not covs:
            print(f"{doc_id:>10}  (no run or no reference)")
            continue
        v = list(covs.values()); every += v
        print(f"{doc_id:>10} {len(v):4d}p  mean {statistics.mean(v)*100:5.1f}%  "
              f"median {statistics.median(v)*100:5.1f}%  "
              f">=95% {sum(1 for x in v if x >= .95):4d}  "
              f"80-95% {sum(1 for x in v if .8 <= x < .95):3d}  "
              f"<50% {sum(1 for x in v if x < .5):3d}")
    if every:
        print(f"\n{len(every)} pages across {len(docs)} documents")
        print(f"  mean {statistics.mean(every)*100:.1f}%  median {statistics.median(every)*100:.1f}%")
        for lo, hi, label in ((.95, 1.01, ">=95%"), (.8, .95, "80-95%"),
                              (.5, .8, "50-80%"), (0, .5, "<50%")):
            n = sum(1 for x in every if lo <= x < hi)
            print(f"  {label:>7}: {n:5d} ({n/len(every)*100:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
