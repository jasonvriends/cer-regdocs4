"""Substitution candidates from stored numeric conflicts.

A single conflict is an omission relative to another reading. A substitution is
a *pair*: one reading has a value the other does not, and the other has a value
this one does not, on the same page, differing by a character operation, and
found/missed by the same variant. Either half alone is much weaker evidence.

The pipeline's "bidirectional" direction label is not this. It means one variant
had a token and another did not; it is reported here as mixed_variant_presence
to stop that reading leaking into results.
"""
import json, re, collections, sys
from pathlib import Path

OUT = Path("/home/jasonvriends/repos/cer-regdocs4/output")
# Values that repeat all over a filing and would pair combinatorially with
# anything: page numbers, years, bare small integers.
COMMON = re.compile(r"^[+-]?(?:\d|1\d|19\d\d|20\d\d)$")


def strip_num(t: str) -> tuple:
    """Split a token into comparator, sign, and digits, keeping what matters."""
    m = re.match(r"^([<>]?)\s*([+-]?)(.*)$", t)
    return m.group(1), m.group(2), m.group(3)


def operation(a: str, b: str) -> str | None:
    """How one token would have to change to become the other, if plausibly."""
    ca, sa, da = strip_num(a)
    cb, sb, db = strip_num(b)
    if ca != cb and da == db and sa == sb:
        return "comparator_change"          # <0.010 read as 0.010
    if sa != sb and da == db and ca == cb:
        return "sign_change"
    if ("e" in da.lower()) != ("e" in db.lower()):
        return "exponent_change"
    # Compare the values, not only the characters: 0.010 against 0.10 is a
    # tenfold error however the digits moved, and that is the one that must
    # not sit behind "an extra character somewhere".
    try:
        va, vb = float(da.replace(",", "")), float(db.replace(",", ""))
        if va and vb:
            ratio = max(va, vb) / min(va, vb)
            for power in (10, 100, 1000):
                if abs(ratio - power) < power * 0.001:
                    return "magnitude_shift"
    except ValueError:
        pass
    if da.replace(".", "") == db.replace(".", "") and da != db:
        return "decimal_shift"
    if len(da) == len(db):
        diff = [i for i, (x, y) in enumerate(zip(da, db)) if x != y]
        if len(diff) == 1 and da[diff[0]].isdigit() and db[diff[0]].isdigit():
            return "digit_substitution"
    if abs(len(da) - len(db)) == 1:
        short, long_ = (da, db) if len(da) < len(db) else (db, da)
        for i in range(len(long_)):
            if long_[:i] + long_[i + 1:] == short:
                return "digit_insertion"    # 1.1 read as 1.11
    return None


# A comparator lost turns a detection limit into a measurement; a magnitude
# shift moves it by a factor of ten. Those two first, whatever the characters
# did to get there.
SEPARATOR = {",", "."}


def magnitude_kind(a: str, b: str) -> str:
    """Why two values differ in magnitude, which is not one situation.

    A comma read as a period is a formatting difference and often not a number
    at all -- "(Table 9.3)24,25" is two footnote markers. A separator that
    appeared or vanished, or a digit that did, is a different matter. Keeping
    them apart lets priority follow the cause instead of the ratio.
    """
    _, _, da = strip_num(a)
    _, _, db = strip_num(b)
    if len(da) == len(db):
        diff = [i for i, (x, y) in enumerate(zip(da, db)) if x != y]
        if len(diff) == 1 and {da[diff[0]], db[diff[0]]} <= SEPARATOR:
            return "separator_substitution"
    bare_a = da.replace(",", "").replace(".", "")
    bare_b = db.replace(",", "").replace(".", "")
    if bare_a == bare_b:
        return "separator_moved"
    if abs(len(bare_a) - len(bare_b)) >= 1:
        return "digit_added_or_lost"
    return "value_differs"


PRIORITY = {"comparator_change": 0, "magnitude_shift": 1, "decimal_shift": 2,
            "exponent_change": 3, "sign_change": 4, "digit_substitution": 5,
            "digit_insertion": 6}


def page_candidates(agree: dict) -> list:
    conflicts = agree.get("numeric_conflicts") or []
    winner_missing = [c for c in conflicts if c.get("direction") == "winner_missing"]
    others_missing = [c for c in conflicts if c.get("direction") == "others_missing"]
    out = []
    for w in winner_missing:
        # which variants actually hold the value the winner lacks
        holders = {v for v, n in (w.get("other_counts") or {}).items() if n}
        for o in others_missing:
            lackers = {v for v, n in (o.get("other_counts") or {}).items() if not n}
            if not (holders & lackers):
                continue                      # different variants: weak evidence
            if COMMON.match(w["token"]) or COMMON.match(o["token"]):
                continue
            op = operation(w["token"], o["token"])
            if op:
                entry = {"kept": o["token"], "other": w["token"],
                         "operation": op, "variants": sorted(holders & lackers)}
                if op == "magnitude_shift":
                    entry["cause"] = magnitude_kind(o["token"], w["token"])
                out.append(entry)
    out.sort(key=lambda c: PRIORITY[c["operation"]])
    return out


