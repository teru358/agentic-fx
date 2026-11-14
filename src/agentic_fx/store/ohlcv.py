from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agentic_fx.core.contracts import Bar
from agentic_fx.core.history_epoch import history_epoch
from agentic_fx.store import db, history_conflicts
from agentic_fx.store.db import FLOAT_TOL

_log = logging.getLogger("agentic_fx.store.ohlcv")

_FLOAT_TOL = FLOAT_TOL

# ライブチェーンの永続化名のみ。"mt5" ではなく "mt5-live"
# (datafeed/price_provider.py の _STORAGE_SOURCE 参照)。
LIVE_SOURCES = frozenset({"yfinance", "twelvedata", "mt5-live"})

# 一括インポータの source 名のみ (backtest/importer.py / backtest/mt5_import.py)。
IMPORT_SOURCES = frozenset({"dukascopy", "mt5"})

# プラン 9 Task 16: KNOWN_OHLCV_SOURCES は両集合の union として定義する
# (以前は独立したリテラルだったが、LIVE_SOURCES/IMPORT_SOURCES を単一の
# 真実の源にする — service.py:_validate_startup の producer_source typo
# 検出が参照する)。
KNOWN_OHLCV_SOURCES = LIVE_SOURCES | IMPORT_SOURCES


@dataclass(frozen=True)
class RetentionPlan:
    """Per live-source and native-interval cache cutoffs."""
    cutoffs: dict[tuple[str, str], datetime]

    def __post_init__(self) -> None:
        for cutoff in self.cutoffs.values():
            _require_aware_utc(cutoff, "retention cutoff")

    @classmethod
    def from_registry(cls, registry, *, sources=LIVE_SOURCES, now: datetime | None = None,
                      planner=None, margin_days: int = 3) -> "RetentionPlan":
        from agentic_fx.datafeed.planner import RequirementPlanner

        now = _require_aware_utc(now or datetime.now(timezone.utc), "now")
        planner = planner or RequirementPlanner()
        cutoffs: dict[tuple[str, str], datetime] = {}
        for source in sources:
            for interval in registry.required_intervals:
                if interval == "1m":
                    days = 2
                else:
                    required = max((registry.required_closed_bars(symbol, interval)
                                    for symbol in {symbol for entry in registry.entries
                                                   for symbol in entry.symbols}), default=1)
                    result = planner.plan(source, interval, required)
                    days = (result.days if result.ok else 0) + margin_days
                cutoffs[(source, interval)] = now - timedelta(days=days)
        return cls(cutoffs)


def build_retention_plan(retention: dict | None, *, legacy_days: int | None,
                         required_intervals, now: datetime | None = None,
                         margin_days: int = 3) -> RetentionPlan:
    if retention is not None and legacy_days is not None:
        raise ValueError("datafeed.retention and cache_retention_days cannot coexist")
    now = _require_aware_utc(now or datetime.now(timezone.utc), "now")
    cutoffs: dict[tuple[str, str], datetime] = {}
    if retention is None:
        if legacy_days is None:
            legacy_days = 30
        for source in LIVE_SOURCES:
            for interval in required_intervals:
                cutoffs[(source, interval)] = now - timedelta(days=legacy_days)
        return RetentionPlan(cutoffs)
    for source, intervals in retention.items():
        source = "mt5-live" if source == "mt5" else source
        for interval, days in intervals.items():
            if not isinstance(days, str) or not days.endswith("d") or not days[:-1].isdigit():
                raise ValueError("retention values must be whole-day strings such as '2d'")
            cutoffs[(source, interval)] = now - timedelta(days=int(days[:-1]))
    return RetentionPlan(cutoffs)


def retention_shortfalls(registry, plan: RetentionPlan, *, now: datetime,
                         planner=None) -> tuple[tuple[object, str], ...]:
    """Identify entries whose configured cache lifetime cannot cover their window."""
    from agentic_fx.datafeed.planner import RequirementPlanner, startup_disposition

    now = _require_aware_utc(now, "now")
    planner = planner or RequirementPlanner()
    failures = []
    for entry in registry.entries:
        native = "1h" if entry.interval in {"4h", "1d", "1wk"} else entry.interval
        for source, interval in plan.cutoffs:
            if interval != native:
                continue
            required = planner.plan(source, native, entry.required_closed_bars,
                                    consumer=entry.consumer_id)
            retained_days = (now - plan.cutoffs[(source, interval)]).total_seconds() / 86400
            if not required.ok or retained_days < required.days:
                failures.append((entry, startup_disposition(entry, required)))
    return tuple(failures)


