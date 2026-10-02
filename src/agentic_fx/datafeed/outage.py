"""MT5 (価格源) 不通の検出・永続化。

`ready` / `restricted` / `degraded` の 3 状態を扱う永続状態機械。
`restricted` は建玉も未約定指値も無いときの 1m 停滞を新規リスク停止だけで
受ける状態 (期限内に健全な tick が続けば自動で ready へ戻る)。`degraded` は
従来どおりの不通状態で、復旧は自動 (flat かつ設定が許すとき) か、人間が
明示コマンドで要求してその要求が次の観測 tick で判定・消費される。
`backfilling` への自動復旧・連続性検査・SL/TP replay は別モジュールで扱う
対象で、ここには含まない。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agentic_fx.activity import Category
from agentic_fx.core.executor import _EXPOSURE
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


@dataclass(frozen=True, slots=True)
class _Outcome:
    """1 tick の遷移の計算結果。commit が成功した後にだけ使う値を運ぶ。"""

    state: str
    epoch: int
    activities: list
    attempt_ok: dict
    problem_streak: int
    unconfirmed: frozenset


def _iso(dt: datetime) -> str:
    return as_utc(dt).isoformat()


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


class OutageStateMachine:
    """`ready` / `restricted` / `degraded` の永続状態機械。

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
                 grace: timedelta, storage_source: str, activity=None,
                 ready_confirm_ticks: int = 3,
                 auto_resume_when_flat: bool = True,
                 flat_stall_max_sec: int = 1800) -> None:
        self.conn = conn
        self.hard_keys = frozenset(hard_keys)
        self.interval_widths = dict(interval_widths)
        self.grace = grace
        self.storage_source = storage_source
        self.activity = activity
        self.ready_confirm_ticks = ready_confirm_ticks
        self.auto_resume_when_flat = auto_resume_when_flat
        self.flat_stall_max_sec = flat_stall_max_sec
        # 継続した健全 tick はプロセスをまたいで数えない。
        self._reset_ready_streak = True
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
        # ready → restricted/degraded (新 epoch 開始) の tick では、その tick の
        # 取得結果を記録する前に全 key をクリアする — 前 episode の成功実績を
        # 今回の episode の証拠として残さず、当 tick の結果だけを残すため。
        self._last_attempt_ok: dict[tuple[str, str], bool] = {}
        # 直近に activity へ出した未処理建玉の内訳 (pair 単位)。episode が
        # 開いている間、内訳が前回と変わった tick でだけ再送する — 通知専用
        # のプロセス内メモ (再起動後は「変化あり」扱いで 1 回出る)。
        # episode が閉じる (state == "ready") たびに None へ戻す。
        self._last_unprocessed_by_pair: dict[str, dict[str, int]] | None = None
        # observe() が例外で抜けた間 True (次に observe が正常に完了すると下りる)。
        # 失敗の原因が state/gap の保存そのものだと DB の行は書き換えられない
        # ので、行が古い ready のままでも新規リスクが通らないよう、state の読み口
        # だけを degraded に倒す印にする。永続化しないのは、再起動後は DB の
        # state に従えばよく、次の observe が同じ失敗をすればこの印が再び立つ
        # (その間に動く新規リスクの窓は 1 tick 分以下) ため。
        self._observe_failed = False

    # ---- public -------------------------------------------------------

    @property
    def state(self) -> str:
        # observe() をまだ一度も呼んでいない (行が無い) 場合は既定の
        # 'ready' を返す — この読み出し専用アクセサ自体は行を作らない。
        # 全 gate はこの読み口を見るので、直近の observe が失敗している間は
        # 行の値にかかわらず新規リスクを止める側 (degraded) を返す。
        if self._observe_failed:
            return "degraded"
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
            "observe_failed": self._observe_failed,
            "epoch": epoch,
            "entered_degraded_at": row.get("entered_degraded_at"),
            "restricted_since": row.get("restricted_since"),
            "restricted_deadline_at": row.get("restricted_deadline_at"),
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

        state/gap の commit に至らず例外で抜けたら `state` の読み口が
        degraded を返す印を立て、次に commit まで完了するまで下ろさない。
        commit 後の例外 (activity 書き込み等) は DB が確定済みなので印を
        立てない。
        """
        now = as_utc(now)
        try:
            outcome = self._commit_observation(now, report)
        except Exception:
            self._observe_failed = True
            raise
        self._observe_failed = False
        if isinstance(outcome, str):
            return outcome
        # commit 済み。プロセス内の記録は commit が成功した後にだけ進める
        # (失敗した tick の観測を次の tick の判定に持ち越さない)。
        self._last_attempt_ok = outcome.attempt_ok
        self._problem_streak = outcome.problem_streak
        state, epoch = outcome.state, outcome.epoch
        for event, summary in outcome.activities:
            self._write_activity(event, summary)
        if state == "degraded":
            by_pair = self._unprocessed_positions_by_pair(now, epoch)
            if by_pair != self._last_unprocessed_by_pair:
                self._last_unprocessed_by_pair = by_pair
                detail = format_unprocessed_positions(by_pair)
                if detail:
                    self._write_activity("data_outage_unprocessed_bars", f"epoch={epoch} {detail}")
        else:
            self._last_unprocessed_by_pair = None
        return self._consume_resume_request(now, state, epoch, outcome.unconfirmed)

    def _commit_observation(self, now: datetime, report: IngestTickReport):
        """休場なら現在の state (str)、そうでなければ commit 済みの遷移結果。"""
        # observe は自分の transaction を所有する。呼び出し元の未確定の書き込みが
        # ある状態で入ると、それを黙って commit してしまうので入口で拒否する。
        if self.conn.in_transaction:
            raise RuntimeError(
                "observe は自分の transaction を所有する: 呼び出し元の未確定の"
                "書き込みがある状態では呼べない")
        # row の作成・streak のリセット・flat 判定に使う exposure と現在の state の
        # 読み取り・遷移の書き込みを 1 つの write transaction に入れる。読んだ後に
        # 別接続が建玉を作る隙間をなくし、途中で落ちたら全部元に戻す。
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._insert_row_if_missing(self.conn, now)
            if self._reset_ready_streak:
                self.conn.execute(
                    "UPDATE datafeed_outage_state SET ready_streak=0 WHERE id=1")
            if not market_hours.is_market_open(now):
                # 休場中は ingest 自体も due を立てない — 観測しない。
                result = self._load_row()["state"]
            else:
                result = self._observe_open(now, report, self._load_row())
            self.conn.commit()
        except BaseException:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise
        self._reset_ready_streak = False
        return result

    def _observe_open(self, now: datetime, report: IngestTickReport,
                      row: dict) -> "_Outcome":
        """遷移を計算して state/gap を書く (呼び出し側の write transaction の中)。
        インスタンス属性は触らず、commit 後に反映する値を返す。"""
        attempt_ok = dict(self._last_attempt_ok)
        problem_streak = self._problem_streak
        watermarks = self._read_watermarks(now)
        hard_failed = {key for key, _ in report.failed if key in self.hard_keys}
        hard_empty = self.hard_keys & report.empty
        stalled = {key for key in self.hard_keys
                   if self._is_stalled(now, key[1], watermarks.get(key))}
        expected = {key: self._expected(key[1], watermarks[key]) for key in stalled}
        flat = not orders.list_by_status(self.conn, *_EXPOSURE)
        immediate = bool(hard_failed or hard_empty)
        if row["state"] == "ready" and (immediate or stalled):
            attempt_ok.clear()
        for key in self.hard_keys:
            if key in report.succeeded and key not in report.empty:
                attempt_ok[key] = True
            elif key in hard_failed or key in hard_empty:
                attempt_ok[key] = False
        # 回復の根拠として「この tick に succeeded かつ非 empty」を要求するのは
        # 1m の hard key だけ (1m が無い構成では全 key)。上位足は取得の周期が
        # 長く、取りに行かない tick (deferred / not-attempted) が普通にあるので、
        # この tick で failed / empty / stalled でない限り前回の結果を据え置く。
        gate_keys = frozenset(key for key in self.hard_keys if key[1] == "1m") or self.hard_keys
        healthy = (not hard_failed and not hard_empty and not stalled
                   and all(key in report.succeeded and key not in report.empty
                           for key in gate_keys)
                   and all(attempt_ok.get(key) is not False
                           for key in self.hard_keys - gate_keys))
        unconfirmed = frozenset(
            key for key in self.hard_keys
            if key in stalled or attempt_ok.get(key) is not True)
        state = row["state"]
        epoch = row["epoch"]
        confirmed = row["confirmed"]
        ready_streak = row.get("ready_streak", 0)
        entered = row["entered_degraded_at"]
        restricted_since = row.get("restricted_since")
        deadline = row.get("restricted_deadline_at")
        pending = row.get("pending_human_confirmation", 0)
        recovered_notified_epoch = row.get("recovered_notified_epoch")
        activities: list[tuple[str, str]] = []
        opened = False

        def degrade() -> None:
            nonlocal state, entered, restricted_since, deadline, ready_streak, pending
            was_restricted = state == "restricted"
            state = "degraded"
            entered = _iso(now)
            if not was_restricted:
                restricted_since = None
                deadline = None
            ready_streak = 0
            pending = int(bool(pending) or not flat or not self.auto_resume_when_flat)

        def degraded_activity() -> tuple[str, str]:
            return ("datafeed_degraded",
                    f"epoch={epoch} failed={sorted(hard_failed)} "
                    f"empty={sorted(hard_empty)} stalled={sorted(stalled)}")

        def recovered_activity() -> tuple[str, str]:
            return ("datafeed_recovered_auto",
                    f"epoch={epoch} streak={self.ready_confirm_ticks}")

        if state == "ready" and (immediate or stalled):
            epoch += 1
            confirmed = 0
            recovered_notified_epoch = None
            opened = True
            origin = min(expected.values()) if expected else None
            # 停滞を初めて見た時点で既に期限を過ぎているとき (長い停滞の途中での
            # 再起動、tick の大幅な遅延) は、restricted を経由させず直接 degraded。
            past_deadline = (
                origin is not None and self.flat_stall_max_sec > 0
                and now > origin + timedelta(seconds=self.flat_stall_max_sec))
            if immediate or not flat or self.flat_stall_max_sec == 0 or past_deadline:
                degrade()
                activities.append(degraded_activity())
            else:
                state = "restricted"
                ready_streak = 0
                restricted_since = _iso(origin)
                deadline = _iso(origin + timedelta(seconds=self.flat_stall_max_sec))
                activities.append(("datafeed_restricted", f"epoch={epoch} stalled={sorted(stalled)} expected={_iso(origin)} deadline={deadline}"))
        elif state == "restricted":
            if (deadline is not None and now > _parse(deadline)) or immediate or not flat:
                degrade()
                activities.append(degraded_activity())
            elif healthy:
                ready_streak += 1
                if ready_streak >= self.ready_confirm_ticks:
                    if self.auto_resume_when_flat and not pending:
                        state = "ready"
                        confirmed = 0
                        ready_streak = 0
                        restricted_since = None
                        deadline = None
                        activities.append(recovered_activity())
                    else:
                        # 復帰はしないが健全に戻っている — 人の resume 待ちとして
                        # 同じ tick で知らせる。
                        degrade()
                        activities.append(degraded_activity())
                        if recovered_notified_epoch != epoch:
                            recovered_notified_epoch = epoch
                            activities.append(("datafeed_recovered_awaiting_resume", f"epoch={epoch}"))
            else:
                ready_streak = 0
        elif state == "degraded":
            if healthy and flat and self.auto_resume_when_flat and not pending:
                ready_streak += 1
                if ready_streak >= self.ready_confirm_ticks:
                    state = "ready"
                    confirmed = 0
                    ready_streak = 0
                    restricted_since = None
                    deadline = None
                    activities.append(recovered_activity())
            else:
                ready_streak = 0
                if healthy and (not flat or not self.auto_resume_when_flat) and recovered_notified_epoch != epoch:
                    recovered_notified_epoch = epoch
                    activities.append(("datafeed_recovered_awaiting_resume", f"epoch={epoch}"))

        if immediate or stalled:
            problem_streak += 1
            if state == "degraded" and not confirmed and problem_streak >= 2:
                confirmed = 1
                activities.append(("data_outage_degraded", f"epoch={epoch} unprocessed_positions={self._unprocessed_position_count(now, epoch)}"))
        else:
            problem_streak = 0

        if opened:
            self._open_gaps(epoch, now, watermarks)
        self._save_state(state=state, epoch=epoch, confirmed=confirmed,
                         entered_degraded_at=entered,
                         restricted_since=restricted_since,
                         restricted_deadline_at=deadline,
                         pending_human_confirmation=pending,
                         recovered_notified_epoch=recovered_notified_epoch,
                         ready_streak=ready_streak, updated_at=_iso(now))
        return _Outcome(state=state, epoch=epoch, activities=activities,
                        attempt_ok=attempt_ok, problem_streak=problem_streak,
                        unconfirmed=unconfirmed)

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
            self._insert_row_if_missing(c, now)

    def _insert_row_if_missing(self, c, now: datetime) -> None:
        """commit はしない。呼び出し側の transaction の中で使う。"""
        row = c.execute(
            "SELECT 1 FROM datafeed_outage_state WHERE id=1").fetchone()
        if row is None:
            c.execute(
                "INSERT INTO datafeed_outage_state "
                "(id, state, epoch, confirmed, entered_degraded_at, "
                "restricted_since, restricted_deadline_at, "
                "ready_streak, pending_human_confirmation, "
                "resume_requested_at, resume_acknowledge, updated_at) "
                "VALUES (1, 'ready', 0, 0, NULL, NULL, NULL, 0, 0, NULL, 0, ?)",
                (_iso(now),))

    def _load_row(self, conn=None) -> dict:
        c = conn if conn is not None else self.conn
        row = c.execute(
            "SELECT * FROM datafeed_outage_state WHERE id=1").fetchone()
        return dict(row) if row is not None else {}

    def _save_state(self, *, state: str, epoch: int, confirmed: int,
                    entered_degraded_at: str | None,
                    restricted_since: str | None = None,
                    restricted_deadline_at: str | None = None,
                    pending_human_confirmation: int,
                    recovered_notified_epoch: int | None,
                    ready_streak: int,
                    updated_at: str) -> None:
        """呼び出し側の `with self.conn:` の中で使う (自前では commit しない)。"""
        self.conn.execute(
            "UPDATE datafeed_outage_state SET state=?, epoch=?, confirmed=?, "
            "entered_degraded_at=?, restricted_since=?, restricted_deadline_at=?, "
            "pending_human_confirmation=?, recovered_notified_epoch=?, "
            "ready_streak=?, updated_at=? WHERE id=1",
            (state, epoch, confirmed, entered_degraded_at, restricted_since,
             restricted_deadline_at, pending_human_confirmation,
             recovered_notified_epoch, ready_streak, updated_at))

    def _open_gaps(self, epoch: int, now: datetime,
                   watermarks: dict[tuple[str, str], datetime | None]) -> None:
        """episode 突入時、各 (pair, interval) の `gap_start` を記録する。

        `gap_start` = 直前 tick で succeeded だった直近の watermark
        (= この tick の observe 時点で DB にまだ残っている最新の確定足)。
        一度もバーが無い key は `now` を仮の起点にする (登録直後の pair 等、
        必要本数からの計算開始点で厳密化する余地は残る — ここでは
        gap_start 列の NOT NULL 制約を安全側の値で満たすことを優先する)。
        """
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
        """`expected = next_bar_confirmation(watermark, 足幅, grace)` を過ぎた最初の tick で
        停滞と判定する単一の式 (ingest 内部の処理時間予算などを重ねて
        二重に猶予を加算しない)。
        """
        if watermark is None:
            # データが一度も無い key は「不通」ではなく「未整備」— 停滞式の
            # 対象外にする (ingest 自体の失敗は report.failed 側で拾う)。
            return False
        width = self.interval_widths[interval]
        expected = market_hours.next_bar_confirmation(watermark, width, self.grace)
        return now > expected

    def _expected(self, interval: str, watermark: datetime) -> datetime:
        return market_hours.next_bar_confirmation(
            watermark, self.interval_widths[interval], self.grace)

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
            with self.conn:
                self._clear_resume_request()
            return state
        watermark_healthy = not unconfirmed_keys
        unprocessed_by_pair = self._unprocessed_positions_by_pair(now, epoch)
        unprocessed = sum(v["positions"] for v in unprocessed_by_pair.values())
        detail = format_unprocessed_positions(unprocessed_by_pair)
        if watermark_healthy and (unprocessed == 0 or acknowledge):
            state = "ready"
            self._last_unprocessed_by_pair = None
            # state の更新と要求の消去は 1 つの transaction。途中で落ちたら
            # どちらも元のまま残り、次 tick が同じ要求を判定し直す。
            with self.conn:
                self._save_state(state=state, epoch=epoch, confirmed=0,
                                 entered_degraded_at=row.get("entered_degraded_at"),
                                 restricted_since=None,
                                 restricted_deadline_at=None,
                                 pending_human_confirmation=0,
                                 recovered_notified_epoch=row.get("recovered_notified_epoch"),
                                 ready_streak=0,
                                 updated_at=_iso(now))
                self._clear_resume_request()
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
            with self.conn:
                self._clear_resume_request()
            self._write_activity(
                "data_resume_rejected", f"epoch={epoch} reason={','.join(reason)}")
        return state

    def _clear_resume_request(self) -> None:
        """呼び出し側の `with self.conn:` の中で使う (自前では commit しない)。"""
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
