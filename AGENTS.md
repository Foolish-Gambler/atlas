# ATLAS agent guide

This file is the operating contract for humans and coding agents working in this repository.

## Mission

ATLAS is an autonomous AI stock investment **research** and **paper trading** platform. The current codebase is a modular skeleton: ingest labeled inputs, analyze them, forecast candidate signals, run deterministic risk checks, then simulate execution. No live trading.

## Architecture

Source lives under `src/atlas` (src layout). Tests live under `tests/`.

| Module | Package | Responsibility |
| --- | --- | --- |
| Market data | `atlas.market_data` | Quotes/bars from providers. Default provider is **simulated** and must label data as such. |
| News analysis | `atlas.news` | Deterministic scoring of **caller-supplied** headlines. No live news APIs in this stage. |
| Fundamental analysis | `atlas.fundamentals` | Ratios and scores from **provided** statements. Do not invent filings. |
| Forecasting | `atlas.forecasting` | Candidate signals only. Forecasts never execute trades. |
| Risk management | `atlas.risk` | Deterministic gates every signal must pass before paper execution. |
| Ingest | `atlas.ingest` | Local fixtures, immutable snapshots, read-only Alpaca bars, and read-only SEC EDGAR filings/companyfacts. Network is off unless `ATLAS_ALLOW_NETWORK` is granted. |
| Backtesting | `atlas.backtesting` | Research path is `PitBacktestEngine` (decide on published close, fill a later session open). Legacy `BacktestEngine` has look-ahead bias and must not be used for research. |
| Paper trading | `atlas.paper_trading` | In-memory simulated broker. The only allowed execution mode. |
| CLI / status | `atlas.cli`, `atlas.status` | Operator interface. `atlas status` reports mode, modules, and live-broker isolation. |

Shared runtime types live in `atlas.domain`. Long-lived stream contracts (timestamps, provenance, filings, research, predictions, event clock) live in `atlas.contracts`. Product intent is in `docs/PRODUCT_VISION.md` and `docs/ROADMAP.md`. Process-wide safety (paper-only mode) lives in `atlas.safety` and `atlas.config`.

Pipeline:

1. Research modules produce structured artifacts (quotes, news scores, fundamentals, forecasts).
2. Forecasting emits a `Signal`. `Signal.reference_price` is indicative for risk checks only, not a fill price.
3. `RiskManager.evaluate` issues a `RiskDecision` with a stable `decision_id`. Rejected IDs are never executable later.
4. `PaperBroker.submit` accepts only IDs issued by **that broker's** `RiskManager`, re-checks the current book, then applies a broker-controlled simulated fill.
5. Backtests reuse the same risk manager; they never bypass it. Incomplete marks leave `PortfolioSnapshot.equity` as `None`.

## Development conventions

- Python 3.12 only (see `.python-version` and `requires-python`).
- Package manager: **uv**. Do not introduce pip/poetry/conda lockfiles.
- Preserve `[build-system]` (`uv_build`) unless there is an explicit, reviewed reason to change it.
- Keep the installable package under `src/atlas`.
- Prefer the standard library. Add a runtime dependency only when a module cannot be correct or testable without it.
- Dev tools (`pytest`, `ruff`) belong in `[dependency-groups] dev`, not in runtime `dependencies`.
- Public functions should have type hints and return dataclasses or other explicit types.
- Tests go in `tests/` and should cover safety invariants, not just happy paths.
- Do not present simulated or fixture numbers as live market data.
- Format and lint with ruff (`uv run ruff check src tests` and `uv run ruff format src tests`).
- Run tests with `uv run pytest`.

## Security and safety rules

These are hard constraints. Do not weaken them in code, docs, or demos.

1. **Paper trading only.** `TradingMode.LIVE` is not a supported configuration. Do not add live brokerage adapters without explicit human approval and a separate design review.
2. **Never connect to a live brokerage account.** No order-routing, no production broker SDKs, no "just a test against my real account."
3. **Never expose or commit credentials.** No API keys in source, tests, logs, CLI output, or git history. Use env vars locally; keep `.env` gitignored. If a secret appears in a diff, stop and rotate it.
4. **Do not invent financial data and present it as real.** Synthetic series must be labeled `simulated` (or equivalent). Callers supply research inputs; modules must not scrape or fabricate "actual" prices, filings, or headlines.
5. **All AI-generated trading signals must pass deterministic risk checks** before any paper fill. LLM or heuristic output is advisory until `RiskManager` approves it. Never auto-approve by default.
6. **Ask for approval** before destructive operations (deleting data, resetting git, force-push) or connecting any external network service (market-data APIs, news APIs, brokers, cloud LLMs).
7. **Least privilege.** The CLI must not print secrets. Status output should include `live_broker_connected: false`.

## What agents may do without asking

- Create or edit project source, tests, and local config that does not contact the network.
- Install already-declared dependency groups with `uv sync`.
- Run pytest and ruff locally.

## What agents must not do

- Place live orders or stub a "live" path that can be enabled by a flag.
- Commit `.env`, keys, or account identifiers.
- Bypass `RiskManager` from forecasting, news, or fundamentals.
- Treat ATLAS output as investment advice or guaranteed returns.
