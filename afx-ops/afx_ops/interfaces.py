"""本機の up 状態の interface が持つ IPv4 address の権威的な列挙。

非 loopback の待受 address が本機に実在することの確認と、`afx-ops interfaces` の出力は、
この module の 1 実装だけを使う。列挙は iproute2 の `ip -j addr show up` を有界の
subprocess として 1 回だけ実行して得る (interface ごとに address を 1 個だけ返す ioctl は
secondary address を列挙できないので使わない)。

executable は固定の絶対 path 候補の先頭に見つかった regular file で、`PATH` は使わない。
argv は配列で shell を介さず、stdin は閉じ、stderr は捨て、cwd は `/`、env は `LC_ALL=C`
だけ。timeout と stdout の上限を超えたら子 process を kill し、どの終了経路でも
`waitpid` で回収してから pipe fd を閉じる。列挙できなかった場合は部分結果を使わず
`InterfaceLookupError` (固定 reason `interface_lookup_failed`) で止める。
"""
from __future__ import annotations

import ipaddress
import json
import os
import select
import stat
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass

from afx_ops.isolation.accept_loop import LOOPBACK_NETWORK

REASON_LOOKUP_FAILED = "interface_lookup_failed"

IP_CANDIDATES: tuple[str, ...] = ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip")
IP_ARGS: tuple[str, ...] = ("-j", "addr", "show", "up")
IP_TIMEOUT = 5.0
MAX_STDOUT = 65536
# deadline を確かめる間隔。timeout はこの粒度で超過し得る
_SLICE = 0.05
_READ = 8192

SCOPE_ORDER = {"global": 0, "link": 1}
_LINK_LOCAL = ipaddress.IPv4Network("169.254.0.0/16")


class InterfaceLookupError(Exception):
    """列挙できなかった。理由の詳細は持たない。"""

    reason = REASON_LOOKUP_FAILED

    def __init__(self) -> None:
        super().__init__(REASON_LOOKUP_FAILED)


@dataclass(frozen=True)
class Candidate:
    address: str
    ifindex: int
    scope: str


def _resolve_ip(candidates: Sequence[str]) -> str:
    for path in candidates:
        try:
            if stat.S_ISREG(os.stat(path).st_mode):
                return path
        except (OSError, ValueError):
            continue
    raise InterfaceLookupError


def _run_bounded(executable: str, timeout: float) -> bytes:
    """`ip` を 1 回走らせ、stdout を上限つきで読んで返す。失敗は `InterfaceLookupError`。"""
    try:
        proc = subprocess.Popen(
            [executable, *IP_ARGS], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd="/", env={"LC_ALL": "C"}, close_fds=True,
            shell=False)
    except (OSError, ValueError):
        raise InterfaceLookupError from None
    out = proc.stdout
    try:
        assert out is not None
        fd = out.fileno()
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InterfaceLookupError
            ready, _w, _x = select.select([fd], [], [], min(_SLICE, remaining))
            if not ready:
                continue
            chunk = os.read(fd, _READ)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_STDOUT:
                raise InterfaceLookupError
            chunks.append(chunk)
        try:
            rc = proc.wait(timeout=max(0.0, deadline - time.monotonic()) + _SLICE)
        except subprocess.TimeoutExpired:
            raise InterfaceLookupError from None
        if rc != 0:
            raise InterfaceLookupError
        return b"".join(chunks)
    except InterfaceLookupError:
        raise
    except (OSError, ValueError):
        raise InterfaceLookupError from None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if out is not None:
            out.close()


def _is_int(value: object) -> bool:
    return type(value) is int


def _classify(address: str) -> str | None:
    """候補にできる unicast IPv4 の scope。loopback・multicast・unspecified・broadcast は None。"""
    ip = ipaddress.IPv4Address(address)
    if (ip in LOOPBACK_NETWORK or ip.is_multicast or ip.is_unspecified
            or ip == ipaddress.IPv4Address("255.255.255.255")):
        return None
    return "link" if ip in _LINK_LOCAL else "global"


def sort_key(candidate: Candidate) -> tuple[int, int, int]:
    """(scope の順 global, link、ifindex、address の数値) の順。"""
    return (SCOPE_ORDER[candidate.scope], candidate.ifindex,
            int(ipaddress.IPv4Address(candidate.address)))


def parse_ip_json(raw: bytes) -> tuple[Candidate, ...]:
    """`ip -j addr show up` の出力を検査して候補にする。違反は `InterfaceLookupError`。"""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InterfaceLookupError from None
    if type(document) is not list:
        raise InterfaceLookupError
    found: dict[str, Candidate] = {}
    for entry in document:
        if type(entry) is not dict:
            raise InterfaceLookupError
        ifindex, infos = entry.get("ifindex"), entry.get("addr_info")
        if not _is_int(ifindex) or ifindex < 1 or type(infos) is not list:
            raise InterfaceLookupError
        for info in infos:
            if type(info) is not dict:
                raise InterfaceLookupError
            if info.get("family") != "inet":
                continue
            local, prefixlen = info.get("local"), info.get("prefixlen")
            if type(local) is not str or not _is_int(prefixlen) or not 0 <= prefixlen <= 32:
                raise InterfaceLookupError
            try:
                canonical = str(ipaddress.IPv4Address(local)) == local
            except ValueError:
                canonical = False
            if not canonical:
                raise InterfaceLookupError
            scope = _classify(local)
            if scope is None:
                continue
            current = found.get(local)
            if current is None or ifindex < current.ifindex:
                found[local] = Candidate(local, ifindex, scope)
    return tuple(sorted(found.values(),
                        key=sort_key))


def list_interfaces(*, candidates: Sequence[str] | None = None,
                    timeout: float | None = None) -> tuple[Candidate, ...]:
    """up 状態の interface の global / link unicast IPv4 を安定 sort で返す。

    probe・production の起動・`afx-ops interfaces` が共用する。
    """
    executable = _resolve_ip(IP_CANDIDATES if candidates is None else candidates)
    raw = _run_bounded(executable, IP_TIMEOUT if timeout is None else timeout)
    return parse_ip_json(raw)


def render_json(found: Sequence[Candidate]) -> str:
    """`afx-ops interfaces` の成功出力 (UTF-8、末尾に改行 1 個)。"""
    document = {"interfaces": [{"address": c.address, "ifindex": c.ifindex, "scope": c.scope}
                               for c in found], "version": 1}
    return json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
