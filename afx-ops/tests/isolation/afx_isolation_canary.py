"""隔離関数を呼んだ後に、指定された操作を 1 つ試して結果を 1 行出す helper。

usage: python afx_isolation_canary.py '<json spec>'

spec:
  feature: bot | web | notify
  root: 一時領域 (<root>/<feature>/{config,secret,state}、<root>/keys に実 manifest と
        <principal>.token、<root>/root が instance root)。設定 (OpsConfig) は ops.toml を
        読まずにこの配置から組み立てる (ops.toml の読取りは test_config が受け持つ)
  key_dir / instance_root: 設定の key_dir / instance_root を差し替える
  wait: 省略時は server 認証の待ちを即成功にする。{"interval": s, "limit": s} なら本物の
         wait_ready で待ち、最初の待ちで "WAITING" を出す (自己検査の後には "CHECKED-NOHOLD")
  trace: startup.<名前> の list。呼ばれるたびに "CALL <名前>" を出す
  config_fd: 真なら設定に検査済みの config dir の fd を入れて渡す (起動口と同じ経路)
  op: 試す操作の名前 (下の OPS)
  params: 操作の引数
  nnp: 真なら隔離前に no_new_privs を立てる (unit の NoNewPrivileges=yes の代わり)
  fault: 自己検査の変形 (skip_landlock / skip_seccomp / keep_dumpable / extra_fd /
         extra_thread)。list なら全部を掛ける
  dirs: "xdg" なら設定・状態領域を production と同じ XDG の配置から取る
  plan: "production" なら起動口と同じ `StartupPlan.for_feature` を使う。"widen:<field>" なら
         その plan の境界を 1 つ広げてから起動する (固定境界から外れた plan が止まることの確認)
  raise: {"target": 名前, "nth": n, "exc": 例外名} なら startup.<名前> の n 回目の呼出しで
         その例外を投げる (各段の想定外の失敗が固定 reason になることの確認用)
  fault_self_check: "off" なら自己検査を飛ばす (層を外したときに攻撃が kernel まで届く
         ことを確かめる対照用)
  unit: 真なら unit 下の起動 (cgroup と親) も確かめる
  pause_after_bind: 真なら listener の pre-bind 後・隔離前に "BOUND" を出して stdin を待つ
  wait_before: 真なら "READY <pid> <port>" を出し、stdin の 1 行を待ってから隔離する
  hold_after: 真なら操作の後 "HELD" を出し、stdin の 1 行を待つ
  hold_after_check: 真なら自己検査の成功直後 (READY=1 の送信前) に "CHECKED" を出し、
         stdin の 1 行を待つ
  require_notify: 真なら NOTIFY_SOCKET を必須にする (unit の Type=notify と同じ扱い)
  swap_after_prepare: {"path": p, "to": q} なら領域の検査の直後 (Landlock の前) に p を
         p.orig へ退け、p を q への symlink に置き換える (検査と規則の間の差し替え)
  toml: 真なら web の設定を起動口と同じ `read_config` で XDG の ops.toml から読み、その
         port / bind / auth で plan を作る (設定の違反は起動口と同じ固定 reason で止まる)
  foreign_owner: 真なら ops.toml の所有者として期待する uid を実 uid と別の値にする
         (他 uid の file を実 process で作れないための再現)
  fake_ip: interface 列挙に使う実行 file の path を差し替える
  resolv_conf: 名前解決の規則に使う resolv.conf の path を差し替える (実 /etc/resolv.conf を
         書き換えずに、rename 置換で規則の外れた inode を作る試験のため)

出力: 隔離に成功すると "ISOLATED" を出し、操作の結果を "RESULT <json>"、最後に
token 送信を模した "TOKEN-SENT" を出す。隔離に失敗すると非零終了し、どちらも出さない。
"""
from __future__ import annotations

import ctypes
import json
import os
import resource
import socket
import sys
import threading
import time
from pathlib import Path

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

# -I で起動されるので、試験用 plan の helper (同じ dir) は path を明示して読む
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _test_plans import check_feature_only, test_plan  # noqa: E402
# notify の試験用の名前解決と試験 CA (隔離後は tests 配下を読めないので、ここで読み込む)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
import _notify_seams as notify_seams  # noqa: E402
from afx_ops import api_client, cli  # noqa: E402
from afx_ops.config import OpsConfig  # noqa: E402
from afx_ops.isolation import startup  # noqa: E402
from afx_ops.isolation.syscalls_x86_64 import SNAPSHOT_NUMBERS as NR  # noqa: E402

