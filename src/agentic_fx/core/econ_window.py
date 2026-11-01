"""指標回避帯の純粋判定。DB も時計も読まない。

event `ts` の帯は inclusive な `[ts - before, ts + after]`。対象は重要度が
`importance_min` 以上で、country が対象通貨か `All` の event。無関係な通貨・
重要度未満・superseded の event は、どの関数に渡されても帯を作らない
(呼出し側の query が絞っていても、ここで必ず再確認する)。

同じ判定を signal runner 前・OPEN commit・limit fill の 3 か所が使う。
close / cancel / SL / TP には使わない。

対象通貨は instrument 定義の base / quote (`InstrumentSpec`) から導く。
symbol 文字列の切り出しはしない。数値の相関・成績はここに持たない。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Sequence

from agentic_fx.core.contracts import InstrumentSpec

ALL_COUNTRY = "All"
NEXT_EVENT_HORIZON = timedelta(days=7)

FRESHNESS_UNKNOWN = "unknown"
FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_VALUES = (FRESHNESS_UNKNOWN, FRESHNESS_FRESH, FRESHNESS_STALE)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class WindowSettings:
    """`econ.window` の 3 値。範囲は設定の schema と同じ。"""
    importance_min: int = 3
    before_min: int = 30
    after_min: int = 15

    def __post_init__(self) -> None:
        if not _is_int(self.importance_min) or not 1 <= self.importance_min <= 3:
            raise ValueError("importance_min must be an integer in 1..3")
        for name in ("before_min", "after_min"):
            value = getattr(self, name)
            if not _is_int(value) or value < 0:
                raise ValueError(f"{name} must be an integer >= 0")

    @property
    def before(self) -> timedelta:
        return timedelta(minutes=self.before_min)

    @property
    def after(self) -> timedelta:
        return timedelta(minutes=self.after_min)

    @classmethod
    def from_config(cls, cfg) -> "WindowSettings":
        """`config.EconWindowSettings` から作る。"""
        return cls(importance_min=cfg.importance_min,
                   before_min=cfg.before_min, after_min=cfg.after_min)


def pair_currencies(spec: InstrumentSpec) -> tuple[str, str]:
    """pair の対象通貨 `(base, quote)`。instrument 定義の明示値だけを使う。"""
    return spec.base_currency, spec.quote_currency


def currencies_by_pair(pairs: Iterable[str],
                       spec_of: Callable[[str], InstrumentSpec],
                       ) -> dict[str, tuple[str, str]]:
    """settings の並びのまま `pair -> (base, quote)`。未知の pair は `spec_of` が上げる。"""
    return {pair: pair_currencies(spec_of(pair)) for pair in pairs}


def _utc(value: datetime, what: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{what}: naive datetime is not accepted")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class EconEvent:
    """判定に使う event。`ts` は tz-aware UTC に正規化する。"""
    id: int
    ts: datetime
    country: str
    name: str
    importance: int
    superseded_by: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", _utc(self.ts, "event ts"))

    @classmethod
    def from_row(cls, row: Mapping) -> "EconEvent":
        ts = datetime.fromisoformat(row["ts"])
        return cls(id=row["id"], ts=ts, country=row["country"],
                   name=row["name"], importance=row["importance"],
                   superseded_by=row["superseded_by"])


@dataclass(frozen=True, slots=True)
class WindowRule:
    """ある pair に適用する帯の規則: 設定と対象通貨。"""
    settings: WindowSettings
    currencies: frozenset[str]

    @classmethod
    def for_currencies(cls, settings: WindowSettings,
                       currencies: Iterable[str]) -> "WindowRule":
        return cls(settings, frozenset(currencies))


def is_target(event: EconEvent, rule: WindowRule) -> bool:
    """この規則の対象 event か。`All` は常に対象通貨に含まれる。"""
    return (event.superseded_by is None
            and event.importance >= rule.settings.importance_min
            and (event.country == ALL_COUNTRY
                 or event.country in rule.currencies))


def window_of(event: EconEvent, settings: WindowSettings,
              ) -> tuple[datetime, datetime]:
    """event の帯 `[ts - before, ts + after]` (両端を含む)。"""
    return event.ts - settings.before, event.ts + settings.after


def _targets(events: Iterable[EconEvent], rule: WindowRule) -> list[EconEvent]:
    return sorted((e for e in events if is_target(e, rule)),
                  key=lambda e: (e.ts, e.id))


def active_events(now: datetime, events: Iterable[EconEvent],
                  rule: WindowRule) -> tuple[EconEvent, ...]:
    """`now` が帯に入っている対象 event。ts 昇順。"""
    now = _utc(now, "now")
    out = []
    for e in _targets(events, rule):
        start, end = window_of(e, rule.settings)
        if start <= now <= end:
            out.append(e)
    return tuple(out)


def in_window(now: datetime, events: Iterable[EconEvent],
              rule: WindowRule) -> bool:
    return bool(active_events(now, events, rule))


def overlapping_events(bar_open: datetime, bar_close: datetime,
                       events: Iterable[EconEvent], rule: WindowRule,
                       ) -> tuple[EconEvent, ...]:
    """足の閉区間 `[bar_open, bar_close]` と帯が交差する対象 event。

    `now` を使わないので、遅れて処理した足でも同じ結果になる。
    """
    bar_open, bar_close = _utc(bar_open, "bar_open"), _utc(bar_close, "bar_close")
    if bar_close < bar_open:
        raise ValueError("bar_close must not precede bar_open")
    out = []
    for e in _targets(events, rule):
        start, end = window_of(e, rule.settings)
        if start <= bar_close and end >= bar_open:
            out.append(e)
    return tuple(out)


def bars_overlap(bar_open: datetime, bar_close: datetime,
                 events: Iterable[EconEvent], rule: WindowRule) -> bool:
    return bool(overlapping_events(bar_open, bar_close, events, rule))


@dataclass(frozen=True, slots=True)
class Component:
    """重なる帯 (端が触れるものを含む) の推移的な和集合 1 つ。

    `id` は成分に含まれる event id の最小値。`start` / `end` は和集合の両端。
    """
    id: int
    events: tuple[EconEvent, ...]
    start: datetime
    end: datetime


def components(events: Iterable[EconEvent], rule: WindowRule,
               ) -> tuple[Component, ...]:
    """対象 event の帯を連結成分にまとめる。開始の早い順。

    渡された event だけを見る。query が候補を絞っていれば、その外にある
    event が成分をさらに延ばしていることがある。
    """
    spans = sorted(((*window_of(e, rule.settings), e)
                    for e in _targets(events, rule)),
                   key=lambda t: (t[0], t[2].id))
    groups: list[list] = []
    for start, end, event in spans:
        if groups and start <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], end)
            groups[-1][2].append(event)
        else:
            groups.append([start, end, [event]])
    return tuple(
        Component(id=min(e.id for e in members), events=tuple(members),
                  start=start, end=end)
        for start, end, members in groups)


def active_components(now: datetime, events: Iterable[EconEvent],
                      rule: WindowRule) -> tuple[Component, ...]:
    now = _utc(now, "now")
    return tuple(c for c in components(events, rule) if c.start <= now <= c.end)


def bar_components(bar_open: datetime, bar_close: datetime,
                   events: Iterable[EconEvent], rule: WindowRule,
                   ) -> tuple[Component, ...]:
    bar_open, bar_close = _utc(bar_open, "bar_open"), _utc(bar_close, "bar_close")
    if bar_close < bar_open:
        raise ValueError("bar_close must not precede bar_open")
    return tuple(c for c in components(events, rule)
                 if c.start <= bar_close and c.end >= bar_open)


def next_event(now: datetime, events: Iterable[EconEvent], rule: WindowRule,
               horizon: timedelta = NEXT_EVENT_HORIZON) -> EconEvent | None:
    """`now` 以後 `now + horizon` までで最初の対象 event。両端を含み、1µs 超は除く。"""
    now = _utc(now, "now")
    limit = now + horizon
    for e in _targets(events, rule):
        if now <= e.ts <= limit:
            return e
    return None


@dataclass(frozen=True, slots=True)
class EventView:
    """agent に見せる event: country / name / importance / ts だけ。"""
    country: str
    name: str
    importance: int
    ts: datetime

    @classmethod
    def of(cls, event: EconEvent) -> "EventView":
        return cls(event.country, event.name, event.importance, event.ts)

    def to_dict(self) -> dict:
        return {"country": self.country, "name": self.name,
                "importance": self.importance, "ts": self.ts.isoformat()}


@dataclass(frozen=True, slots=True)
class WindowView:
    """agent 表現: pair・対象通貨・in_window・active events・次 event・freshness。"""
    pair: str
    currencies: tuple[str, ...]
    in_window: bool
    active: tuple[EventView, ...]
    next_event: EventView | None
    freshness: str

    def to_dict(self) -> dict:
        return {"pair": self.pair, "currencies": list(self.currencies),
                "in_window": self.in_window,
                "active_events": [e.to_dict() for e in self.active],
                "next_event": (self.next_event.to_dict()
                               if self.next_event is not None else None),
                "freshness": self.freshness}


def build_view(pair: str, now: datetime, rule: WindowRule, *,
               candidates: Sequence[EconEvent],
               upcoming: Sequence[EconEvent],
               freshness: str) -> WindowView:
    """候補 (帯の判定用) と先の event (次 event 用) から agent 表現を作る。"""
    if freshness not in FRESHNESS_VALUES:
        raise ValueError(f"unknown freshness: {freshness!r}")
    active = active_events(now, candidates, rule)
    nxt = next_event(now, upcoming, rule)
    return WindowView(
        pair=pair, currencies=tuple(sorted(rule.currencies)),
        in_window=bool(active),
        active=tuple(EventView.of(e) for e in active),
        next_event=EventView.of(nxt) if nxt is not None else None,
        freshness=freshness)
