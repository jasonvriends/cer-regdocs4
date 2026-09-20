# runs

One file per run configuration: `<run id>.py` is the exact `ingest.py` that
produced the outputs in `output/<doc>/<run id>/`.

The run id is a hash of every setting that changes the output, including the
script's own hash, so the same configuration always lands in the same place and
any change produces a new one.

These are committed rather than ignored. A run made from an edited working copy
records `"dirty": true` in its meta and cannot be recovered from the repository
history — for those runs this directory holds the only copy of the code behind
the extraction, which is the whole reason the copy exists.

Nothing here should be edited. To change behaviour, edit `ingest.py` in the
repository root; the next run copies itself in under a new id.
