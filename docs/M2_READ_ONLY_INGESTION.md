# M2 — Read-Only Financial Data Ingestion

This is a design document. It does not authorize live HTTP, live trading, or credential use.

M1 remains the research backtest path (`PitBacktestEngine`). M2 adds **read-only** adapters that emit the same `atlas.contracts` records already consumed by the event clock. LLM research agents (M3) and quantitative ML (M4) stay downstream consumers; they must not gain an execution path.

## Goals

- Ingest **SEC EDGAR** corporate filings and financial statements.
- Ingest **historical** stock prices from Alpaca (bars only; no order routing).
- Attach point-in-time timestamps and provenance to every record.
- Version datasets as reproducible snapshots (`dataset_id` + `revision`).
- Test with offline fixtures and mocked HTTP. No network call without explicit human approval.
- Strict read-only access, rate limits, and secrets kept out of source, logs, and git.

## Non-goals (M2)

- Live quotes, streaming websockets, or brokerage order APIs.
- Write/POST/PUT/DELETE to any vendor.
- LLM calls, model training, or multi-asset portfolio construction.
- Replacing `PaperBroker` / `RiskManager`.
- Enabling `TradingMode.LIVE`.

## Architecture

```text
                    ┌─────────────────────┐
  env secrets ───►  │ Credential vault    │  (process env only; never logged)
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ RateLimitedClient   │  GET only, retries, circuit breaker
                    └──────────┬──────────┘
           ┌───────────────────┼───────────────────┐
           ▼                   ▼                   ▼
    EdgarFilingsAdapter  AlpacaBarsAdapter   (future vendors)
           │                   │
           └─────────┬─────────┘
                     ▼
            SnapshotStore (dataset_id, revision)
                     │
                     ▼
         LocalFixtureSource-compatible records
         MarketBarRecord / FilingRecord / StatementRecord
                     │
                     ▼
              EventClock + PitBacktestEngine
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
     Research     Quant ML    PaperBroker
     agents (M3)  (M4)        (unchanged)
```

Adapters implement the existing `ReadOnlyBarSource` / `ReadOnlyEventSource` protocols. They never construct `Signal` or call `PaperBroker.submit`.

## Time and provenance

Every record must carry `TimeIndex`:

| Field | Meaning |
| --- | --- |
| `event_time` | When the fact occurred (session close, period end, filing acceptance). |
| `available_time` | Earliest time a strategy may use it (vendor publication / EDGAR acceptance). |
| `ingested_time` | When ATLAS stored the snapshot locally. |

Rules:

- `available_time >= event_time`.
- Bar OHLCV is usable only at `available_time` (>= `close_time`).
- Alpaca historical bars must populate `open_time`, `close_time`, and a conservative `available_time` (not the raw vendor “timestamp” if that field is the close).
- EDGAR `event_time` is the reporting period end (or filing event time for 8-K). `available_time` is the EDGAR acceptance datetime.
- Restatements are new records with a later `available_time` and a distinct accession or revision; they must not rewrite earlier snapshot bytes.

`Provenance`:

- `source_kind=vendor_read_only`
- `vendor` in `{sec_edgar, alpaca}`
- `dataset_id` / `revision` identify the frozen snapshot used by a backtest
- Simulated fixtures stay `simulated` and must not be relabeled as vendor data

## Dataset versioning

A snapshot is an immutable directory (or archive) such as:

```text
data/snapshots/{dataset_id}/{revision}/
  manifest.json
  bars/{symbol}.jsonl
  filings/{accession}.json
  statements/{symbol}-{period}.json
  http_cache/          # redacted request metadata only
```

`manifest.json` records vendor, query window, symbol universe, fetch clock, adapter versions, and content hashes. `PitBacktestEngine` loads a snapshot the same way it loads M1 fixtures: by path, never by “latest”.

Re-fetching creates a **new revision**. Existing revisions are never mutated.

## SEC EDGAR (read-only)

- Endpoints: company submissions and filing archives (GET). User-Agent must identify the operator via env (`ATLAS_EDGAR_USER_AGENT`), not a hardcoded personal email in git.
- Map 10-K / 10-Q / 8-K (and later amendments) to `FilingRecord`.
- Parse a small, explicit statement subset into `StatementRecord` (EPS, book value, and later line items). Do not invent missing line items.
- Prefer accession + acceptance datetime as identity. Period-end is `event_time`, never `available_time`.
- Rate limit: stay under SEC fair-access guidance (default sleep between GETs; configurable, never zero).
- Offline tests: recorded JSON fixtures under `tests/fixtures/ingest/edgar/`.

## Alpaca historical prices (read-only)

- Historical bar API only. Do not import order, account, or streaming trade modules.
- Auth: `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` from the environment. Refuse to start the adapter if keys are missing; never print them.
- Paper vs live Alpaca **data** hosts may differ; M2 uses the market-data host, not the trading host. No `/v2/orders`.
- Convert daily bars to `MarketBarRecord` with US RTH open/close in timezone-aware datetimes. `available_time` must not precede `close_time`.
- Session calendar / DST belongs in the adapter or a shared calendar helper, not in strategies.
- Offline tests: mocked HTTP + `tests/fixtures/ingest/alpaca/`.

## HTTP client contract

```text
GET only
Idempotent retries on 429 / 5xx with bounded backoff
Honor Retry-After
Hard timeout
No redirects to unexpected hosts
Response body stored in the snapshot; Authorization headers stripped from logs and cache metadata
```

A `NetworkGuard` flag defaults to **off**. Production-like fetches require an explicit operator switch **and** human approval in the session. CI never sets the flag.

## Security

- Secrets: environment or a local untracked file. `.env` stays gitignored.
- Config (`AtlasSettings`) stores no keys. Optional `has_vendor_credentials: bool` may be reported; values must not.
- CLI `atlas status` continues to show `live_broker_connected: false`.
- Vendor adapters raise if asked to place an order or if `TradingMode.LIVE` is set.

## Testing plan (no live network in CI)

1. Contract tests: EDGAR/Alpaca JSON fixtures parse into `TimeIndex` + provenance.
2. Mocked HTTP: adapter builds the correct GET, respects 429, never sends POST.
3. Snapshot round-trip: write revision, reload, identical `EventClock` keys.
4. PIT leak tests: filing `available_time` after a bar close is invisible at that close.
5. Credential tests: missing env fails closed; logs/journal contain no key substrings.
6. Existing paper-safety and M1 PIT suites remain green.

## Rollout

1. Snapshot store + manifest (local disk, no HTTP). **M2-A implemented** (`SnapshotStore`, `docs/SNAPSHOT_SCHEMA.md`).
2. Alpaca historical bars behind mocks, then approved one-off fetch to a new revision.
3. EDGAR filings/statements behind mocks, then approved one-off fetch.
4. Wire snapshots into `LocalFixtureSource` or a thin `SnapshotSource`.
5. Stop. LLM agents and ML training wait for M3/M4.

## Long-term fit

M2 is the evidence layer. M3 research artifacts and M4 `ReturnPrediction` objects cite snapshot `revision` and event IDs. Execution remains paper-only through `RiskManager` → `PaperBroker`.