def _iso_utc(ts: datetime) -> str:
    """aware datetime を UTC へ正規化してから isoformat する。naive は
    ValueError (fail closed)。"""
    if ts.tzinfo is None:
        raise ValueError(
            "_iso_utc: naive datetime is rejected (tz-aware UTC required)")
    return ts.astimezone(timezone.utc).isoformat()


def _require_aware_utc(dt: datetime, label: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{label} is naive; tz-aware UTC datetime required "
                          "(cannot safely assume UTC)")
    return dt.astimezone(timezone.utc)


# ---- キャッシュ (ohlcv_cache) — ライブ逐次専用 ------------------------------


def upsert_cache_bars(conn: sqlite3.Connection, bars: list[Bar], *,
                      source: str) -> int:
    """live キャッシュ用。形成中バーの更新があるため常に上書きする。

    `source` は `LIVE_SOURCES` のいずれかでなければならない — 一括インポート
    由来の source (例: "dukascopy") をここから書くと `ohlcv_history` の
    「既存行不変」契約を迂回できてしまうテーブル越境が生じるため
    (テーブルが分かれているので実際には書き込み自体が別テーブルへ向かうが、
    source の取り違えという設定ミスを早期に検出する)。
    """
    if source not in LIVE_SOURCES:
        raise ValueError(
            f"upsert_cache_bars: source={source!r} は LIVE_SOURCES "
            f"{sorted(LIVE_SOURCES)} に含まれません "
            "(一括インポート由来の source は import_history_bars を使うこと)")
    conn.executemany(
        "INSERT INTO ohlcv_cache (symbol, interval, bar_time, open, high, "
        "low, close, volume, source) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, interval, bar_time, source) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        [(b.symbol, b.interval, _iso_utc(b.ts), b.open, b.high, b.low,
          b.close, b.volume, source) for b in bars])
    conn.commit()
    return len(bars)


