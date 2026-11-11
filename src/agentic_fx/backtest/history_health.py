"""評価用履歴 (ohlcv_history) の鮮度と充足の共通検査。

主 pair ごとに、固定した時刻 ``T`` の時点で次を判定する。

- 鮮度: 最新確定足の終端から ``T`` までの開場時間が 24 時間を超えたら
  ``history_stale``。
- 充足: ``[B, N)`` の期待格子に対する存在本数の比が 0.99 未満、または完了した
  UTC 日の欠損が 60 分ぶんを超える日があれば ``history_gap``。
- 判定不能: 期待本数が 0、設定・暦・DB・無足日一覧の読取失敗は
  ``history_check_unavailable``。

判定は純関数 (``expected_grid`` / ``open_duration`` / ``assess_pair``) と、
DB を読む薄い層 (``check_history`` / ``evaluate_history``) に分かれる。時刻は
呼び出し側が渡す ``now`` だけを使い、この module は壁時計を読まない。ネットワーク
取得や履歴の変更もしない。

閾値は人間が持つ定数で、設定や引数では変えられない。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable, NewType

import yaml

from agentic_fx.backtest.dataset import HistoryDataset
from agentic_fx.backtest.holdout import _normalize_now, in_sample_until
from agentic_fx.core.market_hours import is_market_open
from agentic_fx.store.ohlcv import IMPORT_SOURCES

GUARD_VERSION = 1
CALENDAR_VERSION = "fx-ny1700-xmas-newyear-v1"

STALE_OPEN_HOURS = 24
MIN_FILL_RATIO = Fraction(99, 100)
# 完了した UTC 日の欠けがこの時間分を超えたら穴。足の幅で本数に換算する
# (5m なら 12 本、1m なら 60 本、15m なら 4 本)。
MAX_DAY_MISSING = timedelta(minutes=60)

HISTORY_READY = "ready"
HISTORY_STALE = "history_stale"
HISTORY_GAP = "history_gap"
HISTORY_UNAVAILABLE = "history_check_unavailable"
# 代表理由の優先順 (unavailable → stale → gap)。併記もこの順。
REASON_ORDER = (HISTORY_UNAVAILABLE, HISTORY_STALE, HISTORY_GAP)

NO_BARS_RELPATH = Path("config") / "history-no-bars.yaml"

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_HOUR = timedelta(hours=1)
_FILE_ERRORS = (ValueError, KeyError, OSError, ArithmeticError)

IsOpen = Callable[[datetime], bool]

# 人間 (運用者) 専用の証跡。日時・本数・理由・承認者を含むので agent へ渡さない。
OperatorEvidence = NewType("OperatorEvidence", dict)


class CalendarUnavailable(Exception):
    """The market calendar could not answer; the check cannot be judged."""


def _guarded_calendar(is_open: IsOpen) -> IsOpen:
    """暦の呼出しの境界。どんな失敗も判定不能の暦例外に変換する。"""
    def guarded(t: datetime) -> bool:
        try:
            return is_open(t)
        except Exception as exc:
            raise CalendarUnavailable(type(exc).__name__) from exc
    return guarded


def threshold_snapshot() -> dict:
    return {"stale_open_hours": STALE_OPEN_HOURS,
            "min_fill_ratio": f"{MIN_FILL_RATIO.numerator}/{MIN_FILL_RATIO.denominator}",
            "max_day_missing_minutes": int(MAX_DAY_MISSING.total_seconds() // 60)}


# ---- 人が無足を認める日の一覧 -------------------------------------------------

class NoBarsConfigError(ValueError):
    """The no-bars list exists but cannot be read as a valid list."""


@dataclass(frozen=True)
class NoBarsEntry:
    source: str
    base_interval: str
    symbol: str
    date_utc: date
    reason: str
    approved_by: str
    approved_at: datetime

    def as_dict(self) -> dict:
        return {"source": self.source, "base_interval": self.base_interval,
                "symbol": self.symbol, "date_utc": self.date_utc.isoformat(),
                "reason": self.reason, "approved_by": self.approved_by,
                "approved_at": self.approved_at.astimezone(timezone.utc).isoformat()}


def _no_bars_digest(entries: Iterable[NoBarsEntry]) -> str:
    rows = sorted((e.as_dict() for e in entries),
                  key=lambda d: (d["source"], d["base_interval"], d["symbol"], d["date_utc"]))
    blob = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NoBarsList:
    entries: tuple[NoBarsEntry, ...]
    digest: str

    def dates_for(self, source: str, base_interval: str, symbol: str) -> frozenset[date]:
        return frozenset(e.date_utc for e in self.entries
                         if (e.source, e.base_interval, e.symbol) == (source, base_interval, symbol))


EMPTY_NO_BARS = NoBarsList((), _no_bars_digest(()))

class _UniqueKeyLoader(yaml.SafeLoader):
    """重複する mapping キーを黙って上書きせず拒否する。"""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            seen = set()
            for key_node, _value_node in node.value:
                key = self.construct_object(key_node, deep=True)
                try:
                    duplicated = key in seen
                except TypeError as exc:
                    raise yaml.constructor.ConstructorError(
                        None, None, "unhashable mapping key", key_node.start_mark) from exc
                if duplicated:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicated mapping key {key!r}", key_node.start_mark)
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


_ENTRY_KEYS = frozenset({"source", "base_interval", "symbol", "date_utc", "reason",
                         "approved_by", "approved_at"})
_SYMBOL_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BASE_INTERVALS = ("1m", "5m", "15m")
_TEXT_MAX = 200


def _text(entry: dict, key: str, idx: int) -> str:
    value = entry[key]
    if not isinstance(value, str) or not value.strip() or len(value) > _TEXT_MAX:
        raise NoBarsConfigError(f"entries[{idx}].{key} must be a non-empty string")
    return value.strip()


def _parse_entry(idx: int, raw) -> NoBarsEntry:
    if not isinstance(raw, dict) or set(raw) != _ENTRY_KEYS:
        raise NoBarsConfigError(
            f"entries[{idx}] must have exactly the keys {sorted(_ENTRY_KEYS)}")
    source = raw["source"]
    if not isinstance(source, str) or source not in IMPORT_SOURCES:
        raise NoBarsConfigError(f"entries[{idx}].source is not a known history source")
    if not isinstance(raw["base_interval"], str) or raw["base_interval"] not in _BASE_INTERVALS:
        raise NoBarsConfigError(f"entries[{idx}].base_interval must be one of {_BASE_INTERVALS}")
    symbol = raw["symbol"]
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise NoBarsConfigError(f"entries[{idx}].symbol must be one literal symbol (no wildcard)")
    day = raw["date_utc"]
    if isinstance(day, str) and _DATE_RE.fullmatch(day):
        try:
            day = date.fromisoformat(day)
        except ValueError as exc:
            raise NoBarsConfigError(f"entries[{idx}].date_utc is not a date") from exc
    if isinstance(day, datetime) or not isinstance(day, date):
        raise NoBarsConfigError(f"entries[{idx}].date_utc must be a single UTC date (YYYY-MM-DD)")
    approved_at = raw["approved_at"]
    if isinstance(approved_at, str):
        try:
            approved_at = datetime.fromisoformat(approved_at)
        except ValueError as exc:
            raise NoBarsConfigError(f"entries[{idx}].approved_at is not a datetime") from exc
    if not isinstance(approved_at, datetime) or approved_at.tzinfo is None:
        raise NoBarsConfigError(f"entries[{idx}].approved_at must be an aware datetime")
    return NoBarsEntry(
        source=source, base_interval=raw["base_interval"], symbol=symbol, date_utc=day,
        reason=_text(raw, "reason", idx), approved_by=_text(raw, "approved_by", idx),
        approved_at=approved_at.astimezone(timezone.utc))


def load_no_bars(path: Path) -> NoBarsList:
    """無足日一覧を読む。ファイルが無ければ空。あっても読めない・不正なら
    ``NoBarsConfigError`` (空として黙って続けない)。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_NO_BARS
    except (OSError, UnicodeDecodeError) as exc:
        raise NoBarsConfigError(f"cannot read the no-bars list: {type(exc).__name__}") from exc
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 (SafeLoader 派生)
    except (yaml.YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise NoBarsConfigError("the no-bars list is not valid YAML") from exc
    if not isinstance(data, dict) or set(data) != {"entries"} or not isinstance(data["entries"], list):
        # 存在する空・null の文書も不正 (空一覧は「ファイルなし」か `entries: []`)
        raise NoBarsConfigError("the no-bars list must be a mapping with a single 'entries' list")
    try:
        entries = [_parse_entry(i, raw) for i, raw in enumerate(data["entries"])]
    except NoBarsConfigError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as exc:
        raise NoBarsConfigError(f"invalid no-bars entry: {type(exc).__name__}") from exc
    keys = [(e.source, e.base_interval, e.symbol, e.date_utc) for e in entries]
    if len(set(keys)) != len(keys):
        raise NoBarsConfigError("the no-bars list has a duplicated series/day")
    return NoBarsList(tuple(entries), _no_bars_digest(entries))


# ---- 純関数 -------------------------------------------------------------------

def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (aware UTC required)")
    return value.astimezone(timezone.utc)


def expected_grid(window_start: datetime, window_end: datetime, *, width: timedelta,
                  now: datetime, grace: timedelta, is_open: IsOpen = is_market_open,
                  excluded_dates: frozenset[date] = frozenset()) -> list[datetime]:
    """``[window_start, window_end)`` の基底足格子のうち、実時刻で確定済み・FX 暦で
    開場・無足日の対象外である足の開始時刻。"""
    out: list[datetime] = []
    t = window_start
    while t < window_end:
        if (t + width + grace <= now and t.date() not in excluded_dates and is_open(t)):
            out.append(t)
        t += width
    return out


def open_duration(start: datetime, end: datetime, *, is_open: IsOpen = is_market_open,
                  excluded_dates: frozenset[date] = frozenset()) -> timedelta:
    """``[start, end)`` のうち市場が開いている時間。無足日は除く。

    暦の遷移は常に UTC の正時にあるので、正時で区切った区間ごとに開場を判定する。
    """
    total = timedelta(0)
    cur = start
    while cur < end:
        hour_start = cur.replace(minute=0, second=0, microsecond=0)
        seg_end = min(hour_start + _ONE_HOUR, end)
        if cur.date() not in excluded_dates and is_open(cur):
            total += seg_end - cur
        cur = seg_end
    return total


@dataclass(frozen=True)
class PairHealth:
    symbol: str
    reasons: tuple[str, ...]
    latest_stored: datetime | None
    latest_confirmed: datetime | None
    wall_gap: timedelta | None
    open_gap: timedelta | None
    market_expected: int
    market_present: int
    fill: Fraction | None
    missing_days: tuple[tuple[date, int], ...]
    worst_day: tuple[date, int] | None
    detail: str | None = None


def _unavailable_pair(symbol: str, detail: str) -> PairHealth:
    return PairHealth(symbol, (HISTORY_UNAVAILABLE,), None, None, None, None, 0, 0, None,
                      (), None, detail)


def assess_pair(symbol: str, *, window_start: datetime, window_end: datetime,
                now: datetime, width: timedelta, grace: timedelta,
                present_times: Iterable[datetime], latest_confirmed: datetime | None,
                latest_stored: datetime | None = None, is_open: IsOpen = is_market_open,
                excluded_dates: frozenset[date] = frozenset()) -> PairHealth:
    """1 pair の判定。入力は読取済みの値だけで、DB も時計も触らない。"""
    expected = expected_grid(window_start, window_end, width=width, now=now, grace=grace,
                             is_open=is_open, excluded_dates=excluded_dates)
    if not expected:
        return PairHealth(symbol, (HISTORY_UNAVAILABLE,), latest_stored, latest_confirmed,
                          None, None, 0, 0, None, (), None, "no expected bars in the window")
    expected_set = set(expected)
    present = {t.astimezone(timezone.utc) for t in present_times} & expected_set

    per_day: dict[date, list[int]] = {}
    for t in expected:
        per_day.setdefault(t.date(), [0, 0])[0] += 1
    for t in present:
        per_day[t.date()][1] += 1
    completed = [(day, exp - got) for day, (exp, got) in sorted(per_day.items())
                 if day < now.date()]
    missing_days = tuple((day, miss) for day, miss in completed
                         if miss * width > MAX_DAY_MISSING)
    worst_day = max(completed, key=lambda item: (item[1], -item[0].toordinal()),
                    default=None)

    fill = Fraction(len(present), len(expected))
    reasons: list[str] = []
    wall_gap = open_gap = None
    if latest_confirmed is None:
        reasons += [HISTORY_STALE, HISTORY_GAP]
    else:
        latest_end = latest_confirmed + width
        wall_gap = now - latest_end
        open_gap = open_duration(latest_end, now, is_open=is_open,
                                 excluded_dates=excluded_dates)
        if open_gap > timedelta(hours=STALE_OPEN_HOURS):
            reasons.append(HISTORY_STALE)
        if fill < MIN_FILL_RATIO or missing_days:
            reasons.append(HISTORY_GAP)
    return PairHealth(symbol, tuple(reasons), latest_stored, latest_confirmed, wall_gap,
                      open_gap, len(expected), len(present), fill, missing_days, worst_day)


# ---- 結果 ----------------------------------------------------------------------

@dataclass(frozen=True)
class HistoryHealth:
    verdict: str
    reasons: tuple[str, ...]
    pairs: tuple[PairHealth, ...]
    checked_at: datetime
    dataset: HistoryDataset
    holdout_start: datetime | None
    holdout_end: datetime | None
    no_bars_digest: str
    applied_no_bars: tuple[NoBarsEntry, ...] = ()
    detail: str | None = None

    @property
    def ready(self) -> bool:
        return self.verdict == HISTORY_READY

    def to_operator_evidence(self) -> OperatorEvidence:
        """承認の証跡 (payload JSON) に載せる、人間 (運用者) 専用の形。

        日時・pair・本数・無足日の理由と承認者を含むので、agent の観測面
        (tool 応答・prompt・要約・付属ファイル) へは渡さない。agent 向けの形は
        用意しない (agent へは固定語だけを返す)。
        """
        def iso(value):
            return None if value is None else value.isoformat()
        return OperatorEvidence({
            "version": GUARD_VERSION,
            "verdict": self.verdict,
            "checked_at": iso(self.checked_at),
            "dataset": self.dataset.as_dict(),
            "pairs": [p.symbol for p in self.pairs],
            "holdout_start": iso(self.holdout_start),
            "holdout_end": iso(self.holdout_end),
            "latest_confirmed_bar": {p.symbol: iso(p.latest_confirmed) for p in self.pairs},
            "market_present": {p.symbol: p.market_present for p in self.pairs},
            "market_expected": {p.symbol: p.market_expected for p in self.pairs},
            "missing_days": {p.symbol: [d.isoformat() for d, _m in p.missing_days]
                             for p in self.pairs},
            "worst_day": {p.symbol: (None if p.worst_day is None else
                                     {"date": p.worst_day[0].isoformat(),
                                      "missing": p.worst_day[1]})
                          for p in self.pairs},
            "calendar_version": CALENDAR_VERSION,
            "threshold_snapshot": threshold_snapshot(),
            "no_bars_digest": self.no_bars_digest,
            "no_bars_applied": [e.as_dict() for e in self.applied_no_bars],
        })


def _combine(pairs: Iterable[PairHealth]) -> tuple[str, tuple[str, ...]]:
    found = {reason for p in pairs for reason in p.reasons}
    reasons = tuple(r for r in REASON_ORDER if r in found)
    return (reasons[0] if reasons else HISTORY_READY), reasons


# ---- DB 読取 -------------------------------------------------------------------

def _parse_bar_time(raw: str, width: timedelta) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    if (parsed - _EPOCH) % width:
        return None
    return parsed


def _read_series(conn: sqlite3.Connection, dataset: HistoryDataset, symbol: str,
                 window_start: datetime, window_end: datetime, now: datetime,
                 grace: timedelta) -> tuple[list[datetime], datetime | None, datetime | None]:
    """(窓内の存在する足, 最新確定足, 最新保存足 (診断用))。

    最新確定足は格子整列かつ ``t + 幅 + grace <= now`` を満たす行から選ぶ。
    単純な最大 bar_time は使わない。
    """
    width = dataset.width
    key = (symbol, dataset.base_interval, dataset.source)
    present = []
    for (raw,) in conn.execute(
            "SELECT bar_time FROM ohlcv_history WHERE symbol=? AND interval=? AND source=? "
            "AND bar_time >= ? AND bar_time < ?",
            (*key, window_start.isoformat(), window_end.isoformat())):
        parsed = _parse_bar_time(raw, width)
        if parsed is not None:
            present.append(parsed)
    row = conn.execute(
        "SELECT MAX(bar_time) FROM ohlcv_history WHERE symbol=? AND interval=? AND source=?",
        key).fetchone()
    stored = None
    if row is not None and row[0] is not None:
        try:
            stored = datetime.fromisoformat(row[0]).astimezone(timezone.utc)
        except ValueError:
            stored = None
    latest = None
    cutoff = (now - grace - width).isoformat()
    for (raw,) in conn.execute(
            "SELECT bar_time FROM ohlcv_history WHERE symbol=? AND interval=? AND source=? "
            "AND bar_time <= ? ORDER BY bar_time DESC", (*key, cutoff)):
        parsed = _parse_bar_time(raw, width)
        if parsed is not None and parsed + width + grace <= now:
            latest = parsed
            break
    return present, latest, stored


def check_history(conn: sqlite3.Connection, *, dataset: HistoryDataset,
                  pairs: Iterable[str], now: datetime, holdout_months: int,
                  grace: timedelta, no_bars: NoBarsList = EMPTY_NO_BARS,
                  is_open: IsOpen = is_market_open) -> HistoryHealth:
    """主 pair すべてを ``now`` で検査する。1 pair でも不合格なら不合格。

    読取は 1 つの短い read transaction にまとめる (呼び出し側が transaction を
    持っていればそれを使い、閉じない)。DB・暦・設定の読取失敗は例外にせず
    ``history_check_unavailable`` を返す。``now`` が naive なら ``ValueError``。
    """
    now = _aware_utc(now, "now")
    pair_list = list(dict.fromkeys(pairs))
    is_open = _guarded_calendar(is_open)

    def unavailable(detail: str, start=None, end=None) -> HistoryHealth:
        return HistoryHealth(
            HISTORY_UNAVAILABLE, (HISTORY_UNAVAILABLE,),
            tuple(_unavailable_pair(p, detail) for p in pair_list), now, dataset,
            start, end, no_bars.digest, (), detail)

    try:
        dataset.require_configured()
        if not pair_list:
            return unavailable("no primary pairs")
        window_end = _normalize_now(now, dataset.base_interval)
        window_start = in_sample_until(now, holdout_months, base_interval=dataset.base_interval)
    except Exception as exc:  # 設定・暦の不整合は判定不能 (DatasetError など)
        return unavailable(type(exc).__name__)

    series: dict[str, tuple] = {}
    own = False
    try:
        own = not conn.in_transaction
        if own:
            conn.execute("BEGIN")
        for pair in pair_list:
            series[pair] = _read_series(conn, dataset, pair, window_start, window_end,
                                        now, grace)
    except sqlite3.Error as exc:
        return unavailable(type(exc).__name__, window_start, window_end)
    finally:
        if own:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except sqlite3.Error:
                pass

    results: list[PairHealth] = []
    applied: list[NoBarsEntry] = []
    for pair in pair_list:
        present, latest, stored = series[pair]
        excluded = no_bars.dates_for(dataset.source, dataset.base_interval, pair)
        try:
            results.append(assess_pair(
                pair, window_start=window_start, window_end=window_end, now=now,
                width=dataset.width, grace=grace, present_times=present,
                latest_confirmed=latest, latest_stored=stored, is_open=is_open,
                excluded_dates=excluded))
        except (CalendarUnavailable,) + _FILE_ERRORS as exc:
            results.append(_unavailable_pair(pair, type(exc).__name__))
        applied += [e for e in no_bars.entries
                    if (e.source, e.base_interval, e.symbol) ==
                    (dataset.source, dataset.base_interval, pair)
                    and window_start.date() <= e.date_utc <= now.date()]
    verdict, reasons = _combine(results)
    return HistoryHealth(verdict, reasons, tuple(results), now, dataset, window_start,
                         window_end, no_bars.digest, tuple(applied))


def evaluate_history(conn: sqlite3.Connection, *, dataset: HistoryDataset,
                     pairs: Iterable[str], now: datetime, holdout_months: int,
                     grace: timedelta, no_bars_path: Path,
                     is_open: IsOpen = is_market_open) -> HistoryHealth:
    """無足日一覧を ``no_bars_path`` から読んで ``check_history`` を呼ぶ。
    一覧が不正・読取不能なら ``history_check_unavailable`` (空一覧にしない)。"""
    now = _aware_utc(now, "now")
    try:
        no_bars = load_no_bars(no_bars_path)
    except NoBarsConfigError as exc:
        pair_list = list(dict.fromkeys(pairs))
        return HistoryHealth(
            HISTORY_UNAVAILABLE, (HISTORY_UNAVAILABLE,),
            tuple(_unavailable_pair(p, "no-bars list") for p in pair_list), now, dataset,
            None, None, "", (), f"NoBarsConfigError: {exc}")
    return check_history(conn, dataset=dataset, pairs=pairs, now=now,
                         holdout_months=holdout_months, grace=grace, no_bars=no_bars,
                         is_open=is_open)
