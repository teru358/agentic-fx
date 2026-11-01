"""経済指標カレンダー (ForexFactory 週間 JSON)。fail soft — 執行系ではない。

方針の使い分け:
- **ソース単位は fail soft**: 取得に失敗しても取引は止めない (価格の
  fail closed とは違う)。ただし失敗は技術ログ **と activity** に残す
  (Task 7 の「死んだフィードが永久に無音」の再発防止)。
- **イベント単位も fail soft**: 1 件の壊れたイベントで週全体を捨てない
  (fetchers.fetch_feed と同方針)。捨てたものは技術ログに残す。
- **時刻だけは妥協しない**: naive な日時を local / UTC とみなさない
  (プロジェクト絶対制約)。sources._to_utc は fail closed で ValueError を
  投げるが、こちらは執行系ではないので「そのイベントだけ捨てて警告」に
  する — 週全体を捨てるより情報が残り、かつ**間違った時刻は 1 件も
  作らない**。時刻が 1 時間ずれた高インパクト指標は「無いこと」より
  危険 (エージェントが「指標前ではない」と誤認する)。

実測 (2026-07-29 に本番 URL へ実接続、詳細は task-8-report.md):
HTTP 200 / 92 件 / 全件が title,country,date,impact,forecast,previous の
6 キーを持ち、date は必ず NY ローカルのオフセット付き ISO
("2026-07-26T19:50:00-04:00")、impact は High/Medium/Low の 3 種のみ
(Holiday は当該週には出現せず)、空の forecast/previous は "" (null でも
キー欠損でもない)。
"""
from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

import httpx

from agentic_fx._retry_after import retry_after_from
from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core import econ_window
from agentic_fx.core.contracts import Clock
from agentic_fx.core.econ_window import WindowSettings, WindowView
from agentic_fx.store import econ_events, fetch_attempts, ohlcv_slices
from agentic_fx.store.short_tx import Begin, begin_immediate

_log = logging.getLogger("agentic_fx.econ")

_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# ForexFactory の impact → 重要度。**0 は「High/Medium/Low の尺度に乗って
# いない」を意味する** (未知値または Holiday 等)。「重要度が低い」ではない
# ので、下流は 0 を「無害」と読んではいけない。未知値は必ず警告する。
_IMPACT = {"High": 3, "Medium": 2, "Low": 1}
_UNKNOWN_IMPACT = 0


def _event_ts(raw: object) -> datetime:
    """ForexFactory の date 文字列を tz-aware UTC の datetime にする。

    naive (オフセット無し) は ValueError。`.astimezone(timezone.utc)` は
    naive を**ローカル時刻とみなして**変換してしまうため、その前に弾く
    (sources._to_utc と同じ方針)。UTC へ正規化するのは、store が ts を
    ISO 文字列として比較・整列するため — オフセットが混ざると順序も
    範囲検索も壊れる。ForexFactory は NY ローカル (夏 -04:00 / 冬 -05:00)
    を返すので、正規化しないと季節で 1 時間ずれる。
    """
    if not isinstance(raw, str):
        raise ValueError(f"date is not a string: {type(raw).__name__}")
    ts = datetime.fromisoformat(raw)
    if ts.tzinfo is None:
        raise ValueError(
            "ForexFactory returned a naive datetime; tz info is required "
            "(cannot safely assume UTC or local time)")
    return ts.astimezone(timezone.utc)


def _text_or_none(raw: object) -> str | None:
    """空欄 ("" / 空白のみ / 欠損) を None にする。値はそのまま残す。

    **非文字列は None** — 数値や list が来たら「値が無い」として扱う。
    `str()` で文字列化すると `"['3.1%']"` のような偽の予想値が DB に入り、
    LLM の判断材料として本物と区別できなくなる。
    """
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


@dataclass(frozen=True, slots=True)
class CalendarFetch:
    """カレンダー 1 回分の取得結果。

    **なぜ `list[dict]` (ブリーフの契約) ではなく型を返すか** (修正ラウンド 1
    I-1): 「取れたイベント」だけを返すと、呼び出し側は「本当に 0 件の週」と
    「92 件すべてを捨てた (= FF の仕様変更)」を区別できず、後者に対して
    **成功行**を activity に書いてしまう。捨てた件数を一緒に返す必要がある。
    タプルではなく dataclass にしたのは、(a) 呼び出し側が添字ではなく名前で
    読むため取り違えが起きない、(b) 将来 unknown impact 件数などを足すときに
    既存の呼び出し側を壊さずに済むため。frozen+slots は本リポジトリの
    既存の値オブジェクト (Bar / Quote / Article) と同じ形。
    """
    events: list[dict]
    dropped: int


