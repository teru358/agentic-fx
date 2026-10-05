"""シグナル生産 — 承認済み signal/strategy plugin を評価し signals キューへ
詰める (プラン 7 Task 8、設計書 §6)。

**バケット進行検出 (opus R2 C2)**: 本番の ``now`` は非分格子 (秒・マイクロ
秒を含む) なので「今が丁度バケット境界」という一致判定は永久に成立しない。
代わりに「前回評価したバケットより後、かつ現在のバケット (未確定なので
含まない) より前に開始する確定バケット」を評価対象とする — 各 tick で
``floor = floor_to_bucket(now, meta.timeframe)`` を計算し、``cursor < b <
floor`` かつ ``b >= now − tf幅 × signal_freshness_bars`` を満たすバケット
開始時刻 ``b`` を古い順に評価する。

**評価 cursor はメモリ辞書のみ** (``(plugin.name, content_hash, pair)`` →
評価成功済みバケット開始時刻。テーブル追加なし)。この規則が 3 つの障害を
同時に有界回復する (codex R3 I1):

1. **再起動 (cursor 喪失)**: 鮮度窓内の確定バケットを再評価する。hold
   だったバケットも再評価されるが、signals の UNIQUE dedupe が重複挿入を
   防ぎ、再評価コストも窓幅で有界。
2. **複数バケット停止**: 窓を超えた分は評価しない (鮮度ゲートと同じ裁定
   — 古い判断を今さら起こさない)。窓外の古いバケットは評価せずそのまま
   cursor を進める (自然放棄)。
3. **plugin 失敗**: sandbox 呼び出しが失敗したバケットで catch-up を打ち
   切る (cursor をそのバケットの手前で止める) — 次 tick で同じバケットを
   再試行する。同じ理由でデータ欠損 (df 空) も同じ扱いにする — 「本番
   source にまだ取り込まれていないだけ」を fail-open で捌く。

**source** は本番運用の data source (``settings.datafeed.primary``、
既定 "yfinance") — 承認バックテスト (Task 6) の eval source とは意図的に
異なる (承認 payload の "live_source" に差異を記載済み)。

**評価対象**: 承認済み signal/strategy plugin × 宣言 pairs ∩
settings.pairs (交差の外は warning でスキップ)。signal の ``detect`` は
各出力を ``kind="signal"`` の行にする。strategy の ``evaluate`` は
``action="open"`` の結果だけを ``kind="strategy"`` の行にする
(``"hold"`` は保存しない)。行の ``bar_ts`` は評価対象バケットの開始時刻を
ハーネス (このモジュール) が設定する — plugin 出力からは受け取らない
(sandbox.py の signal 経路は ``bar_ts`` キーそのものを reject する)。

**セッション償却**: ``sandbox_run`` 省略時 (既定 None) は plugin ごとに
``PluginSession`` を 1 個だけ開き、``evaluate_due_plugins`` 呼び出し全体
(複数 pair・複数バケットの catch-up を含む) で使い回す — 1 回の producer
tick で plugin×pair×bucket の数だけ worker サブプロセスを起動しては
性能が成立しない (sandbox.py 自身が明記する設計方針と同じ)。
``sandbox_run`` が注入された場合 (テスト) はセッション管理をバイパスし、
``market_tools.build``/``signal_eval.py`` と同じ呼び出し規約
``sandbox_run(meta, payload, settings=settings.plugin)`` で毎回呼ぶ。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from agentic_fx.backtest.timeframes import TF_MINUTES, floor_to_bucket, load_closed_frame
from agentic_fx.activity import Category
from agentic_fx.core import market_hours
from agentic_fx.plugin import sandbox as plugin_sandbox
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.store import signals

if TYPE_CHECKING:
    import sqlite3

    from agentic_fx.config import Settings
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet

_log = logging.getLogger("agentic_fx.plugin.signal_producer")

# market_tools.build / signal_eval.py と同じ注入シグネチャ規約:
# (meta, payload, *, settings) -> dict。
SandboxRunFn = Callable[..., dict]

# 評価失敗 (sandbox 例外・データ欠損) を catch-up ループ内で統一的に扱う
# ための内部呼び出し規約: (meta, payload) -> dict。
_CallFn = Callable[[PluginMeta, dict], dict]


class SignalProducer:
    """評価 cursor (メモリのみ) を保持する signal/strategy plugin 評価器。

    1 インスタンス = 1 cursor 集合。service が再起動して producer を作り
    直せば cursor は失われ、鮮度窓内の確定バケットが catch-up 再評価される
    (冪等 — signals の UNIQUE dedupe が重複挿入を防ぐ)。
    """

    def __init__(self) -> None:
        # (plugin.name, content_hash, pair) -> 最後に評価成功したバケット開始時刻
        self._cursor: dict[tuple[str, str, str], datetime] = {}
        # The set is deliberately in-memory like the cursor: an outage should
        # yield one diagnostic per unchanged shortage, then recover silently.
        self._insufficient_closed: set[tuple[str, str, str, str]] = set()
        # 評価失敗の通知間引き: (plugin, content_hash, pair, bucket, result)
        # -> 同じ失敗を観測した回数。メモリのみ (再起動で初回通知に戻る)。
        self._failure_notices: dict[
            tuple[str, str, str, datetime, str], int] = {}
        self._paused = False

    def evaluate_due_plugins(self, conn: "sqlite3.Connection", *,
                             plugins: list[PluginMeta], now: datetime,
                             source: str, sandbox_run: SandboxRunFn | None = None,
                             settings: "Settings",
                             resolved_by_identity: (
                                 "dict[tuple[str, str], ResolvedIndicatorSet]"),
                             activity=None,
                             ) -> int:
        """承認済み signal/strategy plugin のうち評価期限が来たバケットを
        評価し、新規挿入した signals 行の総数を返す。

        ``plugins`` は ``plugin_loader.approved_plugins()`` の戻り値
        (承認済み・任意 kind 混在) をそのまま渡してよい — kind が
        signal/strategy 以外の要素はここで無視する。

        [indicator-consumption-wiring] §2.3 (codex plan r2 束2 Critical 是正):
        `resolved_by_identity` は `InventoryBuildResult.resolved` をそのまま
        (キー `(name, content_hash)`) 渡したもの。**strategy は解決済みの
        ものしか評価しない** — 未解決の strategy は warning + skip
        (fail closed。producer には再解決の責務を持たせない)。

        `resolved` の取得キーは `(meta.name, meta.content_hash)` (Global
        Constraints の既存 identity と同じ) — `content_hash` だけでは、
        同一 content_hash (= `plugin.py` + `config.yaml` が完全に同一バイト
        列) を持つ別名 strategy が存在したとき、どちらの `ResolvedIndicatorSet`
        を渡すべきか一意に決まらない。

        **session cache のキーは `content_hash` 単独のまま変えない**
        (codex plan r2 束2 Critical への回答、根拠 3 点):
        (1) content_hash は plugin.py + config.yaml のバイト列そのものの
        hash であり、config.yaml には依存宣言 (`indicators:` ブロックと
        pin) も含まれる — content_hash が一致する 2 つの strategy は
        依存宣言も含めてバイト単位で同一なので、同じ inventory 構築の中で
        解決された `ResolvedIndicatorSet` は値として等価になる。
        (2) resolved の**受け渡し**は `(name, hash)` 単位に保つ (上記) ため、
        「渡す値の identity 保証」と「subprocess 再利用の単位」を分離できる。
        (3) `(name, hash)` を session cache キーに含めると、同一コードの
        strategy を改名しただけで無駄に subprocess が増える退行を生む。
        """
        if not market_hours.is_market_open(now):
            if not self._paused:
                next_open = market_hours.next_expected_trading_time(
                    now, timedelta(minutes=1))
                _log.info(
                    "market closed — signal evaluation paused until %s",
                    next_open.isoformat())
                self._paused = True
            return 0
        if self._paused:
            _log.info("market open — signal evaluation resumed")
            self._paused = False

        inserted = 0
        sessions: dict[str, plugin_sandbox.PluginSession] = {}

        def _call(meta: PluginMeta, payload: dict) -> dict:
            if sandbox_run is not None:
                return sandbox_run(meta, payload, settings=settings.plugin)
            session = sessions.get(meta.content_hash)
            if session is None:
                session = plugin_sandbox.PluginSession(
                    meta, settings=settings.plugin,
                    resolved=resolved_by_identity.get(
                        (meta.name, meta.content_hash)))
                session.__enter__()
                sessions[meta.content_hash] = session
            return session.call(payload)

        # 今回評価する (plugin, pair)。対象から外れた plugin・pair や旧 hash の
        # 失敗記録を残すと、plugin の更新のたびに増え続けるので先に落とす。
        work: list[tuple[PluginMeta, str]] = []
        for meta in plugins:
            if meta.kind not in ("signal", "strategy"):
                continue
            if (meta.kind == "strategy"
                    and (meta.name, meta.content_hash)
                    not in resolved_by_identity):
                _log.warning(
                    "plugin %s: indicator dependencies are unresolved — "
                    "skipping (fail closed)", meta.name)
                continue
            for pair in meta.pairs:
                if pair not in settings.pairs:
                    _log.warning(
                        "plugin %s: pair %s is not in settings.pairs — "
                        "skipping", meta.name, pair)
                    continue
                work.append((meta, pair))
        live = {(m.name, m.content_hash, p) for m, p in work}
        for k in [k for k in self._failure_notices if k[:3] not in live]:
            del self._failure_notices[k]

        try:
            for meta, pair in work:
                inserted += self._evaluate_one(
                    conn, meta, pair, now=now, source=source, call=_call,
                    settings=settings, activity=activity)
        finally:
            # 1 本の close の失敗で後続の session の後始末を飛ばさない
            for session in sessions.values():
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    _log.warning("plugin session close failed", exc_info=True)
        return inserted

    def _evaluate_one(self, conn: "sqlite3.Connection", meta: PluginMeta,
                      pair: str, *, now: datetime, source: str,
                      call: _CallFn, settings: "Settings", activity=None) -> int:
        """1 plugin × 1 pair 分の catch-up 評価。古いバケットから順に評価し、
        失敗したバケットの手前で打ち切る (cursor はそこまでしか進めない)。
        """
        tf = meta.timeframe
        width = timedelta(minutes=TF_MINUTES[tf])
        floor = floor_to_bucket(now, tf)
        freshness_cutoff = now - width * settings.plugin.signal_freshness_bars
        freshness_bucket = floor_to_bucket(freshness_cutoff, tf)
        key = (meta.name, meta.content_hash, pair)
        # 鮮度窓を過ぎて評価されなくなった (自然放棄された) バケットの失敗記録。
        # cursor が無いまま窓が進んだ場合はループに入らないので、ここで落とす。
        self._release_failures(meta.name, pair, older_than=freshness_cutoff)
        cursor = self._cursor.get(key)
        start = (cursor + width if cursor is not None
                 else freshness_bucket)

        inserted = 0
        b = start
        while b < floor:
            if b < freshness_cutoff:
                # 窓外の古いバケット — 評価せず自然放棄 (cursor だけ進める)
                self._cursor[key] = b
                b += width
                continue
            try:
                inserted += self._evaluate_bucket(
                    conn, meta, pair, b, now=now, width=width, source=source,
                    call=call, settings=settings)
            except _InsufficientClosedBars as e:
                shortage = (meta.name, pair, source, meta.timeframe)
                if shortage not in self._insufficient_closed:
                    self._insufficient_closed.add(shortage)
                    _write_activity(
                        activity, "plugin_insufficient_closed_bars",
                        "consumer=%s source=%s interval=%s required=%d available=%d"
                        % (meta.name, source, meta.timeframe,
                           meta.max_bars, e.available),
                        meta.name)
                break
            except _BucketDataMissing as e:
                # 取り込みラグ等でデータが無いだけで plugin の失敗ではない。
                # 人間向けの activity にはせず、技術ログだけに残す。
                _log.warning(
                    "plugin %s (%s): evaluation failed at bucket %s (%s) — "
                    "cursor not advanced, retry next tick",
                    meta.name, pair, b.isoformat(), e)
                break
            except Exception as e:  # noqa: BLE001 — plugin 単位で fail-open
                self._report_failure(meta, pair, b, e, activity)
                break
            self._cursor[key] = b
            # 成功したら、この plugin × pair の失敗の間引き状態は解除する。
            self._release_failures(meta.name, pair, hash_=meta.content_hash)
            b += width
        return inserted

    def _release_failures(self, name: str, pair: str, *,
                          hash_: str | None = None,
                          older_than: datetime | None = None) -> None:
        """間引き状態を解除する。`hash_` は「その hash」、`older_than` は
        「それより古いバケット」を対象に絞る。"""
        for k in list(self._failure_notices):
            if k[0] != name or k[2] != pair:
                continue
            if hash_ is not None and k[1] != hash_:
                continue
            if older_than is not None and k[3] >= older_than:
                continue
            del self._failure_notices[k]

    def _report_failure(self, meta: PluginMeta, pair: str, bucket: datetime,
                        exc: Exception, activity) -> None:
        """評価失敗を固定分類にし、初回と 61・121…回目だけ activity と
        warning に出す。同じ失敗を毎 tick 出すと、人間の読む面が埋まる。"""
        result = _failure_result(exc)
        key = (meta.name, meta.content_hash, pair, bucket, result)
        count = self._failure_notices.get(key, 0) + 1
        self._failure_notices[key] = count
        if count % _NOTICE_INTERVAL != 1:
            return
        suffix = _sandbox_reason_suffix(result, exc)
        suffix += (f" suppressed_count={_NOTICE_INTERVAL}" if count > 1 else "")
        _log.warning(
            "plugin %s (%s): evaluation failed at bucket %s (%s) result=%s — "
            "cursor not advanced, retry next tick",
            meta.name, pair, bucket.isoformat(), exc, result)
        # 本文は固定分類だけ (例外文字列・stderr・パスは技術ログにのみ残す)。
        _write_activity(
            activity, "plugin_eval_failed",
            "consumer=%s pair=%s interval=%s bucket=%s result=%s%s"
            % (meta.name, pair, meta.timeframe, bucket.isoformat(),
               result, suffix),
            meta.name)

    def _evaluate_bucket(self, conn: "sqlite3.Connection", meta: PluginMeta,
                         pair: str, bucket_start: datetime, *, now: datetime,
                         width: timedelta, source: str, call: _CallFn,
                         settings: "Settings") -> int:
        bucket_end = bucket_start + width
        df = load_closed_frame(
            conn, pair, meta.timeframe, source, meta.max_bars, now,
            grace=timedelta(seconds=settings.datafeed.closed_bar_grace_sec),
        )
        # fix round 1 F3 (codex): df が非空でも「末尾行 = 対象バケット」と
        # は限らない — 取り込みラグ/欠損で対象バケット分の 1m 行が 1 本も
        # 無い場合、resample はそのバケットの行を生成せず、df の末尾は
        # それより古い (既に評価済みの) バケットのままになる。この状態を
        # 「評価完了」として扱い bar_ts=bucket_start の signal を偽造して
        # cursor を進めてしまうと、後から本物のバーが取り込まれても二度と
        # 再評価されない (cursor はバケット単位の単調増加のみ)。空 df と
        # 同じ扱い (fail-open・cursor を進めず次 tick 再試行) にする。
        if df.empty or df.index[-1] != bucket_start:
            raise _BucketDataMissing(
                f"target bucket not yet present in {source} data for "
                f"{pair} {meta.timeframe} bucket starting "
                f"{bucket_start.isoformat()}")

        # `load_resampled_frame` は「在る分だけ」を返すので、要求 max_bars に
        # 満たない窓でも上の fail-open 分岐には入らない (末尾バケットは
        # 存在する)。切り詰められた系列で指標が計算され signal がそのまま
        # 出るため、無音のまま品質が落ちる。承認時は削除されない履歴
        # テーブルで評価するので、この劣化は本番でしか現れない。窓が
        # 足りない主因はキャッシュ保持期間なので、設定名を出して知らせる。
        if len(df) < meta.max_bars:
            raise _InsufficientClosedBars(len(df))
        self._insufficient_closed.discard((meta.name, pair, source, meta.timeframe))

        bar_ts = bucket_start.isoformat()
        native_interval = "1h" if meta.timeframe in ("4h", "1d") else meta.timeframe
        provenance = {"live_source": source, "source": source,
                      "interval": meta.timeframe, "bar_time": bar_ts,
                      "bars_origin": f"{source}:{native_interval}"}
        if meta.kind == "signal":
            result = call(meta, {"df": df, "params": meta.params})
            inserted = 0
            for i, sig in enumerate(result["signals"]):
                row_id = signals.add(
                    conn, plugin=meta.name, content_hash=meta.content_hash,
                    pair=pair, timeframe=meta.timeframe, bar_ts=bar_ts,
                    kind="signal", payload={**sig, **provenance}, now=now)
                if row_id is not None:
                    inserted += 1
                elif i > 0:
                    # fix round 1 F4 (codex): UNIQUE (plugin, content_hash,
                    # pair, timeframe, bar_ts) は同一バケットで 1 出力しか
                    # 保持できない — DDL は §12 逐語のため変更しない
                    # (plan 由来の構造的制約として最終レビューに送る)。
                    # 2 出力目以降が dedupe で音もなく消えることだけは
                    # observability を確保する (i==0 は「本物の重複」との
                    # 区別がつかないため対象外)。
                    _log.warning(
                        "plugin %s (%s): signal[%d] at bar_ts=%s was "
                        "dropped by the (plugin, content_hash, pair, "
                        "timeframe, bar_ts) UNIQUE constraint — only the "
                        "first signal per bucket is stored",
                        meta.name, pair, i, bar_ts)
            return inserted

        # strategy: action="open" のみ保存 ("hold" は非保存)
        result = call(meta, {"df": df, "params": meta.params})
        if result.get("action") != "open":
            return 0
        row_id = signals.add(
            conn, plugin=meta.name, content_hash=meta.content_hash,
            pair=pair, timeframe=meta.timeframe, bar_ts=bar_ts,
            kind="strategy", payload={**result, **provenance}, now=now)
        return 1 if row_id is not None else 0


# 失敗通知の再通知間隔 (初回の次は 61・121… 回目)。内部定数。
_NOTICE_INTERVAL = 60

# SandboxError.code → live の固定分類。ここに無い code (検証失敗など
# 既定の backtest_failed を含む) は plugin 側の失敗として plugin_error にする。
_SANDBOX_CODE_TO_RESULT = {
    "timeout": "timeout",
    "cpu_limit": "cpu_limit",
    "crashed": "crashed",
    "plugin_error": "plugin_error",
    "protocol_error": "plugin_error",
    "sandbox_unavailable": "sandbox_unavailable",
}


def _write_activity(activity, event: str, summary: str, ref_id: str) -> None:
    """activity への書き込みの失敗で scheduler tick を落とさない。例外文字列は
    人間向けの面に出さないので、技術ログにも固定の文言だけ残す。"""
    if activity is None:
        return
    try:
        activity.write(Category.TECH, event, summary, ref_id=ref_id)
    except Exception:  # noqa: BLE001 — 記録の失敗で評価を止めない
        _log.warning("plugin %s: failed to write %s activity", ref_id, event)


def _sandbox_reason_suffix(result: str, exc: Exception) -> str:
    """`sandbox_unavailable` と SIGSYS による `crashed` にだけ固定 enum の reason を足す。"""
    reason = getattr(exc, "sandbox_reason", None)
    if not isinstance(exc, plugin_sandbox.SandboxError) or not isinstance(reason, str):
        return ""
    if result == "sandbox_unavailable" or (
            result == "crashed" and reason == "sigsys_unattributed"):
        return f" sandbox_reason={reason}"
    return ""


def _failure_result(exc: Exception) -> str:
    if isinstance(exc, plugin_sandbox.SandboxError):
        return _SANDBOX_CODE_TO_RESULT.get(
            getattr(exc, "code", None), "plugin_error")
    return "internal_error"


class _BucketDataMissing(ValueError):
    """対象バケットの確定足がまだ cache に無い (plugin の失敗ではない)。"""


class _InsufficientClosedBars(Exception):
    def __init__(self, available: int) -> None:
        self.available = available
