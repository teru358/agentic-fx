"""loopback の TLS で動く偽 Discord (REST と gateway)。外部の Discord には繋がない。

aiohttp.web を別 thread の event loop で回す。REST は bot が使う範囲 (login、channel message の
送信・取得・edit、interaction の callback と followup) だけを持ち、受けた要求を全部、受信時刻と
一緒に記録する。gateway は HELLO → IDENTIFY → READY、heartbeat ACK、RESUME → RESUMED を返し、
試験から dispatch (INTERACTION_CREATE) の送出と切断 (再接続させる close code) を指示できる。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import WSMsgType, web

BOT_ID = 323456789012345678
APP_ID = 423456789012345678
TS = "2026-10-11T00:00:00.000000+00:00"


def user_json(user_id: int, *, bot: bool = False) -> dict:
    return {"id": str(user_id), "username": f"u{user_id % 1000}", "discriminator": "0",
            "global_name": None, "avatar": None, "bot": bot, "public_flags": 0}


@dataclass
class Call:
    method: str
    path: str
    body: dict | None
    t: float


@dataclass
class Message:
    id: int
    channel_id: int
    author_id: int
    content: str
    embeds: list
    components: list
    ephemeral: bool = False
    history: list = field(default_factory=list)

    def json(self) -> dict:
        return {"id": str(self.id), "channel_id": str(self.channel_id),
                "author": user_json(self.author_id, bot=self.author_id == BOT_ID),
                "content": self.content, "timestamp": TS, "edited_timestamp": None,
                "tts": False, "mention_everyone": False, "mentions": [], "mention_roles": [],
                "attachments": [], "embeds": self.embeds, "pinned": False, "type": 0,
                "components": self.components, "flags": 64 if self.ephemeral else 0}


def _json(data, status: int = 200) -> web.Response:
    # discord.py は Content-Type が厳密に application/json のときだけ JSON として読む
    return web.Response(body=json.dumps(data).encode(), status=status,
                        headers={"Content-Type": "application/json"})


class FakeDiscord:
    def __init__(self, cert: Path, key: Path, *, host: str) -> None:
        self.host = host
        self.calls: list[Call] = []
        self.messages: dict[int, Message] = {}
        self.dispatched: dict[str, float] = {}
        self.gateway_log: list[tuple[str, float]] = []
        self.channel_guild: dict[int, int] = {}
        self.fail_posts = 0
        self._ids = itertools.count(900000000000000001)
        self._seq = itertools.count(2)
        self._ws: web.WebSocketResponse | None = None
        self._lock = threading.Lock()
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert, key)
        self.loop = asyncio.new_event_loop()
        started = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(context, started), daemon=True)
        self._thread.start()
        assert started.wait(10)

    # ------------------------------------------------------------------ server

    def _run(self, context, started) -> None:
        asyncio.set_event_loop(self.loop)
        app = web.Application()
        app.router.add_route("*", "/gw", self._gateway)
        app.router.add_route("*", "/api/v10/{tail:.*}", self._rest)
        self._runner = web.AppRunner(app, access_log=None)
        self.loop.run_until_complete(self._runner.setup())
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=context)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        started.set()
        self.loop.run_forever()

    def close(self) -> None:
        async def stop():
            if self._ws is not None:
                await self._ws.close()
            await self._runner.cleanup()
        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10)

    def new_id(self) -> int:
        return next(self._ids)

    def add_message(self, channel_id: int, *, author_id: int = BOT_ID, content: str = "",
                    footer: str | None = None) -> Message:
        embeds = [] if footer is None else [{"type": "rich", "footer": {"text": footer}}]
        message = Message(self.new_id(), channel_id, author_id, content, embeds, [])
        with self._lock:
            self.messages[message.id] = message
        return message

    def _record(self, method, path, body) -> None:
        with self._lock:
            self.calls.append(Call(method, path, body, time.time()))

    async def _rest(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        body = None
        if raw:
            ctype = request.headers.get("Content-Type", "")
            if ctype.startswith("application/json"):
                body = json.loads(raw)
            else:
                # multipart (payload_json) は bot では使わない。中身は記録しない
                body = {"_raw": True}
        path = "/" + request.match_info["tail"]
        self._record(request.method, path, body)
        parts = path.strip("/").split("/")
        # interaction の callback と followup は interaction token (URL) で認可し、bot token を付けない
        if (parts[0] not in ("interactions", "webhooks")
                and request.headers.get("Authorization") != f"Bot {self.token}"):
            return _json({"message": "401: Unauthorized", "code": 0}, status=401)
        method = request.method
        if path == "/users/@me":
            return _json(user_json(BOT_ID, bot=True))
        if path == "/oauth2/applications/@me":
            return _json({
                "id": str(APP_ID), "name": "afx", "icon": None, "description": "",
                "rpc_origins": [], "bot_public": False, "bot_require_code_grant": False,
                "owner": user_json(BOT_ID - 1), "team": None, "verify_key": "0" * 64,
                "flags": 0, "summary": "", "interactions_endpoint_url": None})
        if path == "/gateway/bot" or path == "/gateway":
            return _json({"url": f"wss://{self.host}/gw", "shards": 1,
                                      "session_start_limit": {"total": 1000, "remaining": 1000,
                                                              "reset_after": 0,
                                                              "max_concurrency": 1}})
        if parts[0] == "channels" and len(parts) == 2 and method == "GET":
            channel = int(parts[1])
            if channel not in self.channel_guild:
                return _json({"message": "Unknown Channel", "code": 10003},
                                         status=404)
            return _json({"id": str(channel), "type": 0,
                                      "guild_id": str(self.channel_guild[channel]),
                                      "name": "afx"})
        if parts[0] == "channels" and len(parts) >= 3 and parts[2] == "messages":
            channel = int(parts[1])
            if len(parts) == 3 and method == "POST":
                if self.fail_posts > 0:
                    self.fail_posts -= 1
                    return _json({"message": "oops", "code": 0}, status=500)
                message = Message(self.new_id(), channel, BOT_ID, body.get("content") or "",
                                  body.get("embeds") or [], body.get("components") or [])
                with self._lock:
                    self.messages[message.id] = message
                return _json(message.json())
            if len(parts) == 3 and method == "GET":
                found = sorted((m for m in self.messages.values()
                                if m.channel_id == channel and not m.ephemeral),
                               key=lambda m: -m.id)
                return _json([m.json() for m in found[:100]])
            message = self.messages.get(int(parts[3]))
            if message is None or message.channel_id != channel:
                return _json({"message": "Unknown Message", "code": 10008},
                                         status=404)
            if method == "GET":
                return _json(message.json())
            if method == "PATCH":
                message.history.append(body)
                if "content" in body:
                    message.content = body["content"] or ""
                if "components" in body:
                    message.components = body["components"] or []
                if "embeds" in body:
                    message.embeds = body["embeds"] or []
                return _json(message.json())
        if parts[0] == "interactions" and parts[-1] == "callback":
            # discord.py は with_response=true で送り、interaction callback response を読む
            flags = ((body or {}).get("data") or {}).get("flags", 0)
            return _json({"interaction": {"id": parts[1], "type": 3,
                                          "response_message_loading": body.get("type") == 5,
                                          "response_message_ephemeral": bool(flags & 64)},
                          "resource": {"type": body.get("type")}})
        if parts[0] == "webhooks":
            if method == "POST":
                message = Message(self.new_id(), 0, BOT_ID, body.get("content") or "",
                                  body.get("embeds") or [], body.get("components") or [],
                                  ephemeral=True)
                with self._lock:
                    self.messages[message.id] = message
                return _json(message.json())
            if method == "PATCH":
                message = Message(self.new_id(), 0, BOT_ID, body.get("content") or "", [],
                                  body.get("components") or [], ephemeral=True)
                with self._lock:
                    self.messages[message.id] = message
                return _json(message.json())
        return _json({"message": "404: Not Found", "code": 0}, status=404)

    token = "x"

    async def _gateway(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 45000}})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            op = data.get("op")
            if op == 1:
                await ws.send_json({"op": 11, "d": None})
            elif op == 2:
                self.gateway_log.append(("identify", time.time()))
                await ws.send_json({"op": 0, "t": "READY", "s": 1, "d": {
                    "v": 10, "user": user_json(BOT_ID, bot=True), "guilds": [],
                    "session_id": "session-1", "resume_gateway_url": f"wss://{self.host}/gw",
                    "application": {"id": str(APP_ID), "flags": 0}, "private_channels": [],
                    "relationships": [], "presences": [], "shard": [0, 1]}})
            elif op == 6:
                self.gateway_log.append(("resume", time.time()))
                await ws.send_json({"op": 0, "t": "RESUMED", "s": next(self._seq), "d": {}})
        if self._ws is ws:
            self._ws = None
        return ws

    # ------------------------------------------------------------------ 試験からの操作

    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def drop(self) -> None:
        """再接続 (RESUME) させる close code で gateway を切る。"""
        ws = self._ws
        assert ws is not None
        self.gateway_log.append(("drop", time.time()))
        asyncio.run_coroutine_threadsafe(ws.close(code=4000), self.loop).result(10)

    def click(self, *, custom_id: str, message_id: int, user_id: int, guild_id: int,
              channel_id: int) -> str:
        """component interaction を gateway で送り、interaction id を返す。"""
        interaction_id = self.new_id()
        message = self.messages.get(message_id)
        message_json = (message.json() if message is not None else
                        Message(message_id, channel_id, BOT_ID, "", [], []).json())
        message_json["channel_id"] = str(channel_id)
        data = {"id": str(interaction_id), "application_id": str(APP_ID), "type": 3,
                "token": f"itoken{interaction_id}", "version": 1, "guild_id": str(guild_id),
                "channel_id": str(channel_id),
                "channel": {"id": str(channel_id), "type": 0, "guild_id": str(guild_id),
                            "name": "afx", "position": 0, "permission_overwrites": [],
                            "nsfw": False, "parent_id": None, "topic": None,
                            "rate_limit_per_user": 0, "last_message_id": None, "flags": 0},
                "member": {"user": user_json(user_id), "roles": [], "joined_at": TS,
                           "deaf": False, "mute": False, "flags": 0, "permissions": "0"},
                "data": {"custom_id": custom_id, "component_type": 2},
                "message": message_json, "app_permissions": "0", "locale": "en-US",
                "entitlements": [], "authorizing_integration_owners": {}, "context": 0,
                "attachment_size_limit": 8388608}
        ws = self._ws
        assert ws is not None
        key = str(interaction_id)

        async def send():
            self.dispatched[key] = time.time()
            await ws.send_json({"op": 0, "t": "INTERACTION_CREATE", "s": next(self._seq),
                                "d": data})
        asyncio.run_coroutine_threadsafe(send(), self.loop).result(10)
        return key

    # ------------------------------------------------------------------ 観測

    def snapshot_calls(self) -> list[Call]:
        with self._lock:
            return list(self.calls)

    def callback_for(self, interaction_id: str) -> Call | None:
        return next((c for c in self.snapshot_calls()
                     if c.path.startswith(f"/interactions/{interaction_id}/")), None)

    def followups_for(self, interaction_id: str) -> list[Call]:
        return [c for c in self.snapshot_calls()
                if c.path.startswith(f"/webhooks/{APP_ID}/itoken{interaction_id}")]

    def channel_posts(self, channel_id: int) -> list[Call]:
        return [c for c in self.snapshot_calls()
                if c.method == "POST" and c.path == f"/channels/{channel_id}/messages"]
