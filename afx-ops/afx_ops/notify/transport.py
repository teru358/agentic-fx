"""webhook への HTTPS POST を 1 回だけ行う (aiohttp)。

TLS は system CA の既定 context (証明書と hostname の検証あり)、環境の proxy は使わず、
redirect は追わない。DNS は送信のたびに引き直し、接続は使い回さない (宛先 IP を覚えない)。
失敗は `DeliveryError` の固定語だけにし、URL・host・例外文を外へ出さない。

aiohttp は起動口の preload が隔離前に import 済みなので、ここでは関数の中で参照するだけに
して、この module の import で aiohttp を読み込まない (import の失敗を起動の固定 reason
`runtime_import_failed` に寄せるため)。
"""
from __future__ import annotations

import asyncio
# 隔離後の送信が初めて読み込む stdlib module を、この module の import 時 (隔離前) に
# 済ませる: TLS の server_hostname を符号化する idna codec と、aiohttp 既定の resolver が
# getaddrinfo を回す既定 executor (thread pool)
import concurrent.futures.thread  # noqa: F401
import encodings.idna  # noqa: F401
import queue  # noqa: F401
import ssl
from collections.abc import Callable
from typing import Any

from afx_ops.notify import retry as _retry
from afx_ops.notify.delivery import (
    DeliveryError,
    WebhookUrl,
    classify_status,
    request_headers,
)

# 恒久的な失敗 (再送しても直らない)。それ以外の通信失敗は retry 規則で再送する
# 応答 body を読み切る上限。成功は body が EOF まで届いてから判定する
_MAX_BODY = 64 * 1024

_RETRYABLE = frozenset({"dns_failed", "connect_failed", "tls_failed", "timeout",
                        "connection_lost"})


def system_ssl_context() -> ssl.SSLContext:
    """system CA だけを信頼する既定 context。検証を緩める設定はここにも呼び側にも無い。"""
    return ssl.create_default_context()


def post(target: WebhookUrl, body: bytes, *,
         ssl_context: ssl.SSLContext | None = None,
         resolver_factory: Callable[[], Any] | None = None,
         timeout: float | None = None, total_timeout: float | None = None) -> None:
    """1 回だけ送る。2xx なら戻り、それ以外は `DeliveryError`。

    ssl_context / resolver_factory / timeout / total_timeout は試験だけが渡す (loopback の試験 CA・試験用の
    名前解決・短い待ち)。production は既定の system CA と aiohttp 既定の resolver を使う。
    """
    if type(body) is not bytes:
        raise DeliveryError("payload_invalid")
    try:
        status, retry_after = asyncio.run(_post(target, body, ssl_context=ssl_context,
                                   resolver_factory=resolver_factory,
                                   timeout=_retry.CONNECT_READ_TIMEOUT if timeout is None
                                   else timeout,
                                   total=_retry.TOTAL_TIMEOUT if total_timeout is None
                                   else total_timeout))
    except DeliveryError:
        raise
    except Exception as exc:  # 例外文 (host:port を含み得る) を外へ出さない
        raise _classify_exception(exc) from None
    failure = classify_status(status, retry_after)
    if failure is not None:
        raise failure


async def _post(target: WebhookUrl, body: bytes, *, ssl_context, resolver_factory,
                timeout: float, total: float) -> tuple[int, str | None]:
    import aiohttp

    context = system_ssl_context() if ssl_context is None else ssl_context
    resolver = None if resolver_factory is None else resolver_factory()
    connector = aiohttp.TCPConnector(ssl=context, use_dns_cache=False, force_close=True,
                                     resolver=resolver)
    limits = aiohttp.ClientTimeout(total=total, sock_connect=timeout, sock_read=timeout)
    async with aiohttp.ClientSession(connector=connector, timeout=limits,
                                     trust_env=False) as session:
        async with session.post(target.url, data=body, headers=dict(request_headers(body)),
                                allow_redirects=False) as response:
            # 途中切れの body を成功にしないため、status に関わらず EOF まで読む。
            # 上限を超える応答は読み続けず打ち切る
            received = 0
            async for chunk in response.content.iter_chunked(8192):
                received += len(chunk)
                if received > _MAX_BODY:
                    raise DeliveryError("response_invalid")
            return response.status, response.headers.get("Retry-After")


def _classify_exception(exc: Exception) -> DeliveryError:
    import aiohttp

    if isinstance(exc, aiohttp.InvalidURL):
        return DeliveryError("url_invalid")
    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        reason = "dns_failed"
    elif isinstance(exc, (aiohttp.ClientSSLError, ssl.SSLError,
                          aiohttp.ServerFingerprintMismatch)):
        reason = "tls_failed"
    elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        reason = "timeout"
    elif isinstance(exc, aiohttp.ClientConnectorError):
        reason = "connect_failed"
    elif isinstance(exc, aiohttp.ClientResponseError):
        # status 行・header が HTTP として読めない応答。再送しても直らない
        reason = "response_invalid"
    elif isinstance(exc, (aiohttp.ClientError, OSError)):
        # 切断・本文の途中切れ (ClientPayloadError 等) は一時的な通信失敗
        reason = "connection_lost"
    else:
        return DeliveryError("transport_failed")
    return DeliveryError(reason, retryable=reason in _RETRYABLE)