def rows_for(doc_dir: Path):
    doc_id = doc_dir.name
    for meta in sorted(doc_dir.glob(f"*/{doc_id}.docling.meta.json")):
        return [(r["page"], r.get("variant_agreement") or {})
                for r in json.loads(meta.read_text()).get("pages", [])]
    rows = []
    for f in sorted(doc_dir.glob("*/chunks/*.warnings.json")):
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        rows.append((r.get("page_start"), r.get("variant_agreement") or {}))
    return rows


# --------------------------------------------------------------------------
# Regression fixtures: real conflicts, with what the analysis must say about
# them. These exist because the analysis is triage, and a change that quietly
# stops surfacing a disagreement -- or starts adjudicating one -- would
# otherwise be invisible.
# --------------------------------------------------------------------------
FIXTURES = [
    {
        "name": "footnote markers read as a decimal (4692070 p47)",
        # The page reads "(Table 9.3)24,25:" -- two footnote references. The
        # exact PDF text kept them; both OCR variants collapsed them into the
        # single number 24.25. The kept output is correct and the dissenters
        # agree with each other, so a majority vote would pick the wrong
        # reading. The analysis must surface the disagreement and stop there.
        "kept_variant": "as-is",
        "agreement": {"numeric_conflicts": [
            {"token": "24.25", "winner_count": 0,
             "other_counts": {"raster-sml": 1, "raster-hi": 1},
             "kind": "token_difference", "direction": "winner_missing"},
            {"token": "24,25", "winner_count": 1,
             "other_counts": {"raster-sml": 0, "raster-hi": 0},
             "kind": "token_difference", "direction": "others_missing"},
        ]},
        "expect": {"pairs": 1, "kept": "24,25", "other": "24.25",
                   "operation": "magnitude_shift",
                   "variants": ["raster-hi", "raster-sml"]},
    },
    {
        "name": "unrelated omissions must not pair",
        # One reading missed a value here, another missed a different value
        # there. Opposing directions, but no competing reading of the same
        # thing, so this is two omissions and not a substitution.
        "kept_variant": "as-is",
        "agreement": {"numeric_conflicts": [
            {"token": "884.5", "winner_count": 0, "other_counts": {"raster": 1},
             "kind": "token_difference", "direction": "winner_missing"},
            {"token": "17.25", "winner_count": 1, "other_counts": {"raster-hi": 0},
             "kind": "token_difference", "direction": "others_missing"},
        ]},
        "expect": {"pairs": 0},
    },
    {
        "name": "a lost comparator outranks everything",
        "kept_variant": "raster",
        "agreement": {"numeric_conflicts": [
            {"token": "0.010", "winner_count": 0, "other_counts": {"raster-hi": 1},
             "kind": "token_difference", "direction": "winner_missing"},
            {"token": "<0.010", "winner_count": 1, "other_counts": {"raster-hi": 0},
             "kind": "token_difference", "direction": "others_missing"},
        ]},
        "expect": {"pairs": 1, "kept": "<0.010", "other": "0.010",
                   "operation": "comparator_change", "variants": ["raster-hi"]},
    },
]


def selftest() -> int:
    failures = 0
    for fx in FIXTURES:
        got = page_candidates(fx["agreement"])
        want = fx["expect"]
        problems = []
        if len(got) != want["pairs"]:
            problems.append(f"expected {want['pairs']} pair(s), got {len(got)}")
        elif got:
            c = got[0]
            for key in ("kept", "other", "operation", "variants"):
                if c[key] != want[key]:
                    problems.append(f"{key}: expected {want[key]!r}, got {c[key]!r}")
        print(("FAIL " if problems else "ok   ") + fx["name"])
        for p in problems:
            print("       " + p)
        failures += bool(problems)
    print(f"{len(FIXTURES) - failures}/{len(FIXTURES)} fixtures pass")
    return 1 if failures else 0


if "--selftest" in sys.argv:
    raise SystemExit(selftest())

docs = [a for a in sys.argv[1:] if not a.startswith("-")] or [
    d.name for d in sorted(OUT.iterdir()) if d.is_dir()]
tp = tc = tpages = 0
ops = collections.Counter()
for name in docs:
    rows = rows_for(OUT / name)
    if not rows:
        continue
    pages_with, pairs, samples = 0, 0, []
    for page, agree in rows:
        cands = page_candidates(agree)
        if cands:
            pages_with += 1
            pairs += len(cands)
            ops.update(c["operation"] for c in cands)
            if len(samples) < 3:
                samples.append((page, cands[0]))
    tpages += len(rows); tp += pages_with; tc += pairs
    print(f"{name}: {len(rows)} pages | candidate pages {pages_with} "
          f"({pages_with/len(rows)*100:.0f}%) | candidate pairs {pairs}")
    for page, c in samples:
        print(f"      p{page}: kept {c['kept']!r} vs {c['other']!r} "
              f"({c['operation']}, {'+'.join(c['variants'])})")
if tpages:
    print(f"\nALL {tpages} pages | candidate pages {tp} ({tp/tpages*100:.0f}%) | pairs {tc}")
    print("  by operation: " + (", ".join(f"{k}={v}" for k, v in ops.most_common()) or "none"))