PRINCIPAL = {"bot": "ops-bot", "web": "ops-web", "notify": "ops-notify"}

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long


def raw(name: str, *args: int) -> list[int]:
    conv = [ctypes.c_long(a) for a in args]
    ctypes.set_errno(0)
    rc = libc.syscall(ctypes.c_long(NR[name]), *conv)
    return [int(rc), ctypes.get_errno() if rc == -1 else 0]


def oserr(fn) -> list[int]:
    try:
        r = fn()
    except OSError as exc:
        return [-1, exc.errno or 0]
    return [0 if r is None else (r if isinstance(r, int) else 0), 0]


def _sockaddr_in(host: str, port: int) -> bytes:
    return (socket.AF_INET).to_bytes(2, "little") + port.to_bytes(2, "big") \
        + socket.inet_aton(host) + b"\0" * 8


def _sockaddr_in6_any(port: int) -> bytes:
    return (socket.AF_INET6).to_bytes(2, "little") + port.to_bytes(2, "big") \
        + b"\0" * 4 + b"\0" * 16 + b"\0" * 4


def _bind_raw(family: int, addr: bytes) -> list[int]:
    # family が作れる機能では本物の socket、作れない機能では UDS の socket で bind を試す
    s = socket.socket(family, socket.SOCK_STREAM) if family is not None else None
    if s is None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    buf = ctypes.create_string_buffer(addr, len(addr))
    try:
        return raw("bind", s.fileno(), ctypes.addressof(buf), len(addr))
    finally:
        s.close()


def op_bind(p, iso):
    host = p["host"]
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    addr = _sockaddr_in6_any(0) if family == socket.AF_INET6 else _sockaddr_in(host, 0)
    usable = None if iso.feature == "web" else family
    return _bind_raw(usable, addr)


def op_listen(p, iso):
    if p.get("listener") and iso.listener is not None:
        return raw("listen", iso.listener.fileno(), 16)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return raw("listen", s.fileno(), 16)
    finally:
        s.close()


def op_ptrace_attach(p, iso):
    return raw("ptrace", 16, p["pid"], 0, 0)


class _IoVec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


def op_process_vm_readv(p, iso):
    buf = ctypes.create_string_buffer(16)
    local = _IoVec(ctypes.addressof(buf), 16)
    remote = _IoVec(0x400000, 16)
    return raw("process_vm_readv", p["pid"], ctypes.addressof(local), 1,
               ctypes.addressof(remote), 1, 0)


def op_process_vm_writev(p, iso):
    buf = ctypes.create_string_buffer(b"x" * 16, 16)
    local = _IoVec(ctypes.addressof(buf), 16)
    remote = _IoVec(0x400000, 16)
    return raw("process_vm_writev", p["pid"], ctypes.addressof(local), 1,
               ctypes.addressof(remote), 1, 0)


def op_kill(p, iso):
    return raw("kill", p["pid"], p.get("sig", 0))


def op_tgkill(p, iso):
    return raw("tgkill", p["pid"], p["pid"], p.get("sig", 0))


def op_listener_misuse(p, iso):
    """保持した listener の fd で bind / listen / 外向き connect / 送信を試す。"""
    fd = iso.listener.fileno()
    addr = _sockaddr_in("127.0.0.1", p["port"])
    buf = ctypes.create_string_buffer(addr, len(addr))
    out = raw("bind", fd, ctypes.addressof(buf), len(addr))
    out += raw("listen", fd, 16)
    out += raw("connect", fd, ctypes.addressof(buf), len(addr))
    out += raw("sendto", fd, ctypes.addressof(buf), 1, 0, ctypes.addressof(buf), len(addr))
    return out


def op_pidfd_getfd(p, iso):
    opened = raw("pidfd_open", p["pid"], 0)
    got = raw("pidfd_getfd", opened[0] if opened[0] >= 0 else 0, 3, 0)
    return opened + got