def load_cache_bars(conn: sqlite3.Connection, symbol: str, interval: str, *,
                    source: str, since: datetime | None = None,
                    until: datetime | None = None) -> list[Bar]:
    if source not in LIVE_SOURCES:
        raise ValueError(
            f"load_cache_bars: source={source!r} は LIVE_SOURCES "
            f"{sorted(LIVE_SOURCES)} に含まれません")
    q = "SELECT * FROM ohlcv_cache WHERE symbol=? AND interval=? AND source=?"
    args: list = [symbol, interval, source]
    if since is not None:
        since_utc = _require_aware_utc(since, "since")
        q += " AND bar_time >= ?"
        args.append(since_utc.isoformat())
    if until is not None:
        until_utc = _require_aware_utc(until, "until")
        q += " AND bar_time <= ?"
        args.append(until_utc.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()
    return [Bar(r["symbol"], r["interval"],
                datetime.fromisoformat(r["bar_time"]),
                r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows]


def latest_closed_cache_bar_time(conn: sqlite3.Connection, symbol: str,
                                 interval: str, *, now: datetime,
                                 width: timedelta, grace: timedelta,
                                 source: str) -> datetime | None:
    """Return the newest committed cache bar which is closed at ``now``.

    ``source`` は必須。``symbol``/``interval``/``bar_time`` だけで絞ると、
    MT5 primary が不通でも別 source (readonly healthcheck 経由の yfinance
    等) の新しい行が残っていれば誤って健全と判定し得る。呼び出し側は必ず
    primary から導出した storage source (``"mt5-live"`` 等) を渡すこと。
    """
    cutoff = _require_aware_utc(now, "now") - width - grace
    row = conn.execute(
        "SELECT MAX(bar_time) AS bar_time FROM ohlcv_cache "
        "WHERE symbol=? AND interval=? AND source=? AND bar_time <= ?",
        (symbol, interval, source, cutoff.isoformat())).fetchone()
    if row is None or row["bar_time"] is None:
        return None
    return datetime.fromisoformat(row["bar_time"]).astimezone(timezone.utc)


def prune_cache(conn: sqlite3.Connection, *, cutoff: datetime | None = None,
                limit: int | None = None, plan: RetentionPlan | None = None) -> int:
    """`ohlcv_cache` の保持ポリシー。`bar_time < cutoff` の行を最大 `limit`
    件だけ削除する (有界バッチ — core_lock 保持窓を無制限に伸ばさないため)。

    `DELETE FROM ohlcv_cache` のみで完結する — source 絞りも「未知 source
    は消さない」fail-safe 分岐も**不要** (`ohlcv_history` はテーブルが
    違うため、この DELETE は原理的に到達できない)。呼び出し側 (maintenance
    hook) が毎 tick 呼ぶことで、有界バッチのまま日次増分に追いつく
    (設計書 D2「追いつくまで毎 maintenance」)。

    削除件数を返す (呼び出し側は使わなくてよいが、テスト・ログで有用)。
    """
    if plan is not None:
        if cutoff is not None or limit is not None:
            raise ValueError("plan cannot be combined with cutoff or limit")
        deleted = 0
        for (source, interval), plan_cutoff in plan.cutoffs.items():
            cur = conn.execute("DELETE FROM ohlcv_cache WHERE source=? AND interval=? AND bar_time < ?",
                               (source, interval, _require_aware_utc(plan_cutoff, "cutoff").isoformat()))
            deleted += cur.rowcount
        conn.commit()
        return deleted
    if cutoff is None or limit is None:
        raise ValueError("cutoff and limit are required without a retention plan")
    cutoff_utc = _require_aware_utc(cutoff, "cutoff")
    if limit < 1:
        raise ValueError(f"prune_cache: limit must be >= 1, got {limit}")
    cur = conn.execute(
        "DELETE FROM ohlcv_cache WHERE rowid IN ("
        "SELECT rowid FROM ohlcv_cache WHERE bar_time < ? LIMIT ?)",
        (cutoff_utc.isoformat(), limit))
    conn.commit()
    return cur.rowcount


# ---- 履歴 (ohlcv_history) — バックテスト・一括インポート専用 ----------------


@dataclass(frozen=True)
class ImportResult:
    inserted: int
    unchanged: int
    conflicted: int
    conflicts: tuple = ()


def _close_enough(a: float | None, b: float | None) -> bool:
    # 許容差の判定は `db.values_match` が唯一の定義。ここに独自の複製を持たない。
    return db.values_match(a, b)


def _validate_and_normalize_row(idx: int, row: tuple) -> tuple:
    """投入前検証 (import_history_bars 用)。

    - 有限値であること (NaN/inf を弾く)
    - `low <= min(open, close) <= max(open, close) <= high`
    - `bar_time` は aware datetime として parse できること (UTC へ正規化
      してから isoformat した文字列に置き換えて返す)

    不正行はここで ValueError を送出する。呼び出し側は**全行をここで
    検証してから**書き込みを始める。
    """
    if len(row) != 9:
        raise ValueError(
            f"import_history_bars: rows[{idx}] has {len(row)} fields, "
            "expected 9 (symbol, interval, bar_time_iso, o, h, l, c, "
            "volume, spread)")
    symbol, interval, bar_time_iso, o, h, l, c, volume, spread = row  # noqa: E741
    if not symbol or not interval:
        raise ValueError(
            f"import_history_bars: rows[{idx}] has empty symbol/interval")
    try:
        dt = datetime.fromisoformat(bar_time_iso)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"import_history_bars: rows[{idx}] bar_time not parseable: "
            f"{bar_time_iso!r}") from e
    if dt.tzinfo is None:
        raise ValueError(
            f"import_history_bars: rows[{idx}] bar_time is naive (aware "
            f"datetime required): {bar_time_iso!r}")
    normalized_bar_time = dt.astimezone(timezone.utc).isoformat()
    for name, val in (("open", o), ("high", h), ("low", l), ("close", c),
                      ("volume", volume)):
        if not isinstance(val, (int, float)) or isinstance(val, bool) \
                or not math.isfinite(val):
            raise ValueError(
                f"import_history_bars: rows[{idx}] {name} is not a finite "
                f"number: {val!r}")
    if spread is not None:
        if not isinstance(spread, (int, float)) or isinstance(spread, bool) \
                or not math.isfinite(spread):
            raise ValueError(
                f"import_history_bars: rows[{idx}] spread is not a finite "
                f"number: {spread!r}")
    lo_bound, hi_bound = min(o, c), max(o, c)
    if not (l <= lo_bound <= hi_bound <= h):
        raise ValueError(
            f"import_history_bars: rows[{idx}] violates low<=min(open,close)"
            f"<=max(open,close)<=high: open={o} high={h} low={l} close={c}")
    return (symbol, interval, normalized_bar_time, o, h, l, c, volume, spread)


def import_history_bars(conn: sqlite3.Connection, rows: list[tuple], *,
                        source: str, commit: bool = True) -> ImportResult:
    """衝突を記録しない低水準の投入。履歴取込の経路ではない。

    取込 (MT5 / Dukascopy の import) は `history_import.run_series` を通り、衝突の記録・
    事前遮断・batch 確定は `classify_history_rows` / `apply_classified_history` が担う。
    この関数は既存行を変えず (同値は unchanged、異値は入力を捨てて conflicted と数える)、
    異値の記録も遮断もしない。fixture の下地など、記録が要らない投入だけに使う。

    rows: (symbol, interval, bar_time_iso, o, h, l, c, volume, spread|None)

    `source` は `IMPORT_SOURCES` のいずれかでなければならない — ライブ
    source 名 (例: "yfinance") をここから書くと `ohlcv_history` の
    「削除しない」保証をライブキャッシュの上書きセマンティクスで穢すため。

    原子性: 書き込み前に全行を検証・正規化し、書き込みは `SAVEPOINT` で囲んで、
    例外時は `ROLLBACK TO SAVEPOINT` してから再送出する。

    `commit=False` は呼び出し側の transaction の中で書くときの指定で、commit も
    rollback もしない。transaction の外で指定すると最外の SAVEPOINT の RELEASE が
    commit になるので拒否する。
    """
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"import_history_bars: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません "
            "(ライブ由来の source は upsert_cache_bars を使うこと)")
    if not commit and not conn.in_transaction:
        raise ValueError(
            "import_history_bars: commit=False requires an open transaction")
    normalized_rows = [_validate_and_normalize_row(i, row)
                       for i, row in enumerate(rows)]

    inserted = 0
    unchanged = 0
    conflicted = 0
    conflicts = []
    conn.execute("SAVEPOINT import_history_bars")
    try:
        # 行単位ループは意図的 — inserted/unchanged/conflicted の三分計上は
        # 集合演算 (executemany + 一括 diff) では再現できない。fixture 用の小さな投入が前提。
        for (symbol, interval, bar_time_iso, o, h, l, c, volume,  # noqa: E741
             spread) in normalized_rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, "
                "bar_time, open, high, low, close, volume, source, spread) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol, interval, bar_time_iso, o, h, l, c, volume, source,
                 spread))
            if cur.rowcount == 1:
                inserted += 1
                continue
            existing = conn.execute(
                "SELECT open, high, low, close, volume, spread FROM "
                "ohlcv_history WHERE symbol=? AND interval=? AND bar_time=? "
                "AND source=?",
                (symbol, interval, bar_time_iso, source)).fetchone()
            same = (
                _close_enough(existing["open"], o)
                and _close_enough(existing["high"], h)
                and _close_enough(existing["low"], l)
                and _close_enough(existing["close"], c)
                and _close_enough(existing["volume"], volume)
                and _close_enough(existing["spread"], spread)
            )
            if same:
                unchanged += 1
            else:
                conflicted += 1
                existing_values = tuple(existing[name] for name in
                                        ("open", "high", "low", "close",
                                         "volume", "spread"))
                incoming_values = (o, h, l, c, volume, spread)
                conflicts.append((symbol, interval, bar_time_iso,
                                  existing_values, incoming_values))
        conn.execute("RELEASE import_history_bars")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT import_history_bars")
        conn.execute("RELEASE import_history_bars")
        raise
    if commit:
        conn.commit()
    if conflicted > 0:
        _log.warning(
            "import_history_bars: %d row(s) conflicted with existing data "
            "for source=%s (existing rows left unchanged)",
            conflicted, source)
    return ImportResult(inserted, unchanged, conflicted, tuple(conflicts))


