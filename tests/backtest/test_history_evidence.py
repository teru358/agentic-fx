"""履歴検査の証跡: 生成・対象範囲の無足日一覧・承認時の照合 (本物の検査を通す)。"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone

import pytest

from agentic_fx.backtest import history_evidence as ev
from agentic_fx.backtest import history_health as hh
from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.core.market_hours import is_market_open
from tests.backtest.factories import _conn

pytestmark = pytest.mark.real_history_guard

UTC = timezone.utc
W = timedelta(minutes=5)
DS = HistoryDataset("mt5", "5m")
NOW = datetime(2026, 11, 10, 6, 2, 20, tzinfo=UTC)
GRACE = timedelta(seconds=30)


def _entry(day="2026-11-04", symbol="USDJPY", source="mt5", interval="5m", reason="休場"):
    return {"source": source, "base_interval": interval, "symbol": symbol, "date_utc": day,
            "reason": reason, "approved_by": "operator", "approved_at": "2026-11-05T00:00:00+00:00"}


def _write_list(path, entries):
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"entries": entries}), encoding="utf-8")


def _insert_full(conn, symbol="USDJPY", skip_days=frozenset()):
    t, end, rows = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 10, 6, 0, tzinfo=UTC), []
    while t < end:
        if is_market_open(t) and t.date() not in skip_days:
            rows.append((symbol, "5m", t.isoformat(), 148.0, 148.2, 147.9, 148.1, 1.0, "mt5"))
        t += W
    conn.executemany(
        "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, bar_time, open, high, low, "
        "close, volume, source, spread) VALUES (?,?,?,?,?,?,?,?,?,NULL)", rows)
    conn.commit()


@pytest.fixture
def world(tmp_path):
    conn = _conn(tmp_path)
    _insert_full(conn)
    path = tmp_path / "root" / "config" / "history-no-bars.yaml"
    yield conn, path
    conn.close()


def _health(conn, path, pairs=("USDJPY",)):
    return hh.evaluate_history(conn, dataset=DS, pairs=pairs, now=NOW, holdout_months=1,
                               grace=GRACE, no_bars_path=path)


def _evidence(conn, path, pairs=("USDJPY",)):
    health = _health(conn, path, pairs)
    assert health.ready, health
    return ev.build_evidence(health)


def _verify(evidence, path, **kw):
    kw.setdefault("eval_source", "mt5")
    kw.setdefault("base_interval", "5m")
    return ev.verify_evidence(evidence, no_bars_path=path, **kw)


def test_a_real_ready_check_builds_an_evidence_that_verifies(world):
    conn, path = world
    evidence = _evidence(conn, path)
    assert evidence["verdict"] == "ready" and evidence["version"] == hh.GUARD_VERSION
    assert evidence["pairs"] == ["USDJPY"] and evidence["dataset"] == DS.as_dict()
    assert evidence["no_bars_entries"] == [] and "no_bars_applied" not in evidence
    assert evidence["no_bars_digest"] == hh.EMPTY_NO_BARS.digest
    assert _verify(evidence, path, pairs=["USDJPY"]).ok


def test_time_passing_alone_does_not_invalidate_an_evidence(world):
    conn, path = world
    evidence = _evidence(conn, path)
    # 照合は DB も時計も使わない。履歴が変わっても (ここでは全削除) 結果は同じ
    conn.execute("DELETE FROM ohlcv_history")
    conn.commit()
    assert _verify(evidence, path).ok


@pytest.mark.parametrize("mutate", [
    lambda e: e.pop("verdict"),
    lambda e: e.update(version=0),
    lambda e: e.update(version=True),
    lambda e: e.update(verdict="history_stale"),
    lambda e: e.update(calendar_version="old"),
    lambda e: e["threshold_snapshot"].update(min_fill_ratio="1/2"),
    lambda e: e.update(dataset={"source": "mt5"}),
    lambda e: e.update(dataset={"source": "none", "base_interval": "5m"}),
    lambda e: e.update(pairs=[]),
    lambda e: e.update(pairs=["USDJPY", "USDJPY"]),
    lambda e: e.update(holdout_start="2026-11-11T00:00:00+00:00"),
    lambda e: e.update(holdout_end="2026-11-10T00:00:00"),
    lambda e: e["market_expected"].update(USDJPY=0),
    lambda e: e["market_present"].update(USDJPY=10 ** 9),
    lambda e: e["missing_days"].update(USDJPY=["2026-11-04"]),
    lambda e: e["latest_confirmed_bar"].pop("USDJPY"),
    lambda e: e.update(no_bars_digest="0" * 64),
    lambda e: e.update(extra=1),
])
def test_a_tampered_or_malformed_evidence_is_invalid(world, mutate):
    conn, path = world
    evidence = copy.deepcopy(_evidence(conn, path))
    mutate(evidence)
    assert _verify(evidence, path).status == ev.CHECK_INVALID


@pytest.mark.parametrize("evidence", [None, [], "ready", 1])
def test_a_missing_or_non_mapping_evidence_is_invalid(world, evidence):
    assert _verify(evidence, world[1]).status == ev.CHECK_INVALID


def test_the_evidence_must_match_what_the_payload_evaluated(world):
    conn, path = world
    evidence = _evidence(conn, path)
    assert _verify(evidence, path, eval_source="dukascopy").status == ev.CHECK_INVALID
    assert _verify(evidence, path, base_interval="1m").status == ev.CHECK_INVALID
    assert _verify(evidence, path, pairs=["EURUSD"]).status == ev.CHECK_INVALID
    assert _verify(evidence, path, pairs=["USDJPY", "EURUSD"]).status == ev.CHECK_INVALID


def test_scope_keeps_only_the_evidence_dataset_pairs_and_range():
    entries = hh.load_no_bars  # noqa: F841 - the real loader is covered elsewhere
    rows = [
        hh.NoBarsEntry("mt5", "5m", "USDJPY", date(2026, 11, 4), "r", "o", NOW),
        hh.NoBarsEntry("mt5", "5m", "EURUSD", date(2026, 11, 4), "r", "o", NOW),   # 別 pair
        hh.NoBarsEntry("dukascopy", "5m", "USDJPY", date(2026, 11, 4), "r", "o", NOW),  # 別 dataset
        hh.NoBarsEntry("mt5", "1m", "USDJPY", date(2026, 11, 4), "r", "o", NOW),   # 別 interval
        hh.NoBarsEntry("mt5", "5m", "USDJPY", date(2026, 9, 1), "r", "o", NOW),    # 区間外
    ]
    start, end = datetime(2026, 10, 10, 6, tzinfo=UTC), datetime(2026, 11, 10, 6, tzinfo=UTC)
    scoped = ev.scope_no_bars(rows, dataset=DS, pairs=["USDJPY"], start=start, end=end)
    assert [(e.source, e.base_interval, e.symbol, e.date_utc) for e in scoped] == [
        ("mt5", "5m", "USDJPY", date(2026, 11, 4))]


@pytest.mark.parametrize("day,expected", [
    (date(2026, 10, 9), False),    # 区間の開始日の前日
    (date(2026, 10, 10), True),    # 開始時刻 06:00 を含む日 (部分日)
    (date(2026, 11, 10), True),    # 終端 06:00 の手前を含む日
    (date(2026, 11, 11), False),
])
def test_a_day_counts_when_it_intersects_the_evaluated_range(day, expected):
    start, end = datetime(2026, 10, 10, 6, tzinfo=UTC), datetime(2026, 11, 10, 6, tzinfo=UTC)
    assert ev.day_intersects(day, start, end) is expected


def test_an_end_exactly_at_midnight_excludes_that_day():
    start, end = datetime(2026, 10, 10, tzinfo=UTC), datetime(2026, 11, 10, tzinfo=UTC)
    assert not ev.day_intersects(date(2026, 11, 10), start, end)
    assert ev.day_intersects(date(2026, 11, 9), start, end)


def test_a_no_bars_entry_in_scope_is_stored_and_a_change_to_it_invalidates(world):
    conn, path = world
    _write_list(path, [_entry()])
    evidence = _evidence(conn, path)
    assert [e["date_utc"] for e in evidence["no_bars_entries"]] == ["2026-11-04"]
    assert _verify(evidence, path).ok
    _write_list(path, [_entry(reason="別の理由")])       # 承認情報の変更
    assert _verify(evidence, path).status == ev.CHECK_INVALID
    _write_list(path, [])                                # 削除
    assert _verify(evidence, path).status == ev.CHECK_INVALID


@pytest.mark.parametrize("entry", [
    _entry(day="2026-11-05", symbol="EURUSD"),          # 別 pair の追加
    _entry(day="2026-11-05", source="dukascopy"),       # 別 dataset の追加
    _entry(day="2026-09-01"),                           # 区間外の日の追加
])
def test_an_unrelated_no_bars_entry_does_not_break_the_check(world, entry):
    conn, path = world
    evidence = _evidence(conn, path)
    _write_list(path, [entry])
    assert _verify(evidence, path).ok


def test_adding_an_in_scope_entry_invalidates(world):
    conn, path = world
    evidence = _evidence(conn, path)
    _write_list(path, [_entry(day="2026-11-05")])
    assert _verify(evidence, path).status == ev.CHECK_INVALID


@pytest.mark.parametrize("text", ["entries: [", "entries: 3", "", "- a\n"])
def test_an_unreadable_list_is_unavailable_not_invalid(world, text):
    conn, path = world
    evidence = _evidence(conn, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert _verify(evidence, path).status == ev.CHECK_UNAVAILABLE
    path.unlink()                                        # 修復 (未配置 = 空) で元の証跡が合う
    assert _verify(evidence, path).ok


def test_the_policy_key_follows_the_rule_calendar_and_thresholds(world):
    conn, path = world
    evidence = _evidence(conn, path)
    assert ev.policy_key(evidence) == ev.current_policy_key()
    for mutate in (lambda e: e.update(version=2), lambda e: e.update(calendar_version="x"),
                   lambda e: e["threshold_snapshot"].update(stale_open_hours=48)):
        other = copy.deepcopy(evidence)
        mutate(other)
        assert ev.policy_key(other) != ev.current_policy_key()
    assert ev.policy_key(None) is None and ev.policy_key({}) is None


def test_a_non_ready_health_has_no_evidence(world):
    conn, path = world
    conn.execute("DELETE FROM ohlcv_history")
    conn.commit()
    with pytest.raises(ValueError):
        ev.build_evidence(_health(conn, path))


def test_the_no_bars_path_is_root_based_not_cwd_based(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert ev.no_bars_path(tmp_path / "r") == tmp_path / "r" / "config" / "history-no-bars.yaml"
    assert ev.no_bars_path().is_absolute() and ev.no_bars_path().name == "history-no-bars.yaml"