def op_open(p, iso):
    def go():
        fd = os.open(p["path"], os.O_RDONLY | os.O_CLOEXEC)
        os.close(fd)
    return oserr(go)


def op_read_file(p, iso):
    return oserr(lambda: len(Path(p["path"]).read_bytes()))


def op_fstat_fd(p, iso):
    return oserr(lambda: os.fstat(p["fd"]) and None)


def op_clone(p, iso):
    r = raw("clone", p["flags"], 0, 0, 0, 0)
    if r[0] == 0:
        os._exit(0)
    if r[0] > 0:
        os.waitpid(r[0], 0)
    return r


def op_unshare(p, iso):
    return raw("unshare", 0x10000000)


def op_setns(p, iso):
    return raw("setns", 0, 0)


def op_io_uring_setup(p, iso):
    params = ctypes.create_string_buffer(120)
    return raw("io_uring_setup", 1, ctypes.addressof(params))


def op_bpf(p, iso):
    return raw("bpf", 0, 0, 0)


def op_socket(p, iso):
    r = raw("socket", p["family"], p["type"], p.get("proto", 0))
    if r[0] >= 0:
        os.close(r[0])
    return r


def op_clone3(p, iso):
    return raw("clone3", 0, 0)


def op_thread(p, iso):
    box = []
    t = threading.Thread(target=lambda: box.append(threading.get_native_id()))
    t.start()
    t.join()
    return [len(box), 0]


def op_futex(p, iso):
    word = ctypes.c_int(0)
    return raw("futex", ctypes.addressof(word), 1, 1, 0, 0, 0)  # FUTEX_WAKE


def op_prctl_dumpable(p, iso):
    return raw("prctl", 4, p["value"], 0, 0, 0)


def op_tcp_connect(p, iso):
    def go():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(5)
            s.connect(("127.0.0.1", p["port"]))
            s.sendall(b"hi")
            return len(s.recv(16))
        finally:
            s.close()
    return oserr(go)


def op_tcp_connect_only(p, iso):
    """connect だけして何も送らずに閉じる (待受の有無に依らず Landlock の判定を見る)。"""
    def go():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(5)
            s.connect(("127.0.0.1", p["port"]))
        finally:
            s.close()
    return oserr(go)


def op_udp_echo(p, iso):
    def go():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(5)
            s.sendto(b"dns?", ("127.0.0.1", p["port"]))
            data, _ = s.recvfrom(64)
            return len(data)
        finally:
            s.close()
    return oserr(go)


def op_unix_connect(p, iso):
    def go():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(5)
            s.connect(p["path"])
            s.sendall(b"hi")
            return len(s.recv(16))
        finally:
            s.close()
    return oserr(go)


def op_getaddrinfo(p, iso):
    def go():
        flags = socket.AI_ADDRCONFIG if p.get("addrconfig") else 0
        return len(socket.getaddrinfo(p["host"], 443, type=socket.SOCK_STREAM, flags=flags))
    return oserr(go)


def op_tls_context(p, iso):
    def go():
        import ssl
        ctx = ssl.create_default_context()
        return len(ctx.get_ca_certs()) + ctx.cert_store_stats()["x509_ca"]
    return oserr(go)


def op_tls_files(p, iso):
    def go():
        import ssl
        ssl.create_default_context()
        certs = sorted(n for n in os.listdir("/etc/ssl/certs") if n.endswith(".0"))
        return len(Path("/etc/ssl/certs", certs[0]).read_bytes())
    return oserr(go)


def op_random(p, iso):
    def go():
        import secrets
        import uuid
        return len(secrets.token_hex(8)) + uuid.uuid4().version
    return oserr(go)


def op_stat(p, iso):
    return oserr(lambda: os.stat(p["path"]) and None)


def op_lstat(p, iso):
    return oserr(lambda: os.lstat(p["path"]) and None)


def op_proc_self_fdinfo(p, iso):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return oserr(lambda: len(Path(f"{iso.proc_dir}/fdinfo/{s.fileno()}").read_bytes()))
    finally:
        s.close()


def op_write_state(p, iso):
    def go():
        state = Path(p["dir"])
        tmp = state / "cursor.tmp"
        tmp.write_text("42\n")
        os.replace(tmp, state / "cursor")
        return len((state / "cursor").read_text())
    return oserr(go)


