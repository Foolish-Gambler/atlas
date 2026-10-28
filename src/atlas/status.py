"""Operator-facing system status. Never includes credentials."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import metadata

from atlas.config import AtlasSettings
from atlas.domain import TradingMode
from atlas.safety import assert_paper_only


@dataclass(frozen=True, slots=True)
class ModuleStatus:
    name: str
    ready: bool
    details: str


@dataclass(frozen=True, slots=True)
class SystemStatus:
    version: str
    python_version: str
    trading_mode: TradingMode
    live_broker_connected: bool
    starting_cash: float
    modules: tuple[ModuleStatus, ...]

    def render(self) -> str:
        lines = [
            "ATLAS system status",
            f"  version: {self.version}",
            f"  python: {self.python_version}",
            f"  trading_mode: {self.trading_mode}",
            f"  live_broker_connected: {self.live_broker_connected}",
            f"  starting_cash: {self.starting_cash:.2f} (paper)",
            "  modules:",
        ]
        for module in self.modules:
            state = "ready" if module.ready else "down"
            lines.append(f"    - {module.name}: {state} ({module.details})")
        lines.append("  safety: paper trading only; live brokerage disabled")
        return "\n".join(lines)


def _package_version() -> str:
    try:
        return metadata.version("atlas")
    except metadata.PackageNotFoundError:
        from atlas import __version__

        return __version__


def collect_status(settings: AtlasSettings | None = None) -> SystemStatus:
    resolved = settings or AtlasSettings.load()
    assert_paper_only(resolved)
    modules = (
        ModuleStatus("market_data", True, "simulated provider"),
        ModuleStatus("news", True, "caller-supplied headlines"),
        ModuleStatus("fundamentals", True, "caller-supplied statements"),
        ModuleStatus("forecasting", True, "signals require risk approval"),
        ModuleStatus("risk", True, "deterministic pre-trade checks"),
        ModuleStatus("ingest", True, "snapshots local; vendor GET off by default"),
        ModuleStatus("backtesting", True, "PIT replay of provided bars"),
        ModuleStatus("paper_trading", True, "in-memory simulated broker"),
    )
    return SystemStatus(
        version=_package_version(),
        python_version=sys.version.split()[0],
        trading_mode=resolved.trading_mode,
        live_broker_connected=resolved.live_broker_connected,
        starting_cash=resolved.starting_cash,
        modules=modules,
    )