def fetch_ff_calendar(*, timeout_sec: float) -> CalendarFetch:
    """ForexFactory の週間カレンダーを取得して正規化する。

    ネットワーク層・ペイロード全体の異常は例外として送出する (呼び出し側
    `EconCalendar.refresh` が fail soft に受ける)。**個々のイベントの
    異常はここで捨てて警告し、捨てた件数を戻り値に載せる** — 1 件で週全体を
    失わないため、かつ「本当に 0 件の週」と「全件捨てた」を呼び出し側が
    区別できるようにするため (修正ラウンド 1: I-1)。

    **1 エントリの処理は最初から最後まで try の中で完結させる** (I-2)。
    以前は `impact` の取得以降が try の外にあり、`{"impact": ["High"]}` の
    ような 1 件が `TypeError: unhashable type` を投げて健全なイベントごと
    週全体を落としていた — docstring がコードより広い主張をしていた。
    """
    r = httpx.get(_URL, timeout=timeout_sec, follow_redirects=True)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list):
        # HTTP 200 のままエラーページや別形式が返る場合への防御。
        # dict をそのまま回すとキー文字列を全件捨てるだけになり、
        # 「0 件だった」と「取得に失敗した」が区別できなくなる。
        # (存在しない週の URL は 404 + text/html なので、その経路は
        #  ここではなく raise_for_status() が受ける — 実測値は報告書 §2)
        raise ValueError(
            f"unexpected calendar payload type: {type(payload).__name__}")

    out: list[dict] = []
    dropped = 0
    unknown_impacts: Counter[str] = Counter()
    for raw in payload:
        try:
            if not isinstance(raw, dict):
                raise ValueError(f"entry is not an object: {type(raw).__name__}")
            country = raw.get("country")
            name = raw.get("title")
            if not isinstance(country, str) or not country.strip():
                raise ValueError("entry has no usable country")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("entry has no usable title")
            ts = _event_ts(raw.get("date"))
            # impact も try の内側で扱う: unhashable な値 (list/dict) は
            # dict の get/in で TypeError になる
            impact = raw.get("impact")
            importance = _IMPACT.get(impact, _UNKNOWN_IMPACT)
            unknown = impact not in _IMPACT
            out.append({"ts": ts, "country": country.strip(),
                        "name": name.strip(), "importance": importance,
                        "forecast": _text_or_none(raw.get("forecast")),
                        "previous": _text_or_none(raw.get("previous"))})
        except Exception as e:  # noqa: BLE001 — 1 件で週全体を落とさない
            # 生のエントリは丸ごとログに出さない (将来の仕様変更で何が
            # 載るか分からない)。title だけは同定に要るので repr で出す。
            dropped += 1
            _log.warning("econ event skipped (%s): title=%r",
                         safe_error_text(e),
                         raw.get("title") if isinstance(raw, dict) else None)
            continue
        if unknown:
            # 週内に同じ未知値が何十件も出るのでここでは溜めて、ループ後に
            # 値ごとに 1 行だけ出す (無音にはしない / ログも溢れさせない)。
            # 集計だけをループ外に出し、値の取得自体は try の内側に置く。
            unknown_impacts[repr(impact)] += 1

    for value, count in unknown_impacts.items():
        _log.warning(
            "econ calendar: unknown impact %s on %d event(s); stored as "
            "importance=%d (means 'not on the High/Medium/Low scale', "
            "not 'harmless')", value, count, _UNKNOWN_IMPACT)
    return CalendarFetch(events=out, dropped=dropped)


@dataclass(frozen=True, slots=True)
class RefreshPass:
    """refresh の直後の pass の結果。`superseded` は `(旧 row id, 新 row id)`。

    `sync` は set 台帳の差分 (作成・cancelled・再有効化・見送り)。pair の設定が
    無い、または transaction が commit されなかったときは `None`。
    """
    superseded: tuple[tuple[int, int], ...]
    sync: ohlcv_slices.SyncResult | None


