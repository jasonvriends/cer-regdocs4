# Entity extraction stage

> **Status:** the entity extraction described here is current and is reused as a
> library by `ledger.py`. The Obsidian vault it originally wrote was dropped —
> a vault shows one static snapshot and cannot record ownership changing over
> time. See [ledger.md](ledger.md) for what replaced it. The vault writer is
> still available via `--vault` for eyeballing a single filing.

`inventory.py` turns a docling extraction into an Obsidian vault: one Markdown
file per real-world entity, linked with `[[wikilinks]]`, every claim carrying a
pointer back to the page it was read from.

```bash
.venv/bin/python ideas/idea1/inventory.py 4647200      # -> ideas/idea1/inventory/4647200/
```

Then open `inventory/4647200/` as an Obsidian vault. `Inventory.md` is the index;
the graph view shows the document's asset structure.

It is a prototype. There is no database and no attempt at a cross-document
identity model — the vault is per filing.

## What it produced for 4647200

986 pages, 18,329 text elements and 887 tables in, 4 seconds, 260 notes out:

| category | count | what they are |
|---|---|---|
| companies | 7 | Foothills, TCPL, TC Energy, BGC, WSP, ALS, CALA |
| pipelines | 2 | British Columbia Mainline Loop No. 2, and its Elko Section |
| stations | 83 | the `ELKO-<chainage>-<type>-<US\|DS>` water quality monitoring network |
| valves | 0 | this filing contains none |
| facilities | 2 | the ALS Calgary laboratory, the WSP EQuIS facility |
| equipment | 3 | the YSI and Hanna field multimeters |
| reports | 38 | 36 ALS work orders, the WSP annual report, the BGC memo |
| locations | 83 | 77 KP chainage markers and 6 places |
| environment | 41 | 38 watercourse/drainage IDs, plus Pioneer Creek, Leach Creek, Banes Lake |
| documents | 1 | the filing itself |

484 relationships, all with evidence — 431 read from table rows and 53 from
prose, 177 at confidence 0.95:

| type | count |
|---|---|
| `MENTIONED_IN` | 143 |
| `PART_OF` | 132 |
| `LOCATED_AT` | 120 |
| `SAMPLED_AT` | 48 |
| `ANALYZED_BY` | 36 |
| `OPERATES` | 2 |
| `RETAINED_BY` | 2 |
| `AFFILIATE_OF` | 1 |

The corporate chain reads as the document states it:

> Foothills Pipe Lines (South B.C.) Ltd. **PART_OF** TransCanada Pipelines Limited
> **·** **AFFILIATE_OF** TC Energy Corporation **·** **OPERATES** Elko Section
> **·** **RETAINED** BGC Engineering Inc. and WSP Canada Inc.

## Where relationships come from

Only two sources, because these are the only two that are evidence:

**Table structure.** A row of the station registry puts a station, its
watercourse, its chainage and its coordinates on one line. That row is the
evidence for `station PART_OF watercourse` and `station LOCATED_AT KP`, and it is
quoted on both notes. This is where 431 of the 484 relationships come from.

**Cue phrases.** `PART_OF` between companies requires "a wholly owned subsidiary
of"; `OPERATES` requires "was constructed by" or "was operated by". The matched
sentence is stored with the relation and printed on the page, so a wrong reading
is visible rather than buried.

Co-occurrence is never evidence. Two entities on the same page, or in the same
paragraph, produce nothing.

## Relationship types

The six requested types are used where the document supports them. `CONTAINS` is
not stored: it is how a stored `PART_OF` renders on the parent's page, so each
relation is asserted once and read from both ends. `OWNS` is never emitted here,
because this filing expresses ownership only as "a wholly owned subsidiary of",
which is recorded as `PART_OF`.

Four further types are stored separately rather than forced into the six, because
collapsing them would assert more than the text does:

| type | cue | why not one of the six |
|---|---|---|
| `AFFILIATE_OF` | "affiliate of" | an affiliate is not a part or an owner |
| `RETAINED_BY` | "was retained by" | a consulting engagement is not ownership |
| `ANALYZED_BY` | ALS work order on an ALS certificate | lab relationship, not containment |
| `SAMPLED_AT` | station and work order on one row | links a result set to a site |

## Things that were harder than they looked

