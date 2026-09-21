# Ideas for using extracted CER regulatory data

This document collects possible products and research projects built on the
REGDOCS extraction pipeline. The common design rule is that every extracted
fact should retain its document, page, element, exact supporting text, and
extraction-quality signals. These are decision-support tools, not substitutes
for regulatory judgment.

## How to compare the ideas

The decision matrix gives every idea the same fields. **Impact** is the likely
value of a successful implementation. **Effort** estimates an evidence-backed
prototype built with AI assistance, not a secure production service. **Data
readiness** indicates how much of the needed information exists in the current
extractions: High means a prototype can start now; Medium means targeted
documents or modest labelling are needed; Low means a materially different
corpus or external data is required. H, M, and L mean high, medium, and low.

| # | Idea | Description | Impact | Effort | Data readiness | Primary audience | One-week proof | Main dependency or risk |
|---:|---|---|:---:|:---:|:---:|---|---|---|
| 1 | Asset ownership and history ledger | Evidence-backed history of asset owners, operators, names, and transfers | H | H | M | CER analysts, industry | Load one transfer-rich filing and show an asset history | Current corpus contains little ownership language |
| 2 | Application completeness checker | Compare a submission with expected filing components | H | H | M | Applicants, intake reviewers | Run one application against a small requirement set | Requirements must be encoded and findings validated |
| 3 | Requirement-to-evidence matrix | Link each requirement to the exact submitted evidence | H | M | M | Reviewers, applicants | Build a matrix for 10–20 requirements | Presence does not prove adequacy |
| 4 | Information-request predictor | Retrieve likely questions from similar historical review issues | H | H | L | Reviewers, applicants | Match one filing to a curated set of prior IRs | Needs applications, IRs, and responses linked together |
| 5 | Similar-project precedent finder | Find comparable projects, decisions, issues, and conditions | H | H | M | Analysts, legal teams | Produce explainable similarity for five projects | “Similar” is purpose-dependent and can mislead |
| 6 | Cross-document consistency checker | Find conflicting facts across a filing | H | M | H | Technical reviewers, applicants | Check dates, coordinates, quantities, and names in one filing | Legitimate revisions can look like contradictions |
| 7 | Environmental risk and mitigation register | Structure effects, receptors, mitigations, monitoring, and owners | H | H | H | Environmental reviewers | Populate a register for one assessment section | Relationships in prose need careful validation |
| 8 | Indigenous and stakeholder commitment tracker | Trace concerns, responses, promises, owners, and status | H | H | M | Engagement teams, participants | Validate 10 commitment chains | Must preserve voice, context, and access controls |
| 9 | Drawing, route, and location intelligence | Convert locations and drawing references into spatial data | H | H | M | GIS and engineering teams | Map extracted stations and crossings | Drawing interpretation and coordinate systems vary |
| 10 | Permit timeline and bottleneck analytics | Reconstruct process events and elapsed time | H | H | L | Executives, process teams | Build one manually validated case timeline | Delay cannot be causally assigned from dates alone |
| 11 | Regulatory knowledge graph | Connect entities, projects, evidence, decisions, and places | M | H | M | Analysts, researchers | Navigate one filing as an evidence graph | Overlaps Idea 1; avoid competing truth stores |
| 12 | Conditions library and recommender | Retrieve comparable approval conditions with context | H | H | L | Commissioners, legal and policy teams | Cluster conditions from a small decision set | Historical wording is not automatically appropriate |
| 13 | Commitment drift detector | Detect promises that disappear, weaken, or change | H | H | M | Reviewers, compliance teams | Compare commitments across two filing versions | Semantic changes require human interpretation |
| 14 | Environmental baseline atlas | Combine observations and measurements across place and time | H | H | M | Scientists, communities | Map one monitoring program with a time slider | Methods and sampling designs may not be comparable |
| 15 | Laboratory results explorer | Normalize and explore measurements, limits, units, and methods | M | M | H | Scientists, compliance teams | Interactive charts for one certificate bundle | Values such as `<0.010` must remain exact |
| 16 | Filing evolution viewer | Explain meaningful changes between document versions | H | M | M | Reviewers, applicants | Side-by-side semantic diff of two versions | Requires reliable document/version matching |
| 17 | Evidence contradiction network | Show conflicting assertions across the record | H | H | M | Analysts, auditors | Visualize ten validated conflicts | Later corrections must not be framed as errors |
| 18 | Indigenous and public concern taxonomy | Organize concerns while retaining original wording | H | H | M | Public engagement and policy teams | Cluster one comment-letter set | Automated labels can erase nuance or voice |
| 19 | Regulatory citation and precedent map | Map laws, standards, cases, decisions, and citations | M | M | H | Legal and policy teams | Citation graph for the authorities corpus | Citation mentions need entity resolution |
| 20 | Plain-language project explorer | Evidence-linked public explanation of a project | H | M | H | Public, communities, media | Publish one accessible project microsite | Summaries must expose uncertainty and omissions |
| 21 | Document quality observatory | Measure searchability, accessibility, OCR, and table quality | H | M | H | Intake, records, applicants | Quality dashboard for the current corpus | Extraction difficulty is not substantive deficiency |
| 22 | Regulatory record semantic search | Search concepts across prose, tables, and metadata | H | M | H | All internal users, researchers | Search one corpus with cited results and filters | Retrieval quality and permissions need evaluation |
| 23 | Study reuse and duplication detector | Find overlapping studies and reused evidence | M | M | M | Reviewers, applicants | Cluster similar reports and show overlaps | Reuse is not necessarily inappropriate |
| 24 | Dataset passport and lineage tracker | Record origin, method, transformations, and reuse of data | H | H | M | Scientists, data stewards | Create passports for five important tables | Lineage is often implicit rather than stated |
| 25 | Condition compliance early-warning dashboard | Connect conditions, deadlines, filings, and evidence | H | H | L | Compliance teams, companies | Track a small set of conditions end to end | Requires a condition/compliance filing corpus |
| 26 | Cumulative-effects history for a place | Show projects, effects, mitigation, and monitoring by geography | H | H | M | Regional analysts, communities | Build one watershed or corridor history | Geographic/entity reconciliation is substantial |
| 27 | Standards and guidance impact scanner | Find records affected by a changed rule or standard | M | M | M | Policy, legal, program owners | Trace references to one superseded standard | Citations may omit version numbers |
| 28 | Review workload and expertise router | Estimate subject-matter review demand from filing content | M | M | M | Review managers | Produce a section-level expertise heatmap | Must not become opaque employee scoring |
| 29 | English–French equivalence checker | Flag potentially material differences between language versions | H | H | H | Translation and legal teams | Align the existing bilingual report and compare numbers | Qualified bilingual review remains essential |
| 30 | Privacy and sensitive-information quality check | Find exposed personal data and ineffective redactions | H | M | H | Privacy, records, filing teams | Scan a controlled sample for candidate exposures | Sensitive output needs strict handling and access |
| 31 | Regulatory data benchmark and research corpus | Publish evaluated difficult pages and extraction tasks | M | H | H | Researchers, vendors, digital teams | Release an internal benchmark and scorecard | Publication requires rights, privacy, and security review |
| 32 | Sixty-second filing x-ray | One-screen visual briefing of a large filing | H | M | H | Executives, reviewers, public | Interactive dashboard for one 900-page filing | Compression can hide nuance; link every claim |
| 33 | Ask the filing with courtroom-style citations | Evidence-only conversational access to a filing | H | M | H | Reviewers, public, applicants | Answer a test set with claim-level citations | Unsupported synthesis must fail closed |
| 34 | Anatomy of a regulatory delay | Animated case study of an application's journey | H | M | L | Executives, process teams, public | One validated event-and-issue timeline | More lifecycle documents are needed |
| 35 | Evidence flight recorder | Trace a fact or commitment through the record | H | M | M | Reviewers, auditors, public | Show 5–10 human-confirmed provenance trails | Text similarity alone cannot establish derivation |
| 36 | Copy-paste genealogy | Visualize reused passages, tables, and templates | M | L | H | Reviewers, researchers | Interactive similarity family tree for the corpus | Do not imply plagiarism or invalidity from reuse |
| 37 | Regulatory record control-room map | Explore evidence and monitoring spatially and over time | H | M | H | Reviewers, communities, executives | Polished map of the Foothills monitoring network | Some chainage needs a route geometry to geolocate |
| 38 | Where did my concern go? | Trace a participant's concern through response and outcome | H | H | M | Participants, engagement teams | Several manually verified concern trails | Inferred links and unresolved concerns need careful labels |
| 39 | Invisible-document challenge | Reveal the machine-readable reality behind visual pages | H | L | H | Executives, applicants, accessibility teams | Before/after viewer using known broken documents | Frame as improvement evidence, not applicant blame |
| 40 | One-click regulatory briefing book | Generate role-specific, source-linked briefings | H | M | H | Executives, technical teams, public | Two audience versions of one filing | Different audiences still need the same factual base |
| 41 | Red-team the application | Produce cited questions a skeptical reviewer may ask | H | M | H | Applicants, reviewers | Human-rate ten evidence-grounded questions | Must not present model questions as CER conclusions |
| 42 | Regulatory pulse: what changed? | Daily or weekly digest of new facts, commitments, and decisions | H | M | M | Executives, analysts, companies | Compare two corpus snapshots and generate a cited digest | Requires reliable change and subscription metadata |
| 43 | Promise-versus-reality scoreboard | Compare predicted effects and mitigation with later monitoring | H | H | L | Compliance, policy, public | Back-test one predicted metric against one follow-up series | Needs linked applications and post-approval reports |
| 44 | Reviewer memory engine | Recover prior reasoning, questions, and accepted evidence by issue | H | M | L | CER reviewers | Issue page for one topic using curated historical records | Needs internal or public review artifacts and permissions |
| 45 | Public comment constellation | Visually map themes, agreements, differences, and unique concerns | H | M | H | Public, engagement teams, decision makers | Interactive map of one comment-letter collection | Majority frequency must not outweigh rights or unique evidence |
| 46 | Regulatory evidence API | Expose documents, elements, tables, entities, and provenance as data | H | H | H | Internal developers, researchers, civic technologists | Read-only API for one corpus with stable citations | Governance, versioning, licensing, and access control |
| 47 | Emergency-plan tabletop generator | Turn filed plans and asset facts into evidence-linked exercise scenarios | H | H | L | Emergency management, operators | Generate and facilitate one bounded scenario | Safety-critical outputs require expert design and validation |
| 48 | Climate resilience stress lens | Find assets, assumptions, and mitigations sensitive to future hazards | H | H | M | Engineering, environment, policy | Screen one project for cited climate-sensitive assumptions | Requires external hazard data for credible risk estimates |
| 49 | Regulatory twin of a project | Interactive model connecting design, location, effects, commitments, and conditions | H | H | M | Executives, reviewers, proponents | Clickable twin of one small project or subsystem | Scope must stay narrow; it is not an engineering simulation |
| 50 | Decision replay room | Recreate what evidence was available at a past decision date | H | H | L | Audit, legal, training, policy | Replay one decision with a time-filtered evidence bundle | Filing timestamps and supersession must be complete |

