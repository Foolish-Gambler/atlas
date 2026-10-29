# M2-D — Filing document ingest for LLM research

Design only for this milestone. No live HTTP, no LLM calls, and no execution path in this document.

## Goal

Attach the **primary filing document** (10-K / 10-Q / 8-K HTML or text) to the same immutable SnapshotStore revision model used by M2-C companyfacts. M3 research agents may cite byte ranges in those documents. They must not create orders.

## Inputs (read-only)

- Already stored `FilingRecord.primary_document` and accession from submissions JSON.
- SEC archives host, GET only, same User-Agent and `ATLAS_ALLOW_NETWORK` gate as M2-C.
- Default sleep under SEC fair-access guidance. Hard request bound per revision.

## Outputs

```text
{root}/{dataset}/{revision}/
  filings/{accession}.json          # existing metadata
  filing-docs/{accession}/{name}    # raw bytes, hashed in manifest
```

Each document record needs `TimeIndex` (event = period end or 8-K acceptance; available = verified acceptance), provenance, SHA-256, source URL, content type, and byte length. Do not extract financials from HTML and present them as XBRL.

## LLM boundary (M3)

- Worker consumes filing-docs + XBRL facts already in the snapshot.
- Emits `ResearchArtifact` with `evidence_refs` pointing at `filing:{accession}` and optional `byte_start:byte_end`.
- Forbidden: `Signal` auto-approve, `PaperBroker.submit`, live brokerage, inventing missing exhibits.

## Offline first

Record archive responses as fixtures. `atlas edgar reprocess` must rebuild metadata without re-fetching documents unless a new revision explicitly requests them. Never overwrite `aapl-edgar-real/2026-10-28-01`.

## Out of scope

- Full exhibit zip trees, XBRL instance XML beyond companyfacts, embeddings, or model training.
- Changing `RiskManager` or `PaperBroker`.
