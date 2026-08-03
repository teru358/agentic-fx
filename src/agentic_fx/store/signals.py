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
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from agentic_fx.backtest.timeframes import PLUGIN_TIMEFRAMES

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
_STALE_CONDITION = f"datetime(bar_ts) < {_CUTOFF_EXPR}"

# requeue / reclaim_expired 共有の遷移式 (controller 解決 #6 逐語):
# requeue_count >= max_requeue なら abandoned (増分なし)、
# 未満なら pending + requeue_count+1。
_REQUEUE_STATUS_EXPR = (
    "CASE WHEN requeue_count >= :max_requeue THEN 'abandoned' "
    "ELSE 'pending' END"
)
_REQUEUE_COUNT_EXPR = (
    "CASE WHEN requeue_count >= :max_requeue THEN requeue_count "
    "ELSE requeue_count + 1 END"
)


def add(conn: sqlite3.Connection, *, plugin: str, content_hash: str,
        pair: str, timeframe: str, bar_ts: str, kind: str, payload: dict,
        now: datetime) -> int | None:
    """signal を 1 件追加する。重複キー (plugin, content_hash, pair,
    timeframe, bar_ts) は INSERT OR IGNORE で無視し None を返す。

    timeframe は PLUGIN_TIMEFRAMES 限定で検証する。DDL 自体には CHECK が
    無い (brief §12 逐語のため変更しない) が、列挙外の timeframe が入ると
    ``_TF_MINUTES_CASE`` が NULL を返し、鮮度条件・失効条件が両方 NULL
    (= 常に偽) になって「claim も expire もされない不死身の pending」が
    生まれる。ここで弾くのが唯一の防波堤。
    """
    if timeframe not in PLUGIN_TIMEFRAMES:
        raise ValueError(
            f"timeframe must be one of {PLUGIN_TIMEFRAMES}: {timeframe!r}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO signals "
        "(plugin, content_hash, pair, timeframe, bar_ts, kind, "
        "payload_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (plugin, content_hash, pair, timeframe, bar_ts, kind,
         json.dumps(payload, ensure_ascii=False), now.isoformat()))
    conn.commit()
    return cur.lastrowid if cur.rowcount > 0 else None


def claim_oldest(conn: sqlite3.Connection, *, mission_id: int,
                  now: datetime, freshness_bars: int | None) -> dict | None:
    """bar_ts 最古の pending 1 行を原子的に claimed へ遷移して返す。

    ``freshness_bars=None`` は鮮度ゲートそのものを WHERE から外す
    (ゲート無効)。それ以外は claim 自身の WHERE に鮮度条件を含める —
    expire_stale との 2 段の間に stale 行が現れても claim されない保証は
    claim 側にある (原子性は単一 UPDATE で成立)。
    """
    params: dict = {"mission_id": mission_id, "now": now.isoformat()}
    freshness_clause = ""
    if freshness_bars is not None:
        params["freshness_bars"] = freshness_bars
        freshness_clause = f" AND {_FRESH_CONDITION}"
    cur = conn.execute(
        "UPDATE signals SET status='claimed', "
        "claimed_by_mission_id=:mission_id, claimed_at=:now "
        "WHERE id IN (SELECT id FROM signals WHERE status='pending'"
        f"{freshness_clause}"
        " ORDER BY datetime(bar_ts) ASC, id ASC LIMIT 1) "
        "RETURNING *",
        params)
    row = cur.fetchone()  # RETURNING を伴う文は commit 前に fetch する
    conn.commit()
    return dict(row) if row is not None else None


def expire_stale(conn: sqlite3.Connection, *, now: datetime,
                  freshness_bars: int) -> int:
    """鮮度切れの pending を一括で abandoned にする。claimed には触れない。

    claim_oldest とは独立の公開関数 (呼び出し側が通知件数に使う)。claim
    前に呼ぶ想定だが、呼び忘れ・競合があっても claim_oldest 自身の鮮度
    条件が防波堤になる (このモジュール docstring 参照)。
    """
    cur = conn.execute(
        "UPDATE signals SET status='abandoned' "
        f"WHERE status='pending' AND {_STALE_CONDITION}",
        {"now": now.isoformat(), "freshness_bars": freshness_bars})
    conn.commit()
    return cur.rowcount


