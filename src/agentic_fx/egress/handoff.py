"""supervisor が代行した proxy 宛 connect と、proxy が accept した socket の同期 handoff。

両者が「同じ接続の両端」であることを、cookie ではなく INET_DIAG の inode と tuple で
確かめてから tunnel にする。照合できない接続 (部分木の外から proxy port に来た接続、
port の再利用、競合、IPv4-mapped) は要求行を読まずに閉じさせる (admit が偽を返す)。

1 mission に 1 個作り、同じ object を supervisor (`proxy_handoff=`) と proxy
(`admit=handoff.admit`) の両方に渡す。

- supervisor 側: `add_pending` (supervisor の lock の中、raw connect の前) →
  `wait_ack` (lock の外、connect が 0 / EINPROGRESS のとき) → `remove_pending`
  (supervisor の lock の中、応答の後)。supervisor はこの後に複製を閉じる。
- proxy 側: `admit` を accept thread が accept の直後に同期で呼ぶ。待たない。

lock の順序は supervisor の lock → pending の lock → 停止の lock。admit は pending の lock を
持ったまま supervisor を呼ばず、netlink の lookup も lock の外で行う (lock の中は表の
読み書きと複製の getsockname だけ)。
複製 fd の番号は、entry が表に居て pending の lock を持っている間だけ使う
(supervisor は remove_pending の後に閉じるので、その間は番号が再利用されない)。
"""
from __future__ import annotations

import collections
import os
import socket
import struct
import threading
import time
from collections.abc import Callable

from agentic_fx.core.connect_supervisor import PendingToken
from agentic_fx.egress.inet_diag import R_NOT_INET, TCP_ESTABLISHED, InetDiag, LookupFailed

# supervisor が ack を待つ上限 (supervisor 側の定数と同じ値)
ACK_DEADLINE_SEC = 1.0
SO_COOKIE = 57
_HISTORY_MAX = 256

# 判定の結果 (固定)
V_ACCEPT = "accept"
V_NO_PEER = "no_peer"
V_LOOKUP = "lookup_failed"
V_NOT_INET = "not_inet"
V_STATE = "state"
V_NO_MATCH = "no_match"
V_MULTIPLE = "multiple_match"
V_CHANGED = "relookup_changed"
V_GONE = "pending_gone"
V_ERROR = "error"
V_STOPPING = "stopping"

Tuple = tuple[str, int]
Seam = Callable[[socket.socket, Tuple], None]


def cookie_index(cookie: int) -> int:
    """bucket の索引。cookie は索引にだけ使い、同一性の根拠にしない。"""
    return cookie


class PendingEntry(PendingToken):
    """supervisor が代行中の proxy 宛 connect 1 本。複製 fd は supervisor の所有。"""

    __slots__ = ("dup_fd", "ino", "key", "acked", "event")

    def __init__(self, dup_fd: int, ino: int, key: object) -> None:
        self.dup_fd = dup_fd
        self.ino = ino
        self.key = key
        self.acked: Tuple | None = None
        self.event = threading.Event()


class UnusableEntry(PendingToken):
    """entry を作れなかったときの印。どの接続とも一致せず、wait_ack は待たずに偽。"""

    __slots__ = ()


def _invalidate(entry: PendingEntry) -> None:
    # ino 0 は一致検査で必ず落ち、fd -1 は借りられないので、番号が再利用されても読まれない
    entry.ino = 0
    entry.dup_fd = -1


def _borrow(fd: int) -> socket.socket:
    # 新しい fd を作らずに番号を借りる (fd 枯渇の下でも動く)。使い終わったら detach する
    return socket.socket(fileno=fd)