# ---- 履歴の取込 core: 分類 → 事前遮断 → batch 確定 --------------------------

CIRCUIT_MAX_NEW_CONFLICTS = 20
CIRCUIT_MIN_NEW_FOR_RATIO = 2
BATCH_MAX_ROWS = 5000
BATCH_BUDGET_SECONDS = 0.05
ORIGINS = ("import", "sync", "live_compare")

_VALUE_COLUMNS = ("open", "high", "low", "close", "volume", "spread")


class InconsistentInputError(ValueError):
    """The same bar key arrived with different values inside one input."""


def conflict_circuit_open(new_conflicts: int, n_test: int) -> bool:
    """新規の異値だけで数える事前遮断。1% ちょうどは通し、新規 1 件の孤立は割合に掛けない。"""
    return (new_conflicts > CIRCUIT_MAX_NEW_CONFLICTS
            or (new_conflicts >= CIRCUIT_MIN_NEW_FOR_RATIO
                and 100 * new_conflicts > n_test))


def inspection_tail_bars(bar_seconds: int) -> int:
    """直前 2 時間相当の確定格子数 K = ceil(2h / 足幅)。"""
    if bar_seconds < 1:
        raise ValueError("bar_seconds must be >= 1")
    return -(-7200 // bar_seconds)


def inspection_sufficient(n_overlap: int, n_test: int, tail_bars: int) -> bool:
    return n_overlap >= tail_bars and n_test >= 2


@dataclass(frozen=True)
class ClassifiedRow:
    kind: str                       # "new" | "unchanged" | "conflict"
    symbol: str
    interval: str
    bar_time: str
    incoming: tuple
    existing: tuple | None = None
    known: bool = False


@dataclass(frozen=True)
class HistoryClassification:
    source: str
    epoch: str
    entries: tuple[ClassifiedRow, ...]

    def _count(self, kind: str, known: bool | None = None) -> int:
        return sum(1 for e in self.entries if e.kind == kind
                   and (known is None or e.known is known))

    @property
    def new_rows(self) -> int:
        return self._count("new")

    @property
    def unchanged(self) -> int:
        return self._count("unchanged")

    @property
    def conflicts_new(self) -> int:
        return self._count("conflict", False)

    @property
    def conflicts_known(self) -> int:
        return self._count("conflict", True)

    @property
    def n_test(self) -> int:
        return self.unchanged + self.conflicts_new

    @property
    def n_overlap(self) -> int:
        return self.unchanged + self.conflicts_new + self.conflicts_known

    @property
    def circuit_open(self) -> bool:
        return conflict_circuit_open(self.conflicts_new, self.n_test)

    def top_new_conflicts(self, limit: int = 5) -> tuple[ClassifiedRow, ...]:
        return tuple([e for e in self.entries
                      if e.kind == "conflict" and not e.known][:limit])


@dataclass(frozen=True)
class BatchProgress:
    batch_bar_time_end: str
    resume_from: str | None
    rows_in_batch: int
    committed_entries: int


@dataclass(frozen=True)
class ApplyResult:
    status: str                     # "ok" | "partial" | "failed"
    inserted: int
    unchanged: int
    conflicted_new: int
    conflicted_known: int
    last_committed_bar_time: str | None
    resume_from: str | None
    failure: str | None = None
    error_type: str | None = None

    @property
    def conflicted(self) -> int:
        return self.conflicted_new + self.conflicted_known


def classify_history_rows(conn: sqlite3.Connection, rows: list[tuple], *,
                          source: str) -> HistoryClassification:
    """書込みなしで入力を new / unchanged / conflict に分ける。

    入力内の同一 key は 1 つに正規化し、値が異なれば InconsistentInputError。
    衝突は現在の epoch と digest 完全一致で open / kept の既知記録があれば known。
    """
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"classify_history_rows: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません")
    normalized = [_validate_and_normalize_row(i, row) for i, row in enumerate(rows)]
    if len({(row[0], row[1]) for row in normalized}) > 1:
        raise InconsistentInputError(
            "classify_history_rows: input mixes more than one (symbol, interval) series")
    seen: dict[tuple, tuple] = {}
    for symbol, interval, bar_time, o, h, l, c, volume, spread in normalized:  # noqa: E741
        key = (symbol, interval, bar_time)
        values = (o, h, l, c, volume, spread)
        prior = seen.get(key)
        if prior is None:
            seen[key] = values
        elif not history_conflicts.values_equal(prior, values):
            raise InconsistentInputError(
                f"classify_history_rows: input has differing values for one bar key "
                f"({symbol} {interval} {bar_time})")
    epoch = history_epoch(conn)
    entries = []
    for (symbol, interval, bar_time), incoming in sorted(
            seen.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
        row = conn.execute(
            "SELECT open, high, low, close, volume, spread FROM ohlcv_history "
            "WHERE symbol=? AND interval=? AND bar_time=? AND source=?",
            (symbol, interval, bar_time, source)).fetchone()
        if row is None:
            entries.append(ClassifiedRow("new", symbol, interval, bar_time, incoming))
            continue
        existing = tuple(row[name] for name in _VALUE_COLUMNS)
        if history_conflicts.values_equal(existing, incoming):
            entries.append(ClassifiedRow("unchanged", symbol, interval, bar_time,
                                         incoming, existing))
            continue
        known = history_conflicts.is_known(
            conn, source=source, interval=interval, symbol=symbol,
            bar_time=bar_time, history_epoch=epoch, existing=existing,
            incoming=incoming)
        entries.append(ClassifiedRow("conflict", symbol, interval, bar_time,
                                     incoming, existing, known))
    return HistoryClassification(source, epoch, tuple(entries))


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, _EpochChanged):
        return "epoch_changed"
    if isinstance(exc, _ConcurrentChange):
        return "concurrent_change"
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return "database_busy"
    return "write_failed"