## 1. Asset ownership and history ledger

Track assets, owners, operators, corporate name changes, transfers, locations,
and the documents proving each event. The ledger is append-only and
bitemporal, so it can answer both “what was true?” and “what did the record say
at the time?”

Status: a working prototype exists in `ideas/idea1/`.

## 2. Application completeness checker

Compare a submission with the documents, sections, forms, studies, maps,
signatures, consultation records, and attachments expected for its application
type. Return source-linked gaps for applicants or reviewers to examine.

Status: being addressed by the completeness pilot.

## 3. Requirement-to-evidence matrix

Connect every applicable regulatory requirement to the exact response and
supporting document, page, table, drawing, map, or study. Classify the evidence
as direct, partial, conflicting, missing, or uncertain.

This goes beyond completeness. Completeness establishes that a required item
is present; the matrix helps establish whether it actually addresses the
requirement and shows reviewers where the evidence is.

## 4. Information-request predictor

Structure historical CER information requests, the application content that
preceded them, the issue involved, and the eventual response. Use this history
to identify passages likely to generate questions and to retrieve similar past
questions. It should explain its matches rather than produce an unexplained
probability.

## 5. Similar-project precedent finder

Build comparable profiles of previous projects using project type, facilities,
location, environmental setting, issues, process, conditions, timeline, and
outcome. Help users find genuinely comparable applications and decisions rather
than relying only on keyword matches.

