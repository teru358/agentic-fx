"""signals 永続化 + 配信キュー (プラン 7 Task 7)。

承認済み signal/strategy plugin の出力を溜め、取引判断 Mission に 1 件ずつ
原子的に配る。状態は 4 つ (pending → claimed → consumed、または
requeue 上限超過 / 鮮度切れで abandoned に終端する)。§5 の「3 段階」は
このうち正常系遷移 (pending → claimed → consumed) の呼称。

**鮮度ゲート (D4)**: signal は timeframe ごとに宣言バー幅が異なる
(15m/1h/4h/1d)。「宣言 tf の何バー分まで新鮮とみなすか」を
``freshness_bars`` として受け取り、行ごとの timeframe から SQL 内で
cutoff を計算する (単一 timedelta では 1h/4h/1d 混在の pending を正しく
扱えない — codex R3 C1)。timeframe → 分数の変換は ``_TF_MINUTES_CASE`` の
CASE 式のみが持つ (``agentic_fx.backtest.timeframes.PLUGIN_TIMEFRAMES``
との同期は test_signals.py の正規表現テストでピンする)。

**格納値の日時表現**: bar_ts/claimed_at/created_at は
``datetime.isoformat()`` (例 "2026-08-03T12:00:00+00:00", 'T' 区切り)。
SQLite の ``datetime()`` はこれを受理するが返す文字列はスペース区切り
なので、生の列値と ``datetime(:now, '-N minutes')`` を直接比較すると
'T' vs ' ' の文字列比較で壊れる。鮮度・lease の判定は必ず両辺を
``datetime()`` で正規化して比較する。

**claim の原子性**: 単一 UPDATE 文 (WHERE id IN (SELECT ... LIMIT 1))
なので、bar_ts 最古の pending 1 行の選択と claimed への遷移が 1 文で
完結する。並行呼び出しは影響行数 (RETURNING で返る行の有無) で勝者が
決まる — 2 接続が同時に呼んでも異なる行を掴む (SQLite は書き込みを
直列化する)。

**requeue / reclaim_expired の上限判定**: 「現在の requeue_count が
max_requeue 以上なら abandoned (増分しない)、未満なら pending へ戻し
requeue_count を +1」。reclaim_expired (lease 切れの回収) も同じ判定を
通す — lease 回収が上限を迂回しない (codex R1 I5)。

**原子性の範囲 (fix round 1 F4)**: claim/requeue/reclaim_expired の原子性は
「単一 Connection 上の単一 SQL 文 + SQLite の書き込み直列化」で成立する。
同一 ``sqlite3.Connection`` を複数スレッドで共有した場合の直列性は保証
しない (本アプリは単一プロセス・Mission を直列実行する前提 — 設計書 §5。
複数プロセス/複数接続からの同時呼び出しは SQLite のロックにより直列化
されるため、その意味での原子性は成立する)。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agentic_fx.backtest.timeframes import PLUGIN_TIMEFRAMES, TF_MINUTES
from agentic_fx.store.db import SIGNAL_ABANDON_REASONS

# timeframe → 分数の CASE 式。鮮度・失効判定はここだけを参照する
# (式を複製すると将来 timeframe を追加したときに片方だけ更新され乖離する
# ため、claim_oldest/expire_stale で共有する — controller 解決 #7)。
# PLUGIN_TIMEFRAMES との同期は test_signals.py の正規表現テストでピンする。
_TF_MINUTES_CASE = (
    "CASE timeframe "
    "WHEN '15m' THEN 15 "
    "WHEN '1h' THEN 60 "
    "WHEN '4h' THEN 240 "
    "WHEN '1d' THEN 1440 "
    "END"
)

# 鮮度 cutoff (= now から「timeframe 幅 × freshness_bars」分だけ遡った
# 時刻)。'-' || (...) || ' minutes' で SQLite datetime() の負の修飾子文字列
# を組み立てる (例 "-120 minutes")。
_CUTOFF_EXPR = (
    f"datetime(:now, '-' || ({_TF_MINUTES_CASE} * :freshness_bars) "
    "|| ' minutes')"
)
_FRESH_CONDITION = f"datetime(bar_ts) >= {_CUTOFF_EXPR}"
# `_TF_MINUTES_CASE` が NULL (= 未知 timeframe) の行は `_CUTOFF_EXPR` も
# NULL になり、通常の日時比較はどちらの向きでも NULL (偽) になる —
# 「claim も expire もされない不死身の pending」(`add` の docstring 参照)。
# expire_stale 側は無条件 stale 扱いにして終端させる (claim_oldest 側は
# 触れない — `_FRESH_CONDITION` が NULL/偽のままなので構造的に claim
# されない、そちらはそれで安全)。
_STALE_CONDITION = (
    f"({_TF_MINUTES_CASE} IS NULL OR datetime(bar_ts) < {_CUTOFF_EXPR})")

_DEPLOY_LINKED_ANTI_JOIN = (
    "NOT EXISTS (SELECT 1 FROM plugin_deploy_op_signals AS deploy_links "
    "WHERE deploy_links.signal_id=signals.id)"
)

_ELIGIBILITY_CASE = (
    "CASE "
    "WHEN kind='strategy' AND contract_version='executable_v1' THEN 'eligible' "
    "WHEN kind='signal' OR contract_version IS NULL THEN 'ineligible_cleanup' "
    "ELSE 'ineligible' END"
)
_ELIGIBLE_CONDITION = f"({_ELIGIBILITY_CASE})='eligible'"
_CLEANUP_CONDITION = f"({_ELIGIBILITY_CASE})='ineligible_cleanup'"

# requeue / reclaim_expired 共有の遷移式 (controller 解決 #6 逐語):
# requeue_count >= max_requeue なら abandoned (増分なし)、
# 未満なら pending + requeue_count+1。
_DEPLOY_LINKED_EXISTS = (
    "EXISTS (SELECT 1 FROM plugin_deploy_op_signals AS deploy_links "
    "WHERE deploy_links.signal_id=signals.id)"
)
REASON_DEPLOY_LINKED = "deploy_linked"
REASON_RETRY_EXHAUSTED = "retry_exhausted"
REASON_REQUEUED_EXPIRED = "requeued_expired"
REASON_FRESHNESS_EXPIRED = "freshness_expired"

# deploy 付随表に結ばれた signal は pending に戻すと anti-join で永久に
# 不可視になるので、上限に関係なく abandoned で終端する (増分なし)。
# 規則表 (上から最初に当たったものが理由): deploy に結ばれていれば
# (上限超過を兼ねても) `deploy_linked`、そうでなく上限以上なら
# `retry_exhausted`。どれにも当たらなければ pending + requeue_count+1。
# SQL の CASE と `requeue_outcome` はこの表だけから作る。
# 各行は (理由, SQL 述語, Python 述語(requeue_count, max_requeue, deploy_linked))。
_REQUEUE_RULES = (
    (REASON_DEPLOY_LINKED, _DEPLOY_LINKED_EXISTS,
     lambda count, limit, linked: linked),
    (REASON_RETRY_EXHAUSTED, "requeue_count >= :max_requeue",
     lambda count, limit, linked: count >= limit),
)


def _requeue_set_sql(rules=_REQUEUE_RULES) -> str:
    """規則表から requeue / reclaim_expired の SET 句を作る。"""
    any_rule = " OR ".join(sql for _, sql, _ in rules)
    reason = "CASE " + " ".join(
        f"WHEN {sql} THEN '{name}'" for name, sql, _ in rules) + " END"
    return (
        f"status = CASE WHEN {any_rule} THEN 'abandoned' ELSE 'pending' END, "
        f"requeue_count = CASE WHEN {any_rule} "
        "THEN requeue_count ELSE requeue_count + 1 END, "
        f"abandon_reason = {reason}, "
        f"abandoned_at = CASE WHEN {any_rule} THEN :abandoned_at END, "
        "claimed_by_mission_id=NULL, claimed_at=NULL"
    )


_REQUEUE_SET = _requeue_set_sql()

# 鮮度切れの abandoned の理由。requeue 歴が無ければ `freshness_expired`、
# あれば `requeued_expired`。
_FRESHNESS_REASON_EXPR = (
    f"CASE WHEN {_CLEANUP_CONDITION} THEN 'ineligible_cleanup' "
    f"WHEN requeue_count > 0 THEN '{REASON_REQUEUED_EXPIRED}' "
    f"ELSE '{REASON_FRESHNESS_EXPIRED}' END"
)


def requeue_outcome(*, requeue_count: int, max_requeue: int,
                    deploy_linked: bool,
                    rules=_REQUEUE_RULES) -> tuple[str, str | None]:
    """claimed の signal を戻すときの `(遷移後 status, abandon_reason)`。

    `_REQUEUE_RULES` を上から評価する。SQL 側 (`_requeue_set_sql`) と
    `missions.recover_interrupted` が同じ表を使う。"""
    for name, _, hit in rules:
        if hit(requeue_count, max_requeue, deploy_linked):
            return "abandoned", name
    return "pending", None


def utc_iso(now: datetime) -> str:
    """abandoned_at 等に書く UTC の ISO 文字列。naive は拒否する。"""
    _require_aware(now, "now")
    return now.astimezone(timezone.utc).isoformat()


def freshness_abandon_reason(signal: dict) -> str:
    """鮮度切れで abandoned にする signal 行の理由 (`_FRESHNESS_REASON_EXPR`
    と同じ規則)。"""
    return (REASON_REQUEUED_EXPIRED if signal["requeue_count"] > 0
            else REASON_FRESHNESS_EXPIRED)


@dataclass(frozen=True, slots=True)
class AbandonedRow:
    id: int
    plugin: str
    timeframe: str


@dataclass(frozen=True, slots=True)
class ExpireResult:
    total: int
    invalid_timeframe: tuple[AbandonedRow, ...]


def _require_aware(dt: datetime, label: str) -> None:
    """tz-aware であることを検証する (fail closed)。

    F2 (レビュー fix round 1, codex Important): SQLite の datetime() は
    naive な文字列を暗黙に UTC とみなす。呼び出し側が JST 等の naive
    datetime を渡すと、鮮度・lease 判定が時差分ズレて **静かに** 誤動作
    する (fail closed でない)。SQL 内で日時比較を行う公開関数の入口で
    必ず検証する。
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"{label} is naive; tz-aware UTC datetime required "
            "(cannot safely assume UTC)")


