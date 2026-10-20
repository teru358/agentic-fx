"""gate と discord.py の間の結線 (plain module、cog にしない)。

役割は写すことだけで、認可・状態遷移・文面は gate が決める:

- interaction: 安価な確認 (component であること、custom_id が文字列、利用者が居る) の後、
  gate や API を呼ぶ前に一度だけ ``defer(ephemeral=True)`` する。defer の後に worker thread
  で ``GateService.handle`` → snapshot 保存 → (決定があれば) ``perform`` → 保存を行い、
  結果を followup で本人だけに返す。同期の ``BotClient`` は pidfd の poll を含みブロックするので、
  event loop では呼ばない。
- 状態: gate の状態を触るもの (interaction、event 配送、reconcile) は全部、1 本の worker thread
  で直列に行う。snapshot の置換もその thread だけが行う (単一 writer)。外部操作 (daemon 呼出し・
  Discord への投稿・edit) の前に、必要な遷移を snapshot に確定する。
- 表示更新: interaction token は使わず、bot token の message edit だけを使う (token の失効や
  再起動の後も同じ経路で View を外せる)。

失敗の扱い: snapshot を保存できない・gate が想定外の例外を出したときは ``BotFailure`` で
process を止める (systemd の再起動に委ねる)。Discord の一時的な失敗は記録を残して次の
周回で再試行する。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import discord

from afx_ops.gate import reconcile as gate_reconcile
from afx_ops.gate import state as gate_state
from afx_ops.gate.reconcile import MessageMetadata
from afx_ops.gate.render import footer as gate_footer
from afx_ops.gate.render import primary_view, render_approval, render_terminal
from afx_ops.gate.service import GateService
from afx_ops.gate.state import ApprovalRecord, StateError
from afx_ops.gate.types import CustomIdError, GateResult, Interaction, SideEffect, decode_custom_id
from afx_ops.bot.views import GateView

EVENTS_LIMIT = 100
APPROVALS_LIMIT = 20
# 表示更新 (bot token の edit) の再試行。上限に達したら、この process では諦めて記録だけ残す
# (snapshot の edit_pending は再起動後の reconcile が拾う)
EDIT_RETRY_LIMIT = 8
EDIT_BACKOFF_SECONDS = 2.0
EDIT_BACKOFF_MAX_SECONDS = 60.0
# 正本が 429 / 503 を返した承認の次の問合せまでの待ち。Retry-After があればそれ (上限つき)、
# 無ければ既定
THROTTLE_DEFAULT_SECONDS = 10.0
THROTTLE_MAX_SECONDS = 300.0
_TERMINAL = frozenset({"done"})
# event の ref は daemon の固定形。int() が受ける Unicode 数字や先頭 0 は承認 ID とみなさない
_APPROVAL_REF = re.compile(r"approval:([1-9][0-9]{0,17})\Z")
# 固定の応答。gate の文面が無い失敗 (処理できなかった) だけに使う
_FAILED_TEXT = "This request could not be processed."


class BotFailure(Exception):
    """bot を止める失敗。``reason`` は固定語。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class BotSettings:
    guild_id: int
    channel_id: int
    mention_role_id: int | None
    mention_user_ids: tuple[int, ...]


@dataclass(frozen=True)
class _Route:
    """View を付けた投稿 1 つ分の dispatch 先。"""

    approval_id: int
    # この View を作った世代 (click の世代は View 作成時に束縛済みで、ここから読み直さない)
    generation: int
    custom_ids: frozenset[str]
    view: GateView


def _post_effect(record: ApprovalRecord, detail=None) -> SideEffect:
    """承認の投稿内容 (文面・footer・primary View)。

    detail は取得済みの approval detail の応答。文面の facts は renderer の allowlist を通る。
    """
    data = getattr(detail, "data", None) if getattr(detail, "status", None) == 200 else None
    return SideEffect("repost", approval_id=record.approval_id,
                      content=render_approval(record, data),
                      footer=gate_footer(record.approval_id, record.payload_sha256,
                                         _post_generation(record)),
                      view=primary_view(record))


def _post_generation(record: ApprovalRecord) -> int:
    """これから出す投稿の世代: 出し直しの意図があればその世代、無ければ record の世代。"""
    return (record.repost_generation if record.repost_generation is not None
            else record.display_generation)


