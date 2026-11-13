"""import_mt5 の終端は固定した時計と余裕 1 本分で切る。"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agentic_fx.backtest import cli, mt5_import
from agentic_fx.backtest.mt5_import import effective_import_end, import_mt5
from agentic_fx.entry import main
from agentic_fx.store import ohlcv
from tests.backtest.factories import _conn
from tests.backtest.test_cli import _install_settings

UTC = timezone.utc
GRACE = timedelta(seconds=30)
D0 = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)          # 水曜
T1 = datetime(2026, 11, 4, 12, 5, 20, tzinfo=UTC)
T2 = datetime(2026, 11, 4, 12, 5, 30, tzinfo=UTC)
NOON = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _grid(start, end, width=timedelta(minutes=5)):
    out, t = [], start
    while t < end:
        out.append(t)
        t += width
    return out


def _bar(ts, **changes):
    bar = {"time": ts.isoformat(), "open": 148.0, "high": 148.2,
           "low": 147.9, "close": 148.1, "volume": 10}
    bar.update(changes)
    return bar


def _bridge(calls=None, *, interval="5m", inclusive=True):
    """窓 [from, to] の格子点を全部返す bridge (to は inclusive)。"""
    width = timedelta(minutes=5)

    def fetch(url):
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(url).query)
        lo = datetime.fromisoformat(q["from"][0])
        hi = datetime.fromisoformat(q["to"][0])
        if calls is not None:
            calls.append((lo, hi))
        end = hi + width if inclusive else hi
        return {"symbol": "USDJPY", "interval": interval,
                "bars": [_bar(t) for t in _grid(lo, end, width)]}
    return fetch


def _stored(conn):
    return [b.ts for b in ohlcv.load_history_bars(conn, "USDJPY", "5m", source="mt5")]


def _run(conn, start, end, now, **kw):
    kw.setdefault("fetch", _bridge())
    return import_mt5(conn, "USDJPY", start, end, base_url="http://x",
                      interval="5m", clock=lambda: now, grace=GRACE, **kw)


# ---- 実効終端 -----------------------------------------------------------

@pytest.mark.parametrize("now, last_stored", [
    (T1, datetime(2026, 11, 4, 11, 50, tzinfo=UTC)),
    (T2, datetime(2026, 11, 4, 11, 55, tzinfo=UTC)),
])
def test_future_end_is_cut_to_one_bar_margin_before_the_clock(tmp_path, now, last_stored):
    conn = _conn(tmp_path)
    _run(conn, NOON - timedelta(hours=1), NOON + timedelta(days=3), now)
    stored = _stored(conn)
    assert stored[-1] == last_stored
    assert stored[0] == NOON - timedelta(hours=1)


def test_effective_end_formula_boundaries():
    five = timedelta(minutes=5)
    far = NOON + timedelta(days=9)
    assert effective_import_end(far, T1, grace=GRACE, interval="5m") == \
        datetime(2026, 11, 4, 11, 55, tzinfo=UTC)
    assert effective_import_end(far, T2, grace=GRACE, interval="5m") == NOON
    # grace ちょうど足りない / 足りる
    just_before = datetime(2026, 11, 4, 12, 5, 29, tzinfo=UTC)
    assert effective_import_end(far, just_before, grace=GRACE, interval="5m") == \
        datetime(2026, 11, 4, 11, 55, tzinfo=UTC)
    # 要求終端が手前なら要求終端
    early = NOON - five
    assert effective_import_end(early, T2, grace=GRACE, interval="5m") == early
    # grace 0 でも 1 本分の余裕は残る
    assert effective_import_end(far, NOON + five, grace=timedelta(0), interval="5m") == NOON


def test_end_before_the_effective_end_is_untouched_and_exclusive(tmp_path):
    conn = _conn(tmp_path)
    end = NOON - timedelta(hours=1)
    _run(conn, end - timedelta(hours=1), end, T2)
    stored = _stored(conn)
    assert stored[-1] == end - timedelta(minutes=5)
    assert end not in stored


def test_only_a_clock_that_is_read_once_decides_every_window(tmp_path):
    """複数日窓でも同じ T を使い、窓終端を実時計の代用にしない。"""
    conn = _conn(tmp_path)
    reads = []

    def clock():
        reads.append(1)
        return T2 + timedelta(days=(len(reads) - 1) * 10)   # 2 回目以降なら遠い未来

    import_mt5(conn, "USDJPY", D0 - timedelta(days=2), D0 + timedelta(days=5),
               base_url="http://x", interval="5m", fetch=_bridge(),
               clock=clock, grace=GRACE)
    assert len(reads) == 1
    assert _stored(conn)[-1] == datetime(2026, 11, 4, 11, 55, tzinfo=UTC)


def test_default_clock_is_the_real_utc_now(tmp_path):
    conn = _conn(tmp_path)
    future = datetime.now(UTC).replace(microsecond=0, second=0, minute=0) + timedelta(days=2)
    calls = []
    import_mt5(conn, "USDJPY", future - timedelta(days=4), future,
               base_url="http://x", interval="5m", fetch=_bridge(calls))
    assert calls and calls[-1][1] <= datetime.now(UTC)


# ---- 対象なし -----------------------------------------------------------

def test_nothing_confirmed_makes_no_request_and_no_write(tmp_path):
    conn = _conn(tmp_path)
    calls = []
    result = _run(conn, datetime(2026, 11, 4, 11, 55, tzinfo=UTC),
                  datetime(2026, 11, 4, 13, 0, tzinfo=UTC), T1,
                  fetch=lambda url: calls.append(url) or pytest.fail("fetched"))
    assert (result.inserted, result.unchanged, result.conflicted) == (0, 0, 0)
    assert calls == []
    assert _stored(conn) == []


def test_start_equal_to_the_effective_end_is_nothing_to_do(tmp_path):
    conn = _conn(tmp_path)
    r = _run(conn, NOON, NOON + timedelta(hours=1), T2,
             fetch=lambda url: pytest.fail("fetched"))
    assert r.inserted == 0


# ---- 容量は実効終端で見る ------------------------------------------------

def test_capacity_check_uses_the_effective_end(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(mt5_import, "MAX_IMPORT_ROWS", 300)
    # 要求は 10 日 (2880 本) だが実効終端では 288 本 → 上限内
    start = NOON - timedelta(days=1)
    _run(conn, start, start + timedelta(days=10), T2)
    assert len(_stored(conn)) == 288
    # 実効終端でも超えるなら拒否
    monkeypatch.setattr(mt5_import, "MAX_IMPORT_ROWS", 100)
    with pytest.raises(mt5_import.ImportTooLargeError):
        _run(conn, start, start + timedelta(days=10), T2)


def test_window_capacity_check_is_bounded_by_the_effective_end(tmp_path):
    """最終窓の容量は実効終端までの本数で、要求終端までではない。"""
    conn = _conn(tmp_path)
    start = datetime(2026, 11, 4, 11, 0, tzinfo=UTC)
    # 実効終端 12:00 までは 12 本。13 本以上返す bridge は容量超過
    def fetch(url):
        return {"symbol": "USDJPY", "interval": "5m",
                "bars": [_bar(start + i * timedelta(minutes=5)) for i in range(20)]}
    with pytest.raises(ValueError, match="capacity"):
        _run(conn, start, start + timedelta(days=2), T2, fetch=fetch)
    assert _stored(conn) == []


# ---- 右端 ---------------------------------------------------------------

def test_inclusive_right_edge_of_the_last_window_is_dropped(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, NOON - timedelta(hours=2), NOON, T2 + timedelta(hours=3))
    stored = _stored(conn)
    assert NOON not in stored and stored[-1] == NOON - timedelta(minutes=5)


def test_intermediate_window_edge_is_fetched_again_by_the_next_window(tmp_path):
    conn = _conn(tmp_path)
    calls = []
    _run(conn, D0 - timedelta(days=1), NOON, T2 + timedelta(days=3),
         fetch=_bridge(calls))
    assert len(calls) == 2
    stored = _stored(conn)
    assert D0 in stored
    assert stored == _grid(D0 - timedelta(days=1), NOON)


# ---- naive 応答 ---------------------------------------------------------

def test_naive_bridge_time_is_rejected_and_nothing_is_written(tmp_path):
    conn = _conn(tmp_path)

    def fetch(url):
        return {"symbol": "USDJPY", "interval": "5m", "bars": [
            _bar(NOON - timedelta(hours=1)),
            _bar(NOON - timedelta(minutes=55),
                 time=(NOON - timedelta(minutes=55)).replace(tzinfo=None).isoformat())]}
    with pytest.raises(ValueError, match="naive"):
        _run(conn, NOON - timedelta(hours=1), NOON, T2 + timedelta(hours=3), fetch=fetch)
    assert _stored(conn) == []


def test_aware_non_utc_bridge_time_is_normalized(tmp_path):
    conn = _conn(tmp_path)
    jst = timezone(timedelta(hours=9))
    t = NOON - timedelta(hours=1)

    def fetch(url):
        return {"symbol": "USDJPY", "interval": "5m",
                "bars": [_bar(t, time=t.astimezone(jst).isoformat())]}
    _run(conn, t, NOON, T2 + timedelta(hours=3), fetch=fetch)
    assert _stored(conn) == [t]


def test_a_naive_clock_is_refused(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="aware"):
        import_mt5(conn, "USDJPY", D0, NOON, base_url="http://x", interval="5m",
                   fetch=_bridge(), clock=lambda: NOON.replace(tzinfo=None), grace=GRACE)


# ---- CLI ----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("2026-11-04", datetime(2026, 11, 4, tzinfo=UTC)),
    ("2026-11-04T01:30", datetime(2026, 11, 4, 1, 30, tzinfo=UTC)),
    ("2026-11-04T01:30Z", datetime(2026, 11, 4, 1, 30, tzinfo=UTC)),
    ("2026-11-04T01:30:00+00:00", datetime(2026, 11, 4, 1, 30, tzinfo=UTC)),
])
def test_parse_date_accepts_naive_and_utc_inputs(text, expected):
    got = cli._parse_date(text)
    assert got == expected and got.utcoffset() == timedelta(0)


@pytest.mark.parametrize("text", [
    "2026-11-04T00:00+09:00", "2026-11-04T00:00:00-05:00", "2026-11-04T00:00+00:30"])
def test_parse_date_rejects_a_nonzero_offset_and_points_to_utc(text):
    with pytest.raises(argparse.ArgumentTypeError, match="UTC"):
        cli._parse_date(text)


def test_cli_rejects_a_nonzero_offset_before_touching_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch.object(mt5_import, "import_history_bars") as store, \
         pytest.raises(SystemExit) as exc:
        main(["history", "import", "--source", "mt5", "--symbol", "USDJPY",
              "--from", "2026-11-04T00:00+09:00", "--to", "2026-11-05"])
    assert exc.value.code == 2
    assert "UTC" in capsys.readouterr().err
    store.assert_not_called()


def _cli_import(tmp_path, monkeypatch, capsys, now, frm, to, fetch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    monkeypatch.setattr(cli, "_utc_now", lambda: now)
    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch.object(mt5_import, "_default_fetch", side_effect=fetch):
        rc = main(["history", "import", "--source", "mt5", "--symbol", "USDJPY",
                   "--interval", "5m", "--from", frm, "--to", to])
    return rc, capsys.readouterr()


def test_cli_nothing_confirmed_is_rc0_and_says_so(tmp_path, monkeypatch, capsys):
    rc, out = _cli_import(tmp_path, monkeypatch, capsys, T1,
                          "2026-11-04T11:55", "2026-11-04T13:00",
                          lambda url: pytest.fail("fetched"))
    assert rc == 0
    assert "確定済み" in out.out and "対象なし" in out.out
    assert "inserted=0" in out.out


def test_cli_prints_requested_effective_end_and_the_margin(tmp_path, monkeypatch, capsys):
    rc, out = _cli_import(tmp_path, monkeypatch, capsys, T2,
                          "2026-11-04T10:00", "2026-11-09", _bridge())
    assert rc == 0
    assert "2026-11-09T00:00:00+00:00" in out.out          # 要求終端
    assert "2026-11-04T12:00:00+00:00" in out.out          # 実効終端
    assert "1 本" in out.out                               # 余裕
    assert "inserted=24" in out.out


def test_cli_uses_the_configured_grace(tmp_path, monkeypatch, capsys):
    """grace は settings の closed_bar_grace_sec。T=12:05:30 は 30 秒なら 12:00 まで。"""
    seen = []
    real = mt5_import.import_mt5

    def spy(*a, **kw):
        seen.append(kw["grace"])
        return real(*a, **kw)
    monkeypatch.setattr(cli, "import_mt5", spy)
    _cli_import(tmp_path, monkeypatch, capsys, T2,
                "2026-11-04T10:00", "2026-11-04T13:00", _bridge())
    assert seen == [timedelta(seconds=30)]
