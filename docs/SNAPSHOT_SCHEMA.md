# Snapshot schema and storage layout (M2-A)

Immutable financial datasets live on local disk. HTTP, credentials, and live brokers are out of scope for this milestone.

## Layout

```text
{root}/{dataset_id}/{revision}/
  manifest.json
  bars/{SYMBOL}.jsonl
  filings/{accession}.json
  news/{SYMBOL}-{event_time}.json
  statements/{SYMBOL}-{period_end}.json
  research/{artifact_id}.json
  predictions/{prediction_id}.json
  ensembles/{ensemble_id}.json
```

- `dataset_id` and `revision` are explicit path-safe identifiers. There is no `latest` alias.
- An existing `{dataset_id}/{revision}` directory is never overwritten.
- Writes go to `{dataset_id}/.{revision}.tmp-{uuid}/` and are renamed into place only after `manifest.json` is complete.

## manifest.json

`schema_version` is currently `"1"`. Unsupported versions are rejected on load.

| Field | Meaning |
| --- | --- |
| `schema_version` | Snapshot format version |
| `dataset_id` | Dataset identifier |
| `revision` | Immutable revision name |
| `source_kind` | `simulated` or `caller_supplied` (vendor adapters come later) |
| `vendor` | Optional vendor name; required when `source_kind` is `vendor_read_only` |
| `created_at` | Timezone-aware snapshot creation time |
| `ingested_at` | Timezone-aware ingest stamp recorded on the snapshot |
| `files` | Relative path → SHA-256 hex digest of exact file bytes |
| `symbols` | Sorted symbols present in bar files |
| `counts` | Record counts by kind |
| `metadata` | Optional vendor query (feed, adjustment, timeframe, `execution_eligible`) |

`manifest.json` itself is not hashed. Payload files are hashed. Load and `atlas snapshot verify` recompute SHA-256 and reject extras, missing files, or mismatches.

## Record timestamps

Every stored record keeps three clocks:

| Field | Meaning |
| --- | --- |
| `event_time` | When the fact occurred (bar close, period end, filing event) |
| `available_time` | When a strategy may use the fact |
| `ingested_time` | When ATLAS stored the bytes |

Rules: `available_time >= event_time`, `ingested_time >= available_time`, all values timezone-aware.

Bars also store `open_time`, `close_time`, and `available_time`. Complete OHLCV is usable only at `available_time`. `event_time` for a bar-close event is `close_time`.

## CLI

```text
atlas snapshot create --root DIR [--dataset ID] [--revision REV] --fixture PATH
atlas snapshot inspect --root DIR --dataset ID --revision REV
atlas snapshot verify --root DIR --dataset ID --revision REV
atlas snapshot replay --root DIR --dataset ID --revision REV
```

Replay uses `PitBacktestEngine` and `PaperBroker` only. It does not fetch data or place live orders.

Alpaca **1Day** aggregates are never execution-eligible. **1Min** sessions are eligible only when the 09:30 ET interval is present with volume. A 09:30 minute bar is a one-minute bucket, not a 09:30:00 print. Mocked HTTP snapshots use `retrieval_mode=mocked_http` and `authenticated_vendor_data=false`.

```text
atlas alpaca ingest-recorded --root DIR --dataset ID --revision REV --recorded PATH
atlas paper demo --root DIR --dataset ID --revision REV
```