def consume(conn: sqlite3.Connection, signal_id: int, *, mission_id: int,
            now: datetime) -> None:
    """claimed かつ claimed_by 一致の行だけを consumed にする (fail closed)。

    ``now`` は他 store モジュールの書き込み関数との呼び出し規約統一のため
    受け取るが、signals テーブルに updated_at 相当の列が無いため未使用。
    """
    cur = conn.execute(
        "UPDATE signals SET status='consumed' "
        "WHERE id=? AND status='claimed' AND claimed_by_mission_id=?",
        (signal_id, mission_id))
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(
            f"signal {signal_id} is not claimed by mission {mission_id}")


def requeue(conn: sqlite3.Connection, signal_id: int, *, now: datetime,
            max_requeue: int) -> str:
    """claimed の行を pending に戻す (上限超過なら abandoned)。

    上限判定: 現在の requeue_count が max_requeue 以上なら abandoned
    (増分なし)、未満なら pending + requeue_count+1。対象が claimed で
    なければ ValueError (fail closed)。

    ``now`` は他 store モジュールとの呼び出し規約統一のため受け取るが、
    signals テーブルに updated_at 相当の列が無いため未使用 (brief 逐語の
    シグネチャを保つ)。
    """
    cur = conn.execute(
        "UPDATE signals SET "
        f"status = {_REQUEUE_STATUS_EXPR}, "
        f"requeue_count = {_REQUEUE_COUNT_EXPR}, "
        "claimed_by_mission_id=NULL, claimed_at=NULL "
        "WHERE id=:id AND status='claimed' "
        "RETURNING status",
        {"id": signal_id, "max_requeue": max_requeue})
    row = cur.fetchone()  # RETURNING を伴う文は commit 前に fetch する
    conn.commit()
    if row is None:
        raise ValueError(f"signal {signal_id} is not claimed")
    return row["status"]


def reclaim_expired(conn: sqlite3.Connection, *, now: datetime,
                     lease_min: int, max_requeue: int) -> list[str]:
    """lease (claimed_at からの経過分) が切れた claimed 行を全件回収する。

    requeue と同一の上限判定を通す (lease 回収は上限を迂回しない —
    codex R1 I5)。lease 境界は ``claimed_at <= now - lease_min`` (以下、
    含む) — ちょうど lease_min 分経過した行も回収対象。
    """
    cur = conn.execute(
        "UPDATE signals SET "
        f"status = {_REQUEUE_STATUS_EXPR}, "
        f"requeue_count = {_REQUEUE_COUNT_EXPR}, "
        "claimed_by_mission_id=NULL, claimed_at=NULL "
        "WHERE status='claimed' AND datetime(claimed_at) <= "
        "datetime(:now, '-' || :lease_min || ' minutes') "
        "RETURNING status",
        {"now": now.isoformat(), "lease_min": lease_min,
         "max_requeue": max_requeue})
    rows = cur.fetchall()  # RETURNING を伴う文は commit 前に fetch する
    conn.commit()
    return [r["status"] for r in rows]


def pending_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM signals WHERE status='pending' LIMIT 1").fetchone()
    return row is not None


def recent(conn: sqlite3.Connection, pair: str, *,
           since: datetime) -> list[dict]:
    """pair 一致かつ bar_ts が since 以降の signal を返す (状態は問わない)。

    bar_ts を時間軸にする — claim の順序付け・鮮度・失効判定はすべて
    bar_ts (市場時刻) 基準であり、created_at (格納時刻) を基準にすると
    「古いバーの signal が再承認/再配信で新しく INSERT された」ケースで
    取引判断 loop に「直近」として誤って見せてしまう (get_signals は
    設計書 §5/§8 で取引判断 loop 専用、最大 lookback を持つ想定)。
    """
    rows = conn.execute(
        "SELECT * FROM signals WHERE pair=? AND datetime(bar_ts) >= "
        "datetime(?) ORDER BY datetime(bar_ts), id",
        (pair, since.isoformat())).fetchall()
    return [dict(r) for r in rows]
