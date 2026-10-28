"""Command-line interface for ATLAS."""

from __future__ import annotations

import argparse
import sys

from atlas.status import collect_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas",
        description="ATLAS paper-trading research platform (simulation only).",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Print system status (default).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {None, "status"}:
        print(collect_status().render())
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