def op_t4_connect(p, iso):
    from afx_ops import server_auth
    try:
        sock = server_auth.connect_verified(Path(p["socket"]), lock_path=Path(p["lock"]),
                                            uid=p["uid"])
    except server_auth.ServerAuthError as exc:
        return [-3, exc.reason]
    try:
        sock.settimeout(5)
        sock.sendall(b"GET /v1/status HTTP/1.1\r\nHost: afx\r\n"
                     b"Authorization: Bearer canary-token\r\nContent-Length: 0\r\n\r\n")
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
        return [len(data), 0]
    finally:
        sock.close()


def op_serve(p, iso):
    """web の自前 accept loop で接続を受け、`handled:<受信>` を返す。"""
    import asyncio
    from afx_ops.isolation.accept_loop import AcceptLoop

    async def main() -> int:
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        handled = []

        async def handle(conn: socket.socket) -> None:
            conn.setblocking(False)
            data = await loop.sock_recv(conn, 64)
            sys.stdout.write("HANDLED\n")
            sys.stdout.flush()
            await loop.sock_sendall(conn, b"handled:" + data)
            conn.close()
            handled.append(1)
            if len(handled) >= p.get("count", 1):
                done.set()

        def on_accept(conn: socket.socket, peer: str) -> None:
            sys.stdout.write("ACCEPTED\n")
            sys.stdout.flush()
            loop.create_task(handle(conn))

        acceptor = AcceptLoop(iso.listener, on_accept)
        acceptor.attach(loop)
        sys.stdout.write("SERVING\n")
        sys.stdout.flush()
        await asyncio.wait_for(done.wait(), 20)
        acceptor.detach(loop)
        return len(handled)

    return [asyncio.run(main()), 0]


NOTIFY_FD: list[int] = []
# socket object も握っておく (参照が切れて GC が閉じると、送信後の明示 close を観測できない)
NOTIFY_SOCKS: list[socket.socket] = []


def op_notify_fd_write(p, iso):
    """隔離前に保持した sd_notify の fd へ書けるか (READY=1 の後は閉じていること)。"""
    if not NOTIFY_FD:
        return [-2, 0]
    return oserr(lambda: os.write(NOTIFY_FD[0], b"MAINPID=1"))


def op_open_fds(p, iso):
    return sorted(startup.open_fds(iso.proc_dir))


def op_import_runtime(p, iso):
    """隔離後に初めて機能の runtime library を import する (遅延 import が許可で通ること)。"""
    import importlib
    for name in p["modules"]:
        importlib.import_module(name)
    return [len(p["modules"]), 0]


