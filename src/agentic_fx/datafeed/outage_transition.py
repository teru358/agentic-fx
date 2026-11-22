"""価格源の停止の状態遷移 (純関数)。

接触・停滞・復帰を 1 つの関数 `outage_transition(row, facts)` で計算する。入力は永続状態の行と、
呼び出し側が集めた facts だけで、DB・時計・インスタンス状態には触れない。呼び出し側 (observe)
は結果を保存するだけで、遷移の規則を持たない。

規則の要点:

- 停滞の起点 S は key ごとの連続未到着の起点 (呼び出し側が保存した facts から固定する)。猶予は
  S 以後の開場秒数で数え、建玉があれば `EXPOSURE_STALL_SEC`、無ければ設定の
  `flat_stall_max_sec` (0 は猶予なし)。猶予内は restricted (新規リスクだけ止める)、超えれば degraded。
- 1m の停滞・失敗・空と同じ tick の上位 hard の不調は 1m の猶予に合流する。1m が健全な
  tick の上位 hard の不調は、建玉ありまたは失敗・空なら即 degraded、flat の停滞は設定の猶予。
- restricted が連続 `RESTRICTED_MAX_SEC` (開場秒数) を超えたら、足が届いていても degraded。
- 接触・不明は degraded と人の確認待ちにし、確認 (ack) があるまで自動では戻さない。
- 自動復帰は restricted なら健全な当 tick 1 回、degraded は flat で健全な tick が
  `ready_confirm_ticks` 回続いたとき。どちらも接触待ち・移行確認待ち・自動復帰の無効で止まる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agentic_fx.core import display_time, market_hours
from agentic_fx.core.timeutil import as_utc

# 建玉があるときの停滞の猶予 (開場秒数)
EXPOSURE_STALL_SEC = 600
# restricted の連続の上限 (開場秒数)。足が届いていても超えたら停止する
RESTRICTED_MAX_SEC = 1800

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_EMPTY = "empty"
STATUS_IDLE = "idle"

CONTACT_NOTICE_KEY = "contact"


@dataclass(frozen=True, slots=True)
class KeyView:
    """hard key 1 つの当 tick の観測。`one_m` は 1m の key (1m が無い構成では全 key)。"""

    key: tuple[str, str]
    one_m: bool
    status: str
    stalled: bool
    stall_start: datetime | None

    @property
    def problem(self) -> bool:
        return self.stalled or self.status in (STATUS_FAILED, STATUS_EMPTY)


@dataclass(frozen=True, slots=True)
class Finding:
    """足と注文の組の接触・不明。`kind` が unknown なら接触の有無を確定できなかった組。"""

    order_id: int
    pair: str
    bar_time: datetime
    kind: str


@dataclass(frozen=True, slots=True)
class Facts:
    now: datetime
    flat: bool
    keys: tuple[KeyView, ...]
    # 当 tick の自動復帰の健全 (1m hard key の非空成功・非停滞と、上位 hard の直近結果が False でない)
    healthy: bool
    # 前回の observe より時刻と水位が進んだ (同じ now・同じ足では健全 tick を加算しない)
    advanced: bool
    # 観測が途切れた (再起動・前回 observe から開場 2 tick 超) とき、旧停滞の起点
    old_stall_starts: tuple[datetime, ...] = ()
    # 必須状態・保存の読取りができなかった
    read_error: bool = False
    # 同じ epoch に未解除の接触参照がある
    contact_wait: bool = False
    # 初回配備の移行確認待ち
    migration_pending: bool = False
    # 後着足の接触 (epoch の外でも先に検査する)
    late_contacts: tuple[Finding, ...] = ()
    # non-ready epoch 内の新規足のうち、最新・鮮度内の足を除いた組 / 最新足と未約定指値の組
    n_contacts: tuple[Finding, ...] = ()
    n_latest_contacts: tuple[Finding, ...] = ()
    # この epoch で停止または待ちを知らせた (復帰の通知の条件)
    noticed: bool = False
    auto_resume: bool = True
    ready_confirm_ticks: int = 3
    flat_stall_max_sec: int = 1800
    problem_streak: int = 0


@dataclass(frozen=True, slots=True)
class Effects:
    opened: bool
    problem_streak: int
    activities: tuple = ()
    notices: tuple = ()
    new_contacts: tuple[Finding, ...] = ()
    # observe が未処理建玉数を足して書く data_outage_degraded を要する
    confirmed_stop: bool = False
    # 保存・読取りの障害で gate を閉じた
    blocked: bool = False
    # 復帰した (ready への遷移)
    recovered: bool = False
    rows: dict = field(default_factory=dict)


def _iso(dt: datetime) -> str:
    return as_utc(dt).isoformat()


def _parse(text):
    return datetime.fromisoformat(text) if text else None


def _open_seconds(start: datetime, end: datetime) -> float:
    return market_hours.open_time_between(start, end).total_seconds()


def _limit(f: Facts, key: KeyView, merged: bool) -> int:
    """猶予 (開場秒)。0 は即 degraded。"""
    if merged:
        return f.flat_stall_max_sec if f.flat else EXPOSURE_STALL_SEC
    if not f.flat or key.status in (STATUS_FAILED, STATUS_EMPTY):
        return 0
    return f.flat_stall_max_sec


def _over(f: Facts, key: KeyView, merged: bool) -> bool:
    limit = _limit(f, key, merged)
    if limit <= 0:
        return True
    start = key.stall_start if key.stall_start is not None else f.now
    return _open_seconds(start, f.now) > limit


def outage_transition(row: dict, f: Facts) -> tuple[dict, Effects]:
    """(次の行の値, 効果)。`row` は `datafeed_outage_state` の行 (dict)。"""
    now = as_utc(f.now)
    state = row["state"]
    epoch = row["epoch"]
    confirmed = row["confirmed"]
    entered = row["entered_degraded_at"]
    restricted_since = row.get("restricted_since")
    deadline = row.get("restricted_deadline_at")
    pending = row.get("pending_human_confirmation", 0)
    notified_epoch = row.get("recovered_notified_epoch")
    streak = row.get("ready_streak", 0)
    request_pending = row.get("resume_requested_at") is not None
    start_state = state
    opened = False
    recovered = False
    confirmed_stop = False
    activities: list[tuple[str, str]] = []
    notices: list[tuple[str, str]] = []
    new_contacts: list[Finding] = []

    problems = [k for k in f.keys if k.problem]
    merged = any(k.one_m for k in problems)
    any_problem = bool(problems)
    limit_old = f.flat_stall_max_sec if f.flat else EXPOSURE_STALL_SEC
    old_overdue = any(_open_seconds(s, now) > limit_old for s in f.old_stall_starts)
    overdue = old_overdue or any(_over(f, k, merged) for k in problems)
    contact_wait = f.contact_wait

    def open_epoch() -> None:
        nonlocal epoch, confirmed, notified_epoch, opened
        if state == "ready" and not opened:
            epoch += 1
            confirmed = 0
            notified_epoch = None
            opened = True

    def degrade() -> None:
        nonlocal state, entered, restricted_since, deadline, streak, pending
        was = state
        state = "degraded"
        entered = _iso(now)
        if was != "restricted":
            restricted_since = None
            deadline = None
        streak = 0
        pending = int(bool(pending) or not f.flat or not f.auto_resume)

    def degraded_activity() -> tuple[str, str]:
        return ("datafeed_degraded",
                f"epoch={epoch} failed={sorted(k.key for k in problems if k.status == STATUS_FAILED)} "
                f"empty={sorted(k.key for k in problems if k.status == STATUS_EMPTY)} "
                f"stalled={sorted(k.key for k in problems if k.stalled)}")

    def contact_found(found: list[Finding]) -> None:
        nonlocal state, entered, pending, confirmed, streak, contact_wait
        open_epoch()
        if state != "degraded":
            degrade()
        pending = 1
        confirmed = 1
        streak = 0
        contact_wait = True
        new_contacts.extend(found)
        for item in found:
            builder = (display_time.late_bar_contact_unknown if item.kind == "unknown"
                       else display_time.late_bar_protection_contact)
            _, event, body = builder(epoch, item.order_id, item.bar_time, item.kind)
            activities.append((event, body))
        notices.append(("stopped", CONTACT_NOTICE_KEY))

    n_applies = start_state != "ready" or old_overdue
    first_found = list(f.late_contacts) + (list(f.n_contacts) if n_applies else [])
    skip_rest = False

    if f.read_error:
        open_epoch()
        if state != "degraded":
            degrade()
        pending = int(bool(pending) or not f.flat)
        streak = 0
        activities.append(("datafeed_state_unreadable", f"epoch={epoch}"))
        blocked = True
        skip_rest = True
    else:
        blocked = False

    if not skip_rest and first_found:
        contact_found(first_found)

    restricted_over = (
        state == "restricted" and restricted_since is not None
        and _open_seconds(_parse(restricted_since), now) > RESTRICTED_MAX_SEC)

    if not skip_rest:
        if overdue:
            open_epoch()
            streak = 0
            if state != "degraded":
                degrade()
                activities.append(degraded_activity())
        elif restricted_over:
            if state != "degraded":
                degrade()
                activities.append(degraded_activity())
            if not confirmed:
                confirmed = 1
                confirmed_stop = True
                if notified_epoch != epoch:
                    notices.append(("stopped", ""))
        elif any_problem:
            if state == "ready":
                open_epoch()
                origin = min((k.stall_start for k in problems if k.stall_start is not None),
                             default=now)
                limit = min(_limit(f, k, merged) for k in problems)
                state = "restricted"
                streak = 0
                restricted_since = _iso(origin)
                deadline = _iso(origin + timedelta(seconds=limit))
                _, event, body = display_time.datafeed_restricted(
                    epoch, [k.key for k in problems], origin,
                    origin + timedelta(seconds=limit))
                activities.append((event, body))
            else:
                streak = 0
        else:
            can_auto = f.auto_resume and not contact_wait and not f.migration_pending
            if state == "restricted":
                if f.healthy and can_auto:
                    state = "ready"
                    confirmed = 0
                    streak = 0
                    restricted_since = deadline = None
                    pending = 0
                    recovered = True
                    activities.append(("datafeed_recovered_auto", f"epoch={epoch} streak=0"))
                    if f.noticed:
                        notices.append(("resumed", ""))
                elif f.healthy:
                    if f.advanced:
                        streak += 1
                    if streak >= f.ready_confirm_ticks:
                        degrade()
                        activities.append(degraded_activity())
                        if notified_epoch != epoch and not request_pending:
                            notified_epoch = epoch
                            activities.append(("datafeed_recovered_awaiting_resume",
                                               f"epoch={epoch}"))
                            notices.append(("awaiting_resume", ""))
                else:
                    streak = 0
            elif state == "degraded":
                if f.healthy and f.flat and can_auto:
                    if f.advanced:
                        streak += 1
                    if streak >= f.ready_confirm_ticks:
                        state = "ready"
                        confirmed = 0
                        streak = 0
                        restricted_since = deadline = None
                        pending = 0
                        recovered = True
                        activities.append(("datafeed_recovered_auto",
                                           f"epoch={epoch} streak={f.ready_confirm_ticks}"))
                        if f.noticed:
                            notices.append(("resumed", ""))
                else:
                    streak = 0
                    if (f.healthy and not request_pending and notified_epoch != epoch
                            and (not f.flat or not f.auto_resume or contact_wait
                                 or f.migration_pending)):
                        notified_epoch = epoch
                        activities.append(("datafeed_recovered_awaiting_resume",
                                           f"epoch={epoch}"))
                        notices.append(("awaiting_resume", ""))

    # 回復の tick の最新足と未約定指値は、ready になる遷移なら通常の約定へ任せる。
    # non-ready のままなら接触として止める
    if not skip_rest and state != "ready" and f.n_latest_contacts and n_applies:
        extra = [c for c in f.n_latest_contacts
                 if (c.order_id, c.bar_time) not in {(x.order_id, x.bar_time) for x in new_contacts}]
        if extra:
            contact_found(extra)

    problem_streak = f.problem_streak + 1 if any_problem else 0
    if any_problem and state == "degraded" and not confirmed and problem_streak >= 2:
        confirmed = 1
        confirmed_stop = True
        if notified_epoch != epoch:
            notices.append(("stopped", ""))

    next_row = {
        "state": state, "epoch": epoch, "confirmed": confirmed,
        "entered_degraded_at": entered, "restricted_since": restricted_since,
        "restricted_deadline_at": deadline, "pending_human_confirmation": pending,
        "recovered_notified_epoch": notified_epoch, "ready_streak": streak,
    }
    return next_row, Effects(
        opened=opened, problem_streak=problem_streak, activities=tuple(activities),
        notices=tuple(notices), new_contacts=tuple(new_contacts),
        confirmed_stop=confirmed_stop, blocked=blocked, recovered=recovered)
