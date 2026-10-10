"""NETLINK_SOCK_DIAG による TCP socket の exact lookup。

proxy が accept した接続の両端 (client 側 = peer → proxy、server 側 = proxy → peer) を
kernel に引き、state・inode・cookie を得る。同期 handoff の同一性の根拠 (inode) はここから
来るので、応答は厳しく検査し、少しでも合わなければ lookup の失敗 (= 拒否) にする。

socket は mission worker が outer Landlock の前に作り、mission の終わりまで保持する。
lookup は proxy の accept thread だけが行う (要求と応答を seq で対にするため、thread 間で
共有しない)。CAP_NET_ADMIN は要らない (exact lookup は自 uid の socket を引くだけ)。
"""
from __future__ import annotations

import errno as _errno
import os
import select
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from agentic_fx.egress.proxy import ProxyStartFailed

NETLINK_SOCK_DIAG = 4
SOCK_DIAG_BY_FAMILY = 20
NLMSG_ERROR = 2
NLM_F_REQUEST = 1
NLM_F_MULTI = 2
NLMSG_DONE = 3
INET_DIAG_NOCOOKIE = 0xFFFFFFFF
TCP_ESTABLISHED = 1
# 1 回の応答を待つ上限。accept thread が止まると proxy 全体が止まるので短く切る
# (実測の lookup は 0.1 ms 程度)。settings からは変えない
DIAG_RECV_TIMEOUT_SEC = 0.1

_NLMSGHDR = struct.Struct("=IHHII")
_REQ_HEAD = struct.Struct("=BBBBI")
_MSG_SIZE = 72  # struct inet_diag_msg
_SOCKID_OFF = 4  # inet_diag_msg の中の idiag_sockid の位置
_TAIL = struct.Struct("=IIIII")  # expires, rqueue, wqueue, uid, inode
_RECV_BUF = 8192
# 取り残しを捨てる回数の上限 (受信 queue が無限に湧いても lookup を終わらせる)
_DRAIN_MAX = 64

# lookup の失敗の理由 (固定)
R_ERROR = "error"  # NLMSG_ERROR (ENOENT = 該当なし を含む)
R_TIMEOUT = "timeout"
R_MALFORMED = "malformed"
R_SEQ = "seq"
R_TUPLE = "tuple"
R_NOT_INET = "not_inet"  # AF_INET6 の socket (IPv4-mapped の接続) が返った
R_IO = "io"


class LookupFailed(Exception):
    """`discarded` は自分の seq でないために捨てた応答の件数 (timeout が遅延か、送りつけ
    られた datagram によるものかを後から区別する材料)。"""

    def __init__(self, reason: str, *, errno: int = 0, family: int = 0,
                 discarded: int = 0) -> None:
        detail = reason if not errno else f"{reason}: {_errno.errorcode.get(errno, errno)}"
        if discarded:
            detail = f"{detail} (discarded={discarded})"
        super().__init__(detail)
        self.reason = reason
        self.errno = errno
        self.family = family
        self.discarded = discarded


@dataclass(frozen=True)
class DiagSocket:
    family: int
    state: int
    inode: int
    cookie: int
    uid: int


SocketFactory = Callable[..., socket.socket]


def open_diag_socket(socket_factory: SocketFactory = socket.socket) -> socket.socket:
    """NETLINK_SOCK_DIAG の socket を CLOEXEC で作る。作れなければ ProxyStartFailed。"""
    try:
        return socket_factory(socket.AF_NETLINK, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC,
                              NETLINK_SOCK_DIAG)
    except OSError as exc:
        raise ProxyStartFailed(f"NETLINK_SOCK_DIAG: {exc}") from exc


def _sockid(src: tuple[str, int], dst: tuple[str, int]) -> bytes:
    return (struct.pack("!HH", src[1], dst[1])
            + socket.inet_aton(src[0]) + b"\0" * 12
            + socket.inet_aton(dst[0]) + b"\0" * 12)


def build_request(src: tuple[str, int], dst: tuple[str, int], *, seq: int) -> bytes:
    """AF_INET・TCP の exact lookup の要求。cookie は指定しない (INET_DIAG_NOCOOKIE)。"""
    body = (_REQ_HEAD.pack(socket.AF_INET, socket.IPPROTO_TCP, 0, 0, 0xFFFFFFFF)
            + _sockid(src, dst)
            + struct.pack("=III", 0, INET_DIAG_NOCOOKIE, INET_DIAG_NOCOOKIE))
    return _NLMSGHDR.pack(_NLMSGHDR.size + len(body), SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST,
                          seq, 0) + body