def op_snapshot_rounds(p, iso):
    """隔離後の state dir で snapshot の書込み・読戻しを繰り返し、thread からも書く。"""
    from afx_ops.snapshot import read_snapshot, write_snapshot
    fd = os.open(iso.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for i in range(p.get("rounds", 200)):
            write_snapshot(fd, "snapshot.json", {"version": 1, "n": i})
            if read_snapshot(fd, "snapshot.json", 1)["n"] != i:
                return [-1, i]
        lock = threading.Lock()
        errors = []

        def writer(k):
            for j in range(p.get("per_thread", 20)):
                try:
                    # 単一 writer の契約なので、thread からの書込みは呼出し側で直列化する
                    with lock:
                        write_snapshot(fd, "snapshot.json", {"version": 1, "n": k * 1000 + j})
                        read_snapshot(fd, "snapshot.json", 1)
                except Exception as exc:  # noqa: BLE001
                    errors.append(type(exc).__name__)
        threads = [threading.Thread(target=writer, args=(k,)) for k in range(p.get("threads", 4))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            return [-2, errors[0]]
        return [read_snapshot(fd, "snapshot.json", 1)["version"],
                sorted(os.listdir(fd))]
    finally:
        os.close(fd)


def op_body_fds(p, iso):
    """(body の open fd、Isolated.kept_fds、旧い通知 fd への書込みの結果)。"""
    fds = sorted(startup.open_fds(iso.proc_dir))
    return [fds, sorted(iso.kept_fds), op_notify_fd_write(p, iso)]


def op_noop(p, iso):
    return [0, 0]


def op_web_fds(p, iso):
    """隔離後に開いている fd・保持を許した fd・listener の fd・子 process の数。"""
    fds = sorted(startup.open_fds(iso.proc_dir))
    tid_children = 0
    for tid in os.listdir(f"{iso.proc_dir}/task"):
        with open(f"{iso.proc_dir}/task/{tid}/children", encoding="ascii") as f:
            tid_children += len(f.read().split())
    return {"fds": fds, "kept": sorted(iso.kept_fds), "listener": iso.listener.fileno(),
            "children": tid_children}


def op_listdir(p, iso):
    return oserr(lambda: len(os.listdir(p["path"])))


def op_api_token(p, iso):
    import hashlib
    return [type(iso.api_token).__name__, hashlib.sha256(iso.api_token).hexdigest()]


def op_config_view(p, iso):
    return {"instance_root": str(iso.config.instance_root), "key_dir": str(iso.config.key_dir),
            "socket_path": str(iso.endpoint.socket_path),
            "lock_path": str(iso.endpoint.lock_path), "state_dir": str(iso.state_dir),
            "config_dir_fd": iso.config.config_dir_fd}


def op_raise_in_body(p, iso):
    raise RuntimeError(iso.api_token.decode("ascii", "replace") + " /secret/path")


def op_repr_isolated(p, iso):
    text = repr(iso) + str(iso)
    sys.stderr.write(text + "\n")
    return [len(text), 0, len(iso.api_token) == 64, len(iso.secrets) == 1]


def op_client_status(p, iso):
    client = startup.client_for(iso, {"bot": api_client.BotClient, "web": api_client.WebClient,
                                      "notify": api_client.NotifyClient}[iso.feature])
    try:
        reply = client.status() if iso.feature == "web" else client.events()
    except api_client.ApiError as exc:
        return [-1, exc.reason]
    return [reply.status, 0]


def op_notify_runtime(p, iso):
    """notify の配送 loop を試験 CA と試験用の名前解決で回す。

    取得 1 回で 1 件も送らなかったら止める。retry の待ちは 0 にする。結果は
    {"rc", "new_modules" (loop の間に import した module), "after" (続けて試した操作の結果)}。
    """
    import functools

    from afx_ops.notify import runtime, transport
    if p.get("resolver") == "nss":
        factory = notify_seams.nss_resolver_factory(port=p["port"])
    else:
        factory = notify_seams.udp_resolver_factory(dns_port=p["dns_port"], port=p["port"],
                                                    resolv_conf=p.get("resolv_conf"))
    send = functools.partial(transport.post, ssl_context=notify_seams.test_ca_context(p["ca_pem"]),
                             resolver_factory=factory, timeout=p.get("timeout"))
    if p.get("crash_after_2xx"):
        # 2xx を受けた後・snapshot の置換の前に落ちる (kill は seccomp で拒否されるので exit)
        runtime.save = lambda _fd, _state: os._exit(86)
    if p.get("pause"):
        sys.stdout.write("PAUSED\n")
        sys.stdout.flush()
        sys.stdin.readline()
    before = set(sys.modules)
    rc = runtime.run(iso, send=send, sleep=lambda _seconds: None,
                     stop=lambda delivered: delivered == 0)
    new_modules = sorted(set(sys.modules) - before)
    after = [OPS[step["op"]](step.get("params", {}), iso) for step in p.get("after", [])]
    return {"rc": rc, "new_modules": new_modules, "after": after}


def op_msg_raw(p, iso):
    """UDS の socketpair で sendmsg / recvmsg を呼ぶ (引数の検査より前に seccomp が判定する)。"""
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return raw("sendmsg", left.fileno(), 0, 0) + raw("recvmsg", right.fileno(), 0, 0)
    finally:
        left.close()
        right.close()


def op_listener_view(p, iso):
    """保持した listener の属性 (SO_REUSEADDR、型、listen 中か、address、CLOEXEC)。"""
    import fcntl
    sock = iso.listener
    return {"reuseaddr": sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0,
            "type": sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE),
            "acceptconn": sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN),
            "address": list(sock.getsockname()),
            "cloexec": bool(fcntl.fcntl(sock.fileno(), fcntl.F_GETFD) & fcntl.FD_CLOEXEC)}