def _validate_bar_ts(conn: sqlite3.Connection, bar_ts: str) -> None:
    """bar_ts がタイムゾーン付き ISO-8601 文字列であり、かつ SQLite の
    ``datetime()`` が NULL を返さないことを検証する (fail closed)。

    F1 (レビュー fix round 1, codex/sonnet 一致 Important): 不正な bar_ts
    (パース不能な文字列) が入ると SQL 内の ``datetime(bar_ts)`` が NULL を
    返し、鮮度条件・失効条件が両方 NULL (SQL 的に偽) になって「claim も
    expire もされない不死身の pending 行」が生まれる (add() の timeframe
    検証と同型の穴)。

    F2: naive (tz 情報なし) な bar_ts も同じ理由 (SQLite が黙って UTC 扱い
    するため時差分ズレる) で拒否する。
    """
    try:
        parsed = datetime.fromisoformat(bar_ts)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"bar_ts is not a valid ISO-8601 datetime string: {bar_ts!r}"
        ) from e
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"bar_ts is naive; tz-aware UTC datetime string required "
            f"(cannot safely assume UTC): {bar_ts!r}")
    # SQLite 側の受理性そのものを最終確認する (この検証の本質的な保証は
    # 「SQL 内 datetime(bar_ts) が NULL にならない」こと)。
    row = conn.execute("SELECT datetime(?)", (bar_ts,)).fetchone()
    if row[0] is None:
        raise ValueError(
            f"bar_ts is not accepted by SQLite datetime(): {bar_ts!r}")