def _attrs_well_formed(data: bytes, off: int) -> bool:
    """本体の後ろの rtattr 列 (kernel が既定で付ける属性) が末尾までちょうど並んでいるか。"""
    while off < len(data):
        if len(data) - off < 4:
            return False
        alen = struct.unpack_from("=H", data, off)[0]
        if alen < 4 or off + alen > len(data):
            return False
        off += (alen + 3) & ~3
    return off == len(data)


def parse_response(data: bytes, *, seq: int, src: tuple[str, int], dst: tuple[str, int],
                   truncated: bool = False, sender_pid: int = 0,
                   portid: int | None = None) -> DiagSocket:
    """1 datagram の応答を検査して取り出す。合わないものは全部 LookupFailed。

    sender_pid は recvmsg の送信元 sockaddr_nl の pid (kernel 起点は 0 だけ)。portid は
    要求した socket 自身の port id で、kernel は応答の nlmsg_pid に宛先として入れる。"""
    if truncated or len(data) < _NLMSGHDR.size or sender_pid != 0:
        raise LookupFailed(R_MALFORMED)
    ln, typ, flags, rseq, pid = _NLMSGHDR.unpack_from(data, 0)
    # exact lookup の応答は kernel が返す単独の 1 message だけ。multi-part (MULTI / DONE) と
    # 自分宛でない datagram (nlmsg_pid が自分の port id と違う) は受けない
    if flags & NLM_F_MULTI or typ == NLMSG_DONE or (portid is not None and pid != portid):
        raise LookupFailed(R_MALFORMED)
    # exact lookup の応答は 1 message だけ。長さが datagram と一致しないものは受けない
    if ln != len(data):
        raise LookupFailed(R_MALFORMED)
    if rseq != seq:
        raise LookupFailed(R_SEQ)
    if typ == NLMSG_ERROR:
        if ln < _NLMSGHDR.size + 4:
            raise LookupFailed(R_MALFORMED)
        err = struct.unpack_from("=i", data, _NLMSGHDR.size)[0]
        # 要求に ACK を付けていないので error 0 も来ないはずの応答として失敗にする
        raise LookupFailed(R_ERROR, errno=-err if err < 0 else 0)
    if typ != SOCK_DIAG_BY_FAMILY:
        raise LookupFailed(R_MALFORMED)
    if ln < _NLMSGHDR.size + _MSG_SIZE or not _attrs_well_formed(data, _NLMSGHDR.size + _MSG_SIZE):
        raise LookupFailed(R_MALFORMED)
    body = data[_NLMSGHDR.size:]
    family, state = body[0], body[1]
    if family != socket.AF_INET:
        raise LookupFailed(R_NOT_INET, family=family)
    if body[_SOCKID_OFF:_SOCKID_OFF + 36] != _sockid(src, dst):
        raise LookupFailed(R_TUPLE)
    c0, c1 = struct.unpack_from("=II", body, _SOCKID_OFF + 40)
    _expires, _rq, _wq, uid, inode = _TAIL.unpack_from(body, _SOCKID_OFF + 48)
    return DiagSocket(family=family, state=state, inode=inode, cookie=c0 | (c1 << 32), uid=uid)


def _pid_of(addr: object) -> int:
    # netlink の sockaddr は (pid, groups)。読めないものは kernel と見なさない
    if isinstance(addr, tuple) and addr and isinstance(addr[0], int):
        return addr[0]
    return -1


def _from_kernel(data: bytes, addr: object, portid: int) -> bool:
    """seq を見る前の検査: 送信元が kernel (pid 0、multicast group 0)、宛先が自分の port id、
    単独の message であること。満たさない datagram は、seq が違っても旧い応答として
    捨てずに失敗にする (捨てると、送りつけられた datagram が lookup を黙って遅らせる)。"""
    if not (isinstance(addr, tuple) and len(addr) >= 2 and addr[0] == 0 and addr[1] == 0):
        return False
    if len(data) < _NLMSGHDR.size:
        return True  # 短すぎる datagram は parse_response が malformed にする
    _ln, typ, flags, _seq, pid = _NLMSGHDR.unpack_from(data, 0)
    return not (flags & NLM_F_MULTI or typ == NLMSG_DONE or pid != portid)