def _detail_status(reply) -> str | None:
    """approval detail の正本の状態。読めなければ None (一時障害として扱う)。"""
    data = getattr(reply, "data", None)
    status = getattr(reply, "status", None)
    if status == 404:
        return "not_found"
    if status != 200 or type(data) is not dict:
        return None
    value = data.get("status")
    return value if type(value) is str else None


def _detail_digest(reply) -> str | None:
    data = getattr(reply, "data", None)
    value = data.get("payload_sha256") if type(data) is dict else None
    return value if type(value) is str else None


def _approval_of(custom_id: object) -> int | None:
    try:
        return decode_custom_id(custom_id)[1]
    except CustomIdError:
        return None


def _approval_id_of(event: dict) -> int | None:
    ref = event.get("ref")
    if event.get("code") != "approval_state_changed" or type(ref) is not str:
        return None
    matched = _APPROVAL_REF.fullmatch(ref)
    return int(matched.group(1)) if matched is not None else None


class BotAdapter:
    def __init__(self, *, gate: GateService, client, dir_fd: int, settings: BotSettings,
                 discord_client: discord.Client, now: Callable[[], float] = time.time) -> None:
        self.gate = gate
        self.client = client
        self.settings = settings
        self.discord = discord_client
        self._dir_fd = dir_fd
        self._now = now
        # gate の状態と snapshot を触る唯一の thread
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="afx-bot-gate")
        self._routes: dict[int, _Route] = {}
        # approval_id → (表示 (世代, 状態), 失敗回数, 次に試してよい時刻)。表示が変われば数え直す
        self._edit_retry: dict[int, tuple[tuple[int, str], int, float]] = {}
        # 終端 edit の再試行の間、一度読んだ文面を使い回す (終端処理ごとに正本を読むのは 1 回)
        self._terminal_text: dict[int, str] = {}
        # approval_id → 次に正本へ問合せてよい時刻。429 / 503 の後だけ載る
        self._next_poll_at: dict[int, float] = {}
        # approval_id → 表示更新と出し直しを排他する lock (footer の世代と record の世代を揃える)
        self._display_locks: dict[int, asyncio.Lock] = {}
        self.failure: BotFailure | None = None
        self.failed = asyncio.Event()
        gate.persist = self._save

    # ------------------------------------------------------------------ 共通

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._worker, fn, *args)

    def _save(self) -> None:
        try:
            gate_state.save(self._dir_fd, self.gate.state)
        except StateError:
            raise BotFailure("bot_state_invalid") from None

    def _fail(self, failure: BotFailure) -> None:
        if self.failure is None:
            self.failure = failure
        self.failed.set()

    def close(self) -> None:
        self._worker.shutdown()

    @property
    def _channel(self) -> discord.PartialMessageable:
        return self.discord.get_partial_messageable(self.settings.channel_id,
                                                    guild_id=self.settings.guild_id)

    def _allowed_mentions(self) -> discord.AllowedMentions:
        roles = ([discord.Object(self.settings.mention_role_id)]
                 if self.settings.mention_role_id is not None else False)
        users = [discord.Object(user) for user in self.settings.mention_user_ids] or False
        return discord.AllowedMentions(everyone=False, roles=roles, users=users,
                                       replied_user=False)

    # ------------------------------------------------------------------ 起動

    def _load_and_invalidate(self) -> None:
        """再起動前の確認を snapshot 1 回の置換で失効させる。

        先に in_flight を正本と照合し、残った claim と job の無い in_flight は世代を進めた fresh に
        戻す。job のある in_flight は決定が daemon に届いているので残す。
        """
        try:
            self.gate.state = gate_state.load(self._dir_fd)
        except StateError:
            raise BotFailure("bot_state_invalid") from None
        self._reconcile_in_flight()
        self.gate.state = gate_reconcile.invalidate_restart(self.gate.state)
        self._save()

    def _reconcile_in_flight(self, approval_id: int | None = None) -> None:
        """応答を失った in_flight を正本と照合する: 再起動時は全部、interaction の前はその承認。"""
        ids = ([approval_id] if approval_id is not None else
               [record.approval_id for record in self.gate.state.approvals
                if record.state == "in_flight"])
        for one in ids:
            record = self.gate.state.record(one)
            if record is not None and record.state == "in_flight":
                self._reconcile_one(one)

    def _reconcile_one(self, approval_id: int) -> None:
        if not self._poll_due(approval_id):
            return
        self.gate.reconcile_in_flight(approval_id)
        # gate が見た 429 / 503 を、この承認の次の問合せ時刻に写す。成功なら先送りを解く
        self._absorb_throttle(approval_id)

    def _absorb_throttle(self, approval_id: int) -> None:
        if approval_id in self.gate.throttled:
            self._defer(approval_id, self.gate.throttled.pop(approval_id))
        else:
            self._next_poll_at.pop(approval_id, None)

    def _poll_due(self, approval_id: int) -> bool:
        return self._now() >= self._next_poll_at.get(approval_id, 0.0)

    def _defer(self, approval_id: int, retry_after: int | None) -> None:
        wait = THROTTLE_DEFAULT_SECONDS if retry_after is None else float(retry_after)
        self._next_poll_at[approval_id] = self._now() + min(wait, THROTTLE_MAX_SECONDS)

    def _observe(self, approval_id: int, reply) -> None:
        """正本の応答を見て、429 / 503 なら先送りし、それ以外なら先送りを解く。"""
        if getattr(reply, "status", None) in {429, 503}:
            self._defer(approval_id, getattr(reply, "retry_after", None))
        else:
            self._next_poll_at.pop(approval_id, None)

    async def prepare(self) -> None:
        """login の前に行う: snapshot を読み、未終端の確認を失効させて保存する。"""
        await self._run(self._load_and_invalidate)

    async def reconcile_startup(self) -> None:
        """login の後・gateway 接続の前に行う。

        View 登録 (未終端の投稿) → 終端の投稿の bot token edit → 既存投稿の adopt / repost →
        snapshot に無い pending 承認の adopt / 投稿、の順。
        """
        state = self.gate.state
        for record in state.approvals:
            if record.state == "fresh":
                self._register(record)
        messages = []
        for record in state.approvals:
            if record.state in _TERMINAL:
                continue
            found = await self._fetch(record.message_id)
            if found is not None:
                messages.append(found)
        plan = gate_reconcile.plan_reconcile(state, tuple(messages))

        def commit_plan() -> None:
            self.gate.state = plan.state
            self._save()
        await self._run(commit_plan)
        for effect in plan.effects:
            await self._apply_effect(effect)
        await self.sync_pending()

    async def _apply_effect(self, effect: SideEffect) -> None:
        record = self.gate.state.record(effect.approval_id)
        if record is None:
            return
        if effect.kind == "edit":
            await self._update_display(record)
            return
        if effect.kind == "adopt":
            # View の登録と edit は通常の表示更新 (再試行・backoff・消えた投稿の再投稿) と同じ経路
            await self._update_display(record)
            return
        if effect.kind == "repost":
            await self._repost(record)

    async def _repost(self, record: ApprovalRecord) -> bool:
        async with self._display_lock(record.approval_id):
            return await self._repost_locked(record)

    async def _repost_locked(self, record: ApprovalRecord) -> bool:
        """投稿を新しく出し直し、record をその message と新しい世代へ付け替える。

        送信の前に出し直す意図を snapshot に確定する。意図が残っている (前回が送信後・保存前に
        止まったかもしれない) ときは、先に bot 自身の投稿を探して、あればそれを採用する。
        """
        if not self._poll_due(record.approval_id):
            return False
        latest = self.gate.state.record(record.approval_id)
        if latest is None or latest.message_id != record.message_id:
            # 待っている間に別の出し直しが済んだ (record は既に新しい投稿を指す)
            return False
        marked = await self._run(self._mark_repost_intent, record.approval_id)
        if marked is None:
            return False
        current, recovering = marked
        message_id = None
        if recovering:
            message_id = await self._find_posted(current, _post_generation(current),
                                                 exclude=current.message_id)
        if message_id is None:
            detail = await self._run(self.client.approval, record.approval_id)
            self._observe(record.approval_id, detail)
            message = await self._send_approval(current, _post_effect(current, detail))
            if message is None:
                return False
            message_id = message.id

        posted_generation = _post_generation(current)

        def commit() -> ApprovalRecord | None:
            adopted = gate_reconcile.record_reposted(
                self.gate.state, record.approval_id, message_id, generation=posted_generation)
            if adopted is None:
                return None
            self.gate.state = adopted
            self._save()
            return self.gate.state.record(record.approval_id)
        committed = await self._run(commit)
        if committed is None:
            # footer の世代と record の意図が食い違った: この投稿は採用せず、View を外した表示にする
            await self._edit(message_id, content=gate_reconcile.NO_LONGER_ACTIVE, view=None)
            return False
        if committed.message_id != current.message_id:
            self._unregister(current.message_id)
        if committed.state in _TERMINAL:
            # 送信の間に終端へ遷移した: 新しい投稿に View を残さず、終端の表示にする
            await self._update_display_locked(committed)
            return True
        self._register(committed)
        return True

    def _mark_repost_intent(self, approval_id: int) -> tuple[ApprovalRecord, bool] | None:
        """意図を確定し、(現在の record、前から意図が残っていたか) を返す。"""
        record = self.gate.state.record(approval_id)
        if record is None:
            return None
        recovering = record.repost_generation is not None
        if not recovering:
            self.gate.state = gate_reconcile.mark_repost_intent(self.gate.state, approval_id)
            self._save()
        return self.gate.state.record(approval_id), recovering

    # ------------------------------------------------------------------ Discord REST

    async def _fetch(self, message_id: int) -> MessageMetadata | None:
        try:
            message = await self._channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            raise BotFailure("discord_unavailable") from None
        return self._metadata(message)

    def _metadata(self, message: discord.Message) -> MessageMetadata:
        text = None
        if message.embeds and message.embeds[0].footer and message.embeds[0].footer.text:
            text = message.embeds[0].footer.text
        # REST の message は guild_id を持たない。channel は guild を跨いで一意なので、設定の
        # channel の投稿だけを設定の guild の投稿とみなす
        channel_id = message.channel.id
        return MessageMetadata(
            message_id=message.id,
            author_is_bot=self.discord.user is not None and message.author.id == self.discord.user.id,
            guild_id=self.settings.guild_id if channel_id == self.settings.channel_id else 0,
            channel_id=channel_id, footer=text)

    async def _edit(self, message_id: int, **kwargs) -> str:
        """bot token の edit。結果は ok、gone (投稿が無い)、failed (一時的な失敗)。

        gone は edit の成功ではない。呼び出し側が、終端なら表示の削除済み、pending なら再投稿として扱う。
        """
        if "content" in kwargs:
            kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        try:
            await self._channel.get_partial_message(message_id).edit(**kwargs)
        except discord.NotFound:
            return "gone"
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            return "failed"
        return "ok"

    async def _send_approval(self, record: ApprovalRecord, effect: SideEffect,
                             prefix: str | None = None) -> discord.Message | None:
        view = GateView(effect.view,
                        self._click_handler(record.approval_id, record.display_generation))
        content = effect.content if prefix is None else f"{prefix}\n{effect.content}"
        try:
            return await self._channel.send(
                content, embed=discord.Embed().set_footer(text=effect.footer), view=view,
                allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            view.stop()
            return None

    # ------------------------------------------------------------------ View の dispatch 表

    def _register(self, record: ApprovalRecord) -> GateView:
        self._unregister(record.message_id)
        model = primary_view(record)
        view = GateView(model, self._click_handler(record.approval_id, record.display_generation))
        self.discord.add_view(view, message_id=record.message_id)
        self._routes[record.message_id] = _Route(record.approval_id, record.display_generation,
                                                 view.custom_ids, view)
        return view

    def _unregister(self, message_id: int | None) -> None:
        route = self._routes.pop(message_id, None) if message_id is not None else None
        if route is not None:
            route.view.stop()

    def _click_handler(self, approval_id: int, generation: int):
        """View を作った時点の世代を束縛する (click 時に dispatch 表から読み直さない)。

        新しい世代の View に差し替わった後も旧 View の callback は旧世代を渡すので、gate が
        record の現在の世代と照合して拒否できる。
        """
        async def clicked(interaction: discord.Interaction) -> None:
            message = interaction.message
            if message is None or message.id not in self._routes:
                await self.on_component(interaction, message_id=None, generation=None)
                return
            await self.on_component(interaction, message_id=message.id, generation=generation)
        return clicked

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """View の dispatch 表に載らない component interaction (旧投稿・改変 custom_id 等)。

        View が受けるものはここでは扱わない (二重処理しない)。載らないものも gate へ渡し、
        拒否・無効化は gate が決める (表示起点が無いので generation は渡さない)。
        """
        if interaction.type is not discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id")
        message = interaction.message
        route = self._routes.get(message.id) if message is not None else None
        if route is not None and custom_id in route.custom_ids:
            return
        await self.on_component(interaction,
                                message_id=message.id if message is not None else None,
                                generation=None)

    # ------------------------------------------------------------------ interaction

    async def on_component(self, interaction: discord.Interaction, *,
                           message_id: int | None, generation: int | None) -> None:
        custom_id = (interaction.data or {}).get("custom_id")
        if interaction.user is None or type(custom_id) is not str:
            return
        # API・gate の前に一度だけ。以後の応答は followup だけ
        await interaction.response.defer(ephemeral=True, thinking=True)
        request = Interaction(custom_id=custom_id, guild_id=interaction.guild_id,
                              channel_id=interaction.channel_id, user_id=interaction.user.id,
                              message_id=message_id, display_generation=generation)
        try:
            result = await self._run(self._decide, request)
        except BotFailure as failure:
            self._fail(failure)
            result = GateResult(_FAILED_TEXT)
        except Exception:
            self._fail(BotFailure("bot_internal_error"))
            result = GateResult(_FAILED_TEXT)
        await self._respond(interaction, result)
        await self._sync_views()

    def _decide(self, request: Interaction) -> GateResult:
        approval_id = _approval_of(request.custom_id)
        if approval_id is not None:
            self._reconcile_in_flight(approval_id)
        # gate は遷移ごとに persist 口で snapshot へ確定する
        result = self.gate.handle(request, now=int(self._now()))
        if result.decision is None:
            return result
        try:
            return self.gate.perform(result.decision)
        finally:
            # perform の事前 detail が受けた 429 / 503 も、その承認の次の問合せ時刻へ写す
            if result.decision.approval_id in self.gate.throttled:
                self._absorb_throttle(result.decision.approval_id)

    async def _respond(self, interaction: discord.Interaction, result: GateResult) -> None:
        view = None
        if result.confirm_view is not None:
            approval_id = _approval_of((interaction.data or {}).get("custom_id"))
            record = self.gate.state.record(approval_id) if approval_id is not None else None
            if record is not None:
                view = GateView(result.confirm_view,
                                self._click_handler(approval_id, record.display_generation))
        try:
            if view is None:
                await interaction.followup.send(result.text, ephemeral=True)
                return
            message = await interaction.followup.send(result.text, ephemeral=True, view=view,
                                                      wait=True)
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            if view is not None:
                view.stop()
            return
        # gate に結べた確認 message だけを dispatch 表に載せる (結べない claim の View は残さない)
        if await self._run(self.gate.bind_confirm, record.approval_id, message.id):
            self._routes[message.id] = _Route(record.approval_id, record.display_generation,
                                              view.custom_ids, view)
        else:
            view.stop()

    async def _sync_views(self) -> None:
        """record の状態を投稿の View に写す (bot token の edit)。

        確認 message (ephemeral) の route は、その claim が生きている間だけ残す (cancel・期限切れ・
        世代更新で claim が解けたら View ごと外す。ephemeral は bot token で edit できない)。
        表示更新が要る record (edit_pending) は、終端なら View を外し、失効で世代が進んだものには
        新しい世代の primary View を付け直す (daemon ではまだ pending なので操作できる形を保つ)。
        """
        records = {record.approval_id: record for record in self.gate.state.approvals}
        for message_id, route in list(self._routes.items()):
            record = records.get(route.approval_id)
            if record is None or (message_id != record.message_id
                                  and (record.state not in {"claimed", "in_flight"}
                                       or message_id != record.confirm_message_id)):
                self._unregister(message_id)
        for record in records.values():
            if record.edit_pending:
                await self._update_display(record)

    def _display_lock(self, approval_id: int) -> asyncio.Lock:
        return self._display_locks.setdefault(approval_id, asyncio.Lock())

    async def _update_display(self, record: ApprovalRecord) -> None:
        async with self._display_lock(record.approval_id):
            current = self.gate.state.record(record.approval_id)
            if current is not None:
                await self._update_display_locked(current)

    async def _update_display_locked(self, record: ApprovalRecord) -> None:
        now = self._now()
        target = (record.display_generation, record.state)
        seen, failures, not_before = self._edit_retry.get(record.approval_id, (target, 0, 0.0))
        if seen != target:
            failures, not_before = 0, 0.0
        if failures >= EDIT_RETRY_LIMIT or now < not_before:
            return
        read_failed = False
        if record.state in _TERMINAL:
            content = await self._terminal_content(record)
            read_failed = content is None
            if content is None:
                # 正本の一時障害: 固定文で確定せず、次の周回で読み直す
                outcome = "failed"
            else:
                outcome = await self._edit(record.message_id, content=content, view=None)
            if outcome == "gone":
                # 外す View を載せた表示が既に無い
                outcome = "ok"
        else:
            # dispatch は edit の成否に関わらず新しい世代へ移す (Discord 側の component は同じ)
            outcome = await self._edit(record.message_id, view=self._register(record))
            if outcome == "gone":
                # daemon ではまだ pending なので、操作できる投稿を出し直す (record も付け替わる)
                if await self._repost_locked(record):
                    after = self.gate.state.record(record.approval_id)
                    if after is None or after.state not in _TERMINAL:
                        # 終端へ遷移していたときの再試行の記録は、その表示更新のものを残す
                        self._edit_retry.pop(record.approval_id, None)
                    return
                outcome = "failed"
        if outcome != "ok":
            # route は残し、次の周回で backoff の後に再試行する
            failures += 1
            if record.state in _TERMINAL and failures >= EDIT_RETRY_LIMIT and read_failed:
                # 正本を読めないままでも View は外す: 固定文の edit を成功するまで改めて試す
                self._terminal_text[record.approval_id] = gate_reconcile.NO_LONGER_ACTIVE
                self._edit_retry[record.approval_id] = (target, 0, 0.0)
                await self._update_display_locked(record)
                return
            if record.state in _TERMINAL and failures >= EDIT_RETRY_LIMIT:
                self._edit_retry.pop(record.approval_id, None)
                self._terminal_text.pop(record.approval_id, None)
                if await self._run(lambda: self.gate.abandon_edit(
                        record.approval_id, generation=record.display_generation)):
                    print("afx-ops bot: edit_abandoned", file=sys.stderr)
                return
            delay = min(EDIT_BACKOFF_SECONDS * 2 ** (failures - 1), EDIT_BACKOFF_MAX_SECONDS)
            self._edit_retry[record.approval_id] = (target, failures, now + delay)
            return
        self._edit_retry.pop(record.approval_id, None)
        self._terminal_text.pop(record.approval_id, None)
        if record.state in _TERMINAL:
            for message_id, route in list(self._routes.items()):
                if route.approval_id == record.approval_id:
                    self._unregister(message_id)
        await self._run(self._edit_done, record)

    async def _terminal_content(self, record: ApprovalRecord) -> str | None:
        """終端の投稿に残す文面。決定結果を正本から読み直して要約と状態を残す。

        正本が 404、又は 200 でも終端の結果でないときは固定の代替文にする (View の取り外しは文面の
        取得に依存させない)。429 / 503 / 例外などの一時障害は None を返し、呼び出し側が再試行に回す。
        読めた文面は終端処理が終わるまで使い回す。
        """
        cached = self._terminal_text.get(record.approval_id)
        if cached is not None:
            return cached
        try:
            reply = await self._run(self.client.approval, record.approval_id)
        except Exception:  # noqa: BLE001 — 取得の失敗は再試行に回す
            return None
        status = getattr(reply, "status", None)
        if status == 200:
            try:
                text = render_terminal(record, getattr(reply, "data", None))
            except Exception:  # noqa: BLE001 — 文面を作れなくても View は外す
                text = None
            text = text or gate_reconcile.NO_LONGER_ACTIVE
        elif status == 404:
            text = gate_reconcile.NO_LONGER_ACTIVE
        else:
            return None
        self._terminal_text[record.approval_id] = text
        return text

    def _edit_done(self, record: ApprovalRecord) -> None:
        self.gate.edit_done(record.approval_id, generation=record.display_generation,
                            state=record.state)

    # ------------------------------------------------------------------ 定期処理

    async def poll_once(self) -> None:
        await self.deliver_events()
        await self.sync_pending()
        # 一覧に載ったままの in_flight (決定の応答を失った等) も毎周回正本と照合する
        await self._run(self._reconcile_in_flight)
        await self._run(lambda: self.gate.expire_due(int(self._now())))
        await self._sync_views()

    async def deliver_events(self) -> None:
        if not self.gate.state.event_cursor_initialized:
            await self._start_event_cursor()
            return
        reply = await self._run(lambda: self.client.events(after=self.gate.state.cursor,
                                                           limit=EVENTS_LIMIT))
        events, high, unreadable = _page(reply)
        if high < self.gate.state.high_watermark:
            raise BotFailure("high_watermark_regressed")
        if high > self.gate.state.high_watermark:
            # event を投稿しない周回でも watermark を進め、daemon の巻戻りを次回以降も検出する
            await self._run(self._observe_high, high)
        for event in events:
            try:
                plan = await self._run(lambda e=event: self.gate.plan_event(
                    e, mention_role_id=self.settings.mention_role_id,
                    mention_user_ids=self.settings.mention_user_ids))
            except Exception:
                raise BotFailure("payload_invalid") from None
            if plan is None:
                continue
            content = " ".join((*plan.mentions, plan.content))
            try:
                await self._channel.send(content, allowed_mentions=self._allowed_mentions())
            except (discord.HTTPException, OSError, asyncio.TimeoutError):
                # 投稿できなかった event から先は確定しない。次の周回で同じ event から再送する
                return
            await self._run(self._commit_post, plan.event_id, max(high, plan.event_id))
            approval_id = _approval_id_of(event)
            if approval_id is not None:
                await self._settle(approval_id)
        if unreadable:
            raise BotFailure("payload_invalid")

    async def _start_event_cursor(self) -> None:
        """最初の正常な応答の時点を開始とし、その page は配送せず末尾から読み始める。"""
        reply = await self._run(lambda: self.client.events(after=self.gate.state.cursor,
                                                           limit=EVENTS_LIMIT))
        _, high, _ = _page(reply)
        await self._run(self._initialize_cursor, high)

    def _initialize_cursor(self, high: int) -> None:
        try:
            self.gate.initialize_event_cursor(high)
        except StateError:
            raise BotFailure("bot_state_invalid") from None

    def _observe_high(self, high: int) -> None:
        self._regression_guard(high)
        try:
            self.gate.observe_high_watermark(high)
        except StateError:
            raise BotFailure("bot_state_invalid") from None

    def _commit_post(self, event_id: int, high: int) -> None:
        """投稿が成功した後にだけ cursor と high watermark を 1 回の置換で確定する。"""
        self._regression_guard(high)
        try:
            self.gate.commit_event(event_id, high_watermark=high)
        except StateError:
            raise BotFailure("bot_state_invalid") from None

    def _regression_guard(self, high: int) -> None:
        if high < self.gate.state.high_watermark:
            raise BotFailure("high_watermark_regressed")

    async def _settle(self, approval_id: int) -> None:
        """正本が pending でなくなった承認の record を終端にし、投稿から View を外す。"""
        record = self.gate.state.record(approval_id)
        if record is None or record.state in _TERMINAL:
            return
        if record.state == "in_flight":
            # 決定が届いた後の status は job の終わりを表さないので、gate の再照合 (job の poll) に任せる
            await self._run(self._reconcile_one, approval_id)
            await self._sync_views()
            return
        if not self._poll_due(approval_id):
            return
        detail = await self._run(self.client.approval, approval_id)
        self._observe(approval_id, detail)
        status = _detail_status(detail)
        if status is None or status == "pending":
            return
        await self._run(self.gate.settle, approval_id)
        await self._sync_views()

    async def sync_pending(self) -> None:
        """pending 承認のうち snapshot に無いものを adopt 又は投稿し、消えたものを決着させる。"""
        reply = await self._run(lambda: self.client.approvals(limit=APPROVALS_LIMIT))
        data = getattr(reply, "data", None)
        rows = data.get("approvals") if getattr(reply, "status", None) == 200 and type(data) is dict else None
        if type(rows) is not list:
            return
        pending = [row["id"] for row in rows
                   if type(row) is dict and type(row.get("id")) is int and row["id"] > 0]
        for approval_id in pending:
            if self.gate.state.record(approval_id) is None:
                await self._post_pending(approval_id)
        if len(rows) < APPROVALS_LIMIT:
            for record in self.gate.state.approvals:
                if record.state not in _TERMINAL and record.approval_id not in pending:
                    await self._settle(record.approval_id)

    async def _post_pending(self, approval_id: int) -> None:
        if not self._poll_due(approval_id):
            return
        detail = await self._run(self.client.approval, approval_id)
        self._observe(approval_id, detail)
        digest = _detail_digest(detail)
        if _detail_status(detail) != "pending" or digest is None:
            return
        draft = ApprovalRecord(approval_id=approval_id, message_id=1,
                               guild_id=self.settings.guild_id,
                               channel_id=self.settings.channel_id, payload_sha256=digest,
                               display_generation=0, state="fresh")
        adopted = await self._find_posted(draft, draft.display_generation)
        if adopted is None:
            message = await self._send_approval(draft, _post_effect(draft, detail))
            if message is None:
                return
            message_id = message.id
        else:
            message_id = adopted

        record = await self._run(self._commit_new_record, approval_id, message_id, digest)
        if record is None:
            return
        if adopted is None:
            self._register(record)
        else:
            await self._update_display(record)

    def _commit_new_record(self, approval_id: int, message_id: int,
                           digest: str) -> ApprovalRecord | None:
        """投稿 (又は adopt) が済んだ後に、承認と投稿の対応を 1 回の置換で確定する。"""
        if self.gate.state.record(approval_id) is not None:
            return None
        record = ApprovalRecord(approval_id=approval_id, message_id=message_id,
                                guild_id=self.settings.guild_id,
                                channel_id=self.settings.channel_id, payload_sha256=digest,
                                display_generation=0, state="fresh")
        self.gate.state = self.gate.state.with_record(record)
        self._save()
        return record

    async def _find_posted(self, draft: ApprovalRecord, generation: int,
                           exclude: int | None = None) -> int | None:
        """投稿後・保存前に止まった承認の投稿を、固定識別子 (footer) と bot author で探す。

        新規の投稿と出し直しの回収の共通の経路。generation はその投稿を出したときの世代で、過去世代の投稿は採用しない。
        exclude は record が今指している (消えた) 投稿。
        """
        expected = gate_footer(draft.approval_id, draft.payload_sha256, generation)
        try:
            async for message in self._channel.history(limit=100):
                meta = self._metadata(message)
                if (meta.message_id != exclude and meta.author_is_bot
                        and meta.guild_id == draft.guild_id
                        and meta.channel_id == draft.channel_id and meta.footer == expected):
                    return meta.message_id
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            raise BotFailure("discord_unavailable") from None
        return None


def _check_watermark(readable: list[dict], high: int) -> None:
    """watermark が読める event の最大 id より小さい応答は不正。cursor が後ろに戻り過去の event が配送される。"""
    if readable and high < max(item["id"] for item in readable):
        raise BotFailure("events_invalid")


def _page(reply) -> tuple[list[dict], int, bool]:
    """events 応答の形を確かめ、(id 順の event、high watermark、id が読めない event があったか)。"""
    data = getattr(reply, "data", None)
    if (getattr(reply, "status", None) != 200 or getattr(reply, "code", "x") is not None
            or type(data) is not dict):
        raise BotFailure("events_invalid")
    events, high = data.get("events"), data.get("high_watermark")
    if type(events) is not list or type(high) is not int or high < 0:
        raise BotFailure("events_invalid")
    readable = []
    for event in events:
        if type(event) is not dict or type(event.get("id")) is not int or event["id"] <= 0:
            _check_watermark(readable, high)
            return sorted(readable, key=lambda item: item["id"]), high, True
        readable.append(event)
    _check_watermark(readable, high)
    return sorted(readable, key=lambda item: item["id"]), high, False


def open_state_dir(path: os.PathLike) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise BotFailure("bot_state_invalid") from None
