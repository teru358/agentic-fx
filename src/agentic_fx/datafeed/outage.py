"""MT5 (価格源) 不通の検出・永続化。

`ready`/`degraded` の 2 状態だけを扱う永続状態機械 (`backfilling` への
自動復旧・連続性検査・SL/TP replay は別モジュールで扱う対象で、ここには
含まない)。復旧は人間が明示コマンドで要求し、その要求は次の観測 tick で
判定・消費される。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agentic_fx.activity import Category
from agentic_fx.core import market_hours
from agentic_fx.core.timeutil import as_utc
from agentic_fx.store import ohlcv, orders


@dataclass(frozen=True, slots=True)
class IngestTickReport:
    """`Ingest.prepare()` が 1 tick ごとに使い捨てる、真に immutable な報告。

    - ``attempted``: この tick に実際にリクエストを試みた key。
    - ``succeeded``: 例外なく応答が返った key (0 本の応答も含む — 空応答か
      どうかは ``empty`` を見る)。
    - ``failed``: 例外が起きた key と、その tick 限りのエラー文字列の組。
      `Ingest.last_errors` (失敗した key が後で成功してもクリアされず残留
      する既存の再試行制御) とは独立 — 前回失敗して今回成功した key は
      ここには現れない。
    - ``deferred``: budget 切れで持ち越した判断足優先度の key (未試行)。
    - ``empty``: 成功したが 0 本だった key。

    ``attempted``/``failed``/``succeeded``/``deferred``/``empty`` の
    いずれにも無い key は「backoff 待ちで今回はそもそも probe しなかった」
    (not-attempted) と読む契約。
    """

    attempted: frozenset[tuple[str, str]]
    succeeded: frozenset[tuple[str, str]]
    failed: frozenset[tuple[tuple[str, str], str]]
    deferred: frozenset[tuple[str, str]]
    empty: frozenset[tuple[str, str]]


EMPTY_REPORT = IngestTickReport(
    attempted=frozenset(), succeeded=frozenset(), failed=frozenset(),
    deferred=frozenset(), empty=frozenset())


def _iso(dt: datetime) -> str:
    return as_utc(dt).isoformat()


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


class OutageStateMachine:
    """`ready`/`degraded` の永続状態機械。

    `datafeed_outage_state` (1 行、id=1) + `datafeed_outage_gap`
    (pair×interval×epoch) に永続化する。呼び出し側は core の書込みロック
    保持下・同一 SQLite 接続で `observe()` を呼ぶこと (commit と同じロック
    区間に置くことで、直前の commit で書かれた watermark を確実に読む)。
    ここでは自動復旧 (replay) を行わないため、`observe()` の内部処理と
    `ingest` 側の commit が別々の SQLite transaction であっても、両者の
    ずれが誤った巻き戻しを引き起こす害は無い (次 tick で watermark を
    読み直せば正しい状態に収束する)。
    """

    def __init__(self, conn, *, hard_keys, interval_widths: dict[str, timedelta],
                 grace: timedelta, storage_source: str, activity=None) -> None:
        self.conn = conn
        self.hard_keys = frozenset(hard_keys)
        self.interval_widths = dict(interval_widths)
        self.grace = grace
        self.storage_source = storage_source
        self.activity = activity
        # 「2 tick 連続」判定用のプロセス内カウンタ。`confirmed` フラグ自体は
        # DB に永続化されるが、再起動直後は最大 1 tick 分だけ再確認が遅れる
        # (confirmed は通知の重み付けにのみ使い、起動可否の判定には使わない)。
        self._problem_streak = 0
        # 各 hard key が「直近に確認できた試行」で succeeded (かつ 0 本では
        # ない) だったかどうかのプロセス内メモ。resume 判定専用 —
        # succeeded かつ非 empty で True、failed または empty (成功はした
        # が 0 本 = watermark が進んでいない) で False に更新し、
        # deferred/not_attempted (この tick ではそもそも試みなかった) では
        # 前回の値のまま据え置く。空応答を「据え置き」にすると、直前の
        # 非 empty 成功による True が残ったまま次の tick が empty でも
        # unconfirmed 扱いされず、停滞式の境界に達する前に resume が誤って
        # 受理されうる — 空応答は明示的に不健全側へ倒す。未知 (再起動直後を
        # 含む) は不健全側に倒すため、キーが無い場合は不健全として扱う。
        # ready → degraded (新 epoch 開始) の tick で全 key クリアする —
        # 前 episode の成功実績を今回の episode の証拠として残さないため。
        self._last_attempt_ok: dict[tuple[str, str], bool] = {}
        # 直近に activity へ出した未処理建玉の内訳 (pair 単位)。episode が
        # 開いている間、内訳が前回と変わった tick でだけ再送する — 通知専用
        # のプロセス内メモ (再起動後は「変化あり」扱いで 1 回出る)。
        # episode が閉じる (state == "ready") たびに None へ戻す。
        self._last_unprocessed_by_pair: dict[str, dict[str, int]] | None = None

    # ---- public -------------------------------------------------------

    @property
    def state(self) -> str:
        # observe() をまだ一度も呼んでいない (行が無い) 場合は既定の
        # 'ready' を返す — この読み出し専用アクセサ自体は行を作らない。
        return self._load_row().get("state", "ready")

    def status(self, *, conn=None) -> dict:
        """人間向け表示用のスナップショット。

        `conn` を渡すと `self.conn` (scheduler tick が core_lock 下で使う
        接続) の代わりにその接続で読む — シェルスレッド (`Commands`) は
        自身の接続 (`conn_shell`) を渡し、scheduler と同一接続を跨いで
        触らない。省略時は `self.conn`。
        """
        return dict(self._load_row(conn=conn))

    def gap_summary(self, now: datetime, *, conn=None) -> dict:
        """`Commands` の `status`/`data resume` 向けの人間可読スナップショット。

        現在の epoch について state・gap_start (pair×interval)・未処理建玉数
        (保守規則) をまとめて返す。読み取り専用 (行を作らない —
        `observe()` が一度も呼ばれていなければ既定値を返す)。

        `conn` は `status()` と同じ意味 — 省略時は `self.conn`。
        """
        now = as_utc(now)
        row = self._load_row(conn=conn)
        epoch = row.get("epoch", 0)
        gaps: dict[tuple[str, str], datetime] = {}
        for pair, interval in self.hard_keys:
            gs = self._gap_start(pair, interval, epoch, conn=conn)
            if gs is not None:
                gaps[(pair, interval)] = gs
        return {
            "state": row.get("state", "ready"),
            "epoch": epoch,
            "entered_degraded_at": row.get("entered_degraded_at"),
            "pending_human_confirmation": row.get("pending_human_confirmation", 0),
            "resume_requested_at": row.get("resume_requested_at"),
            "resume_acknowledge": row.get("resume_acknowledge", 0),
            "unprocessed_positions": self._unprocessed_position_count(now, epoch, conn=conn),
            "unprocessed_by_pair": self._unprocessed_positions_by_pair(now, epoch, conn=conn),
            "gap_starts": gaps,
        }

    def observe(self, now: datetime, report: IngestTickReport) -> str:
        """1 tick 分の観測を反映し、遷移後の `state` を返す。

        `report` は `Ingest.prepare()` が返した tick-local な報告。
        watermark は内部で `storage_source` 絞りで読み直す — 不通中の
        primary とは別の source (readonly healthcheck 経由の yfinance 等)
        の残存行を健全の根拠にしないため。
        """
        now = as_utc(now)
        self._ensure_row(now)
        row = self._load_row()
        if not market_hours.is_market_open(now):
            # 休場中は ingest 自体も due を立てない — 観測しない。
            return row["state"]

        watermarks = self._read_watermarks(now)
        hard_failed = {key for key, _err in report.failed if key in self.hard_keys}
        stalled = {key for key in self.hard_keys
                  if self._is_stalled(now, key[1], watermarks.get(key))}
        hard_problem = bool(hard_failed or stalled)

        if hard_problem and row["state"] == "ready":
            # 新しい episode (epoch) の開始 — 前 episode で他の key が
            # succeeded だった実績を今回の episode の証拠として残さない。
            # 全 hard key が新 epoch で少なくとも 1 回 succeeded を観測する
            # まで resume は拒否される。
            self._last_attempt_ok.clear()

        # resume 判定用のメモ更新 — この tick で succeeded かつ 0 本ではない
        # (empty ではない) と確認できた key は True、failed と確認できた
        # key、および succeeded だが 0 本 (empty) だった key は False に
        # 更新する。deferred/not_attempted (この tick ではそもそも試みな
        # かった) だけ前回の値のまま据え置く (0 本の応答は「成功」では
        # あっても watermark が進んでいないため、resume の健全証拠には
        # せず、次に非 empty 成功を観測するまで拒否する)。
        for key in self.hard_keys:
            if key in report.succeeded and key not in report.empty:
                self._last_attempt_ok[key] = True
            elif key in hard_failed or key in report.empty:
                self._last_attempt_ok[key] = False
        # 直近 tick で succeeded (かつ非 empty) と確認できていない key、
        # または今 tick で停滞式を満たした key は「resume 前提として未確認」
        # とみなす。
        unconfirmed_keys = frozenset(
            key for key in self.hard_keys
            if key in stalled or self._last_attempt_ok.get(key) is not True)

        state = row["state"]
        epoch = row["epoch"]
        confirmed = row["confirmed"]
        entered_degraded_at = row["entered_degraded_at"]
        recovered_notified_epoch = row.get("recovered_notified_epoch")
        was_degraded = state == "degraded"

        if hard_problem:
            self._problem_streak += 1
        else:
            self._problem_streak = 0

        if hard_problem and state == "ready":
            # 最初の失敗・停滞を検出したその tick のうちに遷移する — 2 回連続を
            # 待つと、その間に中間足の SL/TP 判定を永久に取りこぼし得る。
            epoch += 1
            entered_degraded_at = _iso(now)
            confirmed = 0
            state = "degraded"
            recovered_notified_epoch = None
            self._open_gaps(epoch, now, watermarks)
            self._write_activity(
                "datafeed_degraded",
                f"epoch={epoch} failed={sorted(hard_failed)} stalled={sorted(stalled)}")
        elif hard_problem and state == "degraded" and not confirmed and self._problem_streak >= 2:
            # 2 tick 連続の確認は通知の重み付けだけに使う — gate はすでに
            # 最初の tick から効いている。
            confirmed = 1
            self._write_activity(
                "data_outage_degraded",
                f"epoch={epoch} unprocessed_positions={self._unprocessed_position_count(now, epoch)}")
        elif not hard_problem and was_degraded and recovered_notified_epoch != epoch:
            # 復旧を検知したが、自動の連続性検査・replay を持たないため
            # 自動では ready に戻さない — 人間の明示コマンド待ち。
            recovered_notified_epoch = epoch
            self._write_activity(
                "datafeed_recovered_awaiting_resume",
                f"epoch={epoch} unprocessed_positions={self._unprocessed_position_count(now, epoch)}")
        # hard_problem が False でもここでは自動で ready に戻らない。

        self._save_state(state=state, epoch=epoch, confirmed=confirmed,
                          entered_degraded_at=entered_degraded_at,
                          recovered_notified_epoch=recovered_notified_epoch,
                          updated_at=_iso(now))

        if state == "degraded":
            by_pair = self._unprocessed_positions_by_pair(now, epoch)
            if by_pair != self._last_unprocessed_by_pair:
                self._last_unprocessed_by_pair = by_pair
                detail = format_unprocessed_positions(by_pair)
                if detail:
                    self._write_activity(
                        "data_outage_unprocessed_bars", f"epoch={epoch} {detail}")
        else:
            self._last_unprocessed_by_pair = None

        state = self._consume_resume_request(now, state, epoch, unconfirmed_keys)
        return state

    def request_resume(self, now: datetime, *, acknowledge: bool = False,
                       conn=None) -> None:
        """手動復旧コマンドから呼ばれる。state は直接書き換えず、要求を
        永続化するだけ — 呼び出し元は core の書込みロックを取らない別スレッド
        (人間向けシェル) を想定する。実際の判定・遷移は次 tick の
        `observe()` (単一のロック区間) の中で行う。これにより、シェル実行と
        scheduler tick の間に競合があっても、判定は常に commit 直後の
        report/watermark を使って行われる。

        `conn` を渡すと `self.conn` (scheduler tick 側の接続) の代わりに
        その接続で書く — `Commands` は自身の接続 (`conn_shell`) を渡し、
        scheduler と同一の SQLite 接続オブジェクトを複数スレッドから
        叩かない。省略時は `self.conn`。
        """
        now = as_utc(now)
        c = conn if conn is not None else self.conn
        with c:
            self._ensure_row(now, conn=c)
            c.execute(
                "UPDATE datafeed_outage_state SET resume_requested_at=?, "
                "resume_acknowledge=? WHERE id=1",
                (_iso(now), 1 if acknowledge else 0))

    # ---- internal: persistence ----------------------------------------

    def _ensure_row(self, now: datetime, conn=None) -> None:
        c = conn if conn is not None else self.conn
        with c:
            row = c.execute(
                "SELECT 1 FROM datafeed_outage_state WHERE id=1").fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO datafeed_outage_state "
                    "(id, state, epoch, confirmed, entered_degraded_at, "
                    "ready_streak, pending_human_confirmation, "
                    "resume_requested_at, resume_acknowledge, updated_at) "
                    "VALUES (1, 'ready', 0, 0, NULL, 0, 0, NULL, 0, ?)",
                    (_iso(now),))

    def _load_row(self, conn=None) -> dict:
        c = conn if conn is not None else self.conn
        row = c.execute(
            "SELECT * FROM datafeed_outage_state WHERE id=1").fetchone()
        return dict(row) if row is not None else {}

    def _save_state(self, *, state: str, epoch: int, confirmed: int,
                    entered_degraded_at: str | None,
                    recovered_notified_epoch: int | None,
                    updated_at: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE datafeed_outage_state SET state=?, epoch=?, "
                "confirmed=?, entered_degraded_at=?, "
                "recovered_notified_epoch=?, updated_at=? WHERE id=1",
                (state, epoch, confirmed, entered_degraded_at,
                 recovered_notified_epoch, updated_at))

    def _open_gaps(self, epoch: int, now: datetime,
                   watermarks: dict[tuple[str, str], datetime | None]) -> None:
        """episode 突入時、各 (pair, interval) の `gap_start` を記録する。

        `gap_start` = 直前 tick で succeeded だった直近の watermark
        (= この tick の observe 時点で DB にまだ残っている最新の確定足)。
        一度もバーが無い key は `now` を仮の起点にする (登録直後の pair 等、
        必要本数からの計算開始点で厳密化する余地は残る — ここでは
        gap_start 列の NOT NULL 制約を安全側の値で満たすことを優先する)。
        """
        with self.conn:
            for key in self.hard_keys:
                pair, interval = key
                exists = self.conn.execute(
                    "SELECT 1 FROM datafeed_outage_gap WHERE pair=? AND "
                    "interval=? AND epoch=?", (pair, interval, epoch)).fetchone()
                if exists is not None:
                    continue
                gap_start = watermarks.get(key) or now
                self.conn.execute(
                    "INSERT INTO datafeed_outage_gap "
                    "(pair, interval, epoch, gap_start, replay_through) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (pair, interval, epoch, _iso(gap_start)))

    def _gap_start(self, pair: str, interval: str, epoch: int,
                  conn=None) -> datetime | None:
        c = conn if conn is not None else self.conn
        row = c.execute(
            "SELECT gap_start FROM datafeed_outage_gap WHERE pair=? AND "
            "interval=? AND epoch=?", (pair, interval, epoch)).fetchone()
        return _parse(row["gap_start"]) if row is not None else None

    # ---- internal: watermark / stall -----------------------------------

    def _read_watermarks(self, now: datetime) -> dict[tuple[str, str], datetime | None]:
        watermarks: dict[tuple[str, str], datetime | None] = {}
        for pair, interval in self.hard_keys:
            width = self.interval_widths[interval]
            watermarks[(pair, interval)] = ohlcv.latest_closed_cache_bar_time(
                self.conn, pair, interval, now=now, width=width, grace=self.grace,
                source=self.storage_source)
        return watermarks

    def _is_stalled(self, now: datetime, interval: str,
                    watermark: datetime | None) -> bool:
        """`expected = watermark + 2×足幅 + grace` を過ぎた最初の tick で
        停滞と判定する単一の式 (ingest 内部の処理時間予算などを重ねて
        二重に猶予を加算しない)。
        """
        if watermark is None:
            # データが一度も無い key は「不通」ではなく「未整備」— 停滞式の
            # 対象外にする (ingest 自体の失敗は report.failed 側で拾う)。
            return False
        width = self.interval_widths[interval]
        expected = watermark + 2 * width + self.grace
        return now > expected

    # ---- internal: unprocessed positions --------------------------------

    def _unprocessed_positions_by_pair(self, now: datetime, epoch: int,
                                       conn=None) -> dict[str, dict[str, int]]:
        c = conn if conn is not None else self.conn
        return unprocessed_positions_by_pair(
            c, hard_keys=self.hard_keys, epoch=epoch,
            gap_start_fn=lambda pair, interval: self._gap_start(pair, interval, epoch, conn=c),
            now=now, storage_source=self.storage_source, grace=self.grace,
            interval_widths=self.interval_widths)

    def _unprocessed_position_count(self, now: datetime, epoch: int,
                                    conn=None) -> int:
        c = conn if conn is not None else self.conn
        return count_unprocessed_positions(
            c, hard_keys=self.hard_keys, epoch=epoch,
            gap_start_fn=lambda pair, interval: self._gap_start(pair, interval, epoch, conn=c),
            now=now, storage_source=self.storage_source, grace=self.grace,
            interval_widths=self.interval_widths)

    # ---- internal: resume request consumption --------------------------

    def _consume_resume_request(self, now: datetime, state: str, epoch: int,
                                unconfirmed_keys: frozenset) -> str:
        row = self._load_row()
        requested_at = row.get("resume_requested_at")
        if requested_at is None:
            return state
        acknowledge = bool(row.get("resume_acknowledge"))
        if state == "ready":
            # 既に ready — 要求は無意味なので消費して終える。
            self._clear_resume_request()
            return state
        watermark_healthy = not unconfirmed_keys
        unprocessed_by_pair = self._unprocessed_positions_by_pair(now, epoch)
        unprocessed = sum(v["positions"] for v in unprocessed_by_pair.values())
        detail = format_unprocessed_positions(unprocessed_by_pair)
        if watermark_healthy and (unprocessed == 0 or acknowledge):
            state = "ready"
            self._last_unprocessed_by_pair = None
            self._save_state(state=state, epoch=epoch, confirmed=0,
                             entered_degraded_at=row.get("entered_degraded_at"),
                             recovered_notified_epoch=row.get("recovered_notified_epoch"),
                             updated_at=_iso(now))
            self._write_activity(
                "data_resume_accepted",
                f"epoch={epoch} unprocessed_positions={unprocessed}"
                + (f" ({detail})" if detail else "")
                + f" acknowledge={acknowledge}")
        else:
            reason = []
            if not watermark_healthy:
                reason.append("watermark_unhealthy")
                unconfirmed_text = ",".join(
                    f"{pair}:{interval}"
                    for pair, interval in sorted(unconfirmed_keys))
                reason.append(f"unconfirmed_keys={unconfirmed_text}")
            if unprocessed and not acknowledge:
                reason.append(
                    f"unprocessed_positions={unprocessed}"
                    + (f" ({detail})" if detail else ""))
            self._write_activity(
                "data_resume_rejected", f"epoch={epoch} reason={','.join(reason)}")
        self._clear_resume_request()
        return state

    def _clear_resume_request(self) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE datafeed_outage_state SET resume_requested_at=NULL, "
                "resume_acknowledge=0 WHERE id=1")

    # ---- internal: activity --------------------------------------------

    def _write_activity(self, event: str, summary: str) -> None:
        if self.activity is not None:
            self.activity.write(Category.SYSTEM, event, summary)


def unprocessed_positions_by_pair(conn, *, hard_keys, epoch: int, gap_start_fn,
                                  now: datetime, storage_source: str,
                                  grace: timedelta,
                                  interval_widths: dict[str, timedelta]
                                  ) -> dict[str, dict[str, int]]:
    """保守的な近似規則: 不通区間内の対象足は、その pair の全 OPEN 建玉に
    ついて無条件に未処理扱いとする。各建玉の下限は
    ``max(next_expected_trading_time(gap_start), filled_at)``、
    `PENDING_FILL` の下限は `created_at`。

    建玉別の適用 cursor を持たないため、「実際に適用済みかどうか」は判定
    できない — この関数は安全側に倒した近似 (未処理の可能性がある建玉を
    数える) であり、正確な適用済み判定はより精密な cursor を持つ後続の
    仕組みに委ねる。

    返り値は pair ごとの内訳 ``{"positions": 建玉数, "bars": 未処理と
    推定される 1m 足本数の下限}``。``bars`` は pair 内の建玉ごとに
    ``(latest_confirmed - floor) // width_1m + 1`` を計算し、その pair の
    最大値を採る (保守的な近似の枠内で、人間が読める粗い目安として十分)。
    """
    pairs_1m = sorted({pair for pair, interval in hard_keys if interval == "1m"})
    width_1m = interval_widths.get("1m", timedelta(minutes=1))
    result: dict[str, dict[str, int]] = {}
    for pair in pairs_1m:
        gap_start = gap_start_fn(pair, "1m")
        if gap_start is None:
            continue
        lower_bound = market_hours.next_expected_trading_time(gap_start, width_1m)
        latest_confirmed = ohlcv.latest_closed_cache_bar_time(
            conn, pair, "1m", now=now, width=width_1m, grace=grace,
            source=storage_source)
        if latest_confirmed is None:
            continue
        positions = 0
        max_bars = 0
        for row in orders.list_by_status(conn, "open"):
            if row["pair"] != pair:
                continue
            filled_at = _parse(row.get("filled_at"))
            floor = max(lower_bound, filled_at) if filled_at else lower_bound
            if floor <= latest_confirmed:
                positions += 1
                bars = (latest_confirmed - floor) // width_1m + 1
                max_bars = max(max_bars, bars)
        for row in orders.list_by_status(conn, "pending_fill"):
            if row["pair"] != pair:
                continue
            created_at = _parse(row.get("created_at"))
            floor = max(lower_bound, created_at) if created_at else lower_bound
            if floor <= latest_confirmed:
                positions += 1
                bars = (latest_confirmed - floor) // width_1m + 1
                max_bars = max(max_bars, bars)
        if positions:
            result[pair] = {"positions": positions, "bars": max_bars}
    return result


def format_unprocessed_positions(by_pair: dict[str, dict[str, int]]) -> str:
    """`unprocessed_positions_by_pair` の結果を人間可読の内訳文字列にする
    (`""` = 該当なし)。"""
    if not by_pair:
        return ""
    return ", ".join(
        f"{pair}: {v['positions']} positions / ~{v['bars']} bars"
        for pair, v in sorted(by_pair.items()))


def count_unprocessed_positions(conn, *, hard_keys, epoch: int, gap_start_fn,
                                now: datetime, storage_source: str,
                                grace: timedelta,
                                interval_widths: dict[str, timedelta]) -> int:
    """`unprocessed_positions_by_pair` の合計件数だけを返す薄い wrapper
    (既存呼び出し元との互換維持用)。"""
    by_pair = unprocessed_positions_by_pair(
        conn, hard_keys=hard_keys, epoch=epoch, gap_start_fn=gap_start_fn,
        now=now, storage_source=storage_source, grace=grace,
        interval_widths=interval_widths)
    return sum(v["positions"] for v in by_pair.values())