## 6. Cross-document consistency checker

Compare names, dates, coordinates, pipeline lengths, station identifiers,
species counts, technical parameters, schedules, and commitments across all
documents in a filing. Present both conflicting passages without silently
choosing one as correct.

## 7. Environmental risk and mitigation register

Extract environmental receptors, potential effects, monitoring locations,
laboratory results, thresholds, mitigation measures, responsible parties,
follow-up activities, and commitments. Produce a source-linked register of
risks, controls, missing measures, and unresolved questions.

## 8. Indigenous and stakeholder commitment tracker

Connect communities and stakeholders, concerns raised, proponent responses,
commitments, responsible parties, target dates, and later status reports. Keep
the speaker's original language and clearly distinguish a concern from the
proponent's characterization of it.

## 9. Drawing, route, and location intelligence

Extract coordinates, chainage, crossings, stations, watercourses, facilities,
land parcels, and drawing references into a spatial inventory. Detect
coordinate conflicts, unexplained route changes, missing crossings, and nearby
features discussed elsewhere in the filing.

## 10. Permit timeline and bottleneck analytics

Reconstruct application lifecycles from filing metadata, completeness events,
information requests, responses, revisions, hearings, decisions, and
conditions. Measure where elapsed time accumulates and which issues or document
classes are associated with repeated review cycles. Treat associations as
diagnostic leads, not proof of causation.

