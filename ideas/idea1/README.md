# Idea 1 — Asset ownership ledger

**Can a regulatory asset inventory be built out of the docling extractions, so
that when a company changes its name or sells an asset there is a record of what
it held and the source document proving each step?**

Built on top of the extractor in the repo root; nothing here changes `ingest.py`.

| file | what it is |
|---|---|
| [`inventory.py`](inventory.py) | stage 1 — pattern extraction of entities and relationships from a docling JSON |
| [`schema.sql`](schema.sql) | the ledger: append-only, bitemporal, evidence-anchored |
| [`ledger.py`](ledger.py) | loads extractions into the ledger, resolves company names to stable entity IDs, answers holdings/history queries |
| [`ledger.md`](ledger.md) | the design, the entity-resolution rules, and the corpus strategy |
| [`inventory_prototype.md`](inventory_prototype.md) | what stage 1 found in 4647200, and the failure modes that shaped it |

## Run it

```bash
.venv/bin/python ideas/idea1/ledger.py init
.venv/bin/python ideas/idea1/ledger.py load 4647200
.venv/bin/python ideas/idea1/ledger.py holdings "Foothills"
.venv/bin/python ideas/idea1/ledger.py review
```

Paths resolve against this file, so the commands work from any directory.
`ledger.db` is written here and is gitignored, as is the optional Obsidian vault
that `inventory.py --vault` can produce.

## Where it got to

Working: the schema, the loader, entity resolution against a registry seed, the
review queue, and the holdings/history queries. Loading 4647200 resolves all
seven companies to registry entities with zero ambiguity, carrying every
spelling seen — including the OCR damage — so a later filing that writes a name
differently still resolves to the same entity.

Not working yet, and honestly so: `transfer` and `corporate_event` are nearly
empty. Only 2 of 90 assets carry an ownership assertion, because this filing is
a water quality monitoring report and simply does not say who owns the
monitoring stations.

That is the main finding, and it is about corpus selection rather than
extraction: the sixteen filings were chosen for size, to stress the extractor,
so they contain almost no ownership content. Ownership changes live in transfer
applications and Commission orders. Only **0.26%** of the 150,384 extracted text
elements contain any ownership language at all — which is also why an LLM
belongs on retrieved candidate passages rather than on every page.

See [ledger.md](ledger.md) for the reasoning and the suggested order of work.
