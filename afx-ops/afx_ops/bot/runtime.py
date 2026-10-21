"""`afx-ops bot` の業務処理: Discord の承認 interaction と、event の channel への投稿。

起動順 (隔離・自己検査・server 認証・READY=1) は `startup.run` が済ませてから呼ばれる。
入力は `Isolated` が持つものだけで、鍵・secret dir・設定を読み直さない (隔離後は読めない)。
bot は listener を持たない (gateway も REST も外向きの接続だけ)。

この中の順序:

1. snapshot を読み、未終端の確認 (claimed / in_flight) を 1 回の置換で無効化して保存する
2. Discord に login する (REST: 自分の identity と application)
3. snapshot の未終端の投稿に persistent View を登録する
4. 終端の投稿から View を外す edit、既存投稿の adopt / repost、snapshot に無い pending 承認の
   adopt / 投稿 (全部 bot token の REST)
5. gateway に接続する (切断は discord.py の再接続 (RESUME) に任せる)
6. event と pending 承認を短い間隔で読み、投稿する

失敗は固定 reason を stderr に 1 行出して非零終了し、systemd の restart に委ねる。token・
例外文・Discord の応答本文は出さない。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

import discord

from afx_ops.api_client import RC_UNREACHABLE, ApiError, BotClient
from afx_ops.bot.adapter import BotAdapter, BotFailure, BotSettings, open_state_dir
from afx_ops.gate.service import GateService
from afx_ops.gate.state import BotState
from afx_ops.isolation.startup import client_for

SECRET_NAME = "discord.token"
POLL_INTERVAL = 2.0

RC_OK = 0
RC_BOT_FAILED = 1

REASONS = ("token_invalid", "bot_state_invalid", "discord_login_failed", "discord_unavailable",
           "gateway_failed", "events_invalid", "high_watermark_regressed", "payload_invalid",
           "bot_internal_error")

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


def _quiet_discord_logging() -> None:
    # discord.py の log (再接続時の traceback 等) は例外文や URL を含み得るので出さない
    logger = logging.getLogger("discord")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False


def run(isolated, *, client=None, connector_factory: Callable[[], object] | None = None,
        api_base: str | None = None, gateway_url: str | None = None,
        stop: threading.Event | None = None, poll_interval: float = POLL_INTERVAL,
        now: Callable[[], float] = time.time, stderr: TextIO | None = None) -> int:
    """production は `isolated` だけで呼ばれ、失敗するまで戻らない。

    client / connector_factory / api_base / gateway_url / stop / poll_interval / now / stderr は
    試験だけが渡す (loopback の偽 Discord・試験 CA・試験用の名前解決・停止の合図)。stop が
    立つと rc 0 で戻る。
    """
    out = sys.stderr if stderr is None else stderr
    try:
        token = parse_token(isolated.secrets.get(SECRET_NAME))
        if client is None:
            client = client_for(isolated, BotClient)
        if api_base is not None:
            discord.http.Route.BASE = api_base
        if gateway_url is not None:
            import yarl
            discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(gateway_url)
        _quiet_discord_logging()
        dir_fd = open_state_dir(isolated.state_dir)
        try:
            asyncio.run(_main(isolated, token, client, dir_fd, connector_factory=connector_factory,
                              stop=stop, poll_interval=poll_interval, now=now))
        finally:
            os.close(dir_fd)
    except BotFailure as failure:
        _say(out, failure.reason)
        return RC_BOT_FAILED
    except ApiError as exc:
        _say(out, exc.reason)
        return RC_UNREACHABLE
    except Exception:  # 例外文には token や URL が入り得るので、固定語だけにする
        _say(out, "bot_internal_error")
        return RC_BOT_FAILED
    return RC_OK


def _say(out: TextIO, text: str) -> None:
    try:
        print(f"afx-ops: {text}", file=out, flush=True)
    except (OSError, ValueError):
        pass


class _Client(discord.Client):
    """interaction を adapter へ渡すだけの Client (message_content intent は要求しない)。"""

    adapter: BotAdapter | None = None

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if self.adapter is not None:
            await self.adapter.on_interaction(interaction)


async def _main(isolated, token: str, client, dir_fd: int, *, connector_factory, stop,
                poll_interval: float, now) -> None:
    config = isolated.config
    settings = BotSettings(guild_id=config.guild_id, channel_id=config.channel_id,
                           mention_role_id=config.mention_role_id,
                           mention_user_ids=tuple(config.mention_user_ids),
                           trade_notices=config.trade_notices,
                           trade_amounts=config.trade_amounts)
    gate = GateService(BotState.empty(), guild_id=config.guild_id, channel_id=config.channel_id,
                       allowlist_user_ids=tuple(config.allowlist_user_ids), client=client)
    connector = connector_factory() if connector_factory is not None else None
    bot = _Client(intents=discord.Intents.none(), connector=connector)
    adapter = BotAdapter(gate=gate, client=client, dir_fd=dir_fd, settings=settings,
                         discord_client=bot, now=now)
    bot.adapter = adapter
    tasks: list[asyncio.Task] = []
    try:
        await adapter.prepare()
        try:
            await bot.login(token)
        except discord.LoginFailure:
            raise BotFailure("discord_login_failed") from None
        except (discord.HTTPException, OSError, asyncio.TimeoutError):
            raise BotFailure("discord_unavailable") from None
        await adapter.reconcile_startup()
        gateway = asyncio.create_task(_gateway(bot))
        poller = asyncio.create_task(_poll(adapter, poll_interval))
        failed = asyncio.create_task(adapter.failed.wait())
        tasks = [gateway, poller, failed]
        if stop is not None:
            tasks.append(asyncio.create_task(asyncio.to_thread(stop.wait)))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not failed and task.exception() is not None:
                raise task.exception()
        if adapter.failure is not None:
            raise adapter.failure
        if gateway in done:
            raise BotFailure("gateway_failed")
    finally:
        if stop is not None:
            stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.close()
        adapter.close()


async def _gateway(bot: discord.Client) -> None:
    try:
        await bot.connect(reconnect=True)
    except BotFailure:
        raise
    except Exception:
        raise BotFailure("gateway_failed") from None


async def _poll(adapter: BotAdapter, interval: float) -> None:
    while True:
        await adapter.poll_once()
        await asyncio.sleep(interval)