def freshness_window(timeframe: str, bar_ts: str,
                     freshness_bars: int) -> tuple[str, str]:
    """signal の ``(bar_close, fresh_until)`` を ISO 文字列で返す (DB は触らない)。

    ``bar_close`` は strategy 足の確定時刻 (``bar_ts`` + 足幅)。
    ``fresh_until`` は ``bar_ts`` + 足幅 × ``freshness_bars`` で、
    ``_FRESH_CONDITION`` / ``_STALE_CONDITION`` と同じ境界 — now がこの時刻
    以下なら pending のまま claim でき、超えると maintenance の
    ``expire_stale`` が abandoned にする。``freshness_bars`` は
    ``settings.plugin.signal_freshness_bars`` (``Field(ge=1)``) 由来で
    常に int — 鮮度ゲート無効の経路は本番に存在しない。
    """
    start = datetime.fromisoformat(bar_ts)
    width = timedelta(minutes=TF_MINUTES[timeframe])
    fresh_until = (start + width * freshness_bars).isoformat()
    return (start + width).isoformat(), fresh_until


def add(conn: sqlite3.Connection, *, plugin: str, content_hash: str,
        pair: str, timeframe: str, bar_ts: str, kind: str, payload: dict,
        now: datetime, contract_version: str | None = None) -> int | None:
    """signal を 1 件追加する。重複キー (plugin, content_hash, pair,
    timeframe, bar_ts) は INSERT OR IGNORE で無視し None を返す。

    timeframe は PLUGIN_TIMEFRAMES 限定で検証する。DDL 自体には CHECK が
    無い (brief §12 逐語のため変更しない) が、列挙外の timeframe が入ると
    ``_TF_MINUTES_CASE`` が NULL を返し、鮮度条件・失効条件が両方 NULL
    (= 常に偽) になって「claim も expire もされない不死身の pending」が
    生まれる。ここで弾くのが唯一の防波堤。

    bar_ts / now も同じ理由 (fail closed) で tz-aware かつ SQLite が
    受理できる形式であることを検証する (F1/F2)。
    """
    if timeframe not in PLUGIN_TIMEFRAMES:
        raise ValueError(
            f"timeframe must be one of {PLUGIN_TIMEFRAMES}: {timeframe!r}")
    _validate_bar_ts(conn, bar_ts)
    _require_aware(now, "now")
    if kind == "strategy" and contract_version is None:
        contract_version = "executable_v1"
    cur = conn.execute(
        "INSERT OR IGNORE INTO signals "
        "(plugin, content_hash, pair, timeframe, bar_ts, kind, "
        "payload_json, created_at, contract_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (plugin, content_hash, pair, timeframe, bar_ts, kind,
         json.dumps(payload, ensure_ascii=False), now.isoformat(), contract_version))
    conn.commit()
    return cur.lastrowid if cur.rowcount > 0 else None


