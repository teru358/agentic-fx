"""二段確認の純 Python state machine。"""
from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import replace
from typing import Callable

from afx_ops.api_client import Actor, Decision, DecisionJob, Reply

from .render import confirm_view, render_event
from .state import (ApprovalRecord, BotState, advance_event, initialize_event_cursor, load,
                    observe_high, save)
from .types import DecisionPlan, EventPlan, GateResult, Interaction, decode_custom_id

CLAIM_SECONDS = 10 * 60
JOB_POLL_LIMIT = 3
JOB_POLL_INTERVAL_SECONDS = 0.05
_LOCKS: dict[tuple[int, int], threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _actor(user_id: int | None) -> str | None:
    if type(user_id) is not int or not 10 ** 16 <= user_id < 10 ** 20:
        return None
    return f"discord:{user_id}"


def _done(record: ApprovalRecord) -> ApprovalRecord:
    """真の終端: bot の決定が完了したか、daemon が pending でないと答えた。"""
    return replace(record, state="done", actor=None, action=None, nonce=None, expiry=None,
                   confirm_message_id=None, nonce_used=True, job_id=None, edit_pending=True)


def _refreshed(record: ApprovalRecord) -> ApprovalRecord:
    """失効 (期限切れ・無効化): claim を解き、View の世代を進めて fresh に戻す。

    daemon ではまだ pending なので終端にはしない。fresh で解く claim が無ければ何も変えない
    (誰でも押せる偽の操作で表示の更新を起こさせない)。
    """
    if record.state == "fresh":
        return record
    return replace(record.fresh(), display_generation=record.display_generation + 1,
                   edit_pending=True)


class GateService:
    """adapter は ``handle``、``bind_confirm``、worker thread の ``perform`` を呼ぶ。"""

    def __init__(self, state: BotState, *, guild_id: int, channel_id: int,
                 allowlist_user_ids: tuple[int, ...] = (), client: object | None = None,
                 state_dir_fd: int | None = None,
                 nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16)) -> None:
        self.state = state
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.allowlist_user_ids = frozenset(allowlist_user_ids)
        self.client = client
        self._nonce_factory = nonce_factory
        self._state_dir_fd = state_dir_fd
        # state_dir_fd を持たない構成では、遷移ごとに snapshot へ確定する口を adapter が差し込む。
        # 外部操作 (daemon 呼出し・Discord) の前に遷移が確定していることを gate 自身が保証する
        self.persist: Callable[[], None] | None = None
        # 正本が 429 / 503 を返した承認 → Retry-After の秒数 (無ければ None)。adapter が次の問合せの
        # 時刻に写して取り除く
        self.throttled: dict[int, int | None] = {}
        if state_dir_fd is None:
            self._lock = threading.RLock()
        else:
            identity = os.fstat(state_dir_fd)
            key = (identity.st_dev, identity.st_ino)
            with _LOCKS_GUARD:
                self._lock = _LOCKS.setdefault(key, threading.RLock())
            with self._lock:
                disk = load(state_dir_fd)
                if disk == BotState.empty() and state != BotState.empty():
                    save(state_dir_fd, state)
                else:
                    self.state = disk

    def _read_state(self) -> BotState:
        return self.state if self._state_dir_fd is None else load(self._state_dir_fd)

    def _replace(self, state: BotState, record: ApprovalRecord) -> None:
        self._update_state(state.replace_record(record))

    def _update_state(self, state: BotState) -> None:
        if self._state_dir_fd is not None:
            save(self._state_dir_fd, state)
        self.state = state
        if self._state_dir_fd is None and self.persist is not None:
            self.persist()

    def plan_event(self, event: object, *, mention_role_id: int | None = None,
                   mention_user_ids: tuple[int, ...] = ()) -> EventPlan | None:
        rendered = render_event(event, mention_role_id=mention_role_id,
                                mention_user_ids=mention_user_ids)
        event_id = event["id"] if type(event) is dict else None
        if type(event_id) is not int or event_id <= self._read_state().cursor:
            return None
        return EventPlan(event_id, rendered.content, rendered.mentions)

    def commit_event(self, event_id: int, *, high_watermark: int) -> None:
        with self._lock:
            self._update_state(advance_event(self._read_state(), event_id,
                                             high_watermark=high_watermark))

    def initialize_event_cursor(self, high_watermark: int) -> None:
        """cursor と flag を同じ 1 回の保存で確定する。"""
        with self._lock:
            self._update_state(initialize_event_cursor(self._read_state(), high_watermark))

    def observe_high_watermark(self, high_watermark: int) -> None:
        with self._lock:
            self._update_state(observe_high(self._read_state(), high_watermark))

    def bind_confirm(self, approval_id: int, message_id: int) -> bool:
        """ephemeral followup の message ID を active claim へ一回だけ束縛する。"""
        with self._lock:
            state = self._read_state()
            record = state.record(approval_id)
            if (record is None or record.state != "claimed" or record.confirm_message_id is not None
                    or type(message_id) is not int or message_id <= 0):
                return False
            self._replace(state, replace(record, confirm_message_id=message_id))
            return True

    def _expired(self, state: BotState, record: ApprovalRecord, now: int) -> GateResult | None:
        if record.state == "claimed" and record.expiry is not None and now >= record.expiry:
            self._replace(state, _refreshed(record))
            return GateResult("This confirmation has expired.")
        return None

    def expire_due(self, now: int) -> None:
        """押されないまま期限を過ぎた claim を失効させる (確認 message を残さないため)。"""
        with self._lock:
            state = self._read_state()
            records = tuple(_refreshed(record) if record.state == "claimed"
                            and record.expiry is not None and now >= record.expiry else record
                            for record in state.approvals)
            if records != state.approvals:
                self._update_state(replace(state, approvals=records))

    def handle(self, interaction: Interaction, *, now: int) -> GateResult:
        try:
            custom_action, approval_id = decode_custom_id(interaction.custom_id)
        except Exception:
            return GateResult("This control is invalid.")
        with self._lock:
            state = self._read_state()
            record = state.record(approval_id)
            if record is None:
                return GateResult("This approval is no longer available.")
            if record.state == "done":
                return GateResult("This approval is no longer active.")
            expired = self._expired(state, record, now)
            if expired is not None:
                return expired
            actor = _actor(interaction.user_id)
            if record.state == "in_flight":
                # 決定は daemon に届いている。job ID を終端まで保つため、どの操作でも record を変えない
                same = (custom_action not in {"confirm", "cancel"}
                        or interaction.message_id == record.confirm_message_id)
                return GateResult("Decision is already in progress." if same
                                  else "This interaction is invalid.")
            if custom_action in {"confirm", "cancel"}:
                # claim を解くのは claim 者の cancel と期限切れだけ。他の誰の操作も古い表示からの
                # 操作も、固定文言を返すだけで record を変えない
                if interaction.message_id != record.confirm_message_id:
                    if record.confirm_message_id is None and interaction.message_id == record.message_id:
                        return GateResult("This confirmation is no longer valid.")
                    return GateResult("This interaction is invalid.")
                if (record.state != "claimed" or actor is None or actor != record.actor
                        or interaction.guild_id != self.guild_id or interaction.channel_id != self.channel_id
                        or interaction.display_generation != record.display_generation
                        or record.nonce_used or record.confirm_message_id is None):
                    return GateResult("This confirmation is no longer valid.")
                if custom_action == "cancel":
                    self._replace(state, record.fresh())
                    return GateResult("Decision cancelled.")
                self._replace(state, replace(record, state="in_flight", nonce_used=True))
                return GateResult("Decision is being checked.", decision=DecisionPlan(
                    approval_id=record.approval_id, action=record.action, actor=actor,
                    payload_sha256=record.payload_sha256))
            if actor is None or interaction.user_id not in self.allowlist_user_ids:
                return GateResult("You are not allowed to decide this approval.")
            if (interaction.guild_id != self.guild_id or interaction.channel_id != self.channel_id
                    or interaction.message_id != record.message_id
                    or interaction.display_generation != record.display_generation):
                # 古い表示 (旧世代・別の投稿) からの操作。claim も世代も動かさない
                return GateResult("This confirmation is no longer valid.")
            if record.state == "claimed":
                # 二度押しや別の最初のボタンは claim を壊さない (確認は ephemeral の側で行う)
                return GateResult("This approval is already claimed." if record.actor != actor
                                  else "This approval is awaiting confirmation.")
            # ここに来る record は fresh だけ (done・in_flight・claimed は上で返している)
            action = "approve" if custom_action == "claim" else custom_action
            claimed = replace(record, state="claimed", actor=actor, action=action,
                              nonce=self._nonce_factory(), expiry=now + CLAIM_SECONDS,
                              confirm_message_id=None, nonce_used=False, job_id=None)
            self._replace(state, claimed)
            return GateResult("Confirm this decision.", confirm_view=confirm_view(claimed))

    def settle(self, approval_id: int) -> bool:
        """daemon が pending でないと答えた承認の record を終端にする (in_flight は再照合に任せる)。"""
        with self._lock:
            state = self._read_state()
            record = state.record(approval_id)
            if record is None or record.state in {"done", "in_flight"}:
                return False
            self._replace(state, _done(record))
            return True

    def edit_done(self, approval_id: int, *, generation: int, state: str) -> bool:
        """投稿の表示を edit できた。edit した時と同じ表示 (世代と状態) のときだけ記録を解く。

        終端 (done) の record は View を外せた時点で役目を終えるので snapshot から消す
        (再起動のたびに edit し直さず、snapshot も増え続けない)。
        """
        with self._lock:
            current = self._read_state()
            record = current.record(approval_id)
            if (record is None or not record.edit_pending
                    or record.display_generation != generation or record.state != state):
                return False
            if record.state == "done":
                self._update_state(current.without(approval_id))
            else:
                self._replace(current, replace(record, edit_pending=False))
            return True

    def abandon_edit(self, approval_id: int, *, generation: int) -> bool:
        """終端 (done) の表示更新を諦めた承認の record を snapshot から消す。

        終端の表示は情報提供のみで、残っても承認の安全性には関わらない。
        """
        with self._lock:
            current = self._read_state()
            record = current.record(approval_id)
            if (record is None or record.state != "done" or not record.edit_pending
                    or record.display_generation != generation):
                return False
            self._update_state(current.without(approval_id))
            return True

    def _active_plan(self, plan: DecisionPlan) -> ApprovalRecord | None:
        record = self._read_state().record(plan.approval_id)
        if (record is None or record.state != "in_flight" or record.actor != plan.actor
                or record.action != plan.action or record.payload_sha256 != plan.payload_sha256):
            return None
        return record

    def perform(self, plan: DecisionPlan) -> GateResult:
        with self._lock:
            if self._active_plan(plan) is None:
                return GateResult("This confirmation is no longer valid.")
        if self.client is None:
            return GateResult("Decision requires a client.")
        try:
            detail = self.client.approval(plan.approval_id)
        except Exception:
            return GateResult("The authoritative result must be checked again.")
        if isinstance(detail, Reply):
            self._note_throttle(plan.approval_id, detail)
        outcome = self._detail_outcome(detail, plan)
        if outcome == "retry":
            return GateResult("The authoritative result must be checked again.")
        if outcome is not None:
            with self._lock:
                state = self._read_state(); current = self._active_plan(plan)
                if current is not None:
                    self._replace(state, _done(current) if outcome == "settled"
                                  else _refreshed(current))
            return GateResult("The authoritative approval is no longer pending."
                              if outcome == "settled" else "This confirmation is no longer valid.")
        with self._lock:
            state = self._read_state()
            current = self._active_plan(plan)
            if current is None:
                return GateResult("This confirmation is no longer valid.")
            if int(plan.actor.split(":", 1)[1]) not in self.allowlist_user_ids:
                self._replace(state, _refreshed(current))
                return GateResult("This confirmation is no longer valid.")
        actor = Actor(plan.actor)
        try:
            if plan.action == "approve":
                decision = self.client.approve(actor, plan.approval_id, payload_sha256=plan.payload_sha256)
            elif plan.action == "reject":
                decision = self.client.reject(actor, plan.approval_id, reason=None)
            else:
                decision = self.client.retry(actor, plan.approval_id, payload_sha256=plan.payload_sha256)
        except Exception:
            return GateResult("The authoritative result must be checked again.")
        return self._complete(plan, decision)

    @staticmethod
    def _detail_outcome(reply: object, plan: DecisionPlan) -> str | None:
        """決定を送る前の正本の照合。

        None は送ってよい、settled は daemon が終端、stale は失効、retry は一時障害 (5xx・429・
        読めない応答) で record を変えずに次の機会に照合し直す。
        """
        if isinstance(reply, Reply) and reply.status == 404:
            return "settled"
        if not isinstance(reply, Reply):
            return "retry"
        if reply.status >= 500 or reply.status in {408, 429}:
            return "retry"
        if reply.status < 200 or reply.status >= 300:
            return "stale"
        if not isinstance(reply.data, dict) or type(reply.data.get("status")) is not str:
            return "retry"
        if reply.data.get("payload_sha256") != plan.payload_sha256:
            return "stale"
        return None if reply.data["status"] == "pending" else "settled"

    def _note_throttle(self, approval_id: int, reply: Reply) -> None:
        if reply.status in {429, 503}:
            self.throttled[approval_id] = reply.retry_after

    def _reconcile_without_job(self, record: ApprovalRecord) -> GateResult:
        try:
            detail = self.client.approval(record.approval_id)
        except Exception:
            return GateResult("The authoritative result must be checked again.")
        if not isinstance(detail, Reply):
            return GateResult("The authoritative result must be checked again.")
        self._note_throttle(record.approval_id, detail)
        data = detail.data if isinstance(detail.data, dict) else None
        if detail.status == 404:
            settled = True
        elif not 200 <= detail.status < 300 or data is None or type(data.get("status")) is not str:
            return GateResult("The authoritative result must be checked again.")
        elif data.get("payload_sha256") != record.payload_sha256:
            settled = False
        elif data["status"] == "pending":
            # job ID が無いのは決定の応答を失ったとき。daemon がまだ pending なら決定は届いて
            # いない (届いていれば非 pending になり、二重の決定にならない) ので押し直せる形に戻す
            with self._lock:
                state = self._read_state(); current = state.record(record.approval_id)
                if current is not None and current.state == "in_flight" and current.job_id is None:
                    self._replace(state, _refreshed(current))
            return GateResult("The decision did not complete.")
        else:
            settled = True
        with self._lock:
            state = self._read_state(); current = state.record(record.approval_id)
            if current is not None and current.state == "in_flight" and current.job_id is None:
                self._replace(state, _done(current) if settled else _refreshed(current))
        return GateResult("The authoritative approval is no longer pending.")

    def _complete(self, plan: DecisionPlan, decision: object) -> GateResult:
        if not isinstance(decision, Decision):
            return GateResult("The authoritative result must be checked again.")
        # 決定の POST 自体の 429 / 503 も、その承認の次の問合せ時刻へ写す
        self._note_throttle(plan.approval_id, decision.reply)
        status = decision.reply.status
        if 400 <= status < 500 and status not in {408, 429}:
            # daemon が決定を拒否した。正本は pending のままなので、claim を解いて押し直せる形に戻す
            with self._lock:
                state = self._read_state(); record = self._active_plan(plan)
                if record is not None:
                    self._replace(state, _refreshed(record))
            return GateResult("The decision was refused.")
        if not 200 <= status < 300:
            return GateResult("The authoritative result must be checked again.")
        with self._lock:
            state = self._read_state(); record = self._active_plan(plan)
            if record is None:
                return GateResult("This confirmation is no longer valid.")
            if decision.job is None:
                self._replace(state, _done(record))
                return GateResult("Decision completed.")
            # poll の前に job ID を確定し、ここで落ちても再起動後に poll から再開できるようにする
            self._replace(state, replace(record, job_id=decision.job.job_id))
        return self.reconcile_in_flight(plan.approval_id)

    def reconcile_in_flight(self, approval_id: int) -> GateResult:
        """応答を失った又は job が終わっていない in_flight を正本と照合する。

        job ID があれば job だけを見る (決定が通ると approval の status は pending でなくなるので、
        status では job の終わりを判断できない)。job ID が無い (決定の応答を失った) ときだけ
        approval の status を読み、pending でなければ決定は届いたとして done にする。
        """
        with self._lock:
            record = self._read_state().record(approval_id)
            if record is None or record.state != "in_flight":
                return GateResult("This confirmation is no longer valid.")
        if self.client is None:
            return GateResult("The authoritative result must be checked again.")
        if record.job_id is None:
            return self._reconcile_without_job(record)
        for attempt in range(JOB_POLL_LIMIT):
            try:
                reply = self.client.poll(DecisionJob(record.job_id, Actor(record.actor)))
            except Exception:
                return GateResult("The authoritative result must be checked again.")
            if isinstance(reply, Reply):
                self._note_throttle(approval_id, reply)
            if isinstance(reply, Reply) and reply.status == 404:
                # job が無い = daemon に記録が無い。裁定どおり終端 (done) とする
                state = "done"
            else:
                state = reply.data.get("state") if (isinstance(reply, Reply)
                                                      and 200 <= reply.status < 300
                                                      and isinstance(reply.data, dict)) else None
            if state == "done":
                with self._lock:
                    current_state = self._read_state(); current = current_state.record(approval_id)
                    if current is not None and current.state == "in_flight":
                        self._replace(current_state, _done(current))
                return GateResult("Decision completed.")
            if state == "failed":
                with self._lock:
                    current_state = self._read_state(); current = current_state.record(approval_id)
                    if current is not None and current.state == "in_flight":
                        # job は終わったが決定は通っていない。正本が pending なら押し直せる形に戻す
                        self._replace(current_state, _refreshed(current))
                return GateResult("The decision did not complete.")
            if state not in {"queued", "running"}:
                return GateResult("The authoritative result must be checked again.")
            if attempt + 1 < JOB_POLL_LIMIT:
                time.sleep(JOB_POLL_INTERVAL_SECONDS)
        return GateResult("The decision is still in progress.")
