#!/usr/bin/env python3
"""Decide what needs a human, from observations the extraction already stored.

    python tools/triage.py [--write] [doc_id ...]

This is policy, deliberately outside ingest.py. The extraction records what it
saw; this decides what that means today. Changing the decision must not imply
the extraction changed, so it carries its own version and never touches a run
signature -- rerunning with a new policy rewrites triage.json and nothing else.

Tiers, in the order a reviewer should work them:

  review_numeric       a value read two ways: one reading holds a number the
                       other lacks and vice versa, differing by a character
                       operation, found and missed by the same variant
  review_structural    the two table models disagreed about the grid, or a
                       table lost most of its cells
  review_completeness  the page came out empty or not character-like
  diagnostic           real observations that did not predict error: unpaired
                       numeric disagreement, thinness, empty OCR events
  observation          everything above is derived from the meta, which keeps
                       the underlying detail regardless of tier

Why those are separated: page coverage against another extractor cannot see a
lost comparator, a single wrong digit, or a correct value in the wrong row. A
rule that catches those will score near zero against coverage and still be the
only thing standing between a misread detection limit and a dataset. Tiers are
ranked by consequence, not by measured precision.
"""
import collections, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pairing import page_candidates

POLICY_VERSION = 1
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"

# Every substitution class goes to review, including comparator changes. Those
# are 19 pages in this corpus and a lost "<" turns a detection limit into a
# measurement; volume is negligible and consequence is not.
NUMERIC_CLASSES = {"comparator_change", "magnitude_shift", "digit_substitution",
                   "digit_insertion", "sign_change", "exponent_change", "decimal_shift"}
STRUCTURAL = {"table_grid_disputed", "table_mostly_dropped"}
COMPLETENESS = {"almost_no_text", "not_character_like"}
DIAGNOSTIC = {"variants_disagree_on_numbers", "thin_for_this_document",
              "ocr_empty", "bbox_clamped", "needed_retry"}


def triage_page(row: dict) -> dict:
    codes = {d["code"] for d in row.get("doubts", [])}
    pairs = [c for c in page_candidates(row.get("variant_agreement") or {})
             if c["operation"] in NUMERIC_CLASSES]
    tiers, why = [], []
    if pairs:
        tiers.append("review_numeric")
        why += [f"{c['operation']}: kept {c['kept']!r} vs {c['other']!r} "
                f"({'+'.join(c['variants'])})" for c in pairs[:6]]
    if codes & STRUCTURAL:
        tiers.append("review_structural"); why += sorted(codes & STRUCTURAL)
    if codes & COMPLETENESS:
        tiers.append("review_completeness"); why += sorted(codes & COMPLETENESS)
    if not tiers and (codes & DIAGNOSTIC):
        tiers.append("diagnostic"); why += sorted(codes & DIAGNOSTIC)
    return {"page": row["page"], "tiers": tiers, "why": why,
            "shape": row.get("extracted_shape"), "staging": row.get("staging")}


def main() -> int:
    write = "--write" in sys.argv
    docs = [a for a in sys.argv[1:] if not a.startswith("-")] or \
           [d.name for d in sorted(OUT.iterdir()) if d.is_dir()]
    totals = collections.Counter(); pages = 0
    for doc_id in docs:
        metas = sorted((OUT / doc_id).glob(f"*/{doc_id}.docling.meta.json"))
        if not metas:
            continue
        meta = json.loads(metas[0].read_text())
        rows = [triage_page(r) for r in meta.get("pages", [])]
        counts = collections.Counter(t for r in rows for t in r["tiers"])
        pages += len(rows); totals.update(counts)
        queue = sum(v for k, v in counts.items() if k.startswith("review"))
        print(f"{doc_id:>10} {len(rows):4d}p  review {queue:4d}  " +
              "  ".join(f"{k.replace('review_','')}={counts[k]}" for k in
                        ("review_numeric", "review_structural", "review_completeness"))
              + f"  diagnostic={counts['diagnostic']}")
        if write:
            (metas[0].parent / "triage.json").write_text(json.dumps({
                "policy_version": POLICY_VERSION, "doc_id": doc_id,
                "run_id": meta.get("run_id"), "counts": dict(counts),
                "pages": [r for r in rows if r["tiers"]],
            }, indent=2) + "\n")
    q = sum(v for k, v in totals.items() if k.startswith("review"))
    print(f"\n{pages} pages | review queue {q} ({q/max(pages,1)*100:.1f}%)")
    for tier in ("review_numeric", "review_structural", "review_completeness", "diagnostic"):
        print(f"  {tier:>20}: {totals[tier]:5d} ({totals[tier]/max(pages,1)*100:.1f}%)")
    if write:
        print("\nwrote triage.json beside each run's meta")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