def claim_oldest(conn: sqlite3.Connection, *, mission_id: int,
                  now: datetime, freshness_bars: int | None,
                  max_id: int | None = None) -> dict | None:
    """bar_ts 最古の pending 1 行を原子的に claimed へ遷移して返す。

    ``freshness_bars=None`` は鮮度ゲートそのものを WHERE から外す
    (ゲート無効)。それ以外は claim 自身の WHERE に鮮度条件を含める —
    expire_stale との 2 段の間に stale 行が現れても claim されない保証は
    claim 側にある (原子性は単一 UPDATE で成立)。
    """
    _require_aware(now, "now")
    params: dict = {"mission_id": mission_id, "now": now.isoformat()}
    claimable = _claimable_where(freshness_bars, params)
    if max_id is not None:
        params["max_id"] = max_id
        claimable += " AND id <= :max_id"
    cur = conn.execute(
        "UPDATE signals SET status='claimed', "
        "claimed_by_mission_id=:mission_id, claimed_at=:now "
        "WHERE id IN (SELECT id FROM signals WHERE "
        f"{claimable}"
        " ORDER BY datetime(bar_ts) ASC, id ASC LIMIT 1) "
        "RETURNING *",
        params)
    row = cur.fetchone()  # RETURNING を伴う文は commit 前に fetch する
    conn.commit()
    return dict(row) if row is not None else None