## 11. Regulatory knowledge graph

Connect companies, projects, assets, locations, communities, environmental
features, commitments, decisions, conditions, and source documents in a
navigable graph.

This substantially overlaps with Idea 1. The ownership ledger is the trusted,
time-aware system of record for a focused set of corporate and asset facts; the
knowledge graph is a broader navigation and discovery layer across many entity
types. The sensible approach is to extend the ledger or project a graph from
it, not build an independent competing source of ownership truth.

## 12. Conditions library and condition recommender

Extract approval conditions from historical decisions and organize them by
project characteristic, issue, geography, risk, and evidence. Retrieve
conditions used in comparable cases, with their context and outcomes. The tool
would support research; it would not decide that a condition should be imposed.

## 13. Commitment drift detector

Follow commitments through the application, information-request responses,
revisions, final submissions, approval conditions, and compliance reports.
Flag commitments that disappear, weaken, change dates, change responsible
parties, or conflict with later statements.

## 14. Environmental baseline atlas

Turn monitoring stations, coordinates, species observations, watercourses,
wetlands, laboratory measurements, sampling methods, and dates into a
searchable spatial and temporal atlas. Reveal prior studies near a new project
and show how reported baseline conditions differ across projects and years.

## 15. Laboratory results explorer

Normalize laboratory certificates and monitoring tables while preserving
reported values such as `<0.010` exactly. Explore results over time, upstream
versus downstream measurements, exceedances, missing samples, units, detection
limits, laboratories, and analytical methods.

## 16. Filing evolution viewer

Compare successive versions of filings and classify differences as new
evidence, removed text, changed technical values, revised locations, altered
commitments, changed conclusions, or formatting-only edits. Link every change
to both versions.

## 17. Evidence contradiction network

Build a cross-filing view of mutually inconsistent assertions about the same
asset, project, place, measurement, date, or commitment. Preserve provenance
and time so that a legitimate later correction is distinguishable from an
unresolved contradiction.

## 18. Indigenous and public concern taxonomy

Create an evidence-linked dataset of concerns raised in engagement reports,
comment letters, hearings, and submissions. Explore recurring subjects,
geographic patterns, responses, design changes, and resulting conditions.
Automated categories should aid discovery without replacing the meaning or
context of participants' own words.

## 19. Regulatory citation and precedent map

Extract references to legislation, standards, guidance, previous CER
decisions, court cases, and scientific research. Show which authorities are
used for particular issues, how interpretations develop, and which decisions
become important precedents.