def op_web_runtime(p, iso):
    """web の runtime を実 listener・実 WebClient (fake daemon) で回す。

    accept を始めたら "SERVING" を出す。requests 本の接続が閉じたら止め、続けて after の
    操作を同じ process で試す。exhaust なら accept を始めた直後に fd を使い切る (runtime は
    戻らず、AcceptLoop の SystemExit で process が終わる)。結果は {"rc", "new_modules", "after"}。
    """
    from afx_ops.web import runtime

    def serving() -> None:
        if p.get("exhaust"):
            _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            resource.setrlimit(resource.RLIMIT_NOFILE, (p.get("nofile", 128), hard))
            held = []
            try:
                while True:
                    held.append(os.dup(0))
            except OSError:
                pass
        sys.stdout.write("SERVING\n")
        sys.stdout.flush()

    before = set(sys.modules)
    rc = runtime.run(iso, on_serving=serving, stop_after=p.get("requests", 1))
    new_modules = sorted(set(sys.modules) - before)
    after = [OPS[step["op"]](step.get("params", {}), iso) for step in p.get("after", [])]
    return {"rc": rc, "new_modules": new_modules, "after": after}
def op_bot_runtime(p, iso):
    """bot の業務処理を loopback の偽 Discord (試験 CA、試験用の名前解決) へ向けて回す。

    stdin に "stop" が来るか stdin が閉じると止める。exit_before_commit / exit_before_record が
    真なら、event の投稿の後・cursor の確定の前 / 承認の投稿の後・対応の確定の前に落ちる
    (kill は seccomp で拒否されるので exit)。結果は {"rc", "new_modules", "after"}。
    """
    import aiohttp

    from afx_ops.bot import adapter as bot_adapter
    from afx_ops.bot import runtime as bot_runtime
    resolver = notify_seams.udp_resolver_factory(dns_port=p["dns_port"], port=p["port"])
    context = notify_seams.test_ca_context(p["ca_pem"])

    def connector():
        return aiohttp.TCPConnector(ssl=context, resolver=resolver(), use_dns_cache=False,
                                    limit=0)
    stop = threading.Event()

    def watch_stdin():
        for line in sys.stdin:
            if line.strip() == "stop":
                break
        stop.set()
    threading.Thread(target=watch_stdin, daemon=True).start()
    if p.get("exit_before_commit"):
        bot_adapter.BotAdapter._commit_post = lambda *_args: os._exit(86)
    if p.get("exit_before_record"):
        bot_adapter.BotAdapter._commit_new_record = lambda *_args: os._exit(87)
    offset = float(p.get("now_offset", 0))
    sys.stdout.write("BOT-RUNNING\n")
    sys.stdout.flush()
    before = set(sys.modules)
    rc = bot_runtime.run(iso, connector_factory=connector,
                         api_base=f"https://{p['host']}/api/v10",
                         gateway_url=f"wss://{p['host']}/gw", stop=stop,
                         poll_interval=p.get("poll", 0.2), now=lambda: time.time() + offset)
    new_modules = sorted(set(sys.modules) - before)
    after = [OPS[step["op"]](step.get("params", {}), iso) for step in p.get("after", [])]
    return {"rc": rc, "new_modules": new_modules, "after": after}


OPS = {name[3:]: fn for name, fn in globals().items() if name.startswith("op_")}