class _EpochChanged(RuntimeError):
    pass


class _ConcurrentChange(RuntimeError):
    pass


class _StopBeforeCommit(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def apply_classified_history(conn: sqlite3.Connection,
                             classification: HistoryClassification, *,
                             origin: str, seen_at: datetime | str,
                             run_id: str | None = None,
                             max_rows: int = BATCH_MAX_ROWS,
                             budget_seconds: float = BATCH_BUDGET_SECONDS,
                             clock=time.monotonic,
                             on_batch=None, admission=None,
                             before_batch=None, before_commit=None) -> ApplyResult:
    """分類済みの系列を短い BEGIN IMMEDIATE batch に分けて確定する。

    各 batch で履歴 INSERT と衝突 upsert を同じ transaction で確定する。
    `on_batch(conn, BatchProgress)` は commit 直前の同じ transaction で呼ばれ、
    進捗の永続化に使う。失敗した batch は全て rollback し、確定済みの batch は残す。
    遮断条件に当たる分類は書込み 0 件のまま拒否する。

    `admission` (WriterAdmission) があれば batch ごとに BEGIN の前から commit / rollback の
    後まで lease を取る。通らなければ database_busy で止める。`before_batch()` は各 batch の
    前に呼ばれ、失敗コードの文字列を返すとそこで新しい batch を始めずに止める。
    `before_commit()` は各 batch の commit 直前に呼ばれ、失敗コードを返すとその batch を
    rollback して止める。
    unchanged 行も適用時に既存値を再照合し、変わっていれば concurrent_change で batch を戻す。
    """
    if origin not in ORIGINS:
        raise ValueError(f"apply_classified_history: origin={origin!r} is not in {ORIGINS}")
    if classification.circuit_open:
        raise ValueError("apply_classified_history: circuit is open; nothing may be written")
    if max_rows < 1:
        raise ValueError("max_rows must be >= 1")
    if conn.in_transaction:
        raise ValueError("apply_classified_history: batches need their own transaction")
    stamp = _iso_utc(seen_at) if isinstance(seen_at, datetime) else seen_at
    entries = classification.entries
    total = len(entries)
    inserted = unchanged = new_c = known_c = 0
    done = 0
    failure = error_type = None
    while done < total:
        if before_batch is not None:
            stop = before_batch()
            if stop is not None:
                failure = stop
                break
        started = clock()
        batch_end = done
        b_ins = b_unc = b_new = b_known = 0
        admitted = False
        try:
            if admission is not None:
                admitted = admission.admit(lambda: conn.execute("BEGIN IMMEDIATE"))
                if not admitted:
                    failure = "database_busy"
                    break
            else:
                conn.execute("BEGIN IMMEDIATE")
            if history_epoch(conn) != classification.epoch:
                raise _EpochChanged()
            while batch_end < total:
                entry = entries[batch_end]
                if entry.kind == "new":
                    values = entry.incoming
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, bar_time, "
                        "open, high, low, close, volume, source, spread) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (entry.symbol, entry.interval, entry.bar_time, *values[:5],
                         classification.source, values[5]))
                    if cur.rowcount != 1:
                        raise _ConcurrentChange()
                    b_ins += 1
                else:
                    current = conn.execute(
                        "SELECT open, high, low, close, volume, spread FROM ohlcv_history "
                        "WHERE symbol=? AND interval=? AND bar_time=? AND source=?",
                        (entry.symbol, entry.interval, entry.bar_time,
                         classification.source)).fetchone()
                    if current is None or tuple(current[n] for n in _VALUE_COLUMNS) \
                            != entry.existing:
                        raise _ConcurrentChange()
                if entry.kind == "unchanged":
                    b_unc += 1
                elif entry.kind == "conflict":
                    history_conflicts.upsert(
                        conn, source=classification.source, interval=entry.interval,
                        symbol=entry.symbol, bar_time=entry.bar_time,
                        history_epoch=classification.epoch, existing=entry.existing,
                        incoming=entry.incoming, origin=origin, seen_at=stamp,
                        run_id=run_id)
                    if entry.known:
                        b_known += 1
                    else:
                        b_new += 1
                batch_end += 1
                if batch_end - done >= max_rows or clock() - started >= budget_seconds:
                    break
            if on_batch is not None:
                on_batch(conn, BatchProgress(
                    batch_bar_time_end=entries[batch_end - 1].bar_time,
                    resume_from=(entries[batch_end].bar_time if batch_end < total else None),
                    rows_in_batch=batch_end - done, committed_entries=batch_end))
            if before_commit is not None:
                stop = before_commit()
                if stop is not None:
                    raise _StopBeforeCommit(stop)
            conn.commit()
        except _StopBeforeCommit as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            failure = exc.code
            break
        except Exception as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            failure, error_type = _failure_code(exc), type(exc).__name__
            break
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            if admitted:
                admission.end()
        inserted += b_ins
        unchanged += b_unc
        new_c += b_new
        known_c += b_known
        done = batch_end
    status = "ok" if failure is None else ("partial" if done > 0 else "failed")
    return ApplyResult(
        status, inserted, unchanged, new_c, known_c,
        entries[done - 1].bar_time if done else None,
        entries[done].bar_time if done < total else None,
        failure, error_type)


