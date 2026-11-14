"""`afx-ops bot` の業務処理: Discord の承認 interaction と、event の channel への投稿。

起動順 (隔離・自己検査・server 認証・READY=1) は `startup.run` が済ませてから呼ばれる。
入力は `Isolated` が持つものだけで、鍵・secret dir・設定を読み直さない (隔離後は読めない)。
bot は listener を持たない (gateway も REST も外向きの接続だけ)。

この中の順序:

1. snapshot を読み、未終端の確認 (claimed / in_flight) を 1 回の置換で無効化して保存する
2. Discord に login する (REST: 自分の identity と application)。一時の失敗 (429 / 5xx / network /
   timeout) は process 内で上限つきの backoff で再試行し、認証の拒否 (LoginFailure / 401 /
   他の 4xx) は恒久として止まる
3. snapshot の未終端の投稿に persistent View を登録する
4. 終端の投稿から View を外す edit、既存投稿の adopt / repost、snapshot に無い pending 承認の
   adopt / 投稿 (全部 bot token の REST)
5. gateway に接続する (切断は discord.py の再接続 (RESUME) に任せる)
6. event と pending 承認を短い間隔で読み、投稿する

進捗の監視 (`progress.ProgressTracker`、monotonic clock、窓は 10 分): gateway は process 開始から
READY / RESUMED まで・切断から RESUMED までを連続の不健全として数え (login 中は含め、起動 reconcile の間は止めて
connect の開始から数え直す)、配送は
先頭 event の最初の send の直前から send と cursor の保存が済むまで、poll 周回は一周の開始から
完了までを数える。loop 上の独立した monitor が期限を見て、超えたら一時の失敗で止まる。

失敗は固定 reason を stderr に 1 行出して非零終了する。恒久 (rc 78: token・state の内容・payload・
event・high watermark の後退・Discord の配送拒否) は systemd が再起動せず、一時 (rc 79: 進捗の
停止・Discord / state の I/O の一時障害・内部の想定外) は backoff で再起動する。daemon に届かない
ときは rc 3。値と対応は `exits`。token・例外文・Discord の応答本文は出さない。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

import aiohttp
import discord

from afx_ops.api_client import RC_UNREACHABLE, ApiError, BotClient, with_detail
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings, open_state_dir
from afx_ops.bot.exits import (REASONS, RC_BOT_LOOP_STALLED, RC_BOT_PERMANENT,  # noqa: F401
                               RC_BOT_TRANSIENT, RC_OK, rc_for_reason)
from afx_ops.bot.logsafe import LoopErrors, SafeLogging, take_early
from afx_ops.bot.progress import GATEWAY_HEALTHY, ProgressTracker
from afx_ops.bot.rest import PERMANENT, REST_ERRORS, classify
from afx_ops.bot.watchdog import Watchdog
from afx_ops.health import HealthWriter, Publisher
from afx_ops.gate.service import GateService
from afx_ops.gate.state import BotState
from afx_ops.isolation.startup import client_for

SECRET_NAME = "discord.token"
POLL_INTERVAL = 2.0

# 進捗 monitor の確認の間隔と、login の再試行の待ち (倍々で上限まで)
MONITOR_INTERVAL = 5.0
LOGIN_BACKOFF_SECONDS = 2.0
LOGIN_BACKOFF_MAX_SECONDS = 60.0

# 再接続しても直らない gateway の close code (認証の失敗、shard・API version・intents の拒否)
FATAL_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})

# bot token の形 (base64url の 3 区切り)。中身の真偽は login で確かめる
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{16,}\Z")


def parse_token(raw: object) -> str:
    """secret file の bytes を bot token にする。形が違えば ``BotFailure("token_invalid")``。"""
    if type(raw) is not bytes:
        raise BotFailure("token_invalid")
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise BotFailure("token_invalid") from None
    if _TOKEN.fullmatch(text) is None:
        raise BotFailure("token_invalid")
    return text


def run(isolated, *, client=None, connector_factory: Callable[[], object] | None = None,
        api_base: str | None = None, gateway_url: str | None = None,
        stop: threading.Event | None = None, poll_interval: float = POLL_INTERVAL,
        now: Callable[[], float] = time.time, stderr: TextIO | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
        monitor_interval: float = MONITOR_INTERVAL,
        watchdog: Watchdog | None = None) -> int:
    """production は `isolated` だけで呼ばれ、失敗するまで戻らない。

    client / connector_factory / api_base / gateway_url / stop / poll_interval / now / stderr /
    monotonic / sleep / monitor_interval / watchdog は試験だけが渡す (loopback の偽 Discord・試験 CA・試験用の
    名前解決・停止の合図・進捗の時計)。stop が立つと rc 0 で戻る。失敗は理由ごとに `exits` の
    終了コード (恒久 78 / 一時 79) を返す。
    """
    out = sys.stderr if stderr is None else stderr
    watchdog = new_watchdog() if watchdog is None else watchdog
    safe: SafeLogging | None = None
    loop_errors = LoopErrors(lambda t: _say(out, t))
    tracker = ProgressTracker(monotonic, emit=lambda t: _say(out, t))
    health: Publisher | None = None
    try:
        watchdog.start()
        # discord.py / aiohttp / asyncio の log は例外文・URL・ID を含み得るので、record の中身は出さず
        # 系統と件数の固定語だけにする。起動の最初に入れてあればそれを引き継ぐ
        write = lambda text: _say(out, text)  # noqa: E731
        safe = take_early()
        if safe is None:
            safe = SafeLogging(write)
        else:
            safe.set_write(write)
        token = parse_token(isolated.secrets.get(SECRET_NAME))
        if client is None:
            client = client_for(isolated, BotClient)
        if api_base is not None:
            discord.http.Route.BASE = api_base
        if gateway_url is not None:
            import yarl
            discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(gateway_url)
        dir_fd = open_state_dir(isolated.state_dir)
        try:
            # 表示専用の health snapshot。起動時は terminal を null にして先に置く
            health = _start_health(dir_fd, tracker, out, now, monotonic, watchdog)
            asyncio.run(_main(isolated, token, client, dir_fd, connector_factory=connector_factory,
                              stop=stop, poll_interval=poll_interval, now=now,
                              progress=tracker, health=health,
                              sleep=sleep, loop_errors=loop_errors,
                              monitor_interval=monitor_interval, watchdog=watchdog))
        finally:
            os.close(dir_fd)
    except BotFailure as failure:
        watchdog.begin_shutdown()
        _say(out, failure.reason)
        if health is not None:
            health.finalize(failure.reason)
        return rc_for_reason(failure.reason)
    except ApiError as exc:
        watchdog.begin_shutdown()
        _say(out, with_detail(exc.reason, exc.detail))
        if health is not None:
            health.finalize("daemon_unreachable")
        return RC_UNREACHABLE
    except Exception:  # 例外文には token や URL が入り得るので、固定語だけにする
        watchdog.begin_shutdown()
        _say(out, "bot_internal_error")
        if health is not None:
            health.finalize("bot_internal_error")
        return rc_for_reason("bot_internal_error")
    except BaseException:
        # KeyboardInterrupt / SystemExit / 取消し。後始末が詰まっても 60 秒で止まるよう、再送出の前に印を付ける
        watchdog.begin_shutdown()
        raise
    finally:
        if health is not None:
            health.close()
        loop_errors.flush_counts()
        if safe is not None:
            safe.flush_counts()
            safe.restore()
    watchdog.begin_shutdown()
    return RC_OK


def _start_health(dir_fd, tracker, out, wall, mono, watchdog) -> Publisher | None:
    """表示専用の snapshot の writer を起こし、terminal 無しの最初の snapshot を先に置く。失敗しても bot は止めない。"""
    try:
        writer = HealthWriter(os.dup(dir_fd), say=lambda t: _say(out, t))
    except Exception:  # noqa: BLE001
        return None
    publisher = Publisher(writer, tracker, wall=wall, mono=mono,
                          loop_acknowledged=lambda: watchdog.acknowledged_at)
    publisher.publish(force=True)
    return publisher


def new_watchdog() -> Watchdog:
    return Watchdog()


def _say(out: TextIO, text: str) -> None:
    try:
        print(f"afx-ops: {text}", file=out, flush=True)
    except (OSError, ValueError):
        pass


class _Client(discord.Client):
    """interaction を adapter へ渡すだけの Client (message_content intent は要求しない)。"""

    adapter: BotAdapter | None = None
    progress: ProgressTracker | None = None

    # gateway の健全性は READY (on_connect) と RESUMED だけが戻し、disconnect は不健全にする。
    # Client.latency は最後の ACK の往復時間であって健全性の根拠にならない
    async def on_connect(self) -> None:
        if self.progress is not None:
            self.progress.gateway_connected()

    async def on_resumed(self) -> None:
        if self.progress is not None:
            self.progress.gateway_resumed()

    async def on_disconnect(self) -> None:
        if self.progress is not None:
            self.progress.gateway_disconnected()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if self.adapter is not None:
            await self.adapter.on_interaction(interaction)


async def _main(isolated, token: str, client, dir_fd: int, *, connector_factory, stop,
                poll_interval: float, now, progress: ProgressTracker | None = None,
                sleep=asyncio.sleep, monitor_interval: float = MONITOR_INTERVAL,
                watchdog: Watchdog | None = None, loop_errors=None,
                health: Publisher | None = None) -> None:
    # 初期化の途中の中断でも shutdown を記録する (本体の finally に入る前の経路)
    try:
        await _main_body(isolated, token, client, dir_fd, connector_factory=connector_factory,
                         stop=stop, poll_interval=poll_interval, now=now, progress=progress,
                         sleep=sleep, monitor_interval=monitor_interval, watchdog=watchdog,
                         loop_errors=loop_errors, health=health)
    except BaseException:
        if watchdog is not None:
            watchdog.begin_shutdown()
        raise


async def _main_body(isolated, token: str, client, dir_fd: int, *, connector_factory, stop,
                     poll_interval: float, now, progress: ProgressTracker | None = None,
                     sleep=asyncio.sleep, monitor_interval: float = MONITOR_INTERVAL,
                     watchdog: Watchdog | None = None, loop_errors=None,
                     health: Publisher | None = None) -> None:
    if loop_errors is not None:
        # 既定の handler は例外文と traceback を出す。型名と固定語だけにする
        asyncio.get_running_loop().set_exception_handler(loop_errors)
    progress = progress if progress is not None else ProgressTracker()
    if watchdog is not None:
        watchdog.attach(asyncio.get_running_loop())
    config = isolated.config
    settings = BotSettings(guild_id=config.guild_id, channel_id=config.channel_id,
                           mention_role_id=config.mention_role_id,
                           mention_user_ids=tuple(config.mention_user_ids),
                           trade_notices=config.trade_notices,
                           trade_amounts=config.trade_amounts,
                           ops_notices=config.ops_notices,
                           improve_notices=config.improve_notices)
    gate = GateService(BotState.empty(), guild_id=config.guild_id, channel_id=config.channel_id,
                       allowlist_user_ids=tuple(config.allowlist_user_ids), client=client)
    connector = connector_factory() if connector_factory is not None else None
    bot = _Client(intents=discord.Intents.none(), connector=connector)
    bot.progress = progress
    if health is not None:
        health.bind_latency(lambda: bot.latency)
    adapter = BotAdapter(gate=gate, client=client, dir_fd=dir_fd, settings=settings,
                         discord_client=bot, now=now, progress=progress, sleep=sleep)
    bot.adapter = adapter
    tasks: list[asyncio.Task] = []
    try:
        # monitor は login の前から動かす (login 前も gateway の 10 分の窓に含める)
        monitor = _spawn(_monitor(progress, monitor_interval, sleep))
        tasks.append(monitor)
        if health is not None:
            # 周期の task は終わらない。終了の判断には加えず、後始末で取り消す
            tasks.append(_spawn(health.run()))
        startup = _spawn(_startup(adapter, bot, token, sleep, connector_factory))
        tasks.append(startup)
        # 起動の判断は startup と monitor だけで決める。表示用の health task の失敗で bot を止めない
        await asyncio.wait((startup, monitor), return_when=asyncio.FIRST_COMPLETED)
        if startup.done():
            startup.result()
        else:
            monitor.result()
        running = [monitor]
        for make in (lambda: _gateway(bot, progress, sleep),
                     lambda: _poll(adapter, poll_interval),
                     adapter.failed.wait):
            task = _spawn(make())
            tasks.append(task)
            running.append(task)
        gateway, poller, failed = running[1:]
        if stop is not None:
            task = _spawn(asyncio.to_thread(stop.wait))
            tasks.append(task)
            running.append(task)
        done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not failed and task.exception() is not None:
                raise task.exception()
        if adapter.failure is not None:
            raise adapter.failure
        if gateway in done:
            raise BotFailure("gateway_failed")
    finally:
        if watchdog is not None:
            watchdog.begin_shutdown()
        if stop is not None:
            stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await bot.close()
        except Exception:  # 終了処理の失敗で、止まった本来の理由を上書きしない
            pass
        adapter.close()


def _spawn(coro) -> asyncio.Task:
    """task を作り、終わったら例外を必ず回収する (unretrieved task exception を作らない)。

    結果を待つ側は別に `.exception()` / `.result()` で読む。
    """
    task = asyncio.create_task(coro)
    task.add_done_callback(_retrieve)
    return task


def _retrieve(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


async def _startup(adapter: BotAdapter, bot: discord.Client, token: str, sleep,
                   new_connector=None) -> None:
    await adapter.prepare()
    await _login(bot, token, sleep, new_connector=new_connector)
    # 起動 reconcile の間は gateway の窓を止め (connect を始めたときから数え直す)、
    # reconcile 自体は最初の poll の窓が見る
    adapter.progress.gateway_pause()
    adapter.progress.startup_begin()
    await adapter.reconcile_startup()


async def _login(bot: discord.Client, token: str, sleep, *,
                 delay: float = LOGIN_BACKOFF_SECONDS,
                 limit: float = LOGIN_BACKOFF_MAX_SECONDS, new_connector=None) -> None:
    """login する。認証の拒否は恒久、429 / 5xx / network / timeout は backoff で再試行する。

    再試行の期間の上限は gateway の 10 分の窓 (monitor) が持つ。失敗のたびに、その試行が作った
    HTTP session を閉じてから再試行する (session を溜めない)。
    """
    while True:
        try:
            await bot.login(token)
            return
        except discord.LoginFailure:
            raise BotFailure("discord_login_failed") from None
        except REST_ERRORS as exc:
            if classify(exc) == PERMANENT:
                await _reset_http(bot, new_connector)
                raise BotFailure("discord_login_failed") from None
        await _reset_http(bot, new_connector)
        await sleep(delay)
        delay = min(delay * 2, limit)


async def _reset_http(bot, new_connector) -> None:
    """失敗した login が作った HTTP session を閉じ、次の試行が新しい session と connector を使えるようにする。

    session を閉じると connector も閉じるので、次の試行のために作り直す。
    """
    http = getattr(bot, "http", None)
    if http is None:
        return
    try:
        await http.close()
        http.clear()
        http.connector = (new_connector() if new_connector is not None
                          else aiohttp.TCPConnector(limit=0))
    except Exception:  # noqa: BLE001 — 後始末の失敗で login の再試行の分類を変えない
        pass


async def _monitor(progress: ProgressTracker, interval: float, sleep) -> None:
    """loop 上で周期に進捗の期限を見る。await の完了を待たずに判定するので、send などが戻らなくても動く。"""
    while True:
        await sleep(interval)
        reason = progress.expired()
        if reason is not None:
            raise BotFailure(reason)


async def _gateway(bot: discord.Client, progress: ProgressTracker, sleep, *,
                   delay: float = LOGIN_BACKOFF_SECONDS,
                   limit: float = LOGIN_BACKOFF_MAX_SECONDS) -> None:
    """gateway に接続し続ける。恒久の拒否は止まり、一時の REST / 接続の失敗は backoff で再接続する。

    再接続の間は不健全の起点を動かさない (gateway の 10 分の窓を monitor が持つ)。
    """
    initial = delay
    progress.gateway_begin()
    while True:
        try:
            await bot.connect(reconnect=True)
        except BotFailure:
            raise
        except Exception as exc:
            if isinstance(exc, discord.LoginFailure) or (
                    isinstance(exc, discord.HTTPException) and classify(exc) == PERMANENT):
                raise BotFailure("discord_rejected") from None
            if isinstance(exc, discord.ConnectionClosed) and exc.code in FATAL_CLOSE_CODES:
                # 認証の失敗・intents の拒否など、再接続しても直らない close
                raise BotFailure("discord_login_failed") from None
            if not isinstance(exc, REST_ERRORS):
                raise BotFailure("gateway_failed") from None
        else:
            # connect が正常に戻るのは close 後だけ。再接続せず止める
            raise BotFailure("gateway_failed")
        # 一時の失敗。健全だった場合だけ今を不健全の起点にし (待ちも最初から)、既に不健全なら動かさない
        if progress.gateway_state == GATEWAY_HEALTHY:
            delay = initial
        progress.gateway_disconnected()
        await sleep(delay)
        delay = min(delay * 2, limit)


async def _poll(adapter: BotAdapter, interval: float) -> None:
    while True:
        adapter.progress.poll_begin()
        await adapter.poll_once()
        adapter.progress.poll_end()
        await asyncio.sleep(interval)
