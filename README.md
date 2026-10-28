# ATLAS

Autonomous AI stock investment research and paper-trading platform.

ATLAS currently **simulates** market data, research, risk checks, backtests, and paper trades. It does not connect to live brokerages and must not be used with real money.

Architecture intent: [docs/PRODUCT_VISION.md](docs/PRODUCT_VISION.md), [docs/ROADMAP.md](docs/ROADMAP.md), [docs/SNAPSHOT_SCHEMA.md](docs/SNAPSHOT_SCHEMA.md). Vendor HTTP is **off by default**. Mocked Alpaca ingest and a $10,000 paper demo are available; live GETs need explicit approval.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

## Quick start

```powershell
uv sync --group dev
uv run atlas status
uv run pytest
uv run ruff check src tests
```

## Secrets and local datasets

Keep credentials and downloaded datasets out of Git.

- Copy `.env.example` to `.env` and fill values locally. `.env` is gitignored.
- Store snapshot revisions under `data/snapshots/` (also gitignored).
- Never commit `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, or vendor JSON dumps.

Load env vars with uv (do not export keys into the shell history if you can avoid it):

```powershell
Copy-Item .env.example .env
uv run --env-file .env atlas status
```

Network vendor HTTP is **off** until `ATLAS_ALLOW_NETWORK=1` is set **and** a human approves a one-off read-only fetch. `atlas alpaca fetch-once` is GET-only to `data.alpaca.markets`, bounded by `--max-query-days`, `--max-pages`, and `--max-records`. It must never call trading or account endpoints.

```powershell
# After explicit approval only. This is not run in CI or by default.
uv run --env-file .env atlas alpaca fetch-once `
  --root data/snapshots `
  --dataset aapl-iex `
  --revision 2024-01-02-once `
  --symbol AAPL `
  --start 2024-01-02 `
  --end 2024-01-02 `
  --timeframe 1Min `
  --max-query-days 31
```

Unset `ATLAS_ALLOW_NETWORK` afterwards. Mocked ingest (`atlas alpaca ingest-recorded`) does not open a socket.
