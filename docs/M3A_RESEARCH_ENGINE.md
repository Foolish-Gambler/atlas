# M3-A — Evidence-grounded mock research engine

Offline pipeline only. No LLM HTTP, no SEC HTTP, no brokerage.

## Flow

1. `SnapshotEvidenceRetriever` loads and verifies document + financial SnapshotStore revisions.
2. Evidence is filtered with `available_time <= research_as_of`. Unverified items are excluded from intraday research.
3. `MockResearchProvider` emits structured claims that quote retrieved chunks/facts only.
4. `EvidenceValidator` checks hashes, accessions, chunk IDs, offsets, XBRL units, and PIT availability. A valid citation is **not** semantic proof.
5. `ResearchEngine` writes a **new** immutable research revision. Existing AAPL snapshots are never modified.

## CLI

```text
atlas research analyze --root DIR --symbol AAPL \
  --document-dataset aapl-10q-doc-real --document-revision 2026-11-05-02 \
  --financial-dataset aapl-edgar-real --financial-revision 2026-10-28-02 \
  --dataset aapl-research-mock --revision 1 --provider mock \
  --accession 0000320193-26-000020
```

`--provider mock` remains the default. OpenAI is implemented in M3-B behind `ATLAS_ALLOW_LLM_NETWORK` (`docs/M3B_OPENAI_RESEARCH.md`).

## Evidence refs

- `filing-doc:{accession}:{sha256}`
- `filing-text:{accession}:{sha256}:{index}` (`chunk_id`)
- XBRL `fact:{...}` from `XbrlFactRecord.ref_id`

Offsets index `cleaned_text`. Trust banners are not part of citation ranges.

## Isolation

Research modules do not import `RiskManager` or `PaperBroker`. They do not emit orders or expected returns.
