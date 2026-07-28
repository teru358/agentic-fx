from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from mt5_client import Mt5Client


def _seed_utc_server_offset(client: Mt5Client) -> None:
    """サーバ時刻 = UTC (offset 0) を「たった今検出済み」として仕込む。

    Mt5Client は MT5 の時刻をサーバ時間帯のエポック秒として扱い、オフセットが
    未確定なら fail closed で例外を上げる。本ファイルのテストは deal 集計の
    検証が目的なので、offset 0 (= 移植前とまったく同じ時刻) を置いて再検出
    (MT5 への probe) が走らないようにする。期待値は移植前から変えていない。
    """
    client._server_offset_sec = 0
    client._offset_checked_at = time.time()
    client._offset_source = "cached"
    client._offset_disk_loaded = True


def test_get_closed_deal_uses_out_deals_weighted_price_and_total_pnl():
    client = Mt5Client.__new__(Mt5Client)

    class _FakeMt5:
        DEAL_ENTRY_IN = 0
        DEAL_ENTRY_OUT = 1
        DEAL_ENTRY_OUT_BY = 3

        def history_deals_get(self, position):
            assert position == 111
            return [
                SimpleNamespace(
                    entry=self.DEAL_ENTRY_IN,
                    volume=0.01,
                    price=159.0,
                    profit=0.0,
                    swap=0.0,
                    commission=0.0,
                    time=1_700_000_000,
                    reason=0,
                ),
                SimpleNamespace(
                    entry=self.DEAL_ENTRY_OUT,
                    volume=0.006,
                    price=158.50,
                    profit=-300.0,
                    swap=-1.0,
                    commission=-0.5,
                    time=1_700_000_100,
                    reason=4,
                ),
                SimpleNamespace(
                    entry=self.DEAL_ENTRY_OUT,
                    volume=0.004,
                    price=158.40,
                    profit=-200.0,
                    swap=0.0,
                    commission=-0.5,
                    time=1_700_000_120,
                    reason=4,
                ),
            ]

    client._mt5 = _FakeMt5()
    client._lock = threading.Lock()
    _seed_utc_server_offset(client)

    deal = client.get_closed_deal(111)

    assert deal is not None
    assert deal.ticket == 111
    assert deal.close_price == pytest.approx(158.46)
    assert deal.profit == pytest.approx(-500.0)
    assert deal.swap == pytest.approx(-1.0)
    assert deal.commission == pytest.approx(-1.0)
    assert deal.closed_at.startswith("2023-11-14T22:15:20")


def test_get_closed_deal_returns_none_when_no_out_deal():
    client = Mt5Client.__new__(Mt5Client)

    class _FakeMt5:
        DEAL_ENTRY_IN = 0
        DEAL_ENTRY_OUT = 1
        DEAL_ENTRY_OUT_BY = 3

        def history_deals_get(self, position):
            return [
                SimpleNamespace(
                    entry=self.DEAL_ENTRY_IN,
                    volume=0.01,
                    price=159.0,
                    profit=0.0,
                    swap=0.0,
                    commission=0.0,
                    time=1_700_000_000,
                    reason=0,
                ),
            ]

    client._mt5 = _FakeMt5()
    client._lock = threading.Lock()
    _seed_utc_server_offset(client)

    assert client.get_closed_deal(111) is None
