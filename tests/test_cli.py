from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.cli import main

ALPACA_MIN = Path(__file__).parent / "fixtures" / "ingest" / "alpaca" / "aapl_1min_pages.json"
EDGAR_PAGES = Path(__file__).parent / "fixtures" / "ingest" / "edgar" / "aapl_pages.json"

FIXTURE = Path(__file__).parent / "fixtures" / "pit" / "m1_sim_a.json"


def test_status_command_prints_paper_only_banner(capsys) -> None:
    assert main(["status"]) == 0
    output = capsys.readouterr().out
    assert "ATLAS system status" in output
    assert "trading_mode: paper" in output
    assert "live_broker_connected: False" in output
    assert "market_data" in output
    assert "paper_trading" in output
    assert "live brokerage disabled" in output


def test_default_command_is_status(capsys) -> None:
    assert main([]) == 0
    assert "ATLAS system status" in capsys.readouterr().out


def test_snapshot_cli_create_inspect_verify_replay(tmp_path: Path, capsys) -> None:
    root = tmp_path / "snapshots"
    assert (
        main(
            [
                "snapshot",
                "create",
                "--root",
                str(root),
                "--fixture",
                str(FIXTURE),
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "created"
    assert created["dataset_id"] == "m1-sim-a"
    assert created["revision"] == "2"

    assert (
        main(
            [
                "snapshot",
                "inspect",
                "--root",
                str(root),
                "--dataset",
                "m1-sim-a",
                "--revision",
                "2",
            ]
        )
        == 0
    )
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["schema_version"] == "1"
    assert inspected["files"]

    assert (
        main(
            [
                "snapshot",
                "verify",
                "--root",
                str(root),
                "--dataset",
                "m1-sim-a",
                "--revision",
                "2",
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["status"] == "ok"

    assert (
        main(
            [
                "snapshot",
                "replay",
                "--root",
                str(root),
                "--dataset",
                "m1-sim-a",
                "--revision",
                "2",
                "--run-id",
                "cli-replay",
            ]
        )
        == 0
    )
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["simulated"] is True
    assert replayed["fills"] == 3
    assert replayed["run_id"] == "cli-replay"


def test_alpaca_recorded_ingest_and_paper_demo(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    root = tmp_path / "snapshots"
    assert (
        main(
            [
                "alpaca",
                "ingest-recorded",
                "--root",
                str(root),
                "--dataset",
                "alpaca-aapl",
                "--revision",
                "cli-mock",
                "--recorded",
                str(ALPACA_MIN),
                "--start",
                "2024-01-02",
                "--end",
                "2024-01-03",
                "--timeframe",
                "1Min",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["mocked_http"] is True
    assert created["execution_eligible"] is True
    assert "test-secret" not in json.dumps(created)
    assert (
        main(
            [
                "paper",
                "demo",
                "--root",
                str(root),
                "--dataset",
                "alpaca-aapl",
                "--revision",
                "cli-mock",
                "--run-id",
                "cli-10k",
            ]
        )
        == 0
    )
    demo = json.loads(capsys.readouterr().out)
    assert demo["starting_cash"] == 10000.0
    assert demo["fills"] == 1
    assert demo["simulated"] is True
    assert demo["authenticated_vendor_data"] is False
    assert demo["retrieval_mode"] == "mocked_http"
    monkeypatch.delenv("ATLAS_ALLOW_NETWORK", raising=False)
    assert (
        main(
            [
                "alpaca",
                "fetch-once",
                "--root",
                str(root),
                "--dataset",
                "live-should-not-run",
                "--revision",
                "blocked",
                "--start",
                "2024-01-02",
                "--end",
                "2024-01-03",
            ]
        )
        == 2
    )


def test_fetch_once_uses_readonly_http_without_opening_a_socket(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ATLAS_ALLOW_NETWORK", "1")

    def forbid_request(self, method, url, *args, **kwargs):  # noqa: ANN001
        assert method == "GET"
        raise AssertionError(f"real HTTP is forbidden in tests: {method} {url}")

    monkeypatch.setattr("httpx.Client.request", forbid_request)
    root = tmp_path / "snapshots"
    with pytest.raises(AssertionError, match="real HTTP is forbidden"):
        main(
            [
                "alpaca",
                "fetch-once",
                "--root",
                str(root),
                "--dataset",
                "live-blocked",
                "--revision",
                "once",
                "--start",
                "2024-01-02",
                "--end",
                "2024-01-02",
                "--max-query-days",
                "1",
            ]
        )
    assert not (root / "live-blocked" / "once").exists()


def test_edgar_recorded_ingest_and_fetch_once_blocked(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_EDGAR_USER_AGENT", "ATLAS tests contact@example.com")
    root = tmp_path / "snapshots"
    assert (
        main(
            [
                "edgar",
                "ingest-recorded",
                "--root",
                str(root),
                "--dataset",
                "edgar-aapl",
                "--revision",
                "cli-mock",
                "--recorded",
                str(EDGAR_PAGES),
                "--symbol",
                "AAPL",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["mocked_http"] is True
    assert created["cik"] == "0000320193"
    assert created["filings"] >= 3
    assert "contact@example.com" not in json.dumps(created)
    monkeypatch.delenv("ATLAS_ALLOW_NETWORK", raising=False)
    assert (
        main(
            [
                "edgar",
                "fetch-once",
                "--root",
                str(root),
                "--dataset",
                "edgar-should-not-run",
                "--revision",
                "blocked",
                "--symbol",
                "AAPL",
            ]
        )
        == 2
    )
