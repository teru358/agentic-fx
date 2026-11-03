"""`afx history import --source mt5` は bridge が時刻規則を保証できない間は取り込まない。"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from agentic_fx.backtest import mt5_import
from agentic_fx.datafeed.sources import BridgeTimeRuleMismatch, BridgeTimeUnverified
from agentic_fx.entry import main
from tests.backtest.test_cli import _install_settings


def _response(status: int, detail: str | None = None) -> httpx.Response:
    body = {"detail": detail} if detail else {"bars": []}
    return httpx.Response(status, json=body, request=httpx.Request("GET", "http://bridge/x"))


@pytest.mark.parametrize(("detail", "error"), [
    ("time_rule_mismatch", BridgeTimeRuleMismatch),
    ("time_rule_unverified", BridgeTimeUnverified),
])
def test_default_fetch_turns_the_bridge_503_into_a_typed_error(detail, error):
    with patch.object(mt5_import.httpx, "get", return_value=_response(503, detail)):
        with pytest.raises(error):
            mt5_import._default_fetch("http://bridge/ohlcv/USDJPY")


def test_default_fetch_leaves_other_503_as_http_errors():
    with patch.object(mt5_import.httpx, "get", return_value=_response(503, "MT5 not connected")):
        with pytest.raises(httpx.HTTPStatusError):
            mt5_import._default_fetch("http://bridge/ohlcv/USDJPY")


@pytest.mark.parametrize(("detail", "word"), [
    ("time_rule_mismatch", "time_rule_mismatch"),
    ("time_rule_unverified", "time_rule_unverified"),
])
def test_history_import_stops_with_rc1_and_no_rows_when_the_bridge_refuses(
        tmp_path, monkeypatch, capsys, detail, word):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch.object(mt5_import.httpx, "get", return_value=_response(503, detail)), \
         patch.object(mt5_import, "import_history_bars") as store:
        rc = main(["history", "import", "--source", "mt5", "--symbol", "USDJPY",
                   "--from", "2026-11-04", "--to", "2026-11-05"])

    captured = capsys.readouterr()
    assert rc == 1
    assert word in captured.err
    assert "0 行" in captured.err
    assert "--from" in captured.err
    assert "inserted=" not in captured.out
    store.assert_not_called()


def test_history_import_leaves_zero_rows_when_a_later_window_is_refused(tmp_path, monkeypatch, capsys):
    from agentic_fx.store.db import connect

    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    answers = iter([
        httpx.Response(200, json={"symbol": "USDJPY", "interval": "1m", "bars": [{
            "time": "2026-11-04T00:00:00+00:00", "open": 148.0, "high": 148.2,
            "low": 147.9, "close": 148.1, "volume": 10}]},
            request=httpx.Request("GET", "http://bridge/x")),
        _response(503, "time_rule_unverified"),
    ])
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch.object(mt5_import.httpx, "get", side_effect=lambda *a, **k: next(answers)):
        rc = main(["history", "import", "--source", "mt5", "--symbol", "USDJPY",
                   "--from", "2026-11-04", "--to", "2026-11-06"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "time_rule_unverified" in err
    assert "0 行" in err
    assert "--from" in err
    conn = connect(tmp_path / "data" / "agentic.db")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_history_import_refuses_an_oversized_period_with_rc2_and_no_request(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    monkeypatch.setattr(mt5_import, "MAX_IMPORT_ROWS", 100)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch.object(mt5_import.httpx, "get") as get:
        rc = main(["history", "import", "--source", "mt5", "--symbol", "USDJPY",
                   "--from", "2026-11-04", "--to", "2026-11-05"])

    assert rc == 2
    assert "期間を分けて" in capsys.readouterr().err
    get.assert_not_called()
