# ATLAS

Autonomous AI stock investment research and paper-trading platform.

ATLAS currently **simulates** market data, research, risk checks, backtests, and paper trades. It does not connect to live brokerages and must not be used with real money.

Architecture intent: [docs/PRODUCT_VISION.md](docs/PRODUCT_VISION.md), [docs/ROADMAP.md](docs/ROADMAP.md). The next build milestone is point-in-time event replay and read-only local ingestion — not live trading.

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
