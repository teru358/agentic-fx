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
    assert "inserted=" not in captured.out
    store.assert_not_called()