class EconCalendar:
    """`pair_currencies` は settings の並びの `pair -> (base, quote)`
    (`econ_window.currencies_by_pair`)。省略 (空) なら set の pass と
    `window_view` は使えず、従来どおり取得・保存・`upcoming` だけが働く。
    """

    def __init__(self, conn: sqlite3.Connection, activity: ActivityLog,
                 clock: Clock, *, timeout_sec: float,
                 window: WindowSettings | None = None,
                 pair_currencies: Mapping[str, tuple[str, str]] | None = None,
                 refresh_interval: timedelta = fetch_attempts.ECON_REFRESH_INTERVAL,
                 begin: Begin = begin_immediate) -> None:
        self.conn = conn
        self.activity = activity
        self.clock = clock
        self.timeout_sec = timeout_sec
        self.window = window if window is not None else WindowSettings()
        self.pair_currencies = dict(pair_currencies or {})
        self.refresh_interval = refresh_interval
        self.begin = begin
        self.last_pass: RefreshPass | None = None

    def refresh(self) -> int:
        """カレンダーを取り込み、**保存できた件数**を返す。

        失敗しても例外を漏らさない (fail soft)。ただし技術ログと activity
        の両方に残す — activity に出ないと、恒常的な失敗が人の目に触れる
        経路が無くなる。

        **「取得はできたが 1 件も使えなかった」は成功ではない** (修正
        ラウンド 1: I-1)。FF がキー名や日付書式を変えると全件がエントリ
        単位で捨てられ、例外は 1 つも出ないまま `0 events` の成功行だけが
        残る — 「本当に予定が無い週」と見分けが付かず、まさに気づく必要が
        ある日に無音になる。捨てた件数を見て失敗として扱う。
        """
        try:
            fetched = fetch_ff_calendar(timeout_sec=self.timeout_sec)
        except Exception as e:  # noqa: BLE001 — カレンダーで取引を止めない
            return self._record_failure("fetch", e)
        events, dropped = fetched.events, fetched.dropped
        if not events and dropped:
            return self._record_failure("parse", ValueError(
                f"all {dropped} calendar event(s) were dropped; "
                "the upstream format may have changed"))
        stored = 0
        new_ids: list[int] = []
        try:
            for ev in events:
                saved = econ_events.upsert(self.conn, ts=ev["ts"],
                                           country=ev["country"],
                                           name=ev["name"],
                                           importance=ev["importance"],
                                           forecast=ev["forecast"],
                                           previous=ev["previous"])
                if saved.inserted or saved.revived:
                    new_ids.append(saved.id)
                stored += 1
        except Exception as e:  # noqa: BLE001 — DB 障害でも取引を止めない
            # ここまでに保存できた分は DB に残る (upsert は 1 件ずつ commit
            # する) ので、戻り値も実際に保存できた件数にする。0 を返すと
            # 「1 件も入っていない」と読めてしまい、戻り値が嘘になる。
            # 失敗そのものは activity / 技術ログ側で可視化する。
            self._record_failure("store", e)
            return stored       # 成功行 (econ_refreshed) は書かない
        # drop 件数も残す: 0 件なら「全部読めた」ことの記録になり、
        # 増え始めたら仕様変更の予兆として人が気づける
        self.activity.write(Category.NEWS, "econ_refreshed",
                            f"{stored} events ({dropped} dropped)")
        self._record_outcome(ok=True)
        self._after_refresh(events, new_ids)
        return stored

    def _after_refresh(self, events: list[dict], new_ids: list[int]) -> None:
        """全 row を保存し終えた refresh の直後にだけ走る冪等な pass。

        時刻が変わった event の置換 → 対象 event × pair の set の作成 /
        cancelled 化 / 再有効化、の順に別々の transaction で commit する
        (event の保存と同じ transaction は要求しない)。失敗しても取引は止めず、
        保存済みの row と commit されなかった supersede (保留として永続化)
        は次の refresh か起動時の補完 (`sync_sets`) が拾う。
        """
        self.last_pass = None
        try:
            now = self._now()
            econ_events.save_pending_supersede(
                self.conn, new_ids=new_ids, now=now,
                fetched_keys=[(ev["ts"], ev["country"], ev["name"])
                              for ev in events])
            done = self._run_pending_supersede(now)
            if done is None:
                return
            sync = self.sync_sets(now=now)
            self.last_pass = RefreshPass(tuple(done), sync)
        except Exception as e:  # noqa: BLE001 — pass の失敗で取引を止めない
            text = safe_error_text(e)
            _log.warning("econ refresh pass failed: %s", text)
            try:
                self.activity.write(Category.NEWS, "econ_refresh_failed",
                                    f"sets: {text}")
            except Exception:  # noqa: BLE001
                _log.exception("econ refresh pass failed and activity failed")

    def _run_pending_supersede(self, now: datetime,
                               ) -> list[tuple[int, int]] | None:
        """保留中の supersede を実行する。保留が無ければ `[]`、commit
        されなければ `None` (保留は残り、次の refresh か起動時補完が再試行)。"""
        result = econ_events.supersede_pending_tx(
            self.conn, now=now, begin=self.begin)
        if result is None:
            return []
        if not result.committed:
            _log.warning("econ supersede not committed (%s)", result.status)
            return None
        return result.value

    def sync_sets(self, *, now: datetime | None = None,
                  ) -> ohlcv_slices.SyncResult | None:
        """対象 event × pair の set 台帳を保存済み row 全体に揃える (冪等)。

        作成 (`awaiting`)、対象外になった event の未完 set の `cancelled` 化
        (superseded・importance 低下・pair 削除)、設定由来の再有効化。supersede
        は含まない。refresh の直後の pass と worker 起動時の補完が使う。
        pair の設定が無ければ何もしない (`None`)。commit されなければ `None`。
        先に commit されていない supersede の保留を実行する (commit されなければ
        set は触らず `None`、保留は残る)。
        """
        now = self._now() if now is None else now
        if self._run_pending_supersede(now) is None:
            return None
        if not self.pair_currencies:
            return None
        targets = ohlcv_slices.event_set_targets(
            self.conn, importance_min=self.window.importance_min,
            pair_currencies=self.pair_currencies)
        return ohlcv_slices.sync_event_sets(
            self.conn, targets=targets, scope_event_ids=None, now=now,
            begin=self.begin)

    def _record_failure(self, stage: str, e: BaseException) -> int:
        text = safe_error_text(e)
        _log.warning("econ calendar %s failed: %s", stage, text)
        self._remember_retry_after(e)
        self.activity.write(Category.NEWS, "econ_refresh_failed",
                            f"{stage}: {text}")
        self._record_outcome(ok=False)
        return 0

    def _record_outcome(self, *, ok: bool) -> None:
        try:
            fetch_attempts.record_outcome(
                self.conn, fetch_attempts.ECON_KEY, self.clock.now(), ok=ok)
        except Exception as exc:  # noqa: BLE001 — 結果の記録で失敗を増やさない
            _log.warning("econ outcome record failed: %s", safe_error_text(exc))

    def _now(self) -> datetime:
        return self._utc(self.clock.now())

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime; tz info is required "
                "(cannot safely assume UTC)")
        return value.astimezone(timezone.utc)

    def _remember_retry_after(self, e: BaseException) -> None:
        """429 等の Retry-After を残し、scheduler が次の取得時刻に反映する。"""
        try:
            now = self.clock.now()
            wait = retry_after_from(e, now)
            if wait is not None:
                fetch_attempts.record_retry_after(
                    self.conn, fetch_attempts.ECON_KEY, now, wait)
        except Exception as exc:  # noqa: BLE001 — 失敗の記録で失敗を増やさない
            _log.warning("econ retry-after record failed: %s",
                         safe_error_text(exc))

    def upcoming(self, hours: int = 24) -> list[dict]:
        """now から hours 以内のイベント (両端を含む) を ts 昇順で返す。

        store は ts を ISO 文字列として比較するため、**clock の値を UTC に
        正規化してから**渡す。非 UTC の aware を素通しすると窓が無音で
        ずれ、naive だとオフセット無しの文字列が "+00:00" 付きの行と
        比較されて全滅する。naive は fail closed (ValueError) — 外部データ
        ではなく内部契約 (Clock は tz-aware を返す) の違反であり、握り
        つぶすと「指標が無い」と誤って読める。
        """
        now = self.clock.now()
        if now.tzinfo is None:
            raise ValueError(
                "clock returned a naive datetime; tz info is required "
                "(cannot safely assume UTC)")
        return econ_events.upcoming(self.conn, now.astimezone(timezone.utc),
                                    hours)

    def freshness(self, now: datetime | None = None) -> str:
        """取得結果の鮮度 (unknown / fresh / stale)。読めなければ unknown。"""
        now = self._now() if now is None else now
        try:
            return fetch_attempts.freshness(
                self.conn, fetch_attempts.ECON_KEY, now,
                refresh_interval=self.refresh_interval)
        except Exception as e:  # noqa: BLE001 — 鮮度が読めないことで止めない
            _log.warning("econ freshness unreadable: %s", safe_error_text(e))
            return econ_window.FRESHNESS_UNKNOWN

    def rule_for(self, pair: str) -> econ_window.WindowRule:
        """pair の帯の規則。pair の対象通貨が設定に無ければ ValueError。"""
        try:
            currencies = self.pair_currencies[pair]
        except KeyError:
            raise ValueError(f"no currencies configured for {pair}") from None
        return econ_window.WindowRule.for_currencies(self.window, currencies)

    def window_view(self, pair: str, now: datetime | None = None) -> WindowView:
        """pair の回避帯の agent 表現 (pair・対象通貨・in_window・active
        events・7 日以内の次 event・freshness)。

        保存済みの event だけで判定する。カレンダーが stale でも帯は作り、
        未知の event は仮定しない。`now` は tz-aware (省略時は clock)。
        """
        rule = self.rule_for(pair)
        now = self._now() if now is None else self._utc(now)
        ccys = sorted(rule.currencies)
        candidates = econ_events.window_candidates(
            self.conn, now, settings=self.window, currencies=ccys)
        upcoming = econ_events.upcoming_targets(
            self.conn, now, settings=self.window, currencies=ccys)
        return econ_window.build_view(
            pair, now, rule, candidates=candidates, upcoming=upcoming,
            freshness=self.freshness(now))