def _plan(spec: dict) -> tuple[startup.StartupPlan, object]:
    """(plan, plan 検査)。production / widen は本番の検査、test は機能名だけの検査。

    test の plan は領域を <root>/<feature>/ に向ける (secret は機能の固定の名前のまま)。
    """
    root = Path(spec["root"])
    feature = spec["feature"]
    own_key = f"{PRINCIPAL[feature]}.token"

    plan_kind = spec.get("plan", "test")
    if plan_kind != "test":
        plan = startup.StartupPlan.for_feature(feature, web_port=spec.get("web_port", 0),
                                               web_bind=spec.get("web_bind", "127.0.0.1"))
        if plan_kind.startswith("widen:"):
            import dataclasses
            field = plan_kind.split(":", 1)[1]
            other = next(f for f in startup.PLANS if f != feature)
            plan = dataclasses.replace(plan, **{
                "connect_ports": {"connect_ports": (443, 22)},
                "check_unit": {"check_unit": False},
                "require_notify": {"require_notify": False},
                "config_dir": {"config_dir": root / "keys"},
                "state_dir": {"state_dir": root},
                "secret_dir": {"secret_dir": startup.feature_dirs(other).secret},
                "secret_files": {"secret_files": (own_key,)},
            }[field])
        return plan, startup.check_plan
    if spec.get("dirs") == "xdg":
        # 領域と secret は production と同じ XDG の配置 (環境変数で一時領域へ向ける)
        return test_plan(feature, web_port=spec.get("web_port", 0),
                         web_bind=spec.get("web_bind", "127.0.0.1")), check_feature_only
    params = spec.get("params", {})
    ports = params.get("connect_ports")
    return test_plan(
        feature, config_dir=root / feature / "config", state_dir=root / feature / "state",
        secret_dir=root / feature / "secret",
        web_port=spec.get("web_port", 0) if feature == "web" else None,
        web_bind=spec.get("web_bind", "127.0.0.1"),
        connect_ports=tuple(ports) if ports is not None else None,
        check_unit=bool(spec.get("unit")),
        require_notify=bool(spec.get("require_notify"))), check_feature_only


def _config(spec: dict) -> OpsConfig:
    """配置から設定を組み立てる。config dir の fd は渡さず、隔離準備が plan の path から開く。"""
    root = Path(spec["root"])
    extra = {}
    if spec["feature"] == "web":
        extra["port"] = spec.get("web_port", 0)
        if "web_bind" in spec:
            extra["bind"] = spec["web_bind"]
        if "web_allow" in spec:
            import ipaddress
            extra["allow"] = tuple(ipaddress.IPv4Network(item) for item in spec["web_allow"])
    elif spec["feature"] == "bot":
        extra = {"guild_id": 123456789012345678, "channel_id": 223456789012345678}
        # bot の allowlist・mention (ops.toml の読取りは test_config が受け持つ)
        for key, value in spec.get("bot", {}).items():
            extra[key] = tuple(value) if isinstance(value, list) else value
    elif spec["feature"] == "notify":
        extra = dict(spec.get("notify", {}))
    # config_fd が真なら production と同じく検査済みの config dir の fd を渡す
    fd = (startup.open_config_dir(root / spec["feature"] / "config") if spec.get("config_fd")
          else None)
    return OpsConfig(instance_root=Path(spec.get("instance_root", root / "root")),
                     key_dir=Path(spec.get("key_dir", root / "keys")),
                     config_dir_fd=fd, **extra)


def _wait(spec: dict):
    how = spec.get("wait")
    if how is None:
        return lambda endpoint: None
    announced = []

    def sleep(seconds: float) -> None:
        if not announced:
            announced.append(1)
            sys.stdout.write("WAITING\n")
            sys.stdout.flush()
        time.sleep(seconds)

    return lambda endpoint: api_client.wait_ready(endpoint, interval=how["interval"],
                                                   limit=how["limit"], sleep=sleep)


def _trace(names) -> None:
    for name in names:
        original = getattr(startup, name)

        def traced(*args, _name=name, _original=original, **kwargs):
            sys.stdout.write(f"CALL {_name}\n")
            sys.stdout.flush()
            return _original(*args, **kwargs)
        setattr(startup, name, traced)


def _inject(fault) -> None:
    if isinstance(fault, list):
        for one in fault:
            _inject(one)
        return
    if fault == "skip_landlock":
        startup.apply_landlock = lambda plan, proc, dirs: None
    elif fault == "skip_seccomp":
        startup.apply_seccomp = lambda compiled: None
    elif fault == "keep_dumpable":
        startup.set_dumpable_zero = lambda: None
    elif fault == "extra_fd":
        original = startup.close_unlisted_fds

        def leave_one(proc, keep):
            original(proc, keep)
            os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
        startup.close_unlisted_fds = leave_one
    elif fault == "extra_thread":
        original_dumpable = startup.set_dumpable_zero
        stop = threading.Event()

        def with_thread():
            threading.Thread(target=stop.wait, daemon=True).start()
            original_dumpable()
        startup.set_dumpable_zero = with_thread