## 20. Plain-language project explorer

Provide a public-facing view of what is proposed, where it is located, who is
involved, major issues, applicant commitments, regulatory status, and important
documents and dates. Every generated statement should link to source evidence,
and uncertainty or disagreement should remain visible.

## 21. Document quality observatory

Measure how usable submitted regulatory documents are, both for people and for
data processing. Possible signals include:

- image-only pages and unnecessary scanning;
- corrupt or missing text layers;
- OCR confidence and disagreement between extraction variants;
- tables with dropped, merged, or suspicious cells;
- unreadable small text and low-resolution drawings;
- missing bookmarks, headings, page labels, or document structure;
- inconsistent attachment names and metadata;
- inaccessible reading order, forms, or image descriptions;
- pages or values that should receive human verification.

The result could be a quality report for each document, filing, applicant, or
document-production workflow. Aggregate analysis could identify recurring
problems without treating a hard-to-extract document as a poor application on
its merits.

### Connection to the completeness pilot

The completeness pilot asks whether the expected material was submitted. The
quality observatory asks whether that material can be reliably found, read,
linked, and reviewed. A document can therefore be present but operationally
unusable: for example, a required table may exist as a low-resolution scan, or
a report may be searchable while 300 pages contain a corrupt hidden text layer.

The observatory could feed the pilot a second, clearly separate status for each
requirement:

| Completeness status | Evidence-usability status | Meaning |
|---|---|---|
| Present | Reliable | The item was found and its evidence is readily reviewable. |
| Present | Needs verification | The item was found, but extraction or document quality is doubtful. |
| Present | Unusable | The file exists, but relevant content cannot be reliably read or navigated. |
| Partial | Reliable | Usable evidence exists, but it does not cover the whole requirement. |
| Missing | Not applicable | No responsive item was located. |

This separation is important: extraction failure must not be reported as a
substantive filing deficiency. Instead, the pilot can route the page for human
review, request a better electronic copy when appropriate, and avoid making a
false completeness finding.

The current pipeline already records many of the required signals per page:
low confidence, variant disagreement, rasterization, table loss, disputed
table structure, suspect cells, retries, and pages recommended for
reprocessing. A first observatory prototype could therefore be built without a
new extraction system.

Potential benefits include earlier detection of unusable attachments, fewer
manual searches, better accessibility, evidence-based electronic filing
guidance, and feedback to applicants on how to produce more reviewable
documents. It could also measure whether improved document standards actually
reduce review effort over time.

## 22. Regulatory record semantic search

Provide concept-based search across extracted paragraphs, tables, headings,
and metadata. A query such as “winter construction effects on caribou” could
find relevant evidence even where those exact words are absent. Results must
show source excerpts and allow filters for date, company, project, document
type, language, geography, and evidence quality.

## 23. Study reuse and duplication detector

Identify environmental, engineering, socioeconomic, and engagement studies
that cover overlapping areas, periods, species, methods, or questions. This
could reveal usable historical evidence, repeated fieldwork, duplicated report
text, and places where an older study is being reused beyond its appropriate
scope.

## 24. Dataset passport and lineage tracker

Create a “passport” for every important dataset or table: who produced it,
collection dates, location, method, units, laboratory, revisions,
transformations, and every filing that reused it. This would make it easier to
judge whether a number is primary evidence, a transformed result, or a value
copied through several reports.

## 25. Condition compliance and early-warning dashboard

Connect approval conditions with required deliverables, deadlines, filings,
reported status, monitoring results, and supporting evidence. Highlight overdue
items, missing reporting periods, unexplained changes, or results approaching a
condition threshold. Human reviewers would confirm any compliance conclusion.

## 26. Cumulative-effects history for a place

Organize projects, disturbances, assessments, monitoring results, mitigation,
and commitments around geographic areas or valued components. A reviewer or
community could see what the regulatory record has said about the same
watershed, habitat, municipality, or corridor over time.

## 27. Standards and guidance impact scanner

When a law, standard, filing requirement, or technical guidance document
changes, locate filings, templates, conditions, and recurring analyses that
refer to the old version. This creates an impact map for policy updates and
helps identify regulatory material that may need revision.

