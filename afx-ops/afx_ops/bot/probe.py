"""`afx-ops probe bot`: bot token の identity と、設定の guild / channel への到達性だけを確かめる。

Discord の REST に 2 回だけ要求する: 自分の identity (`GET /users/@me`) と、設定の channel
(`GET /channels/<id>`) が設定の guild に属すること。interaction の成立 (allowlist の利用者の
受理等) は enable 時には確かめられないので、ここでは確かめない。何も投稿しない。

TLS は system CA の既定 context、環境の proxy は使わず、redirect は追わない。token の形の
不正は入力の失敗 (rc 71、`token_invalid`)、到達できないことは rc 73 の `probe_<原因>` だけを
返し、token・応答本文・例外文は出さない。
"""
from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from afx_ops.bot.adapter import BotFailure
from afx_ops.bot.runtime import SECRET_NAME, parse_token
from afx_ops.probe import RC_INPUT_FAILED, ProbeError

if TYPE_CHECKING:
    from afx_ops.config import OpsConfig

API_BASE = "https://discord.com/api/v10"
TIMEOUT = 10.0

_CAUSES = ("unauthorized", "channel_unreachable", "guild_mismatch", "http_error",
           "response_invalid", "dns_failed", "connect_failed", "tls_failed", "timeout",
           "transport_failed")
REASONS = ("token_invalid", *(f"probe_{cause}" for cause in _CAUSES))


class _Cause(Exception):
    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def probe(config: OpsConfig, secrets: Mapping[str, bytes], *,
          ssl_context: ssl.SSLContext | None = None,
          resolver_factory: Callable[[], Any] | None = None,
          api_base: str = API_BASE, timeout: float = TIMEOUT) -> None:
    """ssl_context / resolver_factory / api_base / timeout は試験だけが渡す。"""
    try:
        token = parse_token(secrets.get(SECRET_NAME))
    except BotFailure:
        raise ProbeError("token_invalid", RC_INPUT_FAILED) from None
    try:
        asyncio.run(_check(config, token, ssl_context=ssl_context,
                           resolver_factory=resolver_factory, api_base=api_base,
                           timeout=timeout))
    except _Cause as exc:
        raise ProbeError(f"probe_{exc.cause}") from None
    except Exception as exc:  # 例外文 (host や token を含み得る) は出さない
        raise ProbeError(f"probe_{_classify(exc)}") from None


async def _check(config, token: str, *, ssl_context, resolver_factory, api_base: str,
                 timeout: float) -> None:
    import aiohttp

    context = ssl.create_default_context() if ssl_context is None else ssl_context
    resolver = None if resolver_factory is None else resolver_factory()
    connector = aiohttp.TCPConnector(ssl=context, use_dns_cache=False, force_close=True,
                                     resolver=resolver)
    limits = aiohttp.ClientTimeout(total=timeout)
    headers = {"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (afx-ops, 1)"}
    async with aiohttp.ClientSession(connector=connector, timeout=limits, trust_env=False,
                                     headers=headers) as session:
        me = await _get(session, f"{api_base}/users/@me")
        if me is None:
            raise _Cause("unauthorized")
        channel = await _get(session, f"{api_base}/channels/{int(config.channel_id)}")
        if channel is None:
            raise _Cause("channel_unreachable")
        if channel.get("guild_id") != str(config.guild_id):
            raise _Cause("guild_mismatch")


async def _get(session, url: str) -> dict | None:
    """200 の JSON object、拒否 (401 / 403 / 404) なら None。他は _Cause。"""
    async with session.get(url, allow_redirects=False) as response:
        if response.status in (401, 403, 404):
            return None
        if response.status != 200:
            raise _Cause("http_error")
        try:
            data = await response.json(content_type=None)
        except ValueError:
            raise _Cause("response_invalid") from None
        if type(data) is not dict:
            raise _Cause("response_invalid")
        return data


def _classify(exc: Exception) -> str:
    import aiohttp

    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        return "dns_failed"
    if isinstance(exc, (aiohttp.ClientSSLError, ssl.SSLError)):
        return "tls_failed"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, aiohttp.ClientConnectorError):
        return "connect_failed"
    if isinstance(exc, aiohttp.ClientResponseError):
        return "response_invalid"
    return "transport_failed"
