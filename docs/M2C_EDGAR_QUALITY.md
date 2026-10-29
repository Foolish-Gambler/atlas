# M2-C EDGAR quality notes (AAPL real snapshot)

This report describes the first approved SEC GET snapshot `aapl-edgar-real` / `2026-10-28-01` and the offline correctness follow-up. The snapshot itself is gitignored and is never mutated.

## Snapshot under review

| Field | Value |
| --- | --- |
| dataset | `aapl-edgar-real` |
| revision | `2026-10-28-01` (immutable; do not overwrite) |
| adapter | `atlas.edgar.companyfacts.v3` at ingest time |
| retrieval | `vendor_get` |
| filings | 20 |
| facts | 800 stored / 3,396 candidates (`max_facts=800`) |
| statements | 33 complete / 29 incomplete (`missing: shares`) |
| manifest.symbols | `[]` (bug: only bar symbols were recorded) |
| metadata.symbol | `AAPL` |
| intraday eligible facts | 800 / 800 |

## Findings

### Missing shares

Incomplete rows were **comparative periods inside a later 10-Q/10-K**, not missing current-period share counts.

Example: accession `0000320193-26-000020` (2026-06-27 10-Q) restates FY2025 Q3 EPS ending `2025-06-28`. Companyfacts tags:

- comparative **EPS and equity** with the **2026** accession
- `CommonStockSharesOutstanding` at `2025-06-28` with the **original** 2025 10-Q accession `0000320193-25-000073`
- duration `WeightedAverageNumberOfDilutedSharesOutstanding` with the 2026 accession (EPS denominator, not period-end shares)

v3 matched instant shares only on the **same accession**, so book value/share was marked incomplete even when the 2025 outstanding share fact was already stored and had an earlier `available_time`.

`max_facts` truncation dropped some older share rows (`omitted_concepts` included `CommonStockSharesOutstanding`), but it was **not** the cause of these 29 gaps: the `2025-06-28` outstanding share fact was present in the 800-row file.

### Concept coverage in the 800 stored facts

Present: EPS basic/diluted (`USD/shares`), `RevenueFromContractWithCustomerExcludingAssessedTax`, `Assets`, `Liabilities`, `NetCashProvidedByUsedInOperatingActivities`, `CashAndCashEquivalentsAtCarryingValue`, `StockholdersEquity`, `CommonStockSharesOutstanding`, DEI `EntityCommonStockSharesOutstanding`.

Truncation is newest-`available_time` first. After v4, EPS groups pin matching equity/shares before the remaining slots are filled.

### Intraday eligibility

All 20 filings in this snapshot have `acceptanceDateTime` values with a `Z` offset. Those stamps are **verified UTC**. `intraday_eligible` is set only when `acceptance_verified` is true. Naive or date-only stamps stay unverified and are excluded from intraday use. This snapshot simply did not contain unverified acceptances.

Do not treat `intraday_eligible_facts == stored_facts` as proof that timezone was ignored; check `acceptance_verified_*` and `unverified_intraday_facts` (must be 0).

### Symbols

`manifest.symbols` was derived only from bars. An EDGAR-only snapshot has no bars, so the list was empty despite `metadata.symbol=AAPL`. v4 unions symbols from filings, statements, facts, news, and bars.

## Offline reprocess

Original SEC JSON is in `vendor/raw_bars.jsonl` (`company_tickers`, `submissions`, `companyfacts`). Rebuild a **new** revision without HTTP:

```text
atlas edgar reprocess --root data/snapshots --dataset aapl-edgar-real --revision 2026-10-28-01 --to-revision 2026-10-28-02
atlas edgar coverage --root data/snapshots --dataset aapl-edgar-real --revision 2026-10-28-02
```

Never pass `--to-revision 2026-10-28-01`. Existing hashes stay the source of truth for the first fetch.

## Follow-up adapter

`atlas.edgar.companyfacts.v4`:

- PIT fallback: same-end instant shares/equity with `available_time <=` the EPS fact
- Prefer `CommonStockSharesOutstanding` over DEI `EntityCommonStockSharesOutstanding` when both exist
- duration weighted-average shares only if instant outstanding is absent
- truncation pins ratio inputs for kept EPS groups
- `manifest.symbols` includes non-bar records
- `unverified_intraday_facts` must remain 0

Offline reprocess of the stored JSON into **new** revision `2026-10-28-02` (HTTP off):

| Field | `2026-10-28-01` | `2026-10-28-02` |
| --- | --- | --- |
| adapter | v3 | v4 |
| vendor JSON hash | `d743b835…` | same |
| symbols | `[]` | `["AAPL"]` |
| facts | 800 | 800 |
| statements | 33 | 331 |
| incomplete (missing shares) | 29 | 7 |
| verified / intraday facts | 800 / 800 | 563 / 563 |
| unverified_intraday_facts | (not recorded) | 0 |

Pinned older share facts often have only a `filed` date (not in the submissions index), so they are **not** intraday-eligible. That is conservative PIT, not a regression.

The original revision is unchanged. Remaining incomplete statements are periods where no visible outstanding-share or WASO observation exists in the stored concept set.