## 28. Review workload and expertise router

Classify filing content by technical domain—engineering, hydrology, fisheries,
economics, emergency management, Indigenous matters, lands, or other areas—and
estimate the volume and complexity of evidence requiring each kind of review.
This could support workload planning while leaving assignment decisions with
managers.

## 29. English–French equivalence checker

Align English and French versions of decisions, reports, conditions, tables,
and public materials. Flag missing passages, inconsistent numbers, changed
defined terms, or conditions whose apparent meaning differs between versions.
The tool would prioritize passages for qualified bilingual review, not certify
legal equivalence automatically.

## 30. Privacy and sensitive-information quality check

Detect likely personal information, signatures, contact details, culturally
sensitive location data, inconsistent redactions, and text that remains hidden
behind a visual redaction. This would be a review aid with strict access and
retention controls, because the detection process itself handles sensitive
material.

## 31. Regulatory data benchmark and research corpus

Publish a carefully de-identified, permission-checked benchmark of difficult
regulatory pages, tables, drawings, bilingual material, and known extraction
failure modes. It could support reproducible evaluation of OCR, table parsing,
accessibility, retrieval, and grounded question-answering for Canadian public
records.

## One-week, high-impact prototypes

The following ideas are deliberately ambitious in presentation but narrow
enough to demonstrate with roughly one week of focused AI-assisted development.
They are prototypes, not production regulatory systems. Each one should use the
existing provenance model so that a compelling interface never outruns the
evidence behind it.

## 32. Sixty-second filing x-ray

Turn a filing containing hundreds of pages into a one-screen visual briefing:
what the project is, who is involved, where it is, the major document sections,
key numbers, environmental topics, communities mentioned, commitments, possible
contradictions, and pages that need verification.

**One-week demonstration:** process one of the existing large filings into an
interactive HTML dashboard. Every card opens the exact page and supporting
passage. Include a visible “what the machine could not establish” panel.

**Why it could have impact:** an executive, reviewer, or member of the public
could understand the shape of a 900-page filing in a minute and then drill into
the record instead of trusting a detached summary.

## 33. Ask the filing, with courtroom-style citations

Build a question-answer interface that answers only from the extracted filing
and presents each answer as a set of individually cited claims. Selecting a
claim opens the page, highlights its source, and shows any relevant extraction
warning or conflicting passage.

**One-week demonstration:** support a curated set of high-value questions—such
as project location, construction timing, water monitoring, consultation, and
mitigation—plus free-text questions over one filing.

**Why it could have impact:** it demonstrates that conversational access can be
auditable and evidence-first rather than a generic chatbot that merely sounds
confident.

## 34. The anatomy of a regulatory delay

Create an animated timeline that reconstructs one application's journey:
filings, revisions, information requests, responses, decisions, conditions,
and long periods between events. Clicking a delay reveals the issues and
documents active at that point.

**One-week demonstration:** select one application with enough dated material,
use metadata and extracted dates to build the timeline, and manually validate
the small set of critical events.

**Why it could have impact:** it turns an abstract claim that “permitting takes
too long” into a concrete, discussable case study without pretending that every
elapsed day has a single cause.

## 35. Evidence flight recorder

Pick an important fact or commitment and show everywhere it travelled through
the record: its first appearance, copies in later reports, revisions, responses
to questions, final condition, and subsequent compliance evidence. Display it
as a branching provenance trail.

**One-week demonstration:** trace five to ten high-value facts or commitments in
one application, using text similarity to propose links and human confirmation
to establish the final chains.

**Why it could have impact:** users can see whether a statement is independent
evidence, a copied assertion, a revised commitment, or a conclusion repeatedly
derived from the same original source.

## 36. Copy-paste genealogy of regulatory reports

Detect passages and tables reused across documents and visualize which filing
appears to have inherited material from which earlier document. Separate exact
reuse, lightly edited reuse, and reused structure with changed values.

**One-week demonstration:** compare the existing corpus, cluster similar text
elements, and publish an interactive family tree with side-by-side excerpts.
Avoid alleging plagiarism; reuse can be legitimate and valuable.

