"""実取引日の 1m 足 (tests/fixtures/outage) を 60 秒 poll で再生し、状態の列を実時刻で pin する。

再生は実 service と同じ経路を通す: fake の取得関数が「その時刻までに存在する足」
(形成中の足を含む) を返し、`Ingest.prepare` の確定足判定を通ったものだけが
cache に入り、`OutageStateMachine.observe` がその cache の watermark を読む。
期待時刻は fixture の欠落位置から手計算した値で、実装の出力を写していない。
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.ingest import Ingest
from agentic_fx.datafeed.outage import IngestTickReport, OutageStateMachine
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.ohlcv import upsert_cache_bars

PAIR = "USDJPY"
KEY = (PAIR, "1m")
KEY_15M = (PAIR, "15m")
UTC = timezone.utc
FIXTURES = Path(__file__).parents[1] / "fixtures" / "outage"


def _load(name):
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))["bars"]
    return [Bar(PAIR, "1m", datetime.fromisoformat(b["time"]), b["open"], b["high"],
                b["low"], b["close"], b["volume"]) for b in raw]


def _settings():
    return SimpleNamespace(
        pairs=[PAIR],
        datafeed=SimpleNamespace(primary="mt5", intervals=["1m"], primary_intervals=[],
                                 ingest_budget_sec=10, closed_bar_grace_sec=30))


def _rig(tmp_path, bars, *, activity=None, with_15m=False):
    conn = connect(tmp_path / "outage.db")
    init_db(conn)

    def fetch(pair, interval, start, end, *, timeout):
        # 実 broker と同じく、形成中の足も (end までは) 返す。確定判定は ingest 側の仕事。
        return [b for b in bars if start <= b.ts <= end]

    ingest = Ingest(_settings(), fetch=fetch)
    hard_keys = frozenset({KEY, KEY_15M}) if with_15m else frozenset({KEY})
    widths = {"1m": timedelta(minutes=1), "15m": timedelta(minutes=15)}
    machine = OutageStateMachine(
        conn, hard_keys=hard_keys, interval_widths=widths,
        grace=timedelta(seconds=30), storage_source="mt5-live", activity=activity)
    return conn, ingest, machine


def _replay(conn, ingest, machine, start, end):
    """各分の :00 に poll し、tick ごとの (state, row) を返す。"""
    trace = {}
    now = start
    while now <= end:
        _, report = ingest.prepare(now, conn)
        ingest.commit(conn)
        trace[now] = (machine.observe(now, report), machine.status())
        now += timedelta(minutes=1)
    return trace


def _replay_with_15m(conn, ingest, machine, start, end):
    """`_replay` と同じだが、15m の hard key を合成した報告で足す。

    15m の足は fixture に無いので合成する: 実 ingest と同じく 15m は次の足の確定
    時刻 (毎時 :01 / :16 / :31 / :46 の tick) にだけ取得を試みて succeeded とし、
    直前に確定した 15m 足を cache に入れる。それ以外の tick は 15m を attempted
    にも deferred にもしない (not-attempted)。
    """
    trace = {}
    now = start
    while now <= end:
        _, report = ingest.prepare(now, conn)
        ingest.commit(conn)
        succeeded = set(report.succeeded)
        attempted = set(report.attempted)
        if now.minute % 15 == 1:
            bar_start = (now - timedelta(minutes=1)).replace(
                minute=((now - timedelta(minutes=1)).minute // 15) * 15) - timedelta(minutes=15)
            upsert_cache_bars(conn, [Bar(PAIR, "15m", bar_start, 1, 1, 1, 1, 1)],
                              source="mt5-live")
            conn.commit()
            succeeded.add(KEY_15M)
            attempted.add(KEY_15M)
        report = IngestTickReport(
            attempted=frozenset(attempted), succeeded=frozenset(succeeded),
            failed=report.failed, deferred=report.deferred, empty=report.empty)
        trace[now] = (machine.observe(now, report), machine.status())
        now += timedelta(minutes=1)
    return trace


def _at(day, hh, mm, ss=0):
    return datetime(2026, 9, day, hh, mm, ss, tzinfo=UTC)


def _states(trace):
    return {t: s for t, (s, _row) in trace.items()}


def _events(log):
    return [line.split("\t")[2] for line in log.tail(100)]


def _ticks(day, spec):
    """(時, 分) のリストを UTC の時刻列にする。"""
    return [_at(day, hh, mm) for hh, mm in spec]


# 欠けた足 (fixture の実データ): 9/29 の 21:15 / 21:19-20 / 21:26-27。各欠けは直前の水位の次の足の
# 確認時刻 (足の開始 + 2 分 30 秒) が起点 S。後続の足が届いた tick (確定 = 足の開始 + 1 分 30 秒) で
# 復帰する。9/13 の日曜の開場直後は 21:01-18 が 18 分連続で欠け、以後 21:22-23 / 25 / 28-30 / 41-44

ROLLOVER_RESTRICTED = [(21, 17), (21, 21), (21, 22), (21, 28), (21, 29)]
ROLLOVER_READY_AFTER = [(21, 18), (21, 23), (21, 30)]
SUNDAY_RESTRICTED = ([(21, m) for m in range(3, 21)]           # 21:01-18 の欠け: S=21:02:30
                     + [(21, 24), (21, 25), (21, 27)]          # 21:22-23 / 25
                     + [(21, 30), (21, 31), (21, 32)]          # 21:28-30
                     + [(21, m) for m in range(43, 47)])       # 21:41-44
SUNDAY_READY_AFTER = [(21, 21), (21, 26), (21, 28), (21, 33), (21, 47)]


def test_rollover_day_makes_three_restricted_episodes_that_each_recover(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, ingest, machine = _rig(tmp_path, _load("usdjpy-1m-20260929-rollover.json"),
                                 activity=log)
    trace = _replay(conn, ingest, machine, _at(29, 20, 52), _at(29, 21, 40))
    states = _states(trace)

    restricted_ticks = [t for t, s in states.items() if s == "restricted"]
    assert restricted_ticks == _ticks(29, ROLLOVER_RESTRICTED)
    assert "degraded" not in states.values()
    # 復帰は後続の足が届いた最初の tick で、追加の健全 tick を待たない
    for t in _ticks(29, ROLLOVER_READY_AFTER):
        assert states[t] == "ready"
    assert states[_at(29, 20, 52)] == "ready"
    assert states[_at(29, 21, 40)] == "ready"
    # 起点 = 欠けた足の次の足の確認時刻、期限 = 起点 + 1800 秒
    assert trace[_at(29, 21, 17)][1]["restricted_since"] == "2026-09-29T21:16:30+00:00"
    assert trace[_at(29, 21, 17)][1]["restricted_deadline_at"] == "2026-09-29T21:46:30+00:00"
    assert trace[_at(29, 21, 21)][1]["restricted_deadline_at"] == "2026-09-29T21:50:30+00:00"
    assert trace[_at(29, 21, 28)][1]["restricted_deadline_at"] == "2026-09-29T21:57:30+00:00"
    assert [trace[_at(29, 21, m)][1]["epoch"] for m in (17, 18, 21, 23, 28, 30)] == [
        1, 1, 2, 2, 3, 3]
    assert trace[_at(29, 21, 18)][1]["restricted_deadline_at"] is None
    events = _events(log)
    assert events.count("datafeed_restricted") == 3
    assert events.count("datafeed_recovered_auto") == 3
    assert "datafeed_degraded" not in events


def test_rollover_day_with_an_open_position_is_restricted_and_recovers_in_the_same_way(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, ingest, machine = _rig(tmp_path, _load("usdjpy-1m-20260929-rollover.json"),
                                 activity=log)
    trace = _replay(conn, ingest, machine, _at(29, 20, 52), _at(29, 21, 16))
    orders.insert(conn, pair=PAIR, direction="buy", entry_type="market",
                  horizon="swing", status="open", now=_at(29, 21, 16))
    conn.commit()
    trace.update(_replay(conn, ingest, machine, _at(29, 21, 17), _at(29, 21, 24)))
    states = _states(trace)

    assert states[_at(29, 21, 16)] == "ready"
    # 建玉があっても猶予 (10 分) 内の停滞は restricted。人の確認 (pending) は立てない
    assert states[_at(29, 21, 17)] == "restricted"
    assert trace[_at(29, 21, 17)][1]["pending_human_confirmation"] == 0
    assert trace[_at(29, 21, 17)][1]["epoch"] == 1
    # 後続の足が届いた tick で、建玉ありでも同じ tick で ready に戻る
    assert states[_at(29, 21, 18)] == "ready"
    assert states[_at(29, 21, 24)] == "ready"
    events = _events(log)
    assert events.count("datafeed_restricted") == 2
    assert "datafeed_degraded" not in events


def test_sunday_open_day_makes_restricted_episodes_that_each_recover(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, ingest, machine = _rig(tmp_path, _load("usdjpy-1m-20260913-sunday-open.json"),
                                 activity=log)
    trace = _replay(conn, ingest, machine, _at(13, 21, 2), _at(13, 21, 55))
    states = _states(trace)

    restricted_ticks = [t for t, s in states.items() if s == "restricted"]
    assert restricted_ticks == _ticks(13, SUNDAY_RESTRICTED)
    assert "degraded" not in states.values()
    assert states[_at(13, 21, 2)] == "ready"
    for t in _ticks(13, SUNDAY_READY_AFTER + [(21, 55)]):
        assert states[t] == "ready"
    assert trace[_at(13, 21, 3)][1]["restricted_deadline_at"] == "2026-09-13T21:32:30+00:00"
    assert trace[_at(13, 21, 24)][1]["restricted_deadline_at"] == "2026-09-13T21:53:30+00:00"
    assert trace[_at(13, 21, 43)][1]["restricted_deadline_at"] == "2026-09-13T22:12:30+00:00"
    events = _events(log)
    assert events.count("datafeed_restricted") == 5
    assert events.count("datafeed_recovered_auto") == 5


def test_sunday_open_with_an_open_position_degrades_when_the_stall_passes_ten_open_minutes(
        tmp_path):
    conn, ingest, machine = _rig(tmp_path, _load("usdjpy-1m-20260913-sunday-open.json"))
    trace = _replay(conn, ingest, machine, _at(13, 21, 2), _at(13, 21, 2))
    orders.insert(conn, pair=PAIR, direction="buy", entry_type="market",
                  horizon="swing", status="open", now=_at(13, 21, 2))
    conn.commit()
    trace.update(_replay(conn, ingest, machine, _at(13, 21, 3), _at(13, 21, 14)))
    states = _states(trace)

    # 起点 S=21:02:30 から 21:12:00 は 570 秒で猶予内、21:13:00 は 630 秒で超過
    assert states[_at(13, 21, 3)] == "restricted"
    assert states[_at(13, 21, 12)] == "restricted"
    assert states[_at(13, 21, 13)] == "degraded"
    assert trace[_at(13, 21, 13)][1]["pending_human_confirmation"] == 1
    assert trace[_at(13, 21, 13)][1]["epoch"] == 1
    assert states[_at(13, 21, 14)] == "degraded"


def test_contiguous_day_has_no_state_transition_or_activity(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    conn, ingest, machine = _rig(tmp_path, _load("usdjpy-1m-20260930-no-gap.json"),
                                 activity=log)
    trace = _replay(conn, ingest, machine, _at(30, 20, 47), _at(30, 21, 50))

    assert set(_states(trace).values()) == {"ready"}
    assert machine.status()["epoch"] == 0
    assert log.tail(100) == []


def test_real_config_with_a_15m_hard_key_recovers_at_the_same_times_as_1m_alone(tmp_path):
    bars = _load("usdjpy-1m-20260929-rollover.json")
    conn, ingest, machine = _rig(tmp_path, bars, with_15m=True)
    states = _states(_replay_with_15m(conn, ingest, machine, _at(29, 20, 52), _at(29, 21, 40)))

    restricted_ticks = [t for t, s in states.items() if s == "restricted"]
    assert restricted_ticks == _ticks(29, ROLLOVER_RESTRICTED)
    assert "degraded" not in states.values()
    for t in _ticks(29, ROLLOVER_READY_AFTER + [(21, 40)]):
        assert states[t] == "ready"
