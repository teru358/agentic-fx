"""HTTP Basic 認証と認証失敗の rate limit。

username は固定の `afx`、secret は `web.password`。比較は `b"afx:" + password` 全体と
decoded credential 全体をそれぞれ SHA-256 にした 32 byte 同士を `hmac.compare_digest` で
ちょうど 1 回行う (rate limit 中を除く)。欠落・重複・scheme 違い・壊れた Base64・上限超過は
固定の dummy 入力の digest を比較し、失敗の形が外から区別できないようにする。

rate limit は認証失敗だけを monotonic 時計の 60 秒窓に記録する。送信元 IPv4 ごとに 5 回、
process 全体で 100 回。上限に達した試行は比較せず 429 にし、どの窓にも加えない。
送信元は呼出し側が渡す accepted socket の peer だけで、header からは導かない。
journal と counter に IP、Host、Authorization、username、password を入れない。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from afx_ops.isolation.accept_loop import Journal

USERNAME = b"afx"
REALM_HEADER = 'Basic realm="afx"'
WINDOW_SECONDS = 60.0
SOURCE_LIMIT = 5
GLOBAL_LIMIT = 100
MAX_TOKEN_BYTES = 176
MAX_CREDENTIAL_BYTES = 132
_DUMMY = b"\x00dummy-credential-for-uniform-failure"

OK = "ok"
UNAUTHORIZED = "unauthorized"
LIMITED = "limited"


@dataclass(frozen=True)
class Decision:
    outcome: str
    retry_after: int | None = None


def _retry_after(oldest: float, now: float) -> int:
    return min(60, max(1, math.ceil(oldest + WINDOW_SECONDS - now)))


def _credential(values: list[str]) -> bytes | None:
    """Authorization の field-value が canonical な Basic のときだけ decoded credential。

    wire の値は `Basic`、ちょうど 1 個の SP、token の順だけを許す。値の前の区切りとしての
    SP 0 個又は 1 個は呼出し側が `:` の後から外して渡す。
    """
    if len(values) != 1:
        return None
    value = values[0]
    if type(value) is not str or not value.isascii():
        return None
    scheme, sep, token = value.partition(" ")
    if scheme.lower() != "basic" or not sep or not token or len(token) > MAX_TOKEN_BYTES:
        return None
    encoded = token.encode("ascii")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    if base64.b64encode(decoded) != encoded or len(decoded) > MAX_CREDENTIAL_BYTES:
        return None
    return decoded


class BasicAuthenticator:
    """認証と rate limit を 1 つの lock の下で行う。worker thread から共有される。"""

    def __init__(self, password: bytes, *, clock: Callable[[], float] = time.monotonic,
                 journal: Journal | None = None) -> None:
        if type(password) is not bytes or not password:
            raise ValueError("password")
        self._password = bytes(password)
        self._clock = clock
        self._journal = journal if journal is not None else Journal()
        self._lock = threading.Lock()
        self._global: deque[float] = deque()
        self._sources: dict[str, deque[float]] = {}
        self.web_auth_failed_total = 0
        self.web_auth_limited_total = 0

    def __repr__(self) -> str:
        return "BasicAuthenticator()"

    def authenticate(self, values: list[str], peer: str) -> Decision:
        """Authorization の wire の値の list と accepted peer の IPv4 から判定する。

        Authorization が 1 本も無い要求は challenge として 401 を返し、失敗には数えない。
        journal は lock の外で書く。
        """
        event, decision = self._decide(values, peer)
        if event is not None:
            self._journal.sampled(event)
        return decision

    def _decide(self, values: list[str], peer: str) -> tuple[str | None, Decision]:
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._global) >= GLOBAL_LIMIT:
                return self._limited(self._global[0], now)
            queue = self._sources.get(peer)
            if queue is not None and len(queue) >= SOURCE_LIMIT:
                return self._limited(queue[0], now)
            if not values:
                return None, Decision(UNAUTHORIZED)
            credential = _credential(values)
            expected = hashlib.sha256(b"afx:" + self._password).digest()
            presented = hashlib.sha256(_DUMMY if credential is None else credential).digest()
            matched = hmac.compare_digest(expected, presented)
            if matched and credential is not None:
                return None, Decision(OK)
            self._global.append(now)
            if queue is None:
                queue = self._sources[peer] = deque()
            queue.append(now)
            self.web_auth_failed_total += 1
            return "web_auth_failed", Decision(UNAUTHORIZED)

    def _limited(self, oldest: float, now: float) -> tuple[str, Decision]:
        self.web_auth_limited_total += 1
        return "web_auth_limited", Decision(LIMITED, _retry_after(oldest, now))

    def _prune(self, now: float) -> None:
        while self._global and now - self._global[0] >= WINDOW_SECONDS:
            self._global.popleft()
        for peer, queue in list(self._sources.items()):
            while queue and now - queue[0] >= WINDOW_SECONDS:
                queue.popleft()
            if not queue:
                del self._sources[peer]