def _identity(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    s = _borrow(fd)
    try:
        cookie = struct.unpack("Q", s.getsockopt(socket.SOL_SOCKET, SO_COOKIE, 8))[0]
    finally:
        s.detach()
    return st.st_ino, cookie


def _sockname(fd: int) -> object:
    try:
        s = _borrow(fd)
    except (OSError, ValueError):  # 無効にされた entry の fd (-1) は ValueError
        return None
    try:
        return s.getsockname()
    except OSError:
        return None
    finally:
        s.detach()


class SyncHandoff:
    def __init__(self, diag: InetDiag, *,
                 bucket_key: Callable[[int], object] = cookie_index,
                 after_accept: Seam | None = None,
                 between_lookups: Seam | None = None) -> None:
        """`bucket_key`・`after_accept`・`between_lookups` は試験用の注入点 (索引の衝突、
        accept と lookup の間、1 回目と 2 回目の client lookup の間)。settings からは入らない。"""
        self._diag = diag
        self._key = bucket_key
        self._after_accept = after_accept
        self._between_lookups = between_lookups
        self._lock = threading.Lock()
        self._pending: dict[object, list[PendingEntry]] = {}
        self._rec_lock = threading.Lock()
        # 停止の印と、待っている entry の一覧。ack を立てる判定と同じ lock で守り、begin_stop の
        # 後に ack が立たないようにする。pending の lock とは別 (判定の途中で proxy.stop が
        # 呼ばれても詰まらない)
        self._wake_lock = threading.Lock()
        self._stopping = False
        self._waiting: set[PendingEntry] = set()
        self._faults: collections.deque[str] = collections.deque(maxlen=_HISTORY_MAX)
        self._history: collections.deque[dict] = collections.deque(maxlen=_HISTORY_MAX)
        self._counts: collections.Counter[str] = collections.Counter()

    # --- supervisor 側 ---------------------------------------------------------

    def add_pending(self, dup_fd: int) -> PendingToken:
        """待たずに表へ足す。None は返さず、例外も投げない (supervisor の handler の中で
        呼ばれる)。複製の身元が取れない・表に足せないときは、どの接続とも一致しない entry を
        返す。それすら作れない (メモリ不足) ときだけ MemoryError をそのまま上げ、handler の
        fault 経路に任せる。"""
        try:
            ino: int = 0
            key: object = None
            try:
                ino, cookie = _identity(dup_fd)
                key = self._key(cookie)
            except Exception:  # noqa: BLE001 - 一致しない entry にして ack を立てさせない
                ino, key = 0, None
            entry = PendingEntry(dup_fd, ino, key)
            try:
                with self._lock:
                    self._pending.setdefault(key, []).append(entry)
            except Exception:  # noqa: BLE001 - 表に載らなければ誰とも一致しない
                self._fault("add_pending")
                _invalidate(entry)
            self._track(entry)
            return entry
        except Exception:  # noqa: BLE001 - entry すら作れないときは使えない印
            self._fault("add_pending")
        # 呼び出しごとに別の印にする。待ちの一覧には入れない (wait_ack は待たずに偽を返す)
        return UnusableEntry()

    def wait_ack(self, entry: PendingToken, client: Tuple, deadline: float) -> bool:
        """monotonic の deadline まで ack を待つ。ack の tuple が client と一致すれば真。
        timeout・不一致は偽。例外は投げない。lock は取らない (acked は event を set する前に
        書かれるので、set を見た後の読みは書き込みの後になる)。"""
        if not isinstance(entry, PendingEntry):
            return False
        try:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                entry.event.wait(remaining)
            return entry.event.is_set() and entry.acked == client
        except Exception:  # noqa: BLE001 - 待ちの失敗は ack 無しと同じ
            return False

    def remove_pending(self, entry: PendingToken) -> None:
        """bucket から同じ entry を全部除く。複製 fd は閉じない (supervisor の所有)。
        例外は投げない。除けなかった entry は、supervisor が複製を閉じた後の番号の再利用を
        参照しないよう、無効にして以後どの接続とも一致させない。"""
        if not isinstance(entry, PendingEntry):
            return
        try:
            with self._lock:
                bucket = self._pending.get(entry.key)
                if bucket is not None:
                    bucket[:] = [e for e in bucket if e is not entry]
                    if not bucket:
                        del self._pending[entry.key]
        except Exception:  # noqa: BLE001 - 除けなくても無効にして残す
            self._fault("remove_pending")
            _invalidate(entry)
        finally:
            # 表に居なかった entry (追加に失敗した・二重に除いた) も待ちの一覧からは必ず外す
            try:
                with self._wake_lock:
                    self._waiting.discard(entry)
            except Exception:  # noqa: BLE001
                self._fault("remove_pending")

    def _track(self, entry: PendingEntry) -> None:
        try:
            with self._wake_lock:
                self._waiting.add(entry)
                if self._stopping:
                    entry.event.set()  # 停止後に来た待ちは ack を待たせない (acked は空のまま)
        except Exception:  # noqa: BLE001
            self._fault("add_pending")

    def begin_stop(self) -> None:
        """proxy の停止の開始。以後の admit は必ず拒否し、待っている supervisor 側を即座に
        解放する (acked は空のままなので wait_ack は偽)。例外は投げない。"""
        try:
            with self._wake_lock:
                self._stopping = True
                for e in self._waiting:
                    e.event.set()
        except Exception:  # noqa: BLE001
            self._fault("begin_stop")
            self._stopping = True

    def _fault(self, where: str) -> None:
        # 記録そのものの失敗で呼び出し元を落とさない
        try:
            with self._rec_lock:
                self._faults.append(where)
        except Exception:  # noqa: BLE001
            pass

    def faults(self) -> list[str]:
        """pending の操作で握りつぶした例外の場所 (上限つき)。"""
        with self._rec_lock:
            return list(self._faults)

    def pending_count(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._pending.values())

    # --- proxy 側 ----------------------------------------------------------------

    def admit(self, conn: socket.socket) -> bool:
        """accept thread から accept の直後に呼ぶ。真だけが受理。要求は読まない。

        accept thread 1 本で同期に判定するので、判定中は次の accept に進めない。netlink が
        正常なら 1 接続は数 ms だが、応答が止まると lookup 1 回あたり最大
        DIAG_RECV_TIMEOUT_SEC (100 ms) 塞がり、1 接続あたり最大 3 回 (client の lookup・
        server の lookup・再 lookup) で 300 ms になる。外から proxy port へ連続して接続されると、その間の正規の handoff の
        ack が遅れうる (安全側に倒れるだけで誤受理にはならない、可用性の残余)。"""
        ev: dict = {}
        try:
            verdict = self._judge(conn, ev)
        except Exception as exc:  # noqa: BLE001 - 判定の失敗は拒否
            verdict = V_ERROR
            ev["error"] = type(exc).__name__
        ev["verdict"] = verdict
        with self._rec_lock:
            self._history.append(ev)
            self._counts[verdict] += 1
        return verdict == V_ACCEPT

    def _judge(self, conn: socket.socket, ev: dict) -> str:
        # 停止の検査は ack を立てる直前の 1 か所だけ (begin_stop と同じ lock の中)。それより
        # 前で見ても、見た直後に停止が始まりうるので保証にならない
        try:
            peer = conn.getpeername()
            local = conn.getsockname()
        except OSError:
            return V_NO_PEER
        if not (isinstance(peer, tuple) and len(peer) == 2):
            return V_NO_PEER
        ev["peer_port"] = peer[1]
        if self._after_accept is not None:
            self._after_accept(conn, peer)
        try:
            cl = self._diag.lookup(peer, local)
            sv = self._diag.lookup(local, peer)
        except LookupFailed as exc:
            ev["lookup"] = exc.reason
            if exc.discarded:
                ev["discarded"] = exc.discarded
            if exc.reason == R_NOT_INET:
                ev["family"] = exc.family
                return V_NOT_INET
            return V_LOOKUP
        acc_ino = os.fstat(conn.fileno()).st_ino
        ev.update(client_state=cl.state, client_inode=cl.inode, server_state=sv.state,
                  server_inode=sv.inode, accepted_inode=acc_ino)
        if cl.state != TCP_ESTABLISHED or sv.state != TCP_ESTABLISHED:
            return V_STATE
        key = self._key(cl.cookie)
        # 複製 fd を読むのは entry が表に居る間 (pending の lock の中) だけ。lookup は lock の
        # 外で行い、supervisor の add / remove と wait_ack を netlink の待ちで塞がない
        with self._lock:
            bucket = self._pending.get(key, [])
            ev["bucket"] = len(bucket)
            matches = [e for e in bucket
                       if e.ino and e.ino == cl.inode and sv.inode == acc_ino
                       and _sockname(e.dup_fd) == peer]
        ev["matches"] = len(matches)
        if not matches:
            return V_NO_MATCH
        if len(matches) > 1:
            return V_MULTIPLE
        m = matches[0]
        if self._between_lookups is not None:
            self._between_lookups(conn, peer)
        try:
            again = self._diag.lookup(peer, local)
        except LookupFailed as exc:
            ev["relookup"] = exc.reason
            if exc.discarded:
                ev["discarded"] = exc.discarded
            return V_CHANGED
        # 1 回目の後に FIN / RST へ移った client は、同じ inode のままでも受けない
        if (again.state != TCP_ESTABLISHED or again.inode != cl.inode
                or again.cookie != cl.cookie):
            return V_CHANGED
        with self._lock:
            # lookup の間に supervisor が除いた (応答を返し終えた)・無効にした entry には
            # ack を立てない
            if m.ino != cl.inode or not any(e is m for e in self._pending.get(key, ())):
                return V_GONE
            with self._wake_lock:
                # 判定の最中に停止が始まっていたら ack を立てない (立てると tracee の connect が
                # 成功を返した直後に proxy が閉じる)
                if self._stopping:
                    return V_STOPPING
                m.acked = peer
                m.event.set()
        return V_ACCEPT

    # --- 観測 --------------------------------------------------------------------

    def history(self) -> list[dict]:
        """直近の判定 (上限つき)。"""
        with self._rec_lock:
            return [dict(e) for e in self._history]

    def verdicts(self) -> list[str]:
        with self._rec_lock:
            return [e["verdict"] for e in self._history]

    def verdict_counts(self) -> dict[str, int]:
        with self._rec_lock:
            return dict(self._counts)
