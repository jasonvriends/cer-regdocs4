# runs

One entry per run configuration: whatever produced the outputs in
`output/<doc>/<run id>/` is recorded here under the same id.

The run id is a hash of every setting that changes the output, including the
producing code's own hash, so the same configuration always lands in the same
place and any change produces a new one.

## Our extractions — `<run id>.py`

`<run id>.py` is the exact `ingest.py` that produced that run.

These are committed rather than ignored. A run made from an edited working copy
records `"dirty": true` in its meta and cannot be recovered from the repository
history — for those runs this directory holds the only copy of the code behind
the extraction, which is the whole reason the copy exists.

### Pinned runs -- `<run id>.<hash>.py`

A run pinned with `ingest.py --run-id=<id>` keeps its id while the code
changes. Each version of the code that produced documents under it is kept as
`<run id>.<first 8 of its sha256>.py` beside the original, and each document's
meta names the copy it came from (`ingest.copy`). No document is left without
the code that made it.

## Reference extractions — `az<hash>/`

Run ids beginning `az` are **not ours**. They hold the reference extraction
that `output/<doc>/az<hash>/<doc>.azure.json.gz` came from: the cer-regdocs2
stage scripts that called Azure Content Understanding, copied here as a
directory because more than one file produced them.

They are copied for a stronger reason than our own scripts are. That analysis
was a paid API call made by another pipeline against a specific analyzer
version; it cannot be re-run from this repository, and if cer-regdocs2 changes
those scripts there is no other record of which code produced these bytes.

Imported by `tools/import_azure.py`, which is idempotent — a document already
present under the same id is skipped.

## Both kinds

Nothing here should be edited. To change extraction behaviour, edit
`ingest.py` in the repository root; the next run copies itself in under a new
id.