class InetDiag:
    """1 本の NETLINK_SOCK_DIAG socket で exact lookup を行う (1 thread 専用)。

    socket の所有は呼び出し側 (worker が mission の終わりまで保持して閉じる)。"""

    def __init__(self, sock: socket.socket, *, timeout: float = DIAG_RECV_TIMEOUT_SEC) -> None:
        self.sock = sock
        self.timeout = timeout
        self._seq = 0
        # accept thread が lookup ごとに作り直さないよう 1 度だけ作る
        self._poller = select.poll()
        self._poller.register(sock.fileno(), select.POLLIN)

    def _drain(self) -> None:
        # 期限切れで取り残された応答を捨てる。残すと次の要求の seq 検査が永久に外れる
        for _ in range(_DRAIN_MAX):
            try:
                self.sock.recv(_RECV_BUF, socket.MSG_DONTWAIT)
            except OSError:  # 空 (EAGAIN)・割り込み・その他の失敗のどれでも捨てるのをやめる
                return

    def lookup(self, src: tuple[str, int], dst: tuple[str, int]) -> DiagSocket:
        """local = src、remote = dst の TCP socket を引く。失敗は LookupFailed。"""
        self._seq = (self._seq % 0xFFFFFFFF) + 1
        seq = self._seq
        try:
            req = build_request(src, dst, seq=seq)
        except (OSError, ValueError, struct.error) as exc:
            raise LookupFailed(R_MALFORMED) from exc
        self._drain()
        deadline = time.monotonic() + max(0.0, self.timeout)
        try:
            self.sock.send(req)
            # port id は最初の送信で自動に割り当てられる
            portid = _pid_of(self.sock.getsockname())
            # drain の直後に届く遅れた応答 (旧 seq) は、期限までの受信 loop で捨てて自分の
            # seq の応答だけを parse する (捨てずに失敗にすると遅延が続く間ずっと連鎖する)
            discarded = 0
            for _ in range(_DRAIN_MAX):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._poller.poll(remaining * 1000):
                    raise LookupFailed(R_TIMEOUT, discarded=discarded)
                data, _anc, flags, addr = self.sock.recvmsg(_RECV_BUF, 0, socket.MSG_DONTWAIT)
                if not _from_kernel(data, addr, portid):
                    raise LookupFailed(R_MALFORMED, discarded=discarded)
                if len(data) >= _NLMSGHDR.size and _NLMSGHDR.unpack_from(data, 0)[3] != seq:
                    discarded += 1
                    continue
                return parse_response(data, seq=seq, src=src, dst=dst,
                                      truncated=bool(flags & socket.MSG_TRUNC),
                                      sender_pid=_pid_of(addr), portid=portid)
            raise LookupFailed(R_TIMEOUT, discarded=discarded)
        except LookupFailed:
            raise
        except (BlockingIOError, InterruptedError) as exc:
            raise LookupFailed(R_TIMEOUT) from exc
        except OSError as exc:
            raise LookupFailed(R_IO, errno=exc.errno or 0) from exc


def probe_inet_diag(socket_factory: SocketFactory = socket.socket) -> None:
    """admission の probe: socket を作り、自分の loopback の接続 1 対を exact lookup して
    inode が fstat と一致することを確かめる。失敗は ProxyStartFailed。"""
    nl = open_diag_socket(socket_factory)
    held: list[socket.socket] = []
    try:
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        held.append(ls)
        ls.bind(("127.0.0.1", 0))
        ls.listen(1)
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        held.append(c)
        c.connect(ls.getsockname())
        a, peer = ls.accept()
        held.append(a)
        diag = InetDiag(nl)
        cl = diag.lookup(peer, ls.getsockname())
        sv = diag.lookup(ls.getsockname(), peer)
        if (cl.state != TCP_ESTABLISHED or cl.inode != os.fstat(c.fileno()).st_ino
                or sv.inode != os.fstat(a.fileno()).st_ino):
            raise ProxyStartFailed("INET_DIAG の inode が fstat と一致しない")
    except LookupFailed as exc:
        raise ProxyStartFailed(f"INET_DIAG lookup: {exc}") from exc
    except OSError as exc:
        raise ProxyStartFailed(f"INET_DIAG probe: {exc}") from exc
    finally:
        for s in held:
            s.close()
        nl.close()
