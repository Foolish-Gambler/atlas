from __future__ import annotations

import json
from pathlib import Path

from atlas.cli import main

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
