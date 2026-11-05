"""Command-line interface for ATLAS."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from atlas.backtesting import NextOpenBuyStrategy, PitBacktestEngine
from atlas.ingest.http import NetworkDisabledError
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
    ingest.add_argument("--start", default="2024-01-02")
    ingest.add_argument("--end", default="2024-01-02")
    ingest.add_argument("--symbol", default="AAPL")
    fetch = alpaca_sub.add_parser(
        "fetch-once",
        help="One-off read-only GET. Requires ATLAS_ALLOW_NETWORK. Never called in CI.",
    )
    _add_snapshot_ids(fetch, require_ids=True)
    fetch.add_argument("--timeframe", default="1Min", choices=("1Min", "1Day"))
    fetch.add_argument("--start", required=True, help="Inclusive YYYY-MM-DD (bounded query).")
    fetch.add_argument("--end", required=True, help="Inclusive YYYY-MM-DD (bounded query).")
    fetch.add_argument("--symbol", default="AAPL")
    fetch.add_argument("--max-query-days", type=int, default=31)
    fetch.add_argument("--max-pages", type=int, default=20)
    fetch.add_argument("--max-records", type=int, default=50_000)

    edgar = subparsers.add_parser(
        "edgar", help="Read-only SEC EDGAR ingest (mocked unless granted)."
    )
    edgar_sub = edgar.add_subparsers(dest="edgar_command", required=True)
    edgar_ingest = edgar_sub.add_parser(
        "ingest-recorded",
        help="Ingest recorded SEC JSON pages. Does not open a network socket.",
    )
    _add_snapshot_ids(edgar_ingest, require_ids=True)
    edgar_ingest.add_argument("--recorded", required=True, type=Path)
    edgar_ingest.add_argument("--symbol", default="AAPL")
    edgar_fetch = edgar_sub.add_parser(
        "fetch-once",
        help="One-off read-only SEC GET. Requires ATLAS_ALLOW_NETWORK. Never called in CI.",
    )
    _add_snapshot_ids(edgar_fetch, require_ids=True)
    edgar_fetch.add_argument("--symbol", default="AAPL")
    edgar_fetch.add_argument("--max-filings", type=int, default=40)
    edgar_fetch.add_argument("--max-facts", type=int, default=2000)
    edgar_fetch.add_argument("--max-requests", type=int, default=8)
    edgar_reprocess = edgar_sub.add_parser(
        "reprocess",
        help="Rebuild a new revision from stored SEC JSON. No HTTP.",
    )
    _add_snapshot_ids(edgar_reprocess, require_ids=True)
    edgar_reprocess.add_argument("--to-revision", required=True, help="New immutable revision id.")
    edgar_reprocess.add_argument(
        "--to-dataset",
        default=None,
        help="Destination dataset (defaults to the source dataset).",
    )
    edgar_coverage = edgar_sub.add_parser(
        "coverage",
        help="Print EDGAR concept coverage and PIT timestamp checks for a snapshot.",
    )
    _add_snapshot_ids(edgar_coverage, require_ids=True)
    edgar_docs = edgar_sub.add_parser(
        "ingest-documents-recorded",
        help="Ingest recorded SEC filing HTML/text. Does not open a network socket.",
    )
    _add_snapshot_ids(edgar_docs, require_ids=True)
    edgar_docs.add_argument("--source-dataset", required=True)
    edgar_docs.add_argument("--source-revision", required=True)
    edgar_docs.add_argument("--recorded", required=True, type=Path)
    edgar_docs.add_argument("--max-documents", type=int, default=8)
    edgar_inspect_docs = edgar_sub.add_parser(
        "inspect-documents",
        help="Print filing-document metadata and cleaned-text chunk locations.",
    )
    _add_snapshot_ids(edgar_inspect_docs, require_ids=True)
    edgar_inspect_docs.add_argument("--accession", default=None)
    edgar_fetch_docs = edgar_sub.add_parser(
        "fetch-documents-once",
        help="One-off read-only SEC archive GET. Requires ATLAS_ALLOW_NETWORK.",
    )
    _add_snapshot_ids(edgar_fetch_docs, require_ids=True)
    edgar_fetch_docs.add_argument("--source-dataset", required=True)
    edgar_fetch_docs.add_argument("--source-revision", required=True)
    edgar_fetch_docs.add_argument("--max-documents", type=int, default=1)
    edgar_fetch_docs.add_argument("--max-bytes", type=int, default=8_000_000)
    edgar_fetch_docs.add_argument("--max-requests", type=int, default=4)
    edgar_fetch_docs.add_argument(
        "--accession",
        default=None,
        help="Fetch this accession only. Fails if it is missing from the source snapshot.",
    )

    research = subparsers.add_parser(
        "research", help="Evidence-grounded research (mock default; OpenAI opt-in)."
    )
    research_sub = research.add_subparsers(dest="research_command", required=True)
    analyze = research_sub.add_parser(
        "analyze",
        help="Retrieve snapshot evidence and write a research revision. OpenAI is opt-in.",
    )
    analyze.add_argument("--root", required=True, type=Path)
    analyze.add_argument("--symbol", default="AAPL")
    analyze.add_argument("--document-dataset", required=True)
    analyze.add_argument("--document-revision", required=True)
    analyze.add_argument("--financial-dataset", required=True)
    analyze.add_argument("--financial-revision", required=True)
    analyze.add_argument("--dataset", default=None, help="New immutable research dataset id.")
    analyze.add_argument("--revision", default=None, help="New immutable research revision id.")
    analyze.add_argument("--provider", default="mock", help="mock (default) or openai.")
    analyze.add_argument("--accession", default=None)
    analyze.add_argument("--as-of", default=None, help="ISO-8601 research-as-of (aware).")
    analyze.add_argument("--question", default=None)
    analyze.add_argument("--intraday", action="store_true")
    analyze.add_argument(
        "--retrospective",
        action="store_true",
        help="Mark output as historical replay research; unusable as a past trading signal.",
    )
    analyze.add_argument("--max-chunks", type=int, default=16)
    analyze.add_argument("--max-facts", type=int, default=24)
    analyze.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate evidence payload size. Does not call OpenAI or write a snapshot.",
    )
    analyze.add_argument(
        "--save-failures",
        action="store_true",
        help="Store invalid LLM output in a separate unusable diagnostic dataset.",
    )

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
        try:
            return _alpaca_command(args)
        except NetworkDisabledError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "edgar":
        try:
            return _edgar_command(args)
        except NetworkDisabledError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "research":
        try:
            return _research_command(args)
        except Exception as exc:
            from atlas.research.errors import (
                LlmNetworkDisabledError,
                ResearchBudgetError,
                ResearchProviderError,
                ResearchRejectedError,
            )
            from atlas.validation import InvalidInputError

            if isinstance(
                exc,
                (
                    LlmNetworkDisabledError,
                    ResearchBudgetError,
                    ResearchProviderError,
                    ResearchRejectedError,
                    InvalidInputError,
                ),
            ):
                print(str(exc), file=sys.stderr)
                return 2
            raise
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

    from atlas.ingest.alpaca import (
        RETRIEVAL_MOCKED_HTTP,
        RETRIEVAL_VENDOR_GET,
        AlpacaBarQuery,
        AlpacaBarsAdapter,
        adapter_for_tests,
    )
    from atlas.ingest.http import NetworkDisabledError, network_allowed

    if args.alpaca_command == "fetch-once":
        if not network_allowed():
            raise NetworkDisabledError(
                "atlas alpaca fetch-once requires ATLAS_ALLOW_NETWORK=1 after explicit approval."
            )
        query = AlpacaBarQuery(
            symbol=args.symbol,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
            timeframe=args.timeframe,
            max_query_days=args.max_query_days,
            max_pages=args.max_pages,
            max_records=args.max_records,
        )
        result = AlpacaBarsAdapter().ingest(
            SnapshotStore(args.root),
            dataset_id=args.dataset,
            revision=args.revision,
            query=query,
            retrieval_mode=RETRIEVAL_VENDOR_GET,
        )
        print(
            json.dumps(
                {
                    "status": "created",
                    "dataset_id": result.manifest.dataset_id,
                    "revision": result.manifest.revision,
                    "execution_eligible": result.execution_eligible,
                    "retrieval_mode": result.retrieval_mode,
                    "authenticated_vendor_data": result.authenticated_vendor_data,
                    "mocked_http": False,
                    "simulated": False,
                },
                indent=2,
            )
        )
        return 0
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
                "retrieval_mode": result.retrieval_mode,
                "authenticated_vendor_data": result.authenticated_vendor_data,
                "simulated": result.simulated,
                "mocked_http": result.retrieval_mode == RETRIEVAL_MOCKED_HTTP,
            },
            indent=2,
        )
    )
    return 0


def _edgar_command(args: argparse.Namespace) -> int:
    import httpx

    from atlas.ingest.edgar import (
        RETRIEVAL_MOCKED_HTTP,
        RETRIEVAL_VENDOR_GET,
        EdgarAdapter,
        EdgarQuery,
        adapter_for_tests,
    )
    from atlas.ingest.http import network_allowed

    if args.edgar_command == "ingest-documents-recorded":
        from atlas.ingest.filing_docs import (
            RETRIEVAL_MOCKED_HTTP as DOC_MOCKED,
        )
        from atlas.ingest.filing_docs import (
            FilingDocumentQuery,
            FilingDocumentsAdapter,
        )

        recorded = json.loads(Path(args.recorded).read_text(encoding="utf-8"))
        pages = recorded.get("documents", recorded)
        source = SnapshotStore(args.root).load(args.source_dataset, args.source_revision)
        result = FilingDocumentsAdapter(user_agent="ATLAS-offline-reprocess", allow_network=False)
        created = result.ingest_from_source(
            SnapshotStore(args.root),
            source,
            dataset_id=args.dataset,
            revision=args.revision,
            query=FilingDocumentQuery(max_documents=args.max_documents),
            retrieval_mode=DOC_MOCKED,
            recorded=pages,
        )
        print(
            json.dumps(
                {
                    "status": "created",
                    "dataset_id": created.manifest.dataset_id,
                    "revision": created.manifest.revision,
                    "documents": len(created.documents),
                    "chunks": len(created.chunks),
                    "retrieval_mode": created.retrieval_mode,
                    "http": False,
                    "content_trust": "untrusted_sec_document",
                },
                indent=2,
            )
        )
        return 0
    if args.edgar_command == "inspect-documents":
        source = SnapshotStore(args.root).load(args.dataset, args.revision)
        docs = [
            {
                "accession": item.accession,
                "sha256": item.sha256,
                "source_url": item.source_url,
                "byte_length": item.byte_length,
                "mime_type": item.mime_type,
                "acceptance_verified": item.acceptance_verified,
                "intraday_eligible": item.intraday_eligible,
                "untrusted": True,
                "content_trust": item.content_trust,
            }
            for item in source.filing_documents()
            if args.accession is None or item.accession == args.accession
        ]
        chunks = [
            {
                "chunk_id": item.chunk_id,
                "accession": item.accession,
                "section_heading": item.section_heading,
                "text_start": item.text_start,
                "text_end": item.text_end,
                "offset_kind": item.offset_kind,
                "raw_byte_start": item.raw_byte_start,
                "raw_byte_end": item.raw_byte_end,
                "untrusted": True,
            }
            for item in source.filing_text_chunks()
            if args.accession is None or item.accession == args.accession
        ]
        print(json.dumps({"documents": docs, "chunks": chunks}, indent=2))
        return 0
    if args.edgar_command == "fetch-documents-once":
        from atlas.ingest.filing_docs import (
            RETRIEVAL_VENDOR_GET as DOC_GET,
        )
        from atlas.ingest.filing_docs import (
            FilingDocumentQuery,
            FilingDocumentsAdapter,
        )

        if not network_allowed():
            raise NetworkDisabledError(
                "atlas edgar fetch-documents-once requires ATLAS_ALLOW_NETWORK=1 "
                "after explicit approval."
            )
        source = SnapshotStore(args.root).load(args.source_dataset, args.source_revision)
        created = FilingDocumentsAdapter().ingest_from_source(
            SnapshotStore(args.root),
            source,
            dataset_id=args.dataset,
            revision=args.revision,
            query=FilingDocumentQuery(
                max_documents=args.max_documents,
                max_bytes=args.max_bytes,
                max_requests=args.max_requests,
                accession=args.accession,
                require_success=True,
            ),
            retrieval_mode=DOC_GET,
        )
        print(
            json.dumps(
                {
                    "status": "created",
                    "dataset_id": created.manifest.dataset_id,
                    "revision": created.manifest.revision,
                    "documents": len(created.documents),
                    "accessions": [item.accession for item in created.documents],
                    "chunks": len(created.chunks),
                    "retrieval_mode": created.retrieval_mode,
                    "http": True,
                    "content_trust": "untrusted_sec_document",
                },
                indent=2,
            )
        )
        return 0
    if args.edgar_command == "reprocess":
        from atlas.ingest.edgar import EdgarAdapter

        source = SnapshotStore(args.root).load(args.dataset, args.revision)
        dest_dataset = args.to_dataset or args.dataset
        result = EdgarAdapter(
            user_agent="ATLAS-offline-reprocess", allow_network=False
        ).ingest_from_snapshot(
            SnapshotStore(args.root),
            source,
            dataset_id=dest_dataset,
            revision=args.to_revision,
        )
        print(
            json.dumps(
                {
                    "status": "created",
                    "dataset_id": result.manifest.dataset_id,
                    "revision": result.manifest.revision,
                    "source_dataset": args.dataset,
                    "source_revision": args.revision,
                    "cik": result.cik,
                    "filings": len(result.filings),
                    "facts": len(result.facts),
                    "statements": len(result.statements),
                    "retrieval_mode": result.retrieval_mode,
                    "http": False,
                },
                indent=2,
            )
        )
        return 0
    if args.edgar_command == "coverage":
        from atlas.ingest.edgar import coverage_report

        source = SnapshotStore(args.root).load(args.dataset, args.revision)
        print(
            json.dumps(
                coverage_report(
                    filings=source.filings(),
                    facts=source.facts(),
                    statements=source.statements(),
                    metadata=source.manifest.metadata,
                ),
                indent=2,
            )
        )
        return 0
    if args.edgar_command == "fetch-once":
        if not network_allowed():
            raise NetworkDisabledError(
                "atlas edgar fetch-once requires ATLAS_ALLOW_NETWORK=1 after explicit approval."
            )
        query = EdgarQuery(
            symbol=args.symbol,
            max_filings=args.max_filings,
            max_facts=args.max_facts,
            max_requests=args.max_requests,
        )
        result = EdgarAdapter().ingest(
            SnapshotStore(args.root),
            dataset_id=args.dataset,
            revision=args.revision,
            query=query,
            retrieval_mode=RETRIEVAL_VENDOR_GET,
        )
        print(
            json.dumps(
                {
                    "status": "created",
                    "dataset_id": result.manifest.dataset_id,
                    "revision": result.manifest.revision,
                    "cik": result.cik,
                    "filings": len(result.filings),
                    "facts": len(result.facts),
                    "retrieval_mode": result.retrieval_mode,
                    "authenticated_vendor_data": result.authenticated_vendor_data,
                    "mocked_http": False,
                    "simulated": False,
                },
                indent=2,
            )
        )
        return 0
    if args.edgar_command != "ingest-recorded":
        return 1
    recorded = json.loads(Path(args.recorded).read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            return httpx.Response(405, json={"error": "GET only"})
        path = request.url.path
        if path.endswith("company_tickers.json"):
            body = recorded.get("tickers", recorded)
            return httpx.Response(200, json=body)
        if "/submissions/" in path:
            return httpx.Response(200, json=recorded["submissions"])
        if "/companyfacts/" in path:
            return httpx.Response(200, json=recorded["companyfacts"])
        return httpx.Response(404, json={"error": path})

    result = adapter_for_tests(handler).ingest(
        SnapshotStore(args.root),
        dataset_id=args.dataset,
        revision=args.revision,
        query=EdgarQuery(symbol=args.symbol),
    )
    print(
        json.dumps(
            {
                "status": "created",
                "dataset_id": result.manifest.dataset_id,
                "revision": result.manifest.revision,
                "cik": result.cik,
                "filings": len(result.filings),
                "facts": len(result.facts),
                "retrieval_mode": result.retrieval_mode,
                "authenticated_vendor_data": result.authenticated_vendor_data,
                "simulated": result.simulated,
                "mocked_http": result.retrieval_mode == RETRIEVAL_MOCKED_HTTP,
            },
            indent=2,
        )
    )
    return 0


def _research_command(args: argparse.Namespace) -> int:
    from atlas.ingest.codec import parse_datetime
    from atlas.research.engine import ResearchEngine
    from atlas.research.models import DEFAULT_QUESTION, ResearchQuery
    from atlas.research.provider import provider_from_name

    if args.research_command != "analyze":
        return 1
    store = SnapshotStore(args.root)
    as_of = (
        parse_datetime(args.as_of, "as_of")
        if args.as_of
        else _research_as_of(
            store,
            args.document_dataset,
            args.document_revision,
            args.accession,
        )
    )
    query = ResearchQuery(
        symbol=args.symbol,
        research_as_of=as_of,
        question=args.question or DEFAULT_QUESTION,
        accession=args.accession,
        max_chunks=args.max_chunks,
        max_facts=args.max_facts,
        intraday=bool(args.intraday),
        retrospective=bool(args.retrospective),
    )
    if args.dry_run:
        engine = ResearchEngine(store)
        print(
            json.dumps(
                engine.estimate(
                    query,
                    document_dataset=args.document_dataset,
                    document_revision=args.document_revision,
                    financial_dataset=args.financial_dataset,
                    financial_revision=args.financial_revision,
                ),
                indent=2,
            )
        )
        return 0
    if not args.dataset or not args.revision:
        print("--dataset and --revision are required unless --dry-run is set.", file=sys.stderr)
        return 2
    engine = ResearchEngine(store, provider=provider_from_name(args.provider))
    result = engine.analyze(
        query,
        document_dataset=args.document_dataset,
        document_revision=args.document_revision,
        financial_dataset=args.financial_dataset,
        financial_revision=args.financial_revision,
        output_dataset=args.dataset,
        output_revision=args.revision,
        save_failures=bool(args.save_failures),
    )
    print(
        json.dumps(
            {
                "status": "created" if result.published else "invalid",
                "dataset_id": result.dataset_id,
                "revision": result.revision,
                "artifact_id": result.artifact.artifact_id,
                "provider_id": result.artifact.provider_id,
                "simulated": result.artifact.simulated,
                "llm_called": result.artifact.llm_called,
                "http": result.artifact.llm_called,
                "research_as_of": query.research_as_of.isoformat(),
                "generated_at": (
                    result.artifact.generated_at.isoformat()
                    if result.artifact.generated_at
                    else None
                ),
                "available_time": result.artifact.time.available_time.isoformat(),
                "retrospective": result.artifact.retrospective,
                "usable_for_signals": result.artifact.usable_for_signals,
                "validation_status": result.artifact.validation_status,
                "validation_errors": list(result.artifact.validation_errors),
                "chunks": len(result.pack.chunks),
                "facts": len(result.pack.facts),
                "claims": len(result.artifact.claims),
                "disclaimer": (
                    "Not investment advice and not an order. "
                    "Mock output is not LLM analysis; OpenAI output is not a live trade."
                ),
            },
            indent=2,
        )
    )
    return 0 if result.published else 2


def _research_as_of(store: SnapshotStore, dataset: str, revision: str, accession: str | None):
    source = store.load(dataset, revision)
    docs = [
        item
        for item in source.filing_documents()
        if accession is None or item.accession == accession
    ]
    if not docs:
        from atlas.validation import InvalidInputError

        raise InvalidInputError("no filing documents available to derive --as-of")
    return max(item.time.available_time for item in docs)


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
