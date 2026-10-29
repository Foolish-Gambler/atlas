# M2-D — Filing document ingest for LLM research

Implemented as GET-only archive ingest. No live HTTP in CI, no LLM calls, and no execution path.

## Goal

Attach the **primary filing document** (10-K / 10-Q / 8-K HTML or text) to a **new** immutable SnapshotStore revision. M3 research agents may cite document hashes and cleaned-text offsets. They must not create orders.

## Inputs (read-only)

- `FilingRecord.cik`, `accession`, `primary_document` from an existing snapshot.
- Canonical URL: `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-without-dashes}/{basename}`
- Same User-Agent and `ATLAS_ALLOW_NETWORK` gate as M2-C. Redirects are rejected.

## Outputs

```text
{root}/{dataset}/{revision}/
  filings/{accession}.json
  filing-docs/{accession}/{name}     # original bytes
  filing-docs/{accession}/document.json
  filing-text/{accession}.jsonl      # untrusted cleaned-text chunks
```

Each document keeps source URL, SHA-256, MIME type, length, retrieval time, accession, provenance, and the filing's PIT timestamps. Unverified acceptances stay `intraday_eligible=false`.

HTML is converted with the standard library. Tables and `Item` headings are preserved in cleaned text. `offset_kind=raw_bytes` is set only when a chunk maps uniquely into the original bytes. Otherwise citations use `cleaned_text` offsets. Offsets are never invented.

Trust is stored as `content_trust=untrusted_sec_document` on document metadata and `untrusted=true` on chunks. Cleaned text and citation offsets do **not** include a banner. `cleaned_text[text_start:text_end]` must equal `chunk.text`. `offset_kind=raw_bytes` is used only when that exact chunk text occurs once in the original bytes.

## CLI

```text
atlas edgar ingest-documents-recorded --root DIR --dataset NEW --revision NEW \
  --source-dataset SRC --source-revision SRC_REV --recorded pages.json
atlas edgar inspect-documents --root DIR --dataset ID --revision REV [--accession ACCN]
atlas edgar fetch-documents-once --root DIR --dataset NEW --revision NEW \
  --source-dataset SRC --source-revision SRC_REV [--accession ACCN]
```

`fetch-documents-once` requires `ATLAS_ALLOW_NETWORK=1` and never overwrites an existing revision (including `aapl-edgar-real/2026-10-28-01`). `--accession` selects one filing from the source snapshot and fails if that accession, CIK, primary document, or form type is missing or unsupported. It does not substitute another filing. A failed or empty retrieval does not create a successful snapshot. Bounded `max_documents` mode remains available without `--accession`.

## LLM boundary (M3)

- Worker consumes filing-docs + XBRL facts already in the snapshot.
- Emits `ResearchArtifact` with `evidence_refs` pointing at `filing-doc:{accession}:{sha256}` and optional cleaned-text offsets.
- Forbidden: `Signal` auto-approve, `PaperBroker.submit`, live brokerage, inventing missing exhibits or offsets.

## Out of scope

- Full exhibit zip trees, XBRL instance XML beyond companyfacts, embeddings, or model training.
- Changing `RiskManager` or `PaperBroker`.
