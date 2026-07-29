"""econ カレンダー (ForexFactory 週間 JSON) のテスト。

ブリーフの逐語コードから意図的に変えた点はすべてここでテストしている
(naive 日時の扱い / 未知 impact / 1 件の壊れたイベント / 失敗の可視化 /
時間窓の境界 / clock の tz)。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.core.contracts import FixedClock
from agentic_fx.datafeed.econ_calendar import EconCalendar, fetch_ff_calendar
from agentic_fx.store import econ_events
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

# 実測 (2026-07-29, https://nfs.faireconomy.media/ff_calendar_thisweek.json):
# 92 件すべてが title/country/date/impact/forecast/previous の 6 キーを持ち、
# date は必ず NY ローカルのオフセット付き ("...-04:00")、空の forecast /
# previous は "" (null でも欠損でもない)。この fixture はその形に揃えてある。
FF_JSON = [
    # 24h 圏内・High
    {"title": "CPI y/y", "country": "USD", "date": "2026-07-22T15:30:00-04:00",
     "impact": "High", "forecast": "3.1%", "previous": "3.0%"},
    # 42h 後 = 24h 圏外・Medium
    {"title": "Retail Sales", "country": "GBP",
     "date": "2026-07-24T02:00:00-04:00", "impact": "Medium",
     "forecast": "", "previous": "0.2%"},
    # 圏内・Low (importance マッピングを 3 段すべて固定するために必要)
    {"title": "SPPI y/y", "country": "JPY", "date": "2026-07-22T20:00:00-04:00",
     "impact": "Low", "forecast": "3.4%", "previous": "3.3%"},
    # ちょうど now (下端境界)
    {"title": "At Now", "country": "EUR", "date": "2026-07-22T08:00:00-04:00",
     "impact": "High", "forecast": "", "previous": ""},
    # ちょうど now + 24h (上端境界)
    {"title": "At Plus 24h", "country": "CHF",
     "date": "2026-07-23T08:00:00-04:00", "impact": "High",
     "forecast": "1.0%", "previous": "0.9%"},
]


def _resp(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _cal(tmp_path, now: datetime = NOW) -> EconCalendar:
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return EconCalendar(conn, ActivityLog(tmp_path / "a.log"), FixedClock(now))


# ---- fetch_ff_calendar ---------------------------------------------------

def test_fetch_maps_fields_and_normalizes_utc():
    with patch("httpx.get", return_value=_resp(FF_JSON)):
        fetched = fetch_ff_calendar()
    events = fetched.events
    assert len(events) == 5
    assert fetched.dropped == 0            # 全件読めたことも戻り値に載る
    assert events[0]["country"] == "USD"
    assert events[0]["name"] == "CPI y/y"
    # importance は 3 段すべてを固定する (1 つずらす改変を落とすため)
    assert [e["importance"] for e in events[:3]] == [3, 2, 1]
    # ts は UTC へ正規化して保存する (ISO 文字列比較の順序保証のため)。
    # ForexFactory は NY ローカル (夏 -04:00 / 冬 -05:00) を返すので、
    # この正規化が無いと季節で 1 時間ずれる。
    assert events[0]["ts"].tzinfo == timezone.utc
    assert events[0]["ts"] == datetime(2026, 7, 22, 19, 30, tzinfo=timezone.utc)
    assert events[0]["ts"].hour == 19  # 15:30-04:00 → 19:30 UTC


def test_fetch_empty_strings_become_none():
    """実測では空欄は "" (null ではない)。DB には NULL として入れる。"""
    with patch("httpx.get", return_value=_resp(FF_JSON)):
        fetched = fetch_ff_calendar()
    events = fetched.events
    assert events[1]["forecast"] is None      # "" → None
    assert events[1]["previous"] == "0.2%"    # 値はそのまま


def test_fetch_non_string_forecast_becomes_none():
    """非文字列の予想値は None (str() で偽の値を作らない)。"""
    payload = [{"title": "Odd", "country": "USD",
                "date": "2026-07-22T15:30:00-04:00", "impact": "High",
                "forecast": ["3.1%"], "previous": 3.0}]
    with patch("httpx.get", return_value=_resp(payload)):
        events = fetch_ff_calendar().events
    assert events[0]["forecast"] is None
    assert events[0]["previous"] is None


def test_fetch_drops_naive_datetime_and_warns(caplog):
    """オフセットの無い日時は「捨てて警告」— naive を local/UTC とみなさない。

    プロジェクト絶対制約: naive を勝手に解釈しない。カレンダーは執行系では
    ないので週全体を落とさず、その 1 件だけ捨てる。
    """
    payload = [{"title": "No TZ", "country": "USD",
                "date": "2026-07-22T15:30:00", "impact": "High",
                "forecast": "", "previous": ""}, *FF_JSON]
    with caplog.at_level(logging.WARNING, logger="agentic_fx.econ"), \
         patch("httpx.get", return_value=_resp(payload)):
        fetched = fetch_ff_calendar()
    events = fetched.events
    assert len(events) == 5                       # 残り 5 件は生きている
    assert fetched.dropped == 1                   # 捨てた件数を呼び出し側へ
    assert all(e["name"] != "No TZ" for e in events)
    assert "naive" in caplog.text.lower()


def test_fetch_skips_broken_entry_and_keeps_the_rest(caplog):
    """1 件の壊れたイベントで週全体を落とさない (Task 5 fetch_feed と同方針)。"""
    payload = [{"title": "Broken"},                  # date/country が無い
               "not a dict",                          # そもそも dict でない
               {"title": "Bad Date", "country": "USD", "date": "not-a-date",
                "impact": "High", "forecast": "", "previous": ""},
               {"title": "", "country": "USD",   # 名前が空 = 同定できない
                "date": "2026-07-22T15:30:00-04:00", "impact": "High",
                "forecast": "", "previous": ""},
               *FF_JSON]
    with caplog.at_level(logging.WARNING, logger="agentic_fx.econ"), \
         patch("httpx.get", return_value=_resp(payload)):
        fetched = fetch_ff_calendar()
    events = fetched.events
    assert len(events) == 5
    assert fetched.dropped == 4
    assert {e["name"] for e in events} == {
        "CPI y/y", "Retail Sales", "SPPI y/y", "At Now", "At Plus 24h"}
    assert caplog.text.count("skipped") >= 4       # 捨てたものは無音にしない


def test_fetch_unknown_impact_is_zero_and_warns(caplog):
    """未知の impact (ForexFactory の "Holiday" 等) を無音で 0 にしない。"""
    payload = [{"title": "Bank Holiday", "country": "JPY",
                "date": "2026-07-22T15:30:00-04:00", "impact": "Holiday",
                "forecast": "", "previous": ""}]
    with caplog.at_level(logging.WARNING, logger="agentic_fx.econ"), \
         patch("httpx.get", return_value=_resp(payload)):
        fetched = fetch_ff_calendar()
    events = fetched.events
    assert len(events) == 1
    assert events[0]["importance"] == 0
    assert "Holiday" in caplog.text          # 実際の値をログに残すこと


def test_fetch_unhashable_impact_does_not_kill_the_week():
    """`impact` が list/dict でも週全体を落とさない (修正ラウンド 1: I-2)。

    `_IMPACT.get(impact, ...)` / `impact not in _IMPACT` は unhashable な値で
    `TypeError` を投げる。この処理が per-entry の try の外にあると、1 件で
    健全なイベントごと全部失われる。
    """
    payload = [{"title": "Weird Impact", "country": "USD",
                "date": "2026-07-22T15:30:00-04:00", "impact": ["High"],
                "forecast": "", "previous": ""},
               {"title": "Dict Impact", "country": "USD",
                "date": "2026-07-22T16:30:00-04:00", "impact": {"a": 1},
                "forecast": "", "previous": ""},
               *FF_JSON]
    with patch("httpx.get", return_value=_resp(payload)):
        fetched = fetch_ff_calendar()          # 例外を貫通させない
    assert len(fetched.events) == 5            # 健全な 5 件は生きている
    assert fetched.dropped == 2


def test_fetch_rejects_non_list_payload():
    """JSON が list でない (エラーページ等) 場合は例外にする。

    dict をそのまま for で回すとキー文字列を 92 回捨てるだけになり、
    「0 件取れた」と「取得に失敗した」が区別できなくなる。
    """
    with patch("httpx.get", return_value=_resp({"error": "nope"})):
        with pytest.raises(ValueError):
            fetch_ff_calendar()


# ---- EconCalendar.refresh ------------------------------------------------

def test_refresh_upserts_and_upcoming(tmp_path):
    cal = _cal(tmp_path)
    with patch("httpx.get", return_value=_resp(FF_JSON)):
        assert cal.refresh() == 5
        assert cal.refresh() == 5          # upsert 冪等
    rows = cal.upcoming(hours=24)
    # 境界は両端とも含む: ちょうど now と ちょうど now+24h
    # ts 昇順。GBP (42h 後) は圏外
    assert [r["name"] for r in rows] == [
        "At Now", "CPI y/y", "SPPI y/y", "At Plus 24h"]
    assert rows[0]["importance"] == 3


def test_upcoming_boundaries_are_inclusive(tmp_path):
    """`<=` を `<` に変える改変を落とすための、境界そのものの検証。"""
    cal = _cal(tmp_path)
    with patch("httpx.get", return_value=_resp(FF_JSON)):
        cal.refresh()
    names = {r["name"] for r in cal.upcoming(hours=24)}
    assert "At Now" in names               # 下端 (now ちょうど)
    assert "At Plus 24h" in names          # 上端 (now+24h ちょうど)
    # 窓を狭めれば上端は外れる
    assert "At Plus 24h" not in {r["name"] for r in cal.upcoming(hours=23)}


def test_upcoming_normalizes_non_utc_clock(tmp_path):
    """clock が非 UTC の aware を返しても窓は同じ (store は ISO 文字列比較)。"""
    jst_now = NOW.astimezone(timezone(timedelta(hours=9)))
    cal = _cal(tmp_path, now=jst_now)
    with patch("httpx.get", return_value=_resp(FF_JSON)):
        cal.refresh()
    assert {r["name"] for r in cal.upcoming(hours=24)} == {
        "At Now", "CPI y/y", "SPPI y/y", "At Plus 24h"}


def test_upcoming_rejects_naive_clock(tmp_path):
    """naive な clock は内部契約違反 — 無音で窓がずれるより落とす (fail closed)。"""
    cal = _cal(tmp_path, now=datetime(2026, 7, 22, 12, 0))
    with pytest.raises(ValueError):
        cal.upcoming(hours=24)


def test_refresh_partial_store_failure_returns_stored_count(tmp_path):
    """途中で DB が落ちても、例外は漏らさず**実際に保存できた件数**を返す。

    upsert は 1 件ずつ commit するので、ここで 0 を返すと戻り値が嘘になる。
    """
    cal = _cal(tmp_path)
    calls = {"n": 0}
    real = econ_events.upsert

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    with patch("httpx.get", return_value=_resp(FF_JSON)), \
         patch.object(econ_events, "upsert", flaky):
        assert cal.refresh() == 2
    # 実際に 2 件 commit 済み (1 件目 CPI は 24h 圏内、2 件目 GBP は圏外)
    assert cal.conn.execute("SELECT COUNT(*) FROM econ_events").fetchone()[0] == 2
    assert [r["name"] for r in cal.upcoming(hours=24)] == ["CPI y/y"]
    tail = "\n".join(cal.activity.tail(10))
    assert "\tNEWS\tecon_refresh_failed\tstore: OperationalError" in tail
    assert "\tNEWS\tecon_refreshed\t" not in tail   # 失敗を成功行で上書きしない


def test_refresh_all_dropped_is_recorded_as_failure(tmp_path):
    """全件が捨てられたら「成功 0 件」ではなく**失敗**として残す (I-1)。

    FF がキー名や日付書式を変えると、例外は 1 つも出ないまま全件が
    エントリ単位で捨てられる。成功行を書くと「本当に予定が無い週」と
    見分けが付かず、まさに気づく必要がある日に無音になる。
    """
    payload = [dict(e, date=e["date"][:19]) for e in FF_JSON]   # 全件 naive
    cal = _cal(tmp_path)
    with patch("httpx.get", return_value=_resp(payload)):
        assert cal.refresh() == 0
    tail = "\n".join(cal.activity.tail(10))
    assert "\tNEWS\tecon_refresh_failed\tparse: ValueError:" in tail
    assert "\tNEWS\tecon_refreshed\t" not in tail


def test_refresh_success_summary_reports_dropped_count(tmp_path):
    """成功時の summary にも drop 件数を残す (増え始めたら予兆として見える)。"""
    payload = [{"title": "No TZ", "country": "USD",
                "date": "2026-07-22T15:30:00", "impact": "High",
                "forecast": "", "previous": ""}, *FF_JSON]
    cal = _cal(tmp_path)
    with patch("httpx.get", return_value=_resp(payload)):
        assert cal.refresh() == 5
    assert "\tNEWS\tecon_refreshed\t5 events (1 dropped)\t" in \
        "\n".join(cal.activity.tail(10))


def test_refresh_fail_soft(tmp_path):
    cal = _cal(tmp_path)
    with patch("httpx.get", side_effect=OSError("down")):
        assert cal.refresh() == 0          # 例外を漏らさない


def test_refresh_failure_is_recorded_in_activity(tmp_path):
    """失敗が activity に残ること (Task 7 の「死んだフィードが永久に無音」対策)。"""
    cal = _cal(tmp_path)
    with patch("httpx.get", side_effect=OSError("down")):
        cal.refresh()
    tail = "\n".join(cal.activity.tail(10))
    assert "\tNEWS\tecon_refresh_failed\tfetch: OSError: down\t" in tail


def test_refresh_success_is_recorded_in_activity(tmp_path):
    cal = _cal(tmp_path)
    with patch("httpx.get", return_value=_resp(FF_JSON)):
        cal.refresh()
    tail = "\n".join(cal.activity.tail(10))
    # summary フィールドに限定して照合する (行全体を対象にすると ISO
    # タイムスタンプの数字が偶然一致して、件数が何でも通ってしまう)
    assert "\tNEWS\tecon_refreshed\t5 events (0 dropped)\t" in tail


def test_refresh_genuinely_empty_week_is_success_not_failure(tmp_path):
    """**本当に 0 件の週**は成功として記録する (再レビュー指摘)。

    I-1 の修正で入った `if not events and dropped:` の `and dropped` 側に
    ピンが無く、`and dropped` を落とす改変が全テスト緑のまま生存していた
    (refresh 経路に空ペイロードを流すテストが 1 本も無かったため)。
    これは Task 7 の「bozo=False の 0 件はエラーではない」と同型の論点 —
    「取得できたが 1 件も使えなかった」(dropped > 0) だけが失敗であり、
    「そもそもイベントが無い週」は失敗ではない。
    """
    cal = _cal(tmp_path)
    with patch("httpx.get", return_value=_resp([])):
        assert cal.refresh() == 0
    tail = "\n".join(cal.activity.tail(10))
    assert "\tNEWS\tecon_refreshed\t0 events (0 dropped)\t" in tail
    assert "econ_refresh_failed" not in tail


def test_fetch_raises_for_http_status(tmp_path):
    """`raise_for_status()` の呼び出しにピンを打つ (再レビュー指摘)。

    `_resp()` が `raise_for_status` を no-op の MagicMock にしているため、
    実装から `r.raise_for_status()` を削除しても全テストが緑のままだった。
    ここでは本物の `httpx.Response` を使い、404 が例外になることを確認する
    (404 でエラーページが返る経路は、非 list 検査ではなくここで止まるのが正)。
    """
    req = httpx.Request("GET", "https://ex.example/cal.json")
    resp = httpx.Response(404, request=req, text="<html>not found</html>")
    with patch("httpx.get", return_value=resp):
        with pytest.raises(httpx.HTTPStatusError):
            fetch_ff_calendar()


def test_refresh_failure_does_not_leak_url(tmp_path, caplog):
    """HTTPStatusError の str() には URL が丸ごと入る — ログにも activity にも出さない。"""
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json?apikey=SECRET123"
    req = httpx.Request("GET", url)
    err = httpx.HTTPStatusError(
        f"Server error '503' for url '{url}'",
        request=req, response=httpx.Response(503, request=req))
    cal = _cal(tmp_path)
    with caplog.at_level(logging.WARNING, logger="agentic_fx.econ"), \
         patch("httpx.get", side_effect=err):
        assert cal.refresh() == 0
    tail = "\n".join(cal.activity.tail(10))
    for text in (caplog.text, tail):
        assert "SECRET123" not in text
        assert "faireconomy.media" not in text
        assert "503" in text               # 診断に要る status は残す
