# Test corpus

The sixteen CER REGDOCS filings this pipeline has been developed and measured
against. The PDFs are **not** committed (`source/` is gitignored); download them
from the links below to reproduce any result in
[lessons_learned.md](lessons_learned.md).

Files come from the REGDOCS download endpoint:
`https://apps.cer-rec.gc.ca/REGDOCS/File/Download/<document-id>`

| document | pages | MB | filing | filed | company |
|---|---:|---:|---|---|---|
| [4600563](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4600563) | 799 | 11.5 | C36722 | 2025-10-16 | Powell River Energy Inc. |
| [4647200](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4647200) | 986 | 46.4 | C38088 | 2026-01-30 | Foothills Pipe Lines (South BC) Ltd |
| [4647187](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4647187) | 628 | 3.8 | C38072 | 2026-01-29 | Trans Mountain Pipeline ULC |
| [4673063](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4673063) | 626 | 29.9 | C39643 | 2026-06-08 | Imperial Oil Resources N.W.T. Limited |
| [4596827](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4596827) | 625 | 21.8 | C36328 | 2025-09-22 | Powell River Energy Inc. |
| [4664851](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4664851) | 604 | 17.2 | C39218 | 2026-05-01 | NGTL GP Ltd. |
| [4666748](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4666748) | 603 | 16.8 | C39419 | 2026-05-20 | NGTL GP Ltd. |
| [4646669](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4646669) | 572 | 15.4 | C37921 | 2026-01-19 | Enbridge Pipelines Inc. |
| [4648189](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4648189) | 514 | 4.1 | C38105 | 2026-01-30 | Westcoast Energy Inc. |
| [4597172](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4597172) | 500 | 18.1 | C36325 | 2025-09-22 | Powell River Energy Inc. |
| [4648190](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4648190) | 499 | 4.5 | C38105 | 2026-01-30 | Westcoast Energy Inc. |
| [4572347](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4572347) | 482 | 18.1 | C34599 | 2025-05-09 | Saskatchewan Power Corporation |
| [4664850](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4664850) | 482 | 17.8 | C39218 | 2026-05-01 | NGTL GP Ltd. |
| [4692624](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4692624) | 474 | 7.4 | C40194 | 2026-07-10 | — |
| [4710294](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4710294) | 463 | 20.0 | C40451 | 2026-08-07 | — |
| [4692070](https://apps.cer-rec.gc.ca/REGDOCS/File/Download/4692070) | 437 | 21.0 | C40167 | 2026-07-08 | Westcoast Energy GP Inc. |

9,294 pages in total. Page counts are from the reference extraction, so they
match what the comparisons in `lessons_learned.md` were scored against.

## Titles

| document | title |
|---|---|
| 4572347 | C34599-8 Appendix F — Project Environmental Assessment |
| 4596827 | C36328-1 Letters of Comment |
| 4597172 | C36325-1 Letters of Comment |
| 4600563 | C36722-1 List of Authorities |
| 4646669 | C37921-6 Book of Authorities — Volume 1 |
| 4647187 | C38072-1 C146 Indigenous Groups Engagement Report |
| 4647200 | C38088-2 Foothills Zone 8 West Path Delivery 2023 Cond. 15 Acid Rock Drainage Mitigation Plan Reports, Attach 1-2 |
| 4648189 | C38105-1 Commission — Report GH-001-2024 — Westcoast Sunrise Expansion |
| 4648190 | C38105-2 Commission — Rapport GH-001-2024 (French) |
| 4664850 | C39218-4 Attachment CER 1.1-3 ESA Part 3 |
| 4664851 | C39218-5 Attachment CER 1.1-4 ESA Part 4 |
| 4666748 | C39419-5 Attachment CER 1.1-4 ESA Part 4 of 4 |
| 4673063 | C39643-4 417085-60937-25100-CA-REP-00001 2025 Annual C&R Rev0 Part 4 |
| 4692070 | C40167-4 Attachment 1 to Westcoast Response to CER IR 1.1 — Environmental |
| 4692624 | C40194-3 Attachment 02 — Mississauga Road Improvements North of Bovaird |
| 4710294 | C40451-3 Volume 3 Cahier des pièces du demandeur, Pièce P-16 à P-18 |

## Why these sixteen

They were picked for size, which turned out to mean variety. The set covers
born-digital reports, scanned filings, French-language documents, GIS drawing
sets, laboratory certificate bundles and legal authorities — and that variety is
what exposed the failures in `lessons_learned.md`.

Two are worth naming:

- **4647200** — the document everything was originally tuned against. 315 of its
  986 pages carry a text layer of unmapped glyph codes, which is how that
  failure mode was found.
- **4710294** — a scanned French-language filing, 399 of 463 pages with no text
  layer at all. It scored 13.2% of the reference under settings tuned on
  4647200, and is the reason extraction is now chosen per page by result
  rather than by threshold (§6.1, §7).

Note that four documents share two filings (C38105 is 4648189/4648190, an
English report and its French translation; C39218 is 4664850/4664851, parts 3
and 4 of one assessment). They are not independent samples.
