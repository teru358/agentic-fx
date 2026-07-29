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
from datetime import datetime, timezone

import httpx

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.datafeed._safe_error import safe_error_text
from agentic_fx.store import econ_events

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
    """空欄 ("" / 空白のみ / 欠損) を None にする。値はそのまま残す。"""
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def fetch_ff_calendar() -> list[dict]:
    """ForexFactory の週間カレンダーを取得して正規化する。

    ネットワーク層・ペイロード全体の異常は例外として送出する (呼び出し側
    `EconCalendar.refresh` が fail soft に受ける)。**個々のイベントの
    異常はここで捨てて警告する** — 1 件で週全体を失わないため。
    """
    r = httpx.get(_URL, timeout=30, follow_redirects=True)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list):
        # エラーページや仕様変更で dict / 文字列が返ることがある
        # (実測: ff_calendar_nextweek.json は JSON ですらない応答を返す)。
        # dict をそのまま回すとキー文字列を全件捨てるだけになり、
        # 「0 件だった」と「取得に失敗した」が区別できなくなる。
        raise ValueError(
            f"unexpected calendar payload type: {type(payload).__name__}")

    out: list[dict] = []
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
        except Exception as e:  # noqa: BLE001 — 1 件で週全体を落とさない
            # 生のエントリは丸ごとログに出さない (将来の仕様変更で何が
            # 載るか分からない)。title だけは同定に要るので repr で出す。
            _log.warning("econ event skipped (%s): title=%r",
                         safe_error_text(e),
                         raw.get("title") if isinstance(raw, dict) else None)
            continue

        impact = raw.get("impact")
        importance = _IMPACT.get(impact, _UNKNOWN_IMPACT)
        if impact not in _IMPACT:
            # 週内に同じ未知値が何十件も出るのでここでは溜めて、ループ後に
            # 値ごとに 1 行だけ出す (無音にはしない / ログも溢れさせない)
            unknown_impacts[repr(impact)] += 1

        out.append({"ts": ts, "country": country.strip(), "name": name.strip(),
                    "importance": importance,
                    "forecast": _text_or_none(raw.get("forecast")),
                    "previous": _text_or_none(raw.get("previous"))})

    for value, count in unknown_impacts.items():
        _log.warning(
            "econ calendar: unknown impact %s on %d event(s); stored as "
            "importance=%d (means 'not on the High/Medium/Low scale', "
            "not 'harmless')", value, count, _UNKNOWN_IMPACT)
    return out


class EconCalendar:
    def __init__(self, conn: sqlite3.Connection, activity: ActivityLog,
                 clock: Clock) -> None:
        self.conn = conn
        self.activity = activity
        self.clock = clock

    def refresh(self) -> int:
        """カレンダーを取り込み、**保存できた件数**を返す。

        失敗しても例外を漏らさない (fail soft)。ただし技術ログと activity
        の両方に残す — activity に出ないと、恒常的な失敗が人の目に触れる
        経路が無くなる。
        """
        try:
            events = fetch_ff_calendar()
        except Exception as e:  # noqa: BLE001 — カレンダーで取引を止めない
            return self._record_failure("fetch", e)
        stored = 0
        try:
            for ev in events:
                econ_events.upsert(self.conn, ts=ev["ts"],
                                   country=ev["country"], name=ev["name"],
                                   importance=ev["importance"],
                                   forecast=ev["forecast"],
                                   previous=ev["previous"])
                stored += 1
        except Exception as e:  # noqa: BLE001 — DB 障害でも取引を止めない
            # ここまでに保存できた分は DB に残る (upsert は 1 件ずつ commit
            # する) ので、戻り値も実際に保存できた件数にする。0 を返すと
            # 「1 件も入っていない」と読めてしまい、戻り値が嘘になる。
            # 失敗そのものは activity / 技術ログ側で可視化する。
            self._record_failure("store", e)
            return stored       # 成功行 (econ_refreshed) は書かない
        self.activity.write(Category.NEWS, "econ_refreshed",
                            f"{stored} events")
        return stored

    def _record_failure(self, stage: str, e: BaseException) -> int:
        text = safe_error_text(e)
        _log.warning("econ calendar %s failed: %s", stage, text)
        self.activity.write(Category.NEWS, "econ_refresh_failed",
                            f"{stage}: {text}")
        return 0

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