def expire_stale(conn: sqlite3.Connection, *, now: datetime,
                  freshness_bars: int) -> ExpireResult:
    """鮮度切れの pending を一括で abandoned にする。claimed には触れない。

    claim_oldest とは独立の公開関数 (呼び出し側が通知件数に使う)。claim
    前に呼ぶ想定だが、呼び忘れ・競合があっても claim_oldest 自身の鮮度
    条件が防波堤になる (このモジュール docstring 参照)。

    ``_STALE_CONDITION`` は未知 timeframe の行 (`_TF_MINUTES_CASE` が NULL)
    も無条件に対象へ含む。通知が必要な未知 timeframe の行は戻り値で呼び出し
    側へ渡し、store 層では activity を記録しない。
    """
    _require_aware(now, "now")
    cur = conn.execute(
        "UPDATE signals SET status='abandoned', "
        f"abandon_reason={_FRESHNESS_REASON_EXPR}, abandoned_at=:abandoned_at "
        f"WHERE status='pending' AND {_STALE_CONDITION} "
        f"AND ({_ELIGIBLE_CONDITION} OR {_CLEANUP_CONDITION}) "
        f"RETURNING id, plugin, timeframe, ({_TF_MINUTES_CASE} IS NULL) "
        "AS invalid_timeframe",
        {"now": now.isoformat(), "freshness_bars": freshness_bars,
         "abandoned_at": utc_iso(now)})
    rows = cur.fetchall()  # RETURNING を伴う文は commit 前に fetch する
    conn.commit()
    # RETURNING 列は位置で読む — row_factory を設定していない素の接続でも
    # 動くように (呼び出し側の接続の作り方に依存しない)。
    invalid_timeframe = tuple(
        AbandonedRow(id=row[0], plugin=row[1], timeframe=row[2])
        for row in rows if row[3]
    )
    return ExpireResult(total=len(rows), invalid_timeframe=invalid_timeframe)


def consume_in_tx(conn: sqlite3.Connection, signal_id: int, *, mission_id: int,
                  now: datetime) -> None:
    """claimed かつ claimed_by 一致の行だけを consumed にする (fail closed)。

    ``now`` は他 store モジュールの書き込み関数との呼び出し規約統一のため
    受け取るが、signals テーブルに updated_at 相当の列が無いため未使用。
    """
    cur = conn.execute(
        "UPDATE signals SET status='consumed' "
        "WHERE id=? AND status='claimed' AND claimed_by_mission_id=?",
        (signal_id, mission_id))
    if cur.rowcount == 0:
        raise ValueError(
            f"signal {signal_id} is not claimed by mission {mission_id}")


