"""web の seccomp は、LAN で待ち受けても緩めない (新規 INET socket の拒否と accept4 だけの許可)。"""
from __future__ import annotations

import errno
import socket

import pytest

from afx_ops.isolation import profiles as P
from afx_ops.isolation.policy import AUDIT_ARCH_X86_64, RET_ALLOW, build_table, evaluate, ret_errno
from afx_ops.isolation.syscalls_x86_64 import SNAPSHOT_NUMBERS as NR

# 緩和していない表の hash。表を変える変更はこの pin の更新を伴うので、レビューで見える
WEB_PINNED = ("fbb6ae842a14dfaec77f127c2a8cd0379c29ec77f935506159211b817db4247a",
              "291b89949f8c88d77aa6fd8af3d2321c293d796d56e96638a85c637b24a99efe")


def _web(name: str, *args: int) -> int:
    table = build_table(P.make_profile("web"), P.CONDITION_LIBRARY)
    return evaluate(table, AUDIT_ARCH_X86_64, NR[name], args)


def test_the_web_table_hashes_are_unchanged():
    """M-T1-SECCOMP-INET: 隔離後の新規 AF_INET socket を許す変更は、この hash を動かす。"""
    assert P.PINNED_HASHES["web"] == WEB_PINNED
    P.feature_filter.cache_clear()
    assert P.feature_filter("web").feature == "web"


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
@pytest.mark.parametrize("kind", [socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_RAW])
@pytest.mark.parametrize("protocol", [0, 6, 17])
def test_new_inet_sockets_are_refused_with_eacces(family, kind, protocol):
    assert _web("socket", family, kind | socket.SOCK_CLOEXEC, protocol) == ret_errno(errno.EACCES)


def test_only_a_unix_stream_socket_can_be_created():
    assert _web("socket", socket.AF_UNIX, socket.SOCK_STREAM, 0) == RET_ALLOW
    assert _web("socket", socket.AF_UNIX, socket.SOCK_DGRAM, 0) == ret_errno(errno.EACCES)
    assert _web("socket", socket.AF_NETLINK, socket.SOCK_RAW, 0) == ret_errno(errno.EAFNOSUPPORT)


def test_accept4_is_the_only_accept_and_peer_lookup_is_allowed():
    assert _web("accept4", 3, 0, 0, 0) == RET_ALLOW
    assert _web("accept", 3, 0, 0) == ret_errno(errno.EPERM)
    assert _web("getpeername", 3, 0, 0) == RET_ALLOW
    assert _web("getsockname", 3, 0, 0) == RET_ALLOW


@pytest.mark.parametrize("name", ["bind", "listen", "sendmsg", "recvmsg", "sendmmsg",
                                  "recvmmsg"])
def test_bind_listen_and_message_calls_stay_denied(name):
    assert _web(name, 3, 0, 0) != RET_ALLOW