**Why it could have impact:** it could reveal boilerplate, recurring mitigation
language, shared consultant templates, and old assumptions that persist into
new projects.

## 37. Regulatory record “control room” map

Place projects, assets, monitoring stations, watercourses, communities,
laboratory results, and document evidence on one map. A time slider reveals how
the record changes, while selecting a feature opens the underlying table row or
page.

**One-week demonstration:** use the coordinates and chainage already extracted
from the Foothills monitoring filing to build a polished map with upstream and
downstream sites, sampling events, and selected results.

**Why it could have impact:** a spatial interface makes relationships apparent
that are nearly impossible to see across hundreds of pages and tables.

## 38. Where did my concern go?

Trace a concern raised by a person, community, or Indigenous group through the
record: original wording, proponent response, later discussion, project change,
recommendation, condition, or absence of a recorded resolution.

**One-week demonstration:** choose one engagement report or set of comment
letters and build several manually verified concern trails. Preserve original
wording and clearly label inferred links.

**Why it could have impact:** this gives participants a legible account of how
their input moved through the process and exposes where the documentary chain
becomes unclear.

## 39. The invisible-document challenge

Create a dramatic before-and-after viewer showing pages that look perfect to a
person but are effectively invisible or corrupted to search, accessibility
software, and automated review. Overlay the hidden text layer, OCR result,
table structure, confidence, and repaired extraction.

**One-week demonstration:** use known failures such as the hundreds of
mojibake pages in document 4647200 and scanned pages in 4710294. Let users
toggle between visual appearance and machine-readable reality.

**Why it could have impact:** it makes the document-quality observatory tangible
to executives, applicants, records teams, and accessibility specialists in a
way that an error-rate table cannot.

## 40. One-click regulatory briefing book

Generate a compact, role-specific briefing package from a filing. An executive
version could emphasize decisions and risks; a technical version could collect
tables, methods, anomalies, and unresolved questions; a public version could
explain the project and process in plain language.

**One-week demonstration:** generate static HTML or PDF briefings for two roles
from one filing, with every bullet linked to evidence and uncertain claims
visibly marked.

**Why it could have impact:** the same underlying record becomes accessible to
different audiences without maintaining several disconnected summaries.

## 41. Red-team the application

Create an evidence-grounded “skeptical reviewer” that searches for the ten most
important questions a filing leaves open. It should target contradictions,
unsupported conclusions, missing links between effects and mitigation, stale
evidence, undefined terms, and commitments without owners or dates.

**One-week demonstration:** run deterministic checks and retrieval over one
filing, use an LLM only to formulate candidate questions from retrieved
passages, and require every question to cite the evidence that motivated it.
A human reviewer rates usefulness and false positives.

**Why it could have impact:** a pre-submission red-team report could feel like
receiving the first review round before filing, while its citations make it
possible to accept, reject, or refine every proposed question.

## 42. Regulatory pulse: what changed?

Compare the regulatory record with its previous snapshot and deliver a concise,
personalized digest of new applications, changed technical values, new
commitments, responses, decisions, conditions, and corrected documents.

**One-week demonstration:** simulate two corpus snapshots from the available
documents and create an email-style or dashboard digest in which every change
opens a highlighted source comparison.

**Why it could have impact:** people stop repeatedly searching REGDOCS to learn
what changed and can subscribe to projects, companies, places, or issues.

## 43. Promise-versus-reality scoreboard

Compare effects predicted in an application—and the claimed effectiveness of
mitigation—with actual measurements reported after construction or operation.
Show whether the evidence supports, complicates, or cannot test the original
prediction.

**One-week demonstration:** manually pair one prediction with one monitoring
series and build the reusable comparison interface and data model.

**Why it could have impact:** it creates an institutional learning loop: future
assessments can learn which predictions and mitigations performed well in the
real world, rather than treating every project as a fresh start.

## 44. Reviewer memory engine

Organize prior questions, reasoning, evidence, findings, and conditions around
issues such as water crossings, geohazards, caribou, emergency response, or
cumulative effects. A reviewer could ask, “How have we handled this issue
before?” and receive cited examples with their context.

