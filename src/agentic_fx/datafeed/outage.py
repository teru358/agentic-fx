"""MT5 (価格源) 不通の検出・永続化。

`ready` / `restricted` / `degraded` の 3 状態を扱う永続状態機械。遷移の規則は純関数
`outage_transition` (outage_transition.py) にあり、ここは facts の読取りと保存だけを持つ。

`restricted` は 1m の停滞・取得失敗・空を、新規リスク・指値約定・リスク増の停止だけで受ける状態
(決済・取消・既存の SL/TP の保護は続く)。猶予は停滞の起点から開場秒で数え、建玉があれば 600 秒、
全 exposure ゼロなら設定の `flat_stall_max_sec`。後続の足が届いている穴は停滞に数えず、足を合成しない。
足が届けば同じ tick で ready に戻る。`degraded` は猶予超過・restricted の連続 30 分超・接触の検出などの
停止で、復旧は自動 (flat・自動復帰が有効・接触待ちなしで、水位が進む健全 tick が続いたとき) か、人間が
明示コマンドで要求してその要求が次の観測 tick で判定・消費される。

停止中 (non-ready) に届いた足と、穴への後着足は、生存期間が重なる注文の保護価格・指値に触れたかを
検査する (接触の真偽だけで、約定・決済・損益は書き換えない)。接触・不明は人の確認待ちにし、
確認 (ack) があるまで自動では戻さない。`backfilling` への自動復旧・SL/TP replay は別モジュールで扱う
対象で、ここには含まない。
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agentic_fx.activity import Category
from agentic_fx.core import display_time
from agentic_fx.core.contracts import OrderStatus
from agentic_fx.core.executor import _EXPOSURE
from agentic_fx.core import market_hours
from agentic_fx.core.timeutil import as_utc
from agentic_fx.datafeed import notice_rules, outage_inspect, outage_transition
from agentic_fx.datafeed.guidance_text import UNKNOWN_COUNT
from agentic_fx.store import ohlcv, orders, outage_facts

# 永続状態機械の取りうる state。web の状態の印の割当表 (afx-ops) はこの語彙を写して持ち、test が突き合わせる
DATA_STATES = ("ready", "restricted", "degraded")

# 通知の記録の状態語彙。Discord の本文は afx-ops 側が固定語で描く
NOTICE_STATES = ("stopped", "awaiting_resume", "resume_accepted",
                 "resume_rejected", "resume_already_ready", "resumed")
# 要求の終端結果。consume 時に state と同じ transaction で残す
RESUME_RESULTS = ("accepted", "rejected_unprocessed", "rejected_watermark", "already_ready")

log = logging.getLogger(__name__)

_PENDING_NOTICE_LIMIT = 64
# 前回の observe からの開場時間がこれを超えたら観測が途切れたとみなす (scheduler の 2 tick 分)
_INTERRUPTION_SEC = 120


def _no_spread(pair: str) -> float:
    return 0.0


_KNOWN_STATUSES = tuple(status.value for status in OrderStatus)


def unknown_guidance() -> dict:
    """案内を読めないときの値 (state は含めない)。件数はゼロにせず unknown。"""
    return {"phase": "unknown", "next_action": "check_status",
            "unprocessed_positions": UNKNOWN_COUNT, "resume_last_result": None,
            "resume_last_result_at": None, "auto_resume_possible": False}


@dataclass(frozen=True, slots=True)
class GuidanceFacts:
    """案内を決める、observe が保存した現在の facts。API で健全を計算し直さない。

    `auto_possible`: flat・自動復帰が有効・接触待ちも移行確認待ちも無い (人の操作なしに戻り得る)。
    `auto_healthy` / `manual_healthy`: observe が最後に保存した自動復帰・手動再開の健全 (未観測は None)。
    """

    auto_possible: bool
    auto_healthy: bool | None
    manual_healthy: bool | None


def resume_guidance(*, observe_failed: bool, state: str | None, request_pending: bool,
                    last_result: str | None, awaiting_mark: bool,
                    unprocessed: int | None,
                    facts: GuidanceFacts | None = None,
                    startup_wait: bool = False) -> tuple[str, str]:
    """status に出す (phase, next_action)。上の行ほど優先する真理値表で、この 1 関数だけが決める。

    `state` が None は読取り失敗、`unprocessed` が None は件数を読めないこと (ゼロとみなさない)。
    `last_result` は現在の epoch の直近の終端結果、`awaiting_mark` は同 epoch に再開待ちを
    知らせた印。件数は ack の要否を決める行でだけ使い、読めなければ不明に倒す。
    `facts` があれば、restricted / degraded は保存した現在の facts で決め、古い拒否・待ちの印は
    見ない (別欄で出す)。無ければ従来どおり印と直近の終端結果で決める。
    """
    unknown = ("unknown", "check_status")
    if observe_failed or state is None:
        return unknown
    if startup_wait:
        return ("starting", "wait_tick")
    if request_pending:
        return ("resume_requested", "wait_tick")
    if state == "ready":
        return ("ready", "none")
    if state not in ("restricted", "degraded"):
        return unknown
    if facts is not None:
        if state == "restricted":
            return ("restricted", "wait_tick")
        if facts.auto_possible:
            if facts.auto_healthy is True:
                return ("recovering", "wait_tick")
            return ("stopped", "restore_source")
        if facts.manual_healthy is True:
            if unprocessed is None:
                return unknown
            return ("awaiting_resume", "resume_with_ack" if unprocessed else "resume")
        return ("stopped", "restore_source")
    if last_result == "rejected_watermark":
        return ("resume_rejected", "restore_source")
    if last_result == "rejected_unprocessed":
        if unprocessed is None:
            return unknown
        return ("resume_rejected", "resume_with_ack" if unprocessed else "resume")
    if state == "degraded":
        if awaiting_mark:
            if unprocessed is None:
                return unknown
            return ("awaiting_resume", "resume_with_ack" if unprocessed else "resume")
        return ("stopped", "restore_source")
    if not awaiting_mark:
        return ("restricted", "wait_tick")
    return unknown


class _RequestChanged(Exception):
    """読んだ後に要求が置き換えられた。判定は捨て、次の tick が新しい要求を判定する。"""


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
    - ``soft_*``: hard の要求が無い key (参照系列など) だけの同名の集合。
      ``attempted`` 以下の集合には soft key を決して入れない。soft の失敗や
      空応答は outage の判定に使われない。``soft_backoff_waiting`` は
      backoff 待ちで今回は probe しなかった soft key。

    ``attempted``/``failed``/``succeeded``/``deferred``/``empty`` の
    いずれにも無い key は「backoff 待ちで今回はそもそも probe しなかった」
    (not-attempted) と読む契約。
    """

    attempted: frozenset[tuple[str, str]]
    succeeded: frozenset[tuple[str, str]]
    failed: frozenset[tuple[tuple[str, str], str]]
    deferred: frozenset[tuple[str, str]]
    empty: frozenset[tuple[str, str]]
    soft_attempted: frozenset[tuple[str, str]] = frozenset()
    soft_succeeded: frozenset[tuple[str, str]] = frozenset()
    soft_failed: frozenset[tuple[tuple[str, str], str]] = frozenset()
    soft_deferred: frozenset[tuple[str, str]] = frozenset()
    soft_empty: frozenset[tuple[str, str]] = frozenset()
    soft_backoff_waiting: frozenset[tuple[str, str]] = frozenset()
    # hard 1m key の固定窓の取得 (成功した key だけ) の取得前水位・水位より新しい足の時刻・
    # 取得前水位以前で同 source の cache に無かった足 (後着)。いずれも key 順・時刻順の tuple
    pre_watermarks: tuple = ()
    new_times: tuple = ()
    late_bars: tuple = ()


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
    notices: list = field(default_factory=list)
    # 保存・読取りの障害で gate を閉じた tick (要求の判定は次の tick へ送る)
    blocked: bool = False


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
                 flat_stall_max_sec: int = 1800,
                 startup_recovery: bool = False,
                 spread_fn=None) -> None:
        self.conn = conn
        self.hard_keys = frozenset(hard_keys)
        self.interval_widths = dict(interval_widths)
        self.grace = grace
        self.storage_source = storage_source
        self.activity = activity
        self.ready_confirm_ticks = ready_confirm_ticks
        self.auto_resume_when_flat = auto_resume_when_flat
        self.flat_stall_max_sec = flat_stall_max_sec
        # 接触検査の spread (価格単位)。構築後に scheduler の値へ差し替えられる
        self.spread_fn = spread_fn if spread_fn is not None else _no_spread
        # ingest と組んで動く構成だけ真。最初の開場 observe が commit するまで、新規リスク・
        # 指値約定・自動復帰・要求の消費を待たせ、保存済みの停止は保存した水位の後から検査する
        self._startup_recovery = startup_recovery
        self._startup_wait = startup_recovery
        # プロセス内で最初の開場 observe を終えるまで真 (観測の途切れの判定に使う)
        self._first_observe = True
        # 失敗の記憶を保存から復元したか
        self._attempt_restored = False
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
        # observe が自分で開いた transaction を rollback し損ねて接続に残した間
        # True。入口の「transaction 中は拒否」が自分の残骸にも当たって恒久的に
        # non-ready になるのを避けるため、この印があるときだけ入口で片付け直す
        # (呼び出し元の transaction には印が立たず、従来どおり拒否する)。
        self._own_transaction_left_open = False
        # 記録に失敗した通知の保留 (epoch, 発生時刻, entries)。次の tick の冒頭で再試行する。
        # プロセスが落ちれば消える (state commit 後の crash と同じ限界)
        self._pending_notices: list[tuple[int, datetime, list[tuple[str, str]]]] = []

    # ---- public -------------------------------------------------------

    def release_startup_wait(self) -> None:
        """観測が来ない構成 (価格取得を持たない) は、起動待ちを掛けない。"""
        self._startup_wait = False

    @property
    def state(self) -> str:
        # observe() をまだ一度も呼んでいない (行が無い) 場合は既定の
        # 'ready' を返す — この読み出し専用アクセサ自体は行を作らない。
        # 全 gate はこの読み口を見るので、直近の observe が失敗している間は
        # 行の値にかかわらず新規リスクを止める側 (degraded) を返す。
        if self._observe_failed or self._startup_wait:
            return "degraded"
        return self._load_row().get("state", "ready")

    def state_from(self, conn) -> str:
        """`state` と同じ値を、渡された接続だけで読む (`self.conn` には触れない)。

        API thread の status など、scheduler の接続を跨げない読み手向け。
        """
        if self._observe_failed or self._startup_wait:
            return "degraded"
        return self._load_row(conn=conn).get("state", "ready")

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
            "resume_last_result": row.get("resume_last_result"),
            "resume_last_result_at": row.get("resume_last_result_at"),
            "unprocessed_positions": self._ack_required(now, epoch, conn=conn)[0],
            "unprocessed_by_pair": self._unprocessed_positions_by_pair(now, epoch, conn=conn),
            "gap_starts": gaps,
            "contacts": [(c.order_id, c.bar_time, c.kind)
                         for c in outage_facts.list_contacts(
                             conn if conn is not None else self.conn, epoch=epoch)],
            "migration_confirmation_pending": bool(
                row.get("migration_confirmation_pending")),
        }

    def guidance_snapshot(self, now: datetime, *, conn=None) -> dict:
        """API の status `data` に足す案内の値。読み取り専用 (行を作らない)。

        state・phase・次の操作は 1 回読んだ同じ行から作る。未処理建玉数は別の表への
        別の読取りで、行の読取りとの間に tick が遷移すれば 1 回の表示でずれ得る
        (ack の要否は人が建玉を確認してから決める前提)。state は `state_from` と同じ値
        (観測の保存に失敗中は degraded)。行が読めないときは state を含めない
        (呼び出し側が自分の読みに戻す)。件数は ready の行では数えず、読めなければ
        "unknown" を返し、どちらもゼロとはみなさない。`conn` は `status()` と同じ意味で、
        API thread は自分の読取り接続を渡す。
        """
        now = as_utc(now)
        try:
            row = self._load_row(conn=conn)
        except Exception:  # noqa: BLE001 — 読めないものは不明として返す
            return unknown_guidance()
        epoch = row.get("epoch", 0)
        result = row.get("resume_last_result")
        state = row.get("state", "ready")
        unprocessed: int | None = None
        if state in ("restricted", "degraded") or result in ("rejected_watermark",
                                                            "rejected_unprocessed"):
            try:
                unprocessed = self._ack_required(now, epoch, conn=conn)[0]
            except Exception:  # noqa: BLE001 — 読めない件数をゼロにしない
                unprocessed = None
        marked = row.get("recovered_notified_epoch") == epoch
        try:
            facts = self._guidance_facts(row, conn) if state in ("restricted", "degraded") else None
        except Exception:  # noqa: BLE001 — 必須の facts を読めなければ不明として返す
            return unknown_guidance()
        phase, action = resume_guidance(
            observe_failed=self._observe_failed, state=state,
            request_pending=row.get("resume_requested_at") is not None,
            last_result=result, awaiting_mark=marked, unprocessed=unprocessed, facts=facts,
            startup_wait=self._startup_wait)
        try:
            contacts = [{"order_id": c.order_id, "bar_time": _iso(c.bar_time), "kind": c.kind}
                        for c in outage_facts.list_contacts(
                            conn if conn is not None else self.conn, epoch=epoch)]
        except Exception:  # noqa: BLE001 — 参照を読めなければ案内も不明として返す
            return unknown_guidance()
        return {"state": "degraded" if (self._observe_failed or self._startup_wait)
                else row.get("state", "ready"),
                "phase": phase, "next_action": action, "contacts": contacts,
                "unprocessed_positions": UNKNOWN_COUNT if unprocessed is None else unprocessed,
                "resume_last_result": result,
                "resume_last_result_at": row.get("resume_last_result_at"),
                "auto_resume_possible": self._may_resume_unattended(row, conn)}

    def _guidance_facts(self, row: dict, conn) -> GuidanceFacts:
        """案内に使う保存済みの facts。読めなければ例外 (呼び出し側が不明として返す)。"""
        c = conn if conn is not None else self.conn
        health = outage_facts.load_health(c)
        epoch = row.get("epoch", 0)
        contacts = outage_facts.list_contacts(c, epoch=epoch)
        exposure = orders.list_by_status(c, *_EXPOSURE)
        auto_possible = (self.auto_resume_when_flat is True and not exposure and not contacts
                         and not self._has_unknown_status(c)
                         and not row.get("migration_confirmation_pending"))
        return GuidanceFacts(auto_possible=auto_possible, auto_healthy=health.auto_healthy,
                             manual_healthy=health.manual_healthy)

    def _may_resume_unattended(self, row: dict, conn) -> bool:
        """停止中の epoch が、建玉が無く接触待ち・移行確認待ちも無く自動復帰が有効で、人の操作なしに
        戻り得るか。確認できた値だけで真にする: 設定は厳密な True、建玉・接触の読取りは空の list。
        欠落・型違い・読取り失敗は False (戻るとは言わない)。"""
        if row.get("state") != "degraded" or self.auto_resume_when_flat is not True:
            return False
        migration = row.get("migration_confirmation_pending")
        if type(migration) is not int or migration != 0:
            return False
        try:
            c = conn if conn is not None else self.conn
            exposure = orders.list_by_status(c, *_EXPOSURE)
            contacts = outage_facts.list_contacts(c, epoch=row.get("epoch", 0))
            unknown = self._has_unknown_status(c)
        except Exception:  # noqa: BLE001 — 読めないものは戻るとは言わない
            return False
        return type(exposure) is list and not exposure and not contacts and not unknown

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
        # 入口の検査と自分の transaction の commit を通った後。呼び出し元の
        # transaction には触らず、観測の遷移も遅らせない
        self._retry_pending_notices()
        if isinstance(outcome, str):
            return outcome
        # commit 済み。プロセス内の記録は commit が成功した後にだけ進める
        # (失敗した tick の観測を次の tick の判定に持ち越さない)。
        self._first_observe = False
        self._startup_wait = False
        self._attempt_restored = True
        self._last_attempt_ok = outcome.attempt_ok
        self._problem_streak = outcome.problem_streak
        state, epoch = outcome.state, outcome.epoch
        # 通知の記録は activity の書込みより先に。activity の例外で記録を飛ばさない
        self._record_notices(epoch, now, outcome.notices)
        if state == "degraded":
            self._record_timed_stop(epoch, now)
        for event, summary in outcome.activities:
            self._write_activity(event, summary)
        if outcome.blocked:
            return state
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
        if self._own_transaction_left_open:
            # 残骸が既に無ければ (接続側で閉じられた) 何もせず印だけ下ろす。
            # 他人の transaction を巻き込まないため、印が立っていないときは触らない。
            # rollback が失敗すれば例外で抜ける (印は残り、次の tick でまた試す)。
            if self.conn.in_transaction:
                self.conn.rollback()
            self._own_transaction_left_open = False
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
                # 休場中は ingest 自体も due を立てない — 観測しない。連続の健全 tick は 0 に
                # 戻し、停滞の起点・gap の起点・restricted の起点は保持する
                self.conn.execute(
                    "UPDATE datafeed_outage_state SET ready_streak=0 WHERE id=1")
                result = self._load_row()["state"]
            else:
                result = self._observe_open(now, report, self._load_row())
            self.conn.commit()
        except BaseException:
            if self.conn.in_transaction:
                try:
                    self.conn.rollback()
                except Exception:
                    self._own_transaction_left_open = True
            raise
        self._reset_ready_streak = False
        return result

    def _observe_open(self, now: datetime, report: IngestTickReport,
                      row: dict) -> "_Outcome":
        """遷移を計算して state/gap を書く (呼び出し側の write transaction の中)。
        インスタンス属性は触らず、commit 後に反映する値を返す。

        読取り (facts・注文・足) → `outage_transition` (純関数) → 保存、の順。状態・接触参照・
        後着の表の消費・facts は同じ transaction で確定する。"""
        attempt_ok = dict(self._last_attempt_ok)
        problem_streak = self._problem_streak
        epoch0 = row["epoch"]
        read_error = False
        try:
            key_facts = outage_facts.load_key_facts(self.conn, self.storage_source)
            contacts_now = outage_facts.list_contacts(self.conn, epoch=epoch0)
            flat = self._is_flat(self.conn)
        except Exception:  # noqa: BLE001 — 読めない必須状態は gate を閉じる側に倒す
            log.warning("datafeed_state_unreadable")
            key_facts, contacts_now, flat, read_error = {}, [], False, True
        if not self._attempt_restored:
            # 失敗の記憶だけを復元する。成功は新しい試行で確認し直す (手動再開の健全は緩めない)
            for key, kf in key_facts.items():
                if key in self.hard_keys and kf.last_attempt_ok is False:
                    attempt_ok.setdefault(key, False)
        watermarks = self._read_watermarks(now)
        hard_failed = {key for key, _ in report.failed if key in self.hard_keys}
        hard_empty = self.hard_keys & report.empty
        one_m = frozenset(key for key in self.hard_keys if key[1] == "1m") or self.hard_keys
        views: list[outage_transition.KeyView] = []
        stall_starts: dict[tuple[str, str], datetime | None] = {}
        for key in sorted(self.hard_keys):
            mark = watermarks.get(key)
            stalled = self._is_stalled(now, key[1], mark)
            if key in hard_failed:
                status = outage_transition.STATUS_FAILED
            elif key in hard_empty:
                status = outage_transition.STATUS_EMPTY
            elif key in report.succeeded:
                status = outage_transition.STATUS_OK
            else:
                status = outage_transition.STATUS_IDLE
            kf = key_facts.get(key)
            same_run = kf is not None and kf.stall_start is not None and kf.observed_through == mark
            carried = kf.stall_start if same_run else None
            if stalled:
                base = self._expected(key[1], mark)
                start = min(carried, base) if carried is not None else base
            elif status in (outage_transition.STATUS_FAILED, outage_transition.STATUS_EMPTY):
                start = carried if carried is not None else now
            elif status == outage_transition.STATUS_IDLE and carried is not None:
                start = carried
            else:
                start = None
            stall_starts[key] = start
            views.append(outage_transition.KeyView(key, key in one_m, status, stalled, start))
        any_problem = any(v.problem for v in views)
        stalled_keys = {v.key for v in views if v.stalled}
        if row["state"] == "ready" and any_problem:
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
        gate_keys = one_m
        healthy = (not hard_failed and not hard_empty and not stalled_keys
                   and all(key in report.succeeded and key not in report.empty
                           for key in gate_keys)
                   and all(attempt_ok.get(key) is not False
                           for key in self.hard_keys - gate_keys))
        unconfirmed = frozenset(
            key for key in self.hard_keys
            if key in stalled_keys or attempt_ok.get(key) is not True)
        prev_updated = _parse(row.get("updated_at"))
        advanced = prev_updated is None or now > prev_updated
        if advanced and all(key in key_facts for key in one_m):
            advanced = any(watermarks.get(key) != key_facts[key].observed_through
                           for key in one_m)
        gap_break = (prev_updated is not None
                     and market_hours.open_time_between(prev_updated, now).total_seconds()
                     > _INTERRUPTION_SEC)
        interrupted = self._first_observe or gap_break
        old_starts: list[datetime] = []
        if interrupted:
            for key in self.hard_keys:
                kf = key_facts.get(key)
                if kf is None:
                    continue
                if kf.stall_start is not None:
                    old_starts.append(kf.stall_start)
                elif gap_break and kf.observed_through is not None:
                    # 観測が規定を超えて途切れた間の停滞は、保存した水位の次の確認時刻から数える
                    old_starts.append(self._expected(key[1], kf.observed_through))
        late_found: list = []
        late_keys: list = []
        n_first: list = []
        n_latest: list = []
        if not read_error:
            try:
                late_found, late_keys = outage_inspect.late_bar_findings(
                    self.conn, self.storage_source, self.spread_fn)
                if row["state"] != "ready" or interrupted:
                    ranges = {key[0]: (key_facts[key].observed_through, watermarks[key])
                              for key in self.hard_keys
                              if key[1] == "1m" and key in key_facts
                              and watermarks.get(key) is not None}
                    n_first, n_latest = outage_inspect.new_bar_findings(
                        self.conn, self.storage_source, ranges, self.spread_fn, now)
            except Exception:  # noqa: BLE001 — 検査できなかった足は消費せず、gate を閉じる
                log.warning("datafeed_contact_check_unreadable")
                read_error, late_found, late_keys, n_first, n_latest = True, [], [], [], []
        migration_pending = bool(row.get("migration_confirmation_pending"))
        if (not read_error and self._startup_recovery and self._first_observe
                and row["state"] != "ready"
                and any(key not in key_facts for key in self.hard_keys)):
            # 保存した観測済みの水位が無い既存の停止は、現在の cache で検査済みにせず人が確認する
            migration_pending = True
        epoch = row["epoch"]
        known_stop = bool(row["confirmed"]) or row.get("recovered_notified_epoch") == epoch
        noticed = known_stop
        if not noticed and row["state"] != "ready":
            noticed = (self._stop_recorded(epoch)
                       or any(e == epoch and ("stopped", "") in items
                              for e, _t, items in self._pending_notices))
        facts = outage_transition.Facts(
            now=now, flat=flat, keys=tuple(views), healthy=healthy, advanced=advanced,
            old_stall_starts=tuple(old_starts), read_error=read_error,
            contact_wait=bool(contacts_now), migration_pending=migration_pending,
            late_contacts=tuple(late_found), n_contacts=tuple(n_first),
            n_latest_contacts=tuple(n_latest), noticed=noticed,
            auto_resume=self.auto_resume_when_flat,
            ready_confirm_ticks=self.ready_confirm_ticks,
            flat_stall_max_sec=self.flat_stall_max_sec, problem_streak=problem_streak)
        next_row, effects = outage_transition.outage_transition(row, facts)
        migration_pending = migration_pending or effects.set_migration
        epoch = next_row["epoch"]
        if effects.opened:
            self._open_gaps(epoch, self._gap_origins(key_facts, report, watermarks))
            self.conn.execute(
                "UPDATE datafeed_outage_state SET resume_last_result=NULL, "
                "resume_last_result_at=NULL, resume_last_request_at=NULL WHERE id=1")
        self._save_state(updated_at=_iso(now), **next_row)
        self.conn.execute(
            "UPDATE datafeed_outage_state SET migration_confirmation_pending=? WHERE id=1",
            (int(migration_pending),))
        for item in effects.new_contacts:
            outage_facts.add_contact(
                self.conn, epoch=epoch, order_id=item.order_id, pair=item.pair,
                bar_time=item.bar_time, kind=item.kind, detected_at=now)
        if late_keys:
            outage_facts.delete_late_bars(self.conn, self.storage_source, late_keys)
        self._save_facts(now, watermarks, stall_starts, attempt_ok,
                         auto_healthy=healthy, manual_healthy=not unconfirmed,
                         keep_watermarks=read_error)
        notices = list(effects.notices)
        if self._first_observe and contacts_now:
            # 接触の安全状態を commit した後、通知の保存の前に落ちていた場合の再照合
            # (同じ鍵は重複しない)
            notices.append(("stopped", outage_transition.CONTACT_NOTICE_KEY))
        activities = list(effects.activities)
        if effects.confirmed_stop:
            activities.append((
                "data_outage_degraded",
                f"epoch={epoch} unprocessed_positions={self._unprocessed_position_count(now, epoch)}"))
        return _Outcome(state=next_row["state"], epoch=epoch, activities=activities,
                        attempt_ok=attempt_ok, problem_streak=effects.problem_streak,
                        unconfirmed=unconfirmed, notices=notices,
                        blocked=effects.blocked)

    def _is_flat(self, conn) -> bool:
        """全 pair を通じて exposure がゼロで、未知の status の注文も無い。

        数量が 0 でも非終端なら flat ではない。未知の status は終端と証明できないので flat にしない。"""
        return not orders.list_by_status(conn, *_EXPOSURE) and not self._has_unknown_status(conn)

    @staticmethod
    def _has_unknown_status(conn) -> bool:
        marks = ",".join("?" * len(_KNOWN_STATUSES))
        return conn.execute(
            f"SELECT 1 FROM orders WHERE status NOT IN ({marks}) LIMIT 1",
            _KNOWN_STATUSES).fetchone() is not None

    def _gap_origins(self, key_facts: dict, report: IngestTickReport,
                     watermarks: dict) -> dict[tuple[str, str], datetime | None]:
        """epoch を開く tick の gap_start。この tick の取得より前の水位 (前回 observe 済みの水位と
        取得前の水位の古い方) を使う。一括到着の足を起点より後に置かないため。無ければ現在の水位、
        それも無ければ NULL (水位なしを now で代用しない)。"""
        before = dict(report.pre_watermarks)
        origins: dict[tuple[str, str], datetime | None] = {}
        for key in self.hard_keys:
            kf = key_facts.get(key)
            candidates = [t for t in (kf.observed_through if kf else None, before.get(key))
                          if t is not None]
            origins[key] = min(candidates) if candidates else watermarks.get(key)
        return origins

    def request_resume(self, now: datetime, *, acknowledge: bool = False,
                       conn=None) -> dict:
        """手動復旧コマンドから呼ばれる。state は直接書き換えず、要求を
        永続化するだけ — 呼び出し元は core の書込みロックを取らない別スレッド
        (人間向けシェル) を想定する。実際の判定・遷移は次 tick の
        `observe()` (単一のロック区間) の中で行う。これにより、シェル実行と
        scheduler tick の間に競合があっても、判定は常に commit 直後の
        report/watermark を使って行われる。

        未消費の要求の有無を read→判定→更新の 1 つの書込み transaction で決め、
        結果を返す。``outcome`` は次のいずれか。

        - ``new``: 未消費の要求が無く、新しい要求時刻で記録した。
        - ``existing``: 未消費の要求があり、同じ ack、または ack ありの要求に対する
          ack なし。要求時刻と ack は変えない (ack を取り消さない)。
        - ``replaced``: ack なしの要求を ack ありの新しい要求へ置き換えた。

        ``epoch`` は記録時の epoch、``acknowledge`` は要求が持つ有効な ack。

        `conn` を渡すと `self.conn` (scheduler tick 側の接続) の代わりに
        その接続で書く — `Commands` は自身の接続 (`conn_shell`) を渡し、
        scheduler と同一の SQLite 接続オブジェクトを複数スレッドから
        叩かない。省略時は `self.conn`。
        """
        now = as_utc(now)
        c = conn if conn is not None else self.conn
        with c:
            if not c.in_transaction:
                c.execute("BEGIN IMMEDIATE")
            self._insert_row_if_missing(c, now)
            row = dict(c.execute(
                "SELECT * FROM datafeed_outage_state WHERE id=1").fetchone())
            current = row.get("resume_requested_at")
            current_ack = bool(row.get("resume_acknowledge"))
            epoch = row["epoch"]
            if current is not None and (current_ack or not acknowledge):
                return {"outcome": "existing", "epoch": epoch,
                        "acknowledge": current_ack}
            stamp = self._unused_request_key(c, now, epoch, row, current)
            c.execute(
                "UPDATE datafeed_outage_state SET resume_requested_at=?, "
                "resume_acknowledge=? WHERE id=1",
                (stamp, 1 if acknowledge else 0))
            return {"outcome": "new" if current is None else "replaced",
                    "epoch": epoch, "acknowledge": bool(acknowledge)}

    @staticmethod
    def _unused_request_key(c, now: datetime, epoch: int, row: dict,
                            current: str | None) -> str:
        """要求時刻は終端結果の対応キーにもなるので、既に使った値と衝突させない。"""
        used = {current, row.get("resume_last_request_at")}
        try:
            used.update(key for (key,) in c.execute(
                "SELECT request_key FROM datafeed_outage_notices WHERE request_key<>''"))
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            # 通知の表が無い DB でも要求の記録は止めない
        stamp = now
        while _iso(stamp) in used:
            stamp += timedelta(microseconds=1)
        return _iso(stamp)

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

    def _save_facts(self, now: datetime, watermarks: dict, stall_starts: dict,
                    attempt_ok: dict, *, auto_healthy: bool, manual_healthy: bool,
                    keep_watermarks: bool = False) -> None:
        """この tick で見た水位・停滞の起点・直近の試行の成否・健全の真偽を保存する。
        状態の保存と同じ transaction の中 (commit は呼び出し側)。`keep_watermarks` は
        検査を完了できなかった tick で、observe 済みの水位を進めない (次の tick が同じ範囲を
        検査し直す) ための指定で、key ごとの facts は書かない。"""
        if not keep_watermarks:
            for pair, interval in self.hard_keys:
                key = (pair, interval)
                outage_facts.save_key_facts(
                    self.conn, pair, interval, self.storage_source,
                    observed_through=watermarks.get(key), stall_start=stall_starts.get(key),
                    last_attempt_ok=attempt_ok.get(key), now=now)
        outage_facts.save_health(self.conn, auto_healthy=auto_healthy,
                                 manual_healthy=manual_healthy, now=now)

    def _open_gaps(self, epoch: int,
                   origins: dict[tuple[str, str], datetime | None]) -> None:
        """episode 突入時、各 (pair, interval) の `gap_start` を記録する。

        `gap_start` は epoch の固定の起点 (呼び出し側が取得前の水位から決める)。一度も足が
        無い key は NULL のまま記録し、現在時刻で代用しない。"""
        for key in self.hard_keys:
            pair, interval = key
            exists = self.conn.execute(
                "SELECT 1 FROM datafeed_outage_gap WHERE pair=? AND "
                "interval=? AND epoch=?", (pair, interval, epoch)).fetchone()
            if exists is not None:
                continue
            gap_start = origins.get(key)
            self.conn.execute(
                "INSERT INTO datafeed_outage_gap "
                "(pair, interval, epoch, gap_start, replay_through, source) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (pair, interval, epoch,
                 None if gap_start is None else _iso(gap_start), self.storage_source))

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

    def _ack_required(self, now: datetime, epoch: int, conn=None):
        """再開の ack が確認する対象。(件数, 内訳 by_pair, 接触の参照, 移行確認待ち)。

        件数は、未処理の足がある建玉と接触・不明の注文参照の和集合 (同じ注文は 1 件) に、
        移行確認待ちを 1 件足した値。"""
        c = conn if conn is not None else self.conn
        by_pair = self._unprocessed_positions_by_pair(now, epoch, conn=c)
        ids = unprocessed_order_ids(
            c, hard_keys=self.hard_keys,
            gap_start_fn=lambda pair, interval: self._gap_start(pair, interval, epoch, conn=c),
            now=now, storage_source=self.storage_source, grace=self.grace,
            interval_widths=self.interval_widths)
        contacts = outage_facts.list_contacts(c, epoch=epoch)
        migration = bool(self._load_row(conn=c).get("migration_confirmation_pending"))
        total = len(ids | {x.order_id for x in contacts}) + (1 if migration else 0)
        return total, by_pair, contacts, migration

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
        try:
            return self._consume_read_request(now, state, epoch, unconfirmed_keys)
        except _RequestChanged:
            return state

    def _consume_read_request(self, now: datetime, state: str, epoch: int,
                              unconfirmed_keys: frozenset) -> str:
        row = self._load_row()
        requested_at = row.get("resume_requested_at")
        if requested_at is None:
            return state
        acknowledge = bool(row.get("resume_acknowledge"))
        if state == "ready":
            # 既に ready — 要求は無意味なので消費して終える。結果は残す。
            with self.conn:
                self._finish_resume_request(now, requested_at, acknowledge, "already_ready")
            self._record_notices(epoch, now, [("resume_already_ready", requested_at)])
            return state
        watermark_healthy = not unconfirmed_keys
        unprocessed, unprocessed_by_pair, contact_refs, migration = self._ack_required(now, epoch)
        detail = format_unprocessed_positions(unprocessed_by_pair)
        if contact_refs:
            detail += ("; " if detail else "") + f"接触・不明 {len(contact_refs)} 件"
        if migration:
            detail += ("; " if detail else "") + "移行確認待ち"
        if watermark_healthy and (unprocessed == 0 or acknowledge):
            state = "ready"
            # state の更新と要求の消去・結果の記録は 1 つの transaction。途中で落ちたら
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
                # 接触・不明の確認は ack で済んだ。参照と移行確認待ちは同じ transaction で外す
                self.conn.execute(
                    "DELETE FROM datafeed_outage_contacts WHERE epoch=?", (epoch,))
                self.conn.execute(
                    "UPDATE datafeed_outage_state SET migration_confirmation_pending=0 "
                    "WHERE id=1")
                self._finish_resume_request(now, requested_at, acknowledge, "accepted")
            self._last_unprocessed_by_pair = None
            self._record_notices(epoch, now, [("resume_accepted", requested_at),
                                              ("resumed", "")])
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
            # 両方に該当するときは watermark の拒否を優先する
            result = "rejected_watermark" if not watermark_healthy else "rejected_unprocessed"
            with self.conn:
                self._finish_resume_request(now, requested_at, acknowledge, result)
            self._record_notices(epoch, now, [("resume_rejected", requested_at)])
            self._write_activity(
                "data_resume_rejected", f"epoch={epoch} reason={','.join(reason)}")
        return state

    def _finish_resume_request(self, now: datetime, requested_at: str,
                               acknowledge: bool, result: str) -> None:
        """呼び出し側の `with self.conn:` の中で使う (自前では commit しない)。
        要求を消費し、その終端結果・判定時刻・対象の要求時刻を残す。

        判定の根拠にした要求が今も同じ値のときだけ消費する。読んだ後に置き換えられて
        いたら例外で transaction ごと戻し (state の保存も結果の記録も残さない)、
        次の tick が置換後の要求を判定する。"""
        cur = self.conn.execute(
            "UPDATE datafeed_outage_state SET resume_last_result=?, "
            "resume_last_result_at=?, resume_last_request_at=? "
            "WHERE id=1 AND resume_requested_at=? AND resume_acknowledge=?",
            (result, _iso(now), requested_at, requested_at, 1 if acknowledge else 0))
        if cur.rowcount != 1:
            raise _RequestChanged()
        self._clear_resume_request()

    def _clear_resume_request(self) -> None:
        """呼び出し側の `with self.conn:` の中で使う (自前では commit しない)。"""
        self.conn.execute(
            "UPDATE datafeed_outage_state SET resume_requested_at=NULL, "
            "resume_acknowledge=0 WHERE id=1")

    # ---- internal: notice records --------------------------------------

    def _record_notices(self, epoch: int, now: datetime,
                        entries: list[tuple[str, str]]) -> None:
        """通知の記録を state/gap と要求消費の transaction の外で書く。

        失敗しても状態更新や保護処理を止めない (例外は握って固定語をログに残す)。
        失敗した分はメモリに保留し、次の tick の冒頭で同じ書込みを再試行する。
        書くのは固定語・整数・要求時刻だけ。同じ鍵は `ON CONFLICT DO NOTHING` で重複させない。
        """
        if entries and not self._write_notices(epoch, now, entries):
            # 保留に同じ (epoch, 状態, 要求キー) があれば重ねない。書込みが続けて失敗する間に
            # 同じ記録が積まれて、保留の上限で別の結果の記録を押し出さないため
            queued = {(e, kind, key) for e, _t, items in self._pending_notices
                      for kind, key in items}
            fresh = [(kind, key) for kind, key in entries if (epoch, kind, key) not in queued]
            if fresh:
                self._pending_notices.append((epoch, now, fresh))
                self._trim_pending_notices()

    def _stop_recorded(self, epoch: int) -> bool:
        """この epoch の停止の記録があるか。読めなければ無いとみなす (観測を止めない)。"""
        try:
            return self.conn.execute(
                "SELECT 1 FROM datafeed_outage_notices WHERE epoch=? AND notice_state='stopped'",
                (epoch,)).fetchone() is not None
        except Exception:  # noqa: BLE001 - 通知の記録が読めなくても観測は続ける
            return False

    def _record_timed_stop(self, epoch: int, now: datetime) -> None:
        """連続の問題観測 (confirmed) に届かない停止を、開場時間の経過で一度だけ記録する。

        同じ epoch に待ちの印・停止か待ちの記録があれば何もしない。再開の要求が未消費の間は、
        人が操作中なので知らせない。要求への結果が出ていれば、その判定時刻から開場時間を
        数え直す (この epoch でまだ停止を知らせていないときだけ。知らせた後は結果の通知で足りる)。開場中の観測 tick の commit 後だけで呼ぶ。停止の判定・遷移は変えず、失敗しても例外を出さない。"""
        try:
            row = self._load_row()
            if row.get("state") != "degraded" or row.get("recovered_notified_epoch") == epoch:
                return
            if row.get("resume_requested_at") is not None:
                return
            if self.conn.execute(
                    "SELECT 1 FROM datafeed_outage_notices WHERE epoch=? "
                    "AND notice_state IN ('stopped', 'awaiting_resume')",
                    (epoch,)).fetchone() is not None:
                return
            start = notice_rules.timer_start(row.get("entered_degraded_at"),
                                             row.get("resume_last_result_at"))
            if not notice_rules.stop_after_open_minutes(start, now):
                return
        except Exception:  # noqa: BLE001 - 通知の判断が読めなくても観測は続ける
            log.warning("datafeed_notice_deferred")
            return
        self._record_notices(epoch, now, [("stopped", "")])

    def _write_notices(self, epoch: int, now: datetime,
                       entries: list[tuple[str, str]]) -> bool:
        try:
            with self.conn:
                for notice_state, request_key in entries:
                    self.conn.execute(
                        "INSERT INTO datafeed_outage_notices "
                        "(epoch, notice_state, request_key, occurred_at, event_id) "
                        "VALUES (?, ?, ?, ?, NULL) "
                        "ON CONFLICT (epoch, notice_state, request_key) DO NOTHING",
                        (epoch, notice_state, request_key, _iso(now)))
            return True
        except Exception:  # noqa: BLE001 - 通知の記録の失敗で状態更新を止めない
            log.warning("datafeed_notice_deferred")
            try:
                if self.conn.in_transaction:
                    self.conn.rollback()
            except Exception:  # noqa: BLE001
                log.warning("datafeed_notice_deferred")
            return False

    def _retry_pending_notices(self) -> None:
        """保留を古い順に書き直す。最初の失敗で止め、残りは保留に残す
        (lock 待ちを tick あたり保留分の 1 回に抑える)。"""
        pending, self._pending_notices = self._pending_notices, []
        for index, item in enumerate(pending):
            if not self._write_notices(*item):
                self._pending_notices = pending[index:] + self._pending_notices
                break
        self._trim_pending_notices()

    def _trim_pending_notices(self) -> None:
        """保留は件数の上限つき。超えたら古い順に捨てる。"""
        total = sum(len(entries) for _e, _t, entries in self._pending_notices)
        while total > _PENDING_NOTICE_LIMIT and self._pending_notices:
            total -= len(self._pending_notices[0][2])
            del self._pending_notices[0]
            log.warning("datafeed_notice_dropped")

    # ---- internal: activity --------------------------------------------

    def _write_activity(self, event: str, summary: str) -> None:
        if self.activity is not None:
            self.activity.write(Category.SYSTEM, event, summary)