def consume(conn: sqlite3.Connection, signal_id: int, *, mission_id: int,
            now: datetime) -> None:
    """Consume a signal and commit, preserving the public store API."""
    try:
        consume_in_tx(conn, signal_id, mission_id=mission_id, now=now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def requeue(conn: sqlite3.Connection, signal_id: int, *, now: datetime,
            max_requeue: int) -> str:
    """claimed の行を pending に戻す (上限超過なら abandoned)。

    上限判定: 現在の requeue_count が max_requeue 以上なら abandoned
    (増分なし)、未満なら pending + requeue_count+1。対象が claimed で
    なければ ValueError (fail closed)。abandoned にしたときは ``now`` を
    ``abandoned_at`` に書く。
    """
    cur = conn.execute(
        f"UPDATE signals SET {_REQUEUE_SET} "
        "WHERE id=:id AND status='claimed' "
        "RETURNING status",
        {"id": signal_id, "max_requeue": max_requeue,
         "abandoned_at": utc_iso(now)})
    row = cur.fetchone()  # RETURNING を伴う文は commit 前に fetch する
    conn.commit()
    if row is None:
        raise ValueError(f"signal {signal_id} is not claimed")
    return row["status"]


def abandon_claimed(conn: sqlite3.Connection, signal_id: int, *,
                    now: datetime, reason: str) -> None:
    """claimed の行を requeue_count を増やさず abandoned にする。対象が
    claimed でなければ ValueError (fail closed)。鮮度切れや source の失効が
    確定した signal を pending に戻して再 claim の候補にしないために使う。
    理由は store では判別できないので呼び出し側が渡す
    (鮮度切れは ``freshness_abandon_reason``)。語彙外の理由は書く前に
    ValueError にする。"""
    if reason not in SIGNAL_ABANDON_REASONS:
        raise ValueError(f"unknown signal abandon reason: {reason!r}")
    abandoned_at = utc_iso(now)
    cur = conn.execute(
        "UPDATE signals SET status='abandoned', "
        "abandon_reason=?, abandoned_at=?, "
        "claimed_by_mission_id=NULL, claimed_at=NULL "
        "WHERE id=? AND status='claimed'", (reason, abandoned_at, signal_id))
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"signal {signal_id} is not claimed")


def abandoned_counts(conn: sqlite3.Connection, *, since: datetime,
                     until: datetime) -> dict[str, int]:
    """`[since, until)` に abandoned になった signal の件数を理由別に返す。

    `abandoned_at` は UTC、`created_at` / `claimed_at` / `bar_ts` は clock の
    オフセットのままなので、期間の比較や集計は生の文字列でなく必ず
    ``datetime()`` で両辺を正規化する。この関数と `abandon_lag_seconds` が
    その規律の置き場で、abandoned の期間 query はここを経由する。"""
    _require_aware(since, "since")
    _require_aware(until, "until")
    rows = conn.execute(
        "SELECT abandon_reason, COUNT(*) FROM signals "
        "WHERE status='abandoned' AND abandoned_at IS NOT NULL "
        "AND abandon_reason NOT IN ('ineligible_cleanup','legacy_unknown') "
        "AND datetime(abandoned_at) >= datetime(:since) "
        "AND datetime(abandoned_at) < datetime(:until) "
        "GROUP BY abandon_reason",
        {"since": since.isoformat(), "until": until.isoformat()}).fetchall()
    return {r[0]: r[1] for r in rows}


def human_signal_aggregates(conn: sqlite3.Connection, *, since: datetime,
                            until: datetime) -> dict:
    """Read human-only decision and abandonment counts for a UTC interval."""
    _require_aware(since, "since")
    _require_aware(until, "until")
    params = {"since": since.isoformat(), "until": until.isoformat()}
    decisions = conn.execute(
        "SELECT signals.plugin, signal_decisions.decision, "
        "signal_decisions.actor, signal_decisions.reject_code, COUNT(*) AS count "
        "FROM signal_decisions JOIN signals ON signals.id=signal_decisions.signal_id "
        f"WHERE {_ELIGIBLE_CONDITION} "
        "AND datetime(signal_decisions.decided_at) >= datetime(:since) "
        "AND datetime(signal_decisions.decided_at) < datetime(:until) "
        "GROUP BY signals.plugin, signal_decisions.decision, "
        "signal_decisions.actor, signal_decisions.reject_code "
        "ORDER BY signals.plugin, signal_decisions.decision, signal_decisions.actor, "
        "signal_decisions.reject_code", params).fetchall()
    cleanup = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE status='abandoned' "
        "AND abandon_reason='ineligible_cleanup' "
        "AND datetime(abandoned_at) >= datetime(:since) "
        "AND datetime(abandoned_at) < datetime(:until)", params).fetchone()[0]
    legacy_unknown = conn.execute(
        "SELECT COUNT(*) FROM signals WHERE status='abandoned' "
        "AND abandon_reason='legacy_unknown'").fetchone()[0]
    return {
        "decisions": [dict(row) for row in decisions],
        "abandoned": abandoned_counts(conn, since=since, until=until),
        "cleanup": cleanup,
        "legacy_unknown": legacy_unknown,
    }


