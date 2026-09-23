#!/usr/bin/env python3
"""What finished, what did not, and where it stopped.

    python tools/status.py [output_dir]

A batch runs one document per process, so a hard crash -- a segmentation
fault in a native library, an OOM kill -- takes the interpreter with it and
writes nothing. The document is then simply absent, which looks the same as
one that was never started, and the only trace is the run's own ingest.log.
This reads that trace so a corpus can be checked in one place.

Exit status is 1 when anything is incomplete, so a batch can end with it.
"""
import json
import re
import sys
from pathlib import Path

LAST_PAGE = re.compile(r"\[ingest\] (\d+)/(\d+) pages")


def last_logged_page(run_dir: Path) -> tuple:
    log = run_dir / "ingest.log"
    if not log.exists():
        return None, None
    seen = (None, None)
    try:
        for line in log.read_text(errors="replace").splitlines():
            m = LAST_PAGE.search(line)
            if m:
                seen = (int(m.group(1)), int(m.group(2)))
    except Exception:
        pass
    return seen


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "output")
    if not root.exists():
        print(f"no {root} directory")
        return 1
    incomplete = []
    print(f"{'document':>10} {'run':>9} {'state':>12}  detail")
    for doc_dir in sorted(d for d in root.iterdir() if d.is_dir()):
        doc_id = doc_dir.name
        # A reference extraction (tools/import_azure.py) lives in a run
        # directory too, but it is not ours and cannot be "unfinished". It is
        # recognised by what it holds rather than by its name, so a renamed or
        # re-versioned import is still skipped.
        runs = sorted(d for d in doc_dir.iterdir() if d.is_dir()
                      and not (d / f"{doc_id}.azure.meta.json").exists())
        if not runs:
            print(f"{doc_id:>10} {'-':>9} {'not started':>12}")
            incomplete.append((doc_id, "not started", None))
            continue
        for run_dir in runs:
            meta = run_dir / f"{doc_id}.docling.meta.json"
            document = [run_dir / f"{doc_id}.docling.json.gz",
                        run_dir / f"{doc_id}.docling.json"]
            done, total = last_logged_page(run_dir)
            if meta.exists() and any(d.exists() for d in document):
                m = json.loads(meta.read_text())
                warn = m.get("warning_total", 0)
                print(f"{doc_id:>10} {run_dir.name:>9} {'complete':>12}  "
                      f"{m.get('page_count')} pages, {m.get('elapsed_seconds', 0):.0f}s, "
                      f"{warn} warning(s), {m.get('reprocess_pages', 0)} page(s) to review")
                continue
            chunks = len(list((run_dir / "chunks").glob("*.docling.json"))) \
                if (run_dir / "chunks").exists() else 0
            where = f"stopped after page {done} of {total}" if done else "no pages converted"
            state = "INCOMPLETE" if chunks else "EMPTY"
            print(f"{doc_id:>10} {run_dir.name:>9} {state:>12}  {where}; "
                  f"{chunks} page(s) banked, re-run to resume")
            incomplete.append((doc_id, where, chunks))
    # A PDF fetched without scouting it (ingest.py <url>, or a file copied in)
    # has no record, so its extraction carries no filing number, company or
    # facets. That is not an extraction failure and does not change the exit
    # code, but it should not go unnoticed.
    source = root.parent / "source"
    unrecorded = sorted(p.stem for p in source.glob("*.pdf")
                        if not (root / p.stem / f"{p.stem}.cer.meta.json").exists())
    if unrecorded:
        print(f"\n{len(unrecorded)} PDF(s) with no CER record: "
              + ", ".join(unrecorded[:10]) + (" ..." if len(unrecorded) > 10 else ""))
        print("  scout.py missing   gives them one")
    if incomplete:
        print(f"\n{len(incomplete)} document(s) did not finish:")
        for doc_id, where, chunks in incomplete:
            print(f"  {doc_id}: {where}"
                  + (f" ({chunks} page(s) already converted)" if chunks else ""))
        print("\nRe-running the same command resumes them; nothing is redone.")
        return 1
    print("\nall documents complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
