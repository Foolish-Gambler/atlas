from __future__ import annotations

from atlas.cli import main


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