def abandon_lag_seconds(conn: sqlite3.Connection, signal_id: int) -> int | None:
    """signal が作られてから abandoned になるまでの秒数。abandoned でない、
    または時刻を持たない (`legacy_unknown`) ときは None。`created_at` との
    比較は ``datetime()`` で正規化する (オフセットが違っても正しい)。"""
    row = conn.execute(
        "SELECT CAST(strftime('%s', abandoned_at) AS INTEGER) "
        "- CAST(strftime('%s', created_at) AS INTEGER) "
        "FROM signals WHERE id=? AND status='abandoned'",
        (signal_id,)).fetchone()
    return None if row is None or row[0] is None else row[0]


def reclaim_expired(conn: sqlite3.Connection, *, now: datetime,
                     lease_min: int, max_requeue: int) -> list[str]:
    """lease (claimed_at からの経過分) が切れた claimed 行を全件回収する。

    requeue と同一の上限判定を通す (lease 回収は上限を迂回しない —
    codex R1 I5)。lease 境界は ``claimed_at <= now - lease_min`` (以下、
    含む) — ちょうど lease_min 分経過した行も回収対象。
    """
    _require_aware(now, "now")
    cur = conn.execute(
        f"UPDATE signals SET {_REQUEUE_SET} "
        "WHERE status='claimed' AND datetime(claimed_at) <= "
        "datetime(:now, '-' || :lease_min || ' minutes') "
        "RETURNING status",
        {"now": now.isoformat(), "lease_min": lease_min,
         "max_requeue": max_requeue, "abandoned_at": utc_iso(now)})
    rows = cur.fetchall()  # RETURNING を伴う文は commit 前に fetch する
    conn.commit()
    return [r["status"] for r in rows]


def _claimable_where(freshness_bars: int | None, params: dict) -> str:
    freshness_clause = ""
    if freshness_bars is not None:
        params["freshness_bars"] = freshness_bars
        freshness_clause = f" AND {_FRESH_CONDITION}"
    return (f"status='pending' AND {_ELIGIBLE_CONDITION}{freshness_clause} "
            f"AND {_DEPLOY_LINKED_ANTI_JOIN}")


def pending_exists(conn: sqlite3.Connection, *, now: datetime,
                   freshness_bars: int | None) -> bool:
    _require_aware(now, "now")
    params: dict = {"now": now.isoformat()}
    row = conn.execute(
        "SELECT 1 FROM signals WHERE "
        f"{_claimable_where(freshness_bars, params)} LIMIT 1", params).fetchone()
    return row is not None


def normalize_legacy_strategy_payloads(
        conn: sqlite3.Connection, *, now: datetime | None = None) -> None:
    """Upgrade pending/claimed legacy strategy payloads without committing."""
    from agentic_fx.plugin.strategy_adapter import normalize_strategy_payload

    migrated_at = utc_iso(now or datetime.now(timezone.utc))
    rows = conn.execute(
        "SELECT id,pair,payload_json FROM signals "
        "WHERE kind='strategy' AND contract_version IS NULL "
        "AND status IN ('pending','claimed')").fetchall()
    for row in rows:
        try:
            legacy = json.loads(row["payload_json"])
            provenance = {
                "live_source": legacy["live_source"],
                "source": legacy["source"],
                "interval": legacy["interval"],
                "bar_time": legacy["bar_time"],
                "bars_origin": legacy["bars_origin"],
            }
            payload = normalize_strategy_payload(legacy, row["pair"], provenance)
            if payload is None:
                raise ValueError("hold is not executable")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            conn.execute(
                "UPDATE signals SET status='abandoned', "
                "abandon_reason='normalization_failed', abandoned_at=?, "
                "claimed_by_mission_id=NULL, claimed_at=NULL WHERE id=?",
                (migrated_at, row["id"]))
        else:
            conn.execute(
                "UPDATE signals SET payload_json=?, contract_version='executable_v1' "
                "WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"]))


