"""CONNECT 先 host の解決と、mission 内で固定する address 集合。

host ごとに最初の解決を一度だけ行い、全 address が global のときだけ集合を固定する。
後から返る別の答えで接続先が入れ替わる (DNS rebinding) のを防ぐため、再解決しない。
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Callable

from agentic_fx.egress.policy import (
    CONNECT_PORT,
    RESOLVER_MAX_THREADS,
    RESOLVER_TIMEOUT_SEC,
    connect_hosts,
    is_connect_host_allowed,
)

# global でない範囲の閉集合。IANA の IPv4 / IPv6 special-purpose address registry で
# Globally Reachable が False または N/A の範囲を並べ、行ごとに registry 上の名前を添える。
# 2001::/23 は親の割当ごと拒否し、その内側の個別行は registry との対応が読めるよう残す。
# ipaddress の is_global は Python の版で 6to4 / NAT64 などの扱いが変わるので判定には使わず、
# この表との照合だけに ipaddress を使う。registry で例外つきの範囲 (192.0.0.0/24 内の一部、
# 64:ff9b::/96 など) は例外を設けず範囲ごと拒否する (埋込 IPv4 を経由した迂回を許さないため)。
_DENIED_V4_CIDRS = (
    ("0.0.0.0/8", "This network"),
    ("10.0.0.0/8", "Private-Use"),
    ("100.64.0.0/10", "Shared Address Space"),
    ("127.0.0.0/8", "Loopback"),
    ("169.254.0.0/16", "Link Local"),
    ("172.16.0.0/12", "Private-Use"),
    ("192.0.0.0/24", "IETF Protocol Assignments"),
    ("192.0.2.0/24", "Documentation (TEST-NET-1)"),
    ("192.88.99.0/24", "Deprecated (6to4 Relay Anycast)"),
    ("192.168.0.0/16", "Private-Use"),
    ("198.18.0.0/15", "Benchmarking"),
    ("198.51.100.0/24", "Documentation (TEST-NET-2)"),
    ("203.0.113.0/24", "Documentation (TEST-NET-3)"),
    ("224.0.0.0/4", "Multicast"),
    ("240.0.0.0/4", "Reserved"),
    ("255.255.255.255/32", "Limited Broadcast"),
)
_DENIED_V6_CIDRS = (
    ("::/96", "Unspecified / Loopback / IPv4-compatible"),
    ("::ffff:0:0/96", "IPv4-mapped Address"),
    ("64:ff9b::/96", "IPv4-IPv6 Translat."),
    ("64:ff9b:1::/48", "IPv4-IPv6 Translat. (local-use)"),
    ("100::/64", "Discard-Only Address Block"),
    ("100:0:0:1::/64", "Dummy IPv6 Prefix"),
    ("2001::/23", "IETF Protocol Assignments"),
    ("2001::/32", "TEREDO"),
    ("2001:1::1/128", "Port Control Protocol Anycast"),
    ("2001:1::2/128", "Traversal Using Relays around NAT Anycast"),
    ("2001:2::/48", "Benchmarking"),
    ("2001:3::/32", "AMT"),
    ("2001:4:112::/48", "AS112-v6"),
    ("2001:10::/28", "Deprecated (previously ORCHID)"),
    ("2001:20::/28", "ORCHIDv2"),
    ("2001:30::/28", "Drone Remote ID Protocol Entity Tags (DETs) Prefix"),
    ("2001:db8::/32", "Documentation"),
    ("2002::/16", "6to4"),
    ("3fff::/20", "Documentation"),
    ("5f00::/16", "Segment Routing (SRv6) SIDs"),
    ("fc00::/7", "Unique-Local"),
    ("fe80::/10", "Link-Local Unicast"),
    ("fec0::/10", "Site-Local (deprecated)"),
    ("ff00::/8", "Multicast"),
)
_DENIED_V4 = tuple(ipaddress.ip_network(c) for c, _ in _DENIED_V4_CIDRS)
_DENIED_V6 = tuple(ipaddress.ip_network(c) for c, _ in _DENIED_V6_CIDRS)


def is_eligible_address(text: str) -> bool:
    """global な address だけ True。解釈できない文字列は False。"""
    if "%" in text:
        return False  # scope id つきは解釈の余地を残さず不適格にする
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    denied = _DENIED_V4 if addr.version == 4 else _DENIED_V6
    return not any(addr in net for net in denied)


class ResolveError(Exception):
    """proxy が CONNECT に返す status を持つ解決失敗。"""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"{status} {reason}")
        self.status = status
        self.reason = reason


GetAddrInfo = Callable[..., list]


class _Entry:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.addresses: tuple[str, ...] | None = None
        self.error: ResolveError | None = None


class MissionResolver:
    """mission 一つ分の resolver。host ごとの結果 (成功も失敗も) を捨てずに固定する。

    backend の許可 host 以外は entry も thread も作らずに拒否する。許可判定を呼び出し側の
    順序に任せると、非信頼 CLI が送る未知の authority で待機 thread が増えるため。
    thread は許可 host ごとに高々一本なので、同時に走る解決の数は許可 host 数が上限になる。
    """

    def __init__(
        self,
        backend: str,
        getaddrinfo: GetAddrInfo = socket.getaddrinfo,
        *,
        timeout: float = RESOLVER_TIMEOUT_SEC,
    ) -> None:
        if len(connect_hosts(backend)) > RESOLVER_MAX_THREADS:
            raise ValueError(f"allowed hosts of {backend} exceed the resolver thread limit")
        self._backend = backend
        self._getaddrinfo = getaddrinfo
        self._timeout = timeout
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._threads: list[threading.Thread] = []
        self._closed = False

    def resolve(self, host: str) -> tuple[str, ...]:
        if not is_connect_host_allowed(self._backend, host):
            raise ResolveError(403, "host not allowed")
        with self._lock:
            if self._closed:
                raise ResolveError(503, "resolver closed")
            entry = self._entries.get(host)
            if entry is None:
                entry = _Entry()
                self._entries[host] = entry
                t = threading.Thread(target=self._work, args=(host, entry), daemon=True)
                self._threads.append(t)
                t.start()
        if not entry.done.wait(self._timeout):
            with self._lock:
                if not entry.done.is_set():
                    # 期限後に戻る結果は採用しない。この host は mission 中 504 のまま
                    entry.error = ResolveError(504, "resolver timeout")
                    entry.done.set()
        if entry.error is not None:
            raise entry.error
        assert entry.addresses is not None
        return entry.addresses

    def fixed_addresses(self, host: str) -> tuple[str, ...] | None:
        with self._lock:
            entry = self._entries.get(host)
            return entry.addresses if entry is not None else None

    def close(self, deadline: float) -> None:
        """以後の resolve を拒否し、走行中の worker を time.monotonic() 基準の deadline まで待つ。

        解決関数は中断できないので、残った worker は daemon thread のまま置き去りにして
        戻る。残した分の結果は採用せず、待っている要求側は 503 で解放する。
        """
        with self._lock:
            self._closed = True
            threads = list(self._threads)
        for t in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(remaining)
        with self._lock:
            for entry in self._entries.values():
                if not entry.done.is_set():
                    entry.error = ResolveError(503, "resolver closed")
                    entry.done.set()

    def _work(self, host: str, entry: _Entry) -> None:
        try:
            result = self._resolve_once(host)
        except Exception:  # noqa: BLE001 - 後処理の失敗も待機側を 504 にせず 502 で解放する
            result = ResolveError(502, "name resolution failed")
        with self._lock:
            if entry.done.is_set():
                return  # 期限切れ・close 後に戻った結果は採用しない
            if isinstance(result, ResolveError):
                entry.error = result
            else:
                entry.addresses = result
            entry.done.set()

    def _resolve_once(self, host: str) -> tuple[str, ...] | ResolveError:
        try:
            infos = self._getaddrinfo(host, CONNECT_PORT, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return ResolveError(502, "name resolution failed")
        except Exception:  # noqa: BLE001 - 解決関数の想定外の失敗も 502 に畳む
            return ResolveError(502, "name resolution failed")
        seen: list[str] = []
        for info in infos:
            sockaddr = info[4]
            text = str(sockaddr[0])
            if text not in seen:
                seen.append(text)
        if not seen:
            return ResolveError(502, "no address")
        if not all(is_eligible_address(a) for a in seen):
            return ResolveError(502, "ineligible address")
        return tuple(seen)