def load_history_bars(conn: sqlite3.Connection, symbol: str, interval: str,
                      *, source: str, since: datetime | None = None,
                      until: datetime | None = None) -> list[Bar]:
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"load_history_bars: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません "
            "(履歴はバックテスト・分析専用 — ライブ source は "
            "load_cache_bars を使うこと)")
    q = ("SELECT * FROM ohlcv_history WHERE symbol=? AND interval=? "
        "AND source=?")
    args: list = [symbol, interval, source]
    if since is not None:
        since_utc = _require_aware_utc(since, "since")
        q += " AND bar_time >= ?"
        args.append(since_utc.isoformat())
    if until is not None:
        until_utc = _require_aware_utc(until, "until")
        q += " AND bar_time <= ?"
        args.append(until_utc.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()
    return [Bar(r["symbol"], r["interval"],
                datetime.fromisoformat(r["bar_time"]),
                r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows]


def load_history_spread(conn: sqlite3.Connection, symbol: str, interval: str,
                        bar_time_iso: str, *, source: str) -> float | None:
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"load_history_spread: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません")
    row = conn.execute(
        "SELECT spread FROM ohlcv_history WHERE symbol=? AND interval=? "
        "AND bar_time=? AND source=?",
        (symbol, interval, bar_time_iso, source)).fetchone()
    return row["spread"] if row is not None else None