def _iter_unprocessed(conn, *, hard_keys, gap_start_fn, now: datetime, storage_source: str,
                      grace: timedelta, interval_widths: dict[str, timedelta]):
    """(pair, 注文 id, 未処理と推定される 1m 足本数) を建玉ごとに返す (保守的な近似規則)。"""
    pairs_1m = sorted({pair for pair, interval in hard_keys if interval == "1m"})
    width_1m = interval_widths.get("1m", timedelta(minutes=1))
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
        for status, stamp in (("open", "filled_at"), ("pending_fill", "created_at")):
            for row in orders.list_by_status(conn, status):
                if row["pair"] != pair:
                    continue
                since = _parse(row.get(stamp))
                floor = max(lower_bound, since) if since else lower_bound
                if floor <= latest_confirmed:
                    yield pair, row["id"], (latest_confirmed - floor) // width_1m + 1


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
    仕組みに委ねる。`gap_start` が NULL の key は数えない。

    返り値は pair ごとの内訳 ``{"positions": 建玉数, "bars": 未処理と
    推定される 1m 足本数の下限}``。``bars`` は pair 内の建玉ごとに
    ``(latest_confirmed - floor) // width_1m + 1`` を計算し、その pair の
    最大値を採る (保守的な近似の枠内で、人間が読める粗い目安として十分)。
    """
    result: dict[str, dict[str, int]] = {}
    for pair, _order_id, bars in _iter_unprocessed(
            conn, hard_keys=hard_keys, gap_start_fn=gap_start_fn, now=now,
            storage_source=storage_source, grace=grace, interval_widths=interval_widths):
        entry = result.setdefault(pair, {"positions": 0, "bars": 0})
        entry["positions"] += 1
        entry["bars"] = max(entry["bars"], bars)
    return result


def unprocessed_order_ids(conn, *, hard_keys, gap_start_fn, now: datetime,
                          storage_source: str, grace: timedelta,
                          interval_widths: dict[str, timedelta]) -> set[int]:
    """`unprocessed_positions_by_pair` が数える建玉の注文 id。接触の参照と重複を除くのに使う。"""
    return {order_id for _pair, order_id, _bars in _iter_unprocessed(
        conn, hard_keys=hard_keys, gap_start_fn=gap_start_fn, now=now,
        storage_source=storage_source, grace=grace, interval_widths=interval_widths)}


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
