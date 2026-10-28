"""Command-line interface for ATLAS."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from atlas.backtesting import NextOpenBuyStrategy, PitBacktestEngine
from atlas.ingest.snapshot import SnapshotStore
from atlas.status import collect_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="ATLAS paper-trading research platform (simulation only).",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Print system status (default).")
    snapshot = subparsers.add_parser(
        "snapshot", help="Create, inspect, verify, or replay snapshots."
    )
    snap_sub = snapshot.add_subparsers(dest="snapshot_command", required=True)

    create = snap_sub.add_parser("create", help="Write an immutable snapshot from a local fixture.")
    _add_snapshot_ids(create, require_ids=False)
    create.add_argument("--fixture", required=True, type=Path, help="Local JSON fixture path.")

    inspect = snap_sub.add_parser("inspect", help="Print snapshot manifest metadata.")
    _add_snapshot_ids(inspect, require_ids=True)

    verify = snap_sub.add_parser("verify", help="Validate SHA-256 hashes for a snapshot revision.")
    _add_snapshot_ids(verify, require_ids=True)

    replay = snap_sub.add_parser("replay", help="Paper-replay a snapshot with PitBacktestEngine.")
    _add_snapshot_ids(replay, require_ids=True)
    replay.add_argument("--run-id", default="snapshot-replay", help="Stable paper backtest run id.")

    alpaca = subparsers.add_parser(
        "alpaca", help="Read-only Alpaca historical ingest (mocked unless granted)."
    )
    alpaca_sub = alpaca.add_subparsers(dest="alpaca_command", required=True)
    ingest = alpaca_sub.add_parser(
        "ingest-recorded",
        help="Ingest recorded Alpaca JSON pages. Does not open a network socket.",
    )
    _add_snapshot_ids(ingest, require_ids=True)
    ingest.add_argument("--recorded", required=True, type=Path, help="Recorded HTTP JSON pages.")
    ingest.add_argument("--timeframe", default="1Min", choices=("1Min", "1Day"))
    ingest.add_argument("--start", default="2024-01-01")
    ingest.add_argument("--end", default="2025-12-31")
    ingest.add_argument("--symbol", default="AAPL")

    paper = subparsers.add_parser("paper", help="Paper-only virtual portfolio helpers.")
    paper_sub = paper.add_subparsers(dest="paper_command", required=True)
    demo = paper_sub.add_parser("demo", help="Replay a snapshot with $10,000 virtual cash.")
    _add_snapshot_ids(demo, require_ids=True)
    demo.add_argument("--cash", type=float, default=10_000.0)
    demo.add_argument("--quantity", type=int, default=1)
    demo.add_argument("--run-id", default="virtual-10k")
    return parser


def _add_snapshot_ids(parser: argparse.ArgumentParser, *, require_ids: bool) -> None:
    parser.add_argument("--root", required=True, type=Path, help="Snapshot store root directory.")
    parser.add_argument("--dataset", required=require_ids, help="Dataset identifier.")
    parser.add_argument("--revision", required=require_ids, help="Explicit dataset revision.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {None, "status"}:
        print(collect_status().render())
        return 0
    if args.command == "snapshot":
        return _snapshot_command(args)
    if args.command == "alpaca":
        return _alpaca_command(args)
    if args.command == "paper":
        return _paper_command(args)
    parser.print_help()
    return 1


def _snapshot_command(args: argparse.Namespace) -> int:
    store = SnapshotStore(args.root)
    if args.snapshot_command == "create":
        manifest = store.create_from_fixture(
            args.fixture,
            dataset_id=args.dataset,
            revision=args.revision,
        )
        print(
            json.dumps(
                {
                    "status": "created",
                    "dataset_id": manifest.dataset_id,
                    "revision": manifest.revision,
                    "schema_version": manifest.schema_version,
                    "files": len(manifest.files),
                },
                indent=2,
            )
        )
        return 0
    if args.snapshot_command == "inspect":
        manifest = store.inspect(args.dataset, args.revision)
        print(json.dumps(manifest.to_dict(), indent=2))
        return 0
    if args.snapshot_command == "verify":
        manifest = store.verify(args.dataset, args.revision)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "dataset_id": manifest.dataset_id,
                    "revision": manifest.revision,
                    "files_verified": len(manifest.files),
                },
                indent=2,
            )
        )
        return 0
    if args.snapshot_command == "replay":
        source = store.load(args.dataset, args.revision)
        result = PitBacktestEngine().run(
            source, NextOpenBuyStrategy(quantity=1), run_id=args.run_id
        )
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "dataset_id": source.manifest.dataset_id,
                    "revision": source.manifest.revision,
                    "fills": result.fills,
                    "rejected": result.rejected,
                    "cancelled": result.cancelled,
                    "ending_cash": result.ending_cash,
                    "ending_equity": result.ending_equity,
                    "simulated": True,
                },
                indent=2,
            )
        )
        return 0
    return 1


def _alpaca_command(args: argparse.Namespace) -> int:
    from datetime import date

    import httpx

    from atlas.ingest.alpaca import AlpacaBarQuery, adapter_for_tests

    if args.alpaca_command != "ingest-recorded":
        return 1
    pages = json.loads(Path(args.recorded).read_text(encoding="utf-8"))
    bodies = pages["pages"] if isinstance(pages, dict) else pages
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            return httpx.Response(405, json={"error": "GET only"})
        index = state["i"]
        state["i"] += 1
        page = bodies[index]
        return httpx.Response(int(page.get("status", 200)), json=page.get("body", page))

    adapter = adapter_for_tests(handler)
    query = AlpacaBarQuery(
        symbol=args.symbol,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        timeframe=args.timeframe,
    )
    result = adapter.ingest(
        SnapshotStore(args.root),
        dataset_id=args.dataset,
        revision=args.revision,
        query=query,
    )
    print(
        json.dumps(
            {
                "status": "created",
                "dataset_id": result.manifest.dataset_id,
                "revision": result.manifest.revision,
                "execution_eligible": result.execution_eligible,
                "missing_sessions": list(result.missing_sessions),
                "simulated": False,
                "mocked_http": True,
            },
            indent=2,
        )
    )
    return 0


def _paper_command(args: argparse.Namespace) -> int:
    from atlas.paper_trading.demo import run_virtual_portfolio

    if args.paper_command != "demo":
        return 1
    source = SnapshotStore(args.root).load(args.dataset, args.revision)
    demo = run_virtual_portfolio(
        source,
        starting_cash=args.cash,
        quantity=args.quantity,
        run_id=args.run_id,
    )
    print(json.dumps(demo.report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