**One-week demonstration:** curate one issue across a small set of documents and
build an evidence-linked topic page plus search experience.

**Why it could have impact:** it retains institutional knowledge through staff
changes and makes consistent reasoning easier without turning precedent into a
rigid template.

## 45. Public comment constellation

Visualize a large body of comment letters as a constellation of themes,
locations, organizations, shared concerns, distinct evidence, and direct
responses. Users can move from the overview to the exact words in each letter.

**One-week demonstration:** cluster one of the existing comment-letter filings,
label a manageable sample with a human, and publish an interactive topic map.

**Why it could have impact:** decision makers and participants can see the
diversity of the public record without reading comments only in submission
order. Frequency must never be treated as a vote or used to erase unique,
rights-based, or technically important evidence.

## 46. Regulatory evidence API

Expose filings, pages, document elements, tables, entities, relationships,
quality signals, and immutable citations through a documented read-only API.
This turns the extraction from a single project into reusable infrastructure.

**One-week demonstration:** serve one extracted corpus through a small API with
search, document, page, table, and evidence endpoints, plus a sample notebook or
web application.

**Why it could have impact:** internal teams, universities, civic technologists,
and future prototypes can build on the same traceable evidence layer instead of
repeating PDF extraction.

## 47. Emergency-plan tabletop generator

Extract assets, hazards, roles, notification paths, response resources,
locations, and assumptions from filed emergency material, then help experts
assemble realistic tabletop exercise scenarios tied to the filed plan.

**One-week demonstration:** using an appropriate public plan, generate one
bounded scenario, inject timeline, and evidence pack for expert review. Do not
generate operational instructions beyond what qualified emergency personnel
approve.

**Why it could have impact:** static plans become testable learning material,
and exercises can expose unclear roles or stale assumptions before an event.

## 48. Climate resilience stress lens

Find project assumptions, assets, design thresholds, environmental baselines,
and mitigation measures that may be sensitive to wildfire, flooding,
permafrost thaw, extreme heat, erosion, or changing precipitation.

**One-week demonstration:** identify and map cited climate-sensitive assumptions
in one project. Use clearly labelled public hazard information only for a
demonstration overlay, not for an engineering risk conclusion.

**Why it could have impact:** it provides a rapid, explainable screen for where
deeper climate-resilience analysis is most valuable.

## 49. Regulatory twin of a project

Create an interactive representation linking a project's components and
locations to predicted effects, mitigations, evidence, commitments, approval
conditions, monitoring, and responsible organizations. Selecting any component
reveals its regulatory story.

**One-week demonstration:** build a narrow twin of the Elko Section monitoring
system or another well-described subsystem, using the existing entity and
relationship extraction plus a map and timeline.

**Why it could have impact:** it makes the regulatory record feel like a living
model of the project rather than a pile of PDFs. It is a regulatory information
twin, not a physical or engineering simulator.

## 50. Decision replay room

Reconstruct the record as it existed on the date of a historical decision.
Users can review the evidence available then, advance through later filings,
and see what was corrected, learned, or reported only afterward.

**One-week demonstration:** manually validate the critical dates for one small
decision record and build a time slider that changes the available evidence and
derived briefing.

**Why it could have impact:** it supports training, audit, policy learning, and
fair evaluation of past decisions without importing hindsight into what the
decision maker could have known.

## Suggested near-term sequence

With the completeness pilot already underway, a practical sequence is:

1. Add the document-quality observatory as a separate evidence-usability layer
   in the completeness workflow.
2. Build the requirement-to-evidence matrix from the pilot's requirements and
   located evidence.
3. Add the cross-document consistency checker and filing evolution viewer.
4. Use the accumulated review outcomes to develop information-request
   retrieval and prediction.
5. Extend the existing ownership ledger into the wider regulatory knowledge
   graph only where a concrete use case requires it.

For a one-week showcase, the strongest combination is the **sixty-second filing
x-ray** plus the **invisible-document challenge**. The first demonstrates the
value created from the record; the second proves why extraction quality and
traceability matter. If the audience is primarily regulatory reviewers or
applicants, substitute **red-team the application** for the document challenge.