**Company names are the main source of false entities.** A regex for
`<Capitalised words> Ltd.` finds `Canada Ltd.`, `Accreditation Inc.` and
`Ltd. BGC Engineering Inc.` alongside the real names — table cells abut the name,
and a name longer than the regex window gets truncated from the left. Three rules
fixed it: strip leading role words ("Client", "Project", "Laboratory"); fold a
name that is a fragment of exactly one longer name into it (this is also how the
document's own shorthand, "ALS Ltd." for "ALS Canada Ltd.", resolves); and merge
keys within two edits of a commoner spelling, which catches the OCR slip
"Foothill Pipe Lines" for "Foothills Pipe Lines". Ambiguous fragments are kept
rather than attached to a guess. Two further rules came from looking at other
filings: a leading job title is stripped ("Chief Financial Officer of Enron
Corp." is a person's role), and a conjunction is split only when both halves are
complete names on their own, so "Kinder Morgan Canada Company and Kinder Morgan
Cochin ULC" becomes two companies while "ATCO Gas and Pipelines Ltd." stays one.

Seven companies out of 4647200, all real and all correctly named.

One rule had to be narrowed after it regressed this filing: accepting "Company"
as a terminal legal suffix admitted `To Company`, `Street Company` and — from the
315 mojibake pages — `Centact Company`, all table headers rather than
organisations. "Company" and "Partnership" are ordinary English words as well as
legal suffixes, so they now need at least three words to count, while `Ltd`,
`Inc`, `ULC` and `Corp` identify an organisation on their own. The display name
is likewise the *commonest* spelling rather than the longest, which is what keeps
"Foothills Pipe Lines (South B.C.) Ltd." from being titled after its misread
variant "(South B.C-)".

**Fuzzy matching is unsafe on asset IDs, and the near-miss is the dangerous
case.** `ELKO-111.50-WC-DS` and `ELKO-111.50-WC-US` are one edit apart and are
the downstream and upstream stations of the same creek. The edit-distance rule
that fixes company names would silently fuse them, losing exactly the distinction
the monitoring program exists to make. Station IDs are repaired against their
grammar instead: a non-KP chainage is written to two decimals, so
`ELKO-15400-WC-US` is recognised as `ELKO-154.00-WC-US` by structure, not
similarity.

**A wrapped table cell invents an entity.** `ELKO-155.60-WC` with no position
suffix is a cell that wrapped before `US`. Where exactly one full ID extends the
truncated one, they are merged. Where both a `-US` and a `-DS` station exist the
reading is genuinely ambiguous, so the note is kept and carries
`Incomplete reading:` naming both candidates. Four notes are in that state; that
is a property of the source, not something to average away.

**One cue phrase can carry two relations with one subject.** "Foothills, a wholly
owned subsidiary of TCPL and affiliate of TC Energy" is a coordinated appositive:
both clauses are about Foothills. Reading the nearest company to the left of each
cue independently produced "TCPL affiliate of TC Energy", which the document does
not say. Appositives are now matched whole, before any single cue is tried, and a
sentence yields at most one reading.

## Self-checks

The run fails rather than writing a broken vault if any `[[wikilink]]` does not
resolve, or if two notes would share a name — a dangling link is an invisibly
broken graph. Current run: 2,831 links, 0 dangling, 0 collisions.

Categories with no evidence are written as empty directories and listed in
`Inventory.md` under "Empty categories". `valves` is empty for 4647200 because the
filing genuinely contains no valve tags; the extractor looks for
`MLV`/`MLBV`/`BV`/`RTU` tags and requires the word "valve" nearby, so a filing
that has them will populate it.

## How far it generalises

The vault-writing, evidence model and self-checks are document-agnostic. The
entity patterns are not, and the split is worth being explicit about, because the
counts look healthier than the precision is on filings this was not tuned
against. Run over three other extractions in `output/`:

| filing | companies | notes | links | consistent |
|---|---|---|---|---|
| 4572347 | 14 | ~60 | 184 | yes |
| 4600563 | 85 | ~140 | 577 | yes |
| 4646669 | 241 | ~270 | 1042 | yes |

Nothing crashed and every vault was internally consistent, but 4646669 is a set
of legal decisions citing many corporate parties, and while most of its 241
companies are real (`Husky Oil Operations Limited`, `Kinder Morgan Cochin ULC`),
a residue is not: person names run into the company beside them
(`Robert Prevost and Nicole Coutts-Prevost Trans Mountain Pipeline ULC`), and
bare fragments survive (`Alberta Ltd.`, `Agency Corp.`). Fixing those means
grounding the patterns in that corpus the way they were grounded in this one —
sampling what the documents actually say before writing a regex — rather than
guessing from here.

Stations, reports, facilities and equipment come out empty on all three, as
expected: the `ELKO-` station grammar and the ALS work-order format are specific
to this filing's monitoring program and laboratory.

## Known rough edges

- **KP markers fragment.** `KP 11+240`, `KP 11+241` and `KP 11+300` are separate
  notes. They come from different tables, and the same station is placed at
  11+241 by BGC and 11+300 by WSP. Rounding them together would invent a
  precision the document does not have, so both are kept with their evidence —
  arguably the more useful answer, since the disagreement is visible.
- **Entity vocabulary is tuned to this filing.** Station and watercourse ID
  grammars, the place gazetteer and the instrument list were read off this
  corpus. A filing about compressor stations would need its own patterns; the
  extraction passes are separate functions so that is additive.
- **Evidence is capped at 12 quotes per note**, chosen by page order. A station
  cited on 48 rows shows the first 12.
- **Reports are titles, not documents.** A "CERTIFICATE OF ANALYSIS" heading
  becomes a report note only when the title line is distinctive; the certificates
  themselves are represented by their ALS work order, which is the stable ID.