def _inject_raise(target: str, nth: int, exc_name: str) -> None:
    original = getattr(startup, target)
    calls = [0]
    exc_type = {"OSError": OSError, "ValueError": ValueError, "RuntimeError": RuntimeError,
                "KeyError": KeyError}[exc_name]

    def failing(*args, **kwargs):
        calls[0] += 1
        if calls[0] == nth:
            raise exc_type(5, "injected") if exc_type is OSError else exc_type("injected")
        return original(*args, **kwargs)
    setattr(startup, target, failing)


def main() -> int:
    spec = json.loads(sys.argv[1])
    if spec.get("nnp", True):
        libc.prctl(38, 1, 0, 0, 0)
    _inject(spec.get("fault"))
    if spec.get("raise"):
        r = spec["raise"]
        _inject_raise(r["target"], r["nth"], r["exc"])
    if spec.get("pause_after_bind"):
        original_close = startup.close_unlisted_fds

        def pause(proc, keep):
            sys.stdout.write("BOUND\n")
            sys.stdout.flush()
            sys.stdin.readline()
            original_close(proc, keep)
        startup.close_unlisted_fds = pause
    original_connect = startup.connect_notify

    def remember_notify(value, *, required):
        sock = original_connect(value, required=required)
        if sock is not None:
            NOTIFY_FD.append(sock.fileno())
            NOTIFY_SOCKS.append(sock)
        return sock
    startup.connect_notify = remember_notify
    if spec.get("hold_after_check"):
        original_check = startup.self_check

        def check_then_hold(proc, **kw):
            original_check(proc, **kw)
            sys.stdout.write("CHECKED\n")
            sys.stdout.flush()
            sys.stdin.readline()
        startup.self_check = check_then_hold
    if spec.get("swap_after_prepare"):
        swap = spec["swap_after_prepare"]
        original_prepare = startup.prepare_dirs

        def prepare_then_swap(plan, **kwargs):
            dirs = original_prepare(plan, **kwargs)
            os.rename(swap["path"], swap["path"] + ".orig")
            os.symlink(swap["to"], swap["path"])
            return dirs
        startup.prepare_dirs = prepare_then_swap
    if spec.get("wait") is not None and not spec.get("hold_after_check"):
        original_self_check = startup.self_check

        def check_then_mark(proc, **kw):
            original_self_check(proc, **kw)
            sys.stdout.write("CHECKED-NOHOLD\n")
            sys.stdout.flush()
        startup.self_check = check_then_mark
    _trace(spec.get("trace", ()))
    if spec.get("fault_self_check") == "off":
        startup.self_check = lambda proc, **kw: None
    if spec.get("wait_before"):
        port = 0
        sys.stdout.write(f"READY {os.getpid()} {port}\n")
        sys.stdout.flush()
        sys.stdin.readline()
    if spec.get("resolv_conf"):
        startup.RESOLV_CONF = spec["resolv_conf"]
    if spec.get("foreign_owner"):
        from afx_ops import config as ops_config
        ops_config._expected_owner_uid = lambda: os.getuid() + 1
    if spec.get("fake_ip"):
        from afx_ops import interfaces
        interfaces.IP_CANDIDATES = (spec["fake_ip"],)
    config = None
    if spec.get("toml"):
        from afx_ops.config import ConfigError, read_config
        try:
            config = read_config("web")
        except ConfigError as exc:
            print(f"afx-ops: {exc.reason}", file=sys.stderr, flush=True)
            return startup.RC_ISOLATION_FAILED
        plan, plan_check = (test_plan("web", web_port=config.port, web_bind=config.web_bind,
                                      web_auth=config.auth), check_feature_only)
    else:
        plan, plan_check = _plan(spec)

    def body(iso: startup.Isolated) -> int:
        sys.stdout.write("ISOLATED\n")
        sys.stdout.flush()
        result = OPS[spec["op"]](spec.get("params", {}), iso)
        sys.stdout.write("RESULT " + json.dumps(result) + "\n")
        sys.stdout.write("TOKEN-SENT\n")
        sys.stdout.flush()
        if spec.get("hold_after"):
            sys.stdout.write("HELD\n")
            sys.stdout.flush()
            sys.stdin.readline()
        return 0

    return startup.run(plan, config if config is not None else _config(spec), body,
                       plan_check=plan_check, wait=_wait(spec),
                       preload=cli.PRELOADS[spec["feature"]])


if __name__ == "__main__":
    raise SystemExit(main())