def producer_daily_signal_max(strategies, settings_pairs) -> int:
    """承認済み strategy の識別集合から日次の最大 signal 件数を返す。

    式は Σ (|pairs ∩ settings_pairs| × ceil(1440 / timeframe 分)) を
    (name, content_hash) ごとに 1 回だけ足す。同一 identity は strategy 行が
    重複しても 1 度しか数えず、bar ごと・pair ごとの上限は signals の UNIQUE
    制約が保証する。content hash が更新された直後は新 hash の cursor が鮮度窓
    を再評価するため、更新 1 回につき最大 k (鮮度 bar 数) × pair 数 本だけ
    この値を超え得る。その超過はここに含めず、容量 80% 警告の範囲で扱う。
    """
    seen: set[tuple[str, str]] = set()
    allowed_pairs = set(settings_pairs)
    total = 0
    for strategy in strategies:
        if getattr(strategy, "kind", None) != "strategy":
            continue
        identity = (strategy.name, strategy.content_hash)
        if identity in seen:
            continue
        seen.add(identity)
        pairs = set(strategy.pairs) & allowed_pairs
        total += len(pairs) * ((1440 + TF_MINUTES[strategy.timeframe] - 1)
                               // TF_MINUTES[strategy.timeframe])
    return total


def recent(conn: sqlite3.Connection, pair: str, *, since: datetime,
           exclude_deploy_linked: bool = False) -> list[dict]:
    """pair 一致かつ bar_ts が since 以降の signal を返す (状態は問わない)。

    bar_ts を時間軸にする — claim の順序付け・鮮度・失効判定はすべて
    bar_ts (市場時刻) 基準であり、created_at (格納時刻) を基準にすると
    「古いバーの signal が再承認/再配信で新しく INSERT された」ケースで
    取引判断 loop に「直近」として誤って見せてしまう (get_signals は
    設計書 §5/§8 で取引判断 loop 専用、最大 lookback を持つ想定)。
    """
    _require_aware(since, "since")
    linked_clause = f" AND {_DEPLOY_LINKED_ANTI_JOIN}" if exclude_deploy_linked else ""
    rows = conn.execute(
        "SELECT * FROM signals WHERE pair=? AND datetime(bar_ts) >= "
        f"datetime(?){linked_clause} ORDER BY datetime(bar_ts), id",
        (pair, since.isoformat())).fetchall()
    return [dict(r) for r in rows]


def _abandon_pending_where(conn: sqlite3.Connection, predicate: str, params: tuple,
                           *, op_id: int, now: datetime, commit: bool) -> None:
    abandoned_at = utc_iso(now)
    rows = conn.execute(
        "SELECT id, status FROM signals WHERE status IN ('pending','claimed') "
        f"AND ({predicate})", params).fetchall()
    conn.executemany(
        "INSERT OR IGNORE INTO plugin_deploy_op_signals "
        "(op_id,signal_id,status_at_link) VALUES (?,?,?)",
        ((op_id, row["id"], row["status"]) for row in rows),
    )
    conn.execute(
        "UPDATE signals SET status='abandoned', abandon_reason='deploy_op', "
        "abandoned_at=? WHERE status='pending' "
        f"AND ({predicate})", (abandoned_at, *params))
    if commit:
        conn.commit()


def abandon_pending_for_identities(
        conn: sqlite3.Connection, identities: list[tuple[str, str]], *, op_id: int,
        now: datetime, commit: bool = True) -> None:
    if not identities:
        return
    predicate = " OR ".join("(plugin=? AND content_hash=?)" for _ in identities)
    params = tuple(value for identity in identities for value in identity)
    _abandon_pending_where(conn, predicate, params, op_id=op_id, now=now,
                           commit=commit)


def abandon_pending_for_name(conn: sqlite3.Connection, name: str, *, op_id: int,
                             now: datetime, commit: bool = True) -> None:
    _abandon_pending_where(conn, "plugin=?", (name,), op_id=op_id, now=now,
                           commit=commit)
