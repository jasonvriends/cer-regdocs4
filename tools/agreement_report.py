"""Directional breakdown of variant disagreement, counted by page and by conflict.

Reads finished runs from output/<doc>/<run>/, or in-flight chunk sidecars when a
document is still converting. Pages are the denominator: a handful of dense
table pages can carry most of the conflicts in a document and say nothing about
how often disagreement happens.
"""
import json, collections, gzip, sys
from pathlib import Path

OUT = Path("/home/jasonvriends/repos/cer-regdocs4/output")
DIRECTIONS = ("winner_missing", "others_missing", "bidirectional")


def rows_for(doc_dir: Path):
    """Page rows from a finished meta, else from chunk sidecars mid-run."""
    doc_id = doc_dir.name
    for meta in sorted(doc_dir.glob(f"*/{doc_id}.docling.meta.json")):
        return json.loads(meta.read_text()).get("pages", []), meta.parent.name, True
    chunks = sorted(doc_dir.glob("*/chunks/*.warnings.json"))
    rows = []
    for f in chunks:
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        rows.append({"page": r.get("page_start"),
                     "extracted_shape": r.get("extracted_shape"),
                     "staging": r.get("staging"),
                     "agreement": (r.get("variant_agreement") or {}).get("verdict"),
                     "variant_agreement": r.get("variant_agreement"),
                     "doubts": []})
    run = chunks[0].parent.parent.name if chunks else "-"
    return rows, run, False


def report(doc_dir: Path):
    rows, run, done = rows_for(doc_dir)
    if not rows:
        return None
    verdicts = collections.Counter(r.get("agreement") for r in rows)
    pages_with = collections.Counter()          # pages carrying each direction
    conflicts_by = collections.Counter()        # conflicts of each direction
    per_shape = collections.defaultdict(collections.Counter)
    dissent = collections.Counter()
    winner = collections.Counter()
    flagged_counts = []
    for r in rows:
        a = r.get("variant_agreement") or {}
        winner[r.get("staging")] += 1
        seen = set()
        n = 0
        for c in a.get("numeric_conflicts") or []:
            d = c.get("direction")
            if not d:
                continue
            conflicts_by[d] += 1
            seen.add(d)
            n += 1
            for name, cnt in (c.get("other_counts") or {}).items():
                dissent[name] += 1
        for d in seen:
            pages_with[d] += 1
            per_shape[r.get("extracted_shape")][d] += 1
        if n:
            flagged_counts.append(n)
    flagged_counts.sort()
    return {
        "doc": doc_dir.name, "run": run, "finished": done, "pages": len(rows),
        "verdicts": verdicts, "pages_with": pages_with, "conflicts_by": conflicts_by,
        "per_shape": per_shape, "dissent": dissent, "winner": winner,
        "median_conflicts": flagged_counts[len(flagged_counts)//2] if flagged_counts else 0,
        "flagged_pages": len(flagged_counts),
    }


docs = sys.argv[1:] or [d.name for d in sorted(OUT.iterdir()) if d.is_dir()]
total = collections.Counter(); tpages = 0; tv = collections.Counter()
for name in docs:
    r = report(OUT / name)
    if not r:
        continue
    tpages += r["pages"]; total.update(r["pages_with"]); tv.update(r["verdicts"])
    state = "done" if r["finished"] else "in flight"
    print(f"{r['doc']} [{r['run']}, {state}] {r['pages']} pages")
    print("   verdicts: " + ", ".join(f"{k}={v} ({v/r['pages']*100:.0f}%)"
                                      for k, v in r["verdicts"].most_common()))
    print("   pages with: " + ", ".join(f"{d}={r['pages_with'][d]}" for d in DIRECTIONS)
          + " | conflicts: " + ", ".join(f"{d}={r['conflicts_by'][d]}" for d in DIRECTIONS)
          + f" | median/flagged page={r['median_conflicts']}")
    print(f"   winner: {dict(r['winner'])} | dissent: {dict(r['dissent'].most_common(3))}")
    for shape, c in sorted(r["per_shape"].items()):
        if sum(c.values()):
            print(f"      {shape:>14}: " + ", ".join(f"{d}={c[d]}" for d in DIRECTIONS))
if tpages:
    print(f"\nALL {tpages} pages | verdicts " +
          ", ".join(f"{k}={v} ({v/tpages*100:.0f}%)" for k, v in tv.most_common()))
    print("  pages with: " + ", ".join(f"{d}={total[d]} ({total[d]/tpages*100:.0f}%)"
                                       for d in DIRECTIONS))
