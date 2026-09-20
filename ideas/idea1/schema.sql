-- Regulatory asset ownership ledger.
--
-- Three rules, which the whole schema exists to enforce:
--
-- 1. Nothing is ever UPDATEd or DELETEd. A correction is a new row that
--    supersedes an old one. A regulatory record that can be silently edited is
--    not a record.
-- 2. Every claim points at immutable evidence: document, page, element, and the
--    exact quote, hashed. If a re-extraction changes the quote, the hash breaks
--    and the change is visible.
-- 3. A name change is not an ownership change. "TransCanada PipeLines Limited"
--    becoming "TC Energy Corporation" is the same legal entity under a new name;
--    Foothills selling a pipeline is a different fact. Conflating them is the
--    classic way these inventories go wrong, so names and ownership live in
--    separate tables with separate timelines.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- evidence: immutable, append-only, referenced by everything else
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS document (
    doc_id          TEXT PRIMARY KEY,   -- CER REGDOCS document id
    filing_id       TEXT,               -- e.g. C38088
    title           TEXT,
    filed_date      TEXT,               -- ISO 8601
    filing_company  TEXT,               -- as REGDOCS states it (authoritative)
    page_count      INTEGER,
    source_url      TEXT,
    sha256          TEXT,               -- of the PDF
    ingested_at     TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id     INTEGER PRIMARY KEY,
    doc_id          TEXT NOT NULL REFERENCES document(doc_id),
    page            INTEGER,
    element_ref     TEXT,               -- docling self_ref, e.g. #/texts/18
    quote           TEXT NOT NULL,      -- exact supporting passage
    quote_sha256    TEXT NOT NULL,      -- detects silent drift on re-extraction
    method          TEXT NOT NULL,      -- table-row | cue-phrase | llm-span-verified | registry
    extractor       TEXT,               -- script name + version/hash
    extracted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evidence_doc ON evidence(doc_id, page);

-- --------------------------------------------------------------------------
-- legal entities: identity is stable, names are not
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS legal_entity (
    entity_id       TEXT PRIMARY KEY,   -- internal stable id, never reused
    registry_source TEXT,               -- corporations_canada | cer | manual
    registry_number TEXT,               -- corporate number where known
    jurisdiction    TEXT,
    created_at      TEXT NOT NULL
);

-- Every spelling ever seen, including misreadings, tied to one entity and to
-- the period the name was in use. This is what makes a corpus-wide rename
-- survivable: you resolve a mention to an entity_id, not to a string.
CREATE TABLE IF NOT EXISTS entity_name (
    name_id         INTEGER PRIMARY KEY,
    entity_id       TEXT NOT NULL REFERENCES legal_entity(entity_id),
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL,      -- normalised key used for matching
    kind            TEXT NOT NULL,      -- legal | trade | abbreviation | ocr_variant
    valid_from      TEXT,               -- when this name took effect (NULL = unknown)
    valid_to        TEXT,               -- NULL = still current
    evidence_id     INTEGER REFERENCES evidence(evidence_id),
    confidence      REAL,
    review_status   TEXT NOT NULL DEFAULT 'unreviewed'
);
CREATE INDEX IF NOT EXISTS ix_entity_name_norm ON entity_name(name_norm);
CREATE INDEX IF NOT EXISTS ix_entity_name_ent  ON entity_name(entity_id);

-- Renames, amalgamations, continuances, acquisitions of the entity itself.
CREATE TABLE IF NOT EXISTS corporate_event (
    event_id        INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,      -- rename | amalgamation | acquisition | continuance | dissolution
    effective_date  TEXT,
    predecessor_id  TEXT REFERENCES legal_entity(entity_id),
    successor_id    TEXT REFERENCES legal_entity(entity_id),
    evidence_id     INTEGER REFERENCES evidence(evidence_id),
    confidence      REAL,
    review_status   TEXT NOT NULL DEFAULT 'unreviewed'
);

-- --------------------------------------------------------------------------
-- assets: the physical things that change hands
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS asset (
    asset_id        TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,      -- pipeline | section | station | valve | facility | equipment
    parent_asset_id TEXT REFERENCES asset(asset_id),
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_asset_parent ON asset(parent_asset_id);

CREATE TABLE IF NOT EXISTS asset_name (
    name_id         INTEGER PRIMARY KEY,
    asset_id        TEXT NOT NULL REFERENCES asset(asset_id),
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL,
    kind            TEXT NOT NULL,      -- designation | tag | ocr_variant
    valid_from      TEXT,
    valid_to        TEXT,
    evidence_id     INTEGER REFERENCES evidence(evidence_id),
    confidence      REAL,
    review_status   TEXT NOT NULL DEFAULT 'unreviewed'
);
CREATE INDEX IF NOT EXISTS ix_asset_name_norm ON asset_name(name_norm);

-- Geography is the most stable identifier an asset has: a pipeline gets
-- renamed and re-chainaged, but the ground it sits on does not move. This is
-- what lets the same asset be recognised across filings by different owners.
CREATE TABLE IF NOT EXISTS asset_location (
    location_id     INTEGER PRIMARY KEY,
    asset_id        TEXT NOT NULL REFERENCES asset(asset_id),
    kp              TEXT,               -- chainage as written
    easting         REAL,
    northing        REAL,
    srid            TEXT,               -- e.g. EPSG:26911
    place           TEXT,
    evidence_id     INTEGER REFERENCES evidence(evidence_id),
    confidence      REAL
);
CREATE INDEX IF NOT EXISTS ix_asset_loc ON asset_location(asset_id);

-- --------------------------------------------------------------------------
-- the ledger: who held what, when, and how we know
-- --------------------------------------------------------------------------

-- Bitemporal. valid_from/valid_to is when the fact was true in the world;
-- asserted_at is when we learned it. Both are needed: to defend a position you
-- must be able to answer "what did the record say on the day we acted on it",
-- which is not the same question as "what was true".
CREATE TABLE IF NOT EXISTS assertion (
    assertion_id    INTEGER PRIMARY KEY,
    asset_id        TEXT NOT NULL REFERENCES asset(asset_id),
    entity_id       TEXT NOT NULL REFERENCES legal_entity(entity_id),
    role            TEXT NOT NULL,      -- owner | operator | applicant | agent | consultant
    valid_from      TEXT,
    valid_to        TEXT,
    asserted_at     TEXT NOT NULL,      -- transaction time
    evidence_id     INTEGER NOT NULL REFERENCES evidence(evidence_id),
    confidence      REAL NOT NULL,
    method          TEXT NOT NULL,
    review_status   TEXT NOT NULL DEFAULT 'unreviewed',
    superseded_by   INTEGER REFERENCES assertion(assertion_id),
    note            TEXT
);
CREATE INDEX IF NOT EXISTS ix_assertion_asset  ON assertion(asset_id);
CREATE INDEX IF NOT EXISTS ix_assertion_entity ON assertion(entity_id);
CREATE INDEX IF NOT EXISTS ix_assertion_live   ON assertion(superseded_by) WHERE superseded_by IS NULL;

-- A transfer is a first-class event, not something inferred by diffing two
-- assertions. This is the table the whole exercise exists to fill.
CREATE TABLE IF NOT EXISTS transfer (
    transfer_id     INTEGER PRIMARY KEY,
    asset_id        TEXT NOT NULL REFERENCES asset(asset_id),
    from_entity_id  TEXT REFERENCES legal_entity(entity_id),
    to_entity_id    TEXT REFERENCES legal_entity(entity_id),
    effective_date  TEXT,
    instrument      TEXT,               -- e.g. CER order MO-012-2019
    evidence_id     INTEGER NOT NULL REFERENCES evidence(evidence_id),
    confidence      REAL NOT NULL,
    method          TEXT NOT NULL,
    review_status   TEXT NOT NULL DEFAULT 'unreviewed'
);

-- Anything a machine asserted that a human has not yet confirmed, ranked so the
-- expensive human attention goes where it changes the answer.
CREATE TABLE IF NOT EXISTS review_queue (
    item_id         INTEGER PRIMARY KEY,
    table_name      TEXT NOT NULL,
    row_id          INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    priority        INTEGER NOT NULL,   -- 1 = highest
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT,
    resolution      TEXT
);
CREATE INDEX IF NOT EXISTS ix_review_open ON review_queue(priority) WHERE resolved_at IS NULL;

-- --------------------------------------------------------------------------
-- views: the questions this exists to answer
-- --------------------------------------------------------------------------

-- Current holdings, name resolved as of today, with the proof attached.
CREATE VIEW IF NOT EXISTS v_current_holdings AS
SELECT
    a.asset_id,
    a.kind                AS asset_kind,
    (SELECT name FROM asset_name an WHERE an.asset_id = a.asset_id
      ORDER BY (an.valid_to IS NULL) DESC, an.confidence DESC LIMIT 1) AS asset_name,
    s.entity_id,
    (SELECT name FROM entity_name en WHERE en.entity_id = s.entity_id
       AND en.valid_to IS NULL ORDER BY en.kind = 'legal' DESC, en.confidence DESC
       LIMIT 1) AS entity_name,
    s.role,
    s.valid_from,
    s.valid_to,
    s.confidence,
    s.method,
    s.review_status,
    e.doc_id,
    e.page,
    e.element_ref,
    e.quote
FROM assertion s
JOIN asset a    ON a.asset_id = s.asset_id
JOIN evidence e ON e.evidence_id = s.evidence_id
WHERE s.superseded_by IS NULL
  AND s.valid_to IS NULL;

-- Chain of custody for one asset: every holder, in order, with the source.
CREATE VIEW IF NOT EXISTS v_chain_of_custody AS
SELECT
    t.asset_id,
    t.effective_date,
    t.from_entity_id,
    t.to_entity_id,
    t.instrument,
    t.confidence,
    t.review_status,
    e.doc_id, e.page, e.element_ref, e.quote
FROM transfer t
JOIN evidence e ON e.evidence_id = t.evidence_id
ORDER BY t.asset_id, t.effective_date;
