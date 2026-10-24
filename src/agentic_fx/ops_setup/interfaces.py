"""本機の待受候補の列挙。`afx-ops interfaces` を有界の subprocess で 1 回呼ぶだけで、
afx_ops の package は import しない。

成功契約は rc 0、stderr 空、stdout が UTF-8 の改行 1 個で終わる versioned JSON object 1 個
(version 2、各候補は interface 名 `ifname` を持つ)。
どの失敗 (起動不能・timeout・stdout 上限超過・非 0 rc・signal・stderr 出力・形式違反) も
区別せず `InterfaceLookupFailed` にし、子 process の出力や例外文を持ち出さない。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agentic_fx.ops_setup.feature_config import LOOPBACK, InterfaceLookupFailed

VERSION = 2
TIMEOUT = 10.0
MAX_STDOUT = 65536
_MAX_STDERR = 4096
_READ = 8192
_SLICE = 0.05
_SCOPE_ORDER = {"global": 0, "link": 1}
_LINK_LOCAL = ipaddress.IPv4Network("169.254.0.0/16")
_BROADCAST = ipaddress.IPv4Address("255.255.255.255")


@dataclass(frozen=True)
class Candidate:
    address: str
    ifindex: int
    scope: str
    ifname: str


def _run_bounded(argv: list[str], *, timeout: float) -> bytes:
    """argv を 1 回走らせ、rc 0・stderr 空のときの stdout (上限つき) を返す。

    どの終了経路でも子を kill して wait で回収してから pipe を閉じる。
    """
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env={"PATH": os.defpath},
                                close_fds=True, shell=False)
    except (OSError, ValueError):
        raise InterfaceLookupFailed from None
    out, err = proc.stdout, proc.stderr
    try:
        assert out is not None and err is not None
        deadline = time.monotonic() + timeout
        open_fds = {out.fileno(): "out", err.fileno(): "err"}
        chunks: list[bytes] = []
        size = errsize = 0
        while open_fds:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InterfaceLookupFailed
            ready, _w, _x = select.select(list(open_fds), [], [], min(_SLICE, remaining))
            for fd in ready:
                chunk = os.read(fd, _READ)
                if not chunk:
                    del open_fds[fd]
                    continue
                if open_fds[fd] == "out":
                    size += len(chunk)
                    if size > MAX_STDOUT:
                        raise InterfaceLookupFailed
                    chunks.append(chunk)
                else:
                    errsize += len(chunk)
                    if errsize > _MAX_STDERR:
                        raise InterfaceLookupFailed
        try:
            rc = proc.wait(timeout=max(0.0, deadline - time.monotonic()) + _SLICE)
        except subprocess.TimeoutExpired:
            raise InterfaceLookupFailed from None
        if rc != 0 or errsize:
            raise InterfaceLookupFailed
        return b"".join(chunks)
    except InterfaceLookupFailed:
        raise
    except (OSError, ValueError):
        raise InterfaceLookupFailed from None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if out is not None:
            out.close()
        if err is not None:
            err.close()


_DISPLAY_SAFE = re.compile(r"[a-zA-Z0-9_.-]")


def display_name(ifname: str) -> str:
    """端末への表示用の interface 名。`[a-zA-Z0-9_.-]` 以外の各文字を `?` に置き換える。"""
    return "".join(ch if _DISPLAY_SAFE.fullmatch(ch) else "?" for ch in ifname)


def _scope(address: ipaddress.IPv4Address) -> str | None:
    if (address in LOOPBACK or address.is_multicast or address.is_unspecified
            or address == _BROADCAST):
        return None
    return "link" if address in _LINK_LOCAL else "global"


def _sort_key(candidate: Candidate) -> tuple[int, int, int]:
    return (_SCOPE_ORDER[candidate.scope], candidate.ifindex, int(ipaddress.IPv4Address(candidate.address)))


def parse(raw: bytes) -> tuple[Candidate, ...]:
    """成功出力を契約どおりに検査して候補にする。違反は `InterfaceLookupFailed`。"""
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise InterfaceLookupFailed
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InterfaceLookupFailed from None
    if type(document) is not dict or set(document) != {"interfaces", "version"}:
        raise InterfaceLookupFailed
    if type(document["version"]) is not int or document["version"] != VERSION or type(document["interfaces"]) is not list:
        raise InterfaceLookupFailed
    found: list[Candidate] = []
    for entry in document["interfaces"]:
        if type(entry) is not dict or set(entry) != {"address", "ifindex", "ifname", "scope"}:
            raise InterfaceLookupFailed
        address, ifindex, ifname, scope = entry["address"], entry["ifindex"], entry["ifname"], entry["scope"]
        if (type(address) is not str or type(ifindex) is not int or ifindex < 1
                or type(ifname) is not str or not ifname or scope not in _SCOPE_ORDER):
            raise InterfaceLookupFailed
        try:
            parsed = ipaddress.IPv4Address(address)
        except ValueError:
            raise InterfaceLookupFailed from None
        if str(parsed) != address or _scope(parsed) != scope:
            raise InterfaceLookupFailed
        found.append(Candidate(address, ifindex, scope, ifname))
    if len({c.address for c in found}) != len(found) or found != sorted(found, key=_sort_key):
        raise InterfaceLookupFailed
    return tuple(found)


def list_interfaces(ops_exec: str, *, timeout: float = TIMEOUT) -> tuple[Candidate, ...]:
    """`<ops_exec> interfaces` を呼び、global / link の非 loopback unicast IPv4 の候補を返す。"""
    try:
        argv = [*shlex.split(ops_exec), "interfaces"]
    except ValueError:
        raise InterfaceLookupFailed from None
    return parse(_run_bounded(argv, timeout=timeout))


def provider(resolve_exec: Callable[[], str]) -> Callable[[], Sequence[Candidate]]:
    """起動コマンドの解決を遅らせた候補の取得関数。解決できなければ列挙失敗。"""
    def candidates() -> Sequence[Candidate]:
        try:
            return list_interfaces(resolve_exec())
        except InterfaceLookupFailed:
            raise
        except Exception:
            raise InterfaceLookupFailed from None
    return candidates
