"""ops process の起動順と自己検査。

順序は `run` の 1 関数で固定する:

0. plan が機能の固定境界 (`PLANS`) どおりであること、single thread、`NoNewPrivs: 1`
   (unit の `NoNewPrivileges=yes`)、unit の cgroup と親 (direct exec の代替確認) を、
   鍵を読む前に確かめ、`NOTIFY_SOCKET` へ connect する。
1. config dir と state dir を親 dir から symlink を辿らずに開き、所有者・mode・種別を
   確かめる (config が無ければ鍵を読まずに止まる。state は無ければ 0700 で作る)。開いた
   fd は Landlock の規則を付けるまで持つ。機能の固定 principal の API 鍵を、設定の key_dir
   から manifest の state と digest を照合して読む。続けて機能の secret を plan の secret
   dir から、plan が固定した名前の file だけ読む (鍵 dir と secret dir は Landlock の規則に
   入れない)。web は loopback listener を pre-bind + listen して検査する (accept はしない)。
2. 保持してよい fd (0/1/2、web の listener、sd_notify の fd、1 の領域の fd) 以外を全部閉じる。
3. Landlock (領域の規則は 1 で検査した fd に付ける)。領域の fd を閉じる。
4. 機能別 seccomp。
5. `PR_SET_DUMPABLE=0` と自己検査。
6. server 認証 (`wait_ready`、鍵は送らない) が通ったら `READY=1` を送る。成否に依らず
   通知 fd はここで閉じ、body へ渡さない。
7. 業務処理 (body) へ渡す。

どの段の失敗も固定 reason を stderr に 1 行出して非零終了し、READY=1 も送らず body も
呼ばない (= token を送らない)。`/proc/self` は使わず、起動時の `os.getpid()` で解決した
`/proc/<pid>` だけを読む。
"""
from __future__ import annotations

import ctypes
import contextlib
import dataclasses
import errno
import glob
import os
import socket
import stat
import sys
import sysconfig
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeVar

import afx_ops
from afx_ops import keys
from afx_ops.api_client import RC_UNREACHABLE, ApiError, Endpoint, wait_ready
from afx_ops.isolation import bpf, landlock
from afx_ops.isolation._libc import libc as _libc
from afx_ops.isolation.accept_loop import ListenerError, prebind_listener
from afx_ops.isolation.profiles import feature_filter
from afx_ops.isolation.policy import PolicyBuildError

if TYPE_CHECKING:
    from afx_ops.config import OpsConfig

RC_RUNTIME_IMPORT_FAILED = 70
RC_ISOLATION_FAILED = 71
# 隔離と server 認証が済んだ後の、機能本体の想定外の失敗 (起動失敗とは区別する)
RC_BODY_FAILED = 1

REASONS = (
    "multithreaded",
    "unit_no_new_privs",
    "unit_cgroup",
    "unit_parent",
    "seccomp_table_invalid",
    "secrets_failed",
    "listener_invalid",
    "fd_close_failed",
    "landlock_abi_too_old",
    "landlock_failed",
    "seccomp_failed",
    "dumpable_failed",
    "self_check_unreadable",
    "self_check_seccomp",
    "self_check_no_new_privs",
    "self_check_dumpable",
    "self_check_landlock",
    "self_check_fds",
    "self_check_threads",
    "plan_invalid",
    "unit_notify_socket",
    "notify_failed",
    "startup_internal_error",
    "body_failed",
    "config_dir_missing",
    "config_dir_invalid",
    "state_dir_failed",
    "state_dir_invalid",
    "runtime_import_failed",
)


def key_reason(code: str) -> str:
    """鍵の読取り失敗の code を固定 reason にする (`key_` で始まらなければ前置する)。"""
    return code if code.startswith("key_") else f"key_{code}"


REASONS += tuple(dict.fromkeys(key_reason(code) for code in keys.CODES))

# systemd (Type=notify) の通知先。abstract は先頭 '@'、pathname は絶対 path
NOTIFY_ENV = "NOTIFY_SOCKET"
READY_MESSAGE = b"READY=1"

# 自己検査で「Landlock が効いていれば読めない」ことを確かめる path。どの機能の許可にも入らない
LANDLOCK_PROBE_PATH = "/proc/meminfo"

# bot / notify の名前解決と TLS 設定の読込みで glibc / OpenSSL が開く file (strace で確定)
DNS_TLS_FILES = ("/etc/nsswitch.conf", "/etc/host.conf", "/etc/hosts", "/etc/gai.conf",
                 "/etc/ld.so.cache")
# resolv.conf は symlink 自身と辿った先の file の 2 つに file 規則を与え、実体の親 dir には
# 与えない (systemd-resolved の dir には他の設定や varlink socket がある)。resolver が実体を
# rename で置き換えると新しい inode は読めず、名前解決が失敗して機能は非零終了し、systemd の
# 再起動で新しい inode に規則を張り直す (fail closed)。
RESOLV_CONF = "/etc/resolv.conf"
TLS_DIRS = ("/etc/ssl", "/usr/share/ca-certificates")
NSS_MODULE_GLOBS = ("/usr/lib/x86_64-linux-gnu/libnss_*.so.2", "/lib/x86_64-linux-gnu/libnss_*.so.2")

_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4


class StartupError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        assert reason in REASONS, reason
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason


@dataclass(frozen=True)
class FeaturePlan:
    """機能ごとに固定の隔離境界。production の起動は必ずこれに従う。

    connect_ports は Landlock の TCP connect 許可、listener は web の pre-bind の有無。
    secret_files は機能の secret dir から隔離前に読む file 名 (dir の外は指せない)。
    保持 fd は 0/1/2 と listener と sd_notify の fd だけで、追加の Landlock 規則は無い。
    """

    feature: str
    connect_ports: tuple[int, ...]
    listener: bool
    secret_files: tuple[str, ...]


# bot は Discord の bot token、notify は webhook の URL (宛先そのものが送信の鍵)。web は
# 初版は認証を持たないので読む secret が無い
PLANS: Mapping[str, FeaturePlan] = MappingProxyType({
    "bot": FeaturePlan("bot", (443,), listener=False, secret_files=("discord.token",)),
    "web": FeaturePlan("web", (), listener=True, secret_files=()),
    "notify": FeaturePlan("notify", (443,), listener=False, secret_files=("webhook.url",)),
})


# 機能ごとに固定の principal。設定からは指せない (他 principal の鍵を読ませない)
PRINCIPALS: Mapping[str, str] = MappingProxyType({
    "bot": "ops-bot",
    "web": "ops-web",
    "notify": "ops-notify",
})


@dataclass(frozen=True)
class FeatureDirs:
    """機能の領域。config は隔離後も読める、secret は隔離前に読むだけ、state は読み書き。"""

    config: Path
    secret: Path
    state: Path


def _xdg_base(name: str, default: str) -> Path:
    # XDG Base Directory: 未設定・空・相対 path は無視して既定を使う
    value = os.environ.get(name, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path(os.path.expanduser("~")) / default


def feature_dirs(feature: str) -> FeatureDirs:
    """`$XDG_CONFIG_HOME/agentic-fx/ops/<feature>/{config,secret}` と
    `$XDG_STATE_HOME/agentic-fx/ops/<feature>/`。何も作らない。"""
    if feature not in PLANS:
        raise ValueError(feature)
    base = _xdg_base("XDG_CONFIG_HOME", ".config") / "agentic-fx" / "ops" / feature
    state = _xdg_base("XDG_STATE_HOME", ".local/state") / "agentic-fx" / "ops" / feature
    return FeatureDirs(config=base / "config", secret=base / "secret", state=state)


@dataclass(frozen=True)
class StartupPlan:
    """起動 1 回分の plan。production は `for_feature` だけで作る。

    試験用の印は持たない。試験の緩和は `run` の `plan_check` 引数で検査そのものを
    差し替えて行い、production の起動口はその引数を渡さない (既定 = `check_plan`)。
    """

    feature: str
    config_dir: Path
    secret_dir: Path
    secret_files: tuple[str, ...]
    state_dir: Path
    web_port: int | None
    connect_ports: tuple[int, ...]
    check_unit: bool
    require_notify: bool

    @classmethod
    def for_feature(cls, feature: str, *, web_port: int | None = None) -> StartupPlan:
        """機能の固定境界と XDG の領域から plan を作る。

        web の listener の port は web 機能の設定から読む経路が入るまで渡す元が無く、起動口
        (`afx-ops web`) は `listener_invalid` で止まる。試験は一時 port を直接渡す。
        """
        fixed = PLANS[feature]
        dirs = feature_dirs(feature)
        return cls(feature=feature, config_dir=dirs.config, secret_dir=dirs.secret,
                   secret_files=fixed.secret_files, state_dir=dirs.state,
                   web_port=web_port if fixed.listener else None,
                   connect_ports=fixed.connect_ports, check_unit=True, require_notify=True)


def check_plan(plan: StartupPlan) -> None:
    """production の plan が固定の境界から外れていないこと。外れたら `plan_invalid`。"""
    if plan.feature not in PLANS:
        raise StartupError("plan_invalid", "feature")
    fixed = PLANS[plan.feature]
    dirs = feature_dirs(plan.feature)
    if (plan.connect_ports != fixed.connect_ports or not plan.check_unit
            or not plan.require_notify or plan.config_dir != dirs.config
            or plan.secret_dir != dirs.secret or plan.secret_files != fixed.secret_files
            or plan.state_dir != dirs.state or (plan.web_port is not None and not fixed.listener)):
        raise StartupError("plan_invalid")


@dataclass
class Isolated:
    """隔離が済んだ後に body へ渡すもの。

    secret と API 鍵は repr に出さない (ログや例外の文に載せない)。鍵 dir は隔離後の
    Landlock の許可に無いので、鍵はここで受け取ったもの以外に読めない。
    """

    feature: str
    pid: int
    proc_dir: str
    secrets: Mapping[str, bytes] = field(repr=False)
    listener: socket.socket | None
    kept_fds: frozenset[int]
    config: OpsConfig
    endpoint: Endpoint
    api_token: bytes = field(repr=False)
    state_dir: Path


def endpoint_for(instance_root: Path) -> Endpoint:
    """本体の既定の配置 `<instance_root>/data/run/api.sock` と `<instance_root>/data/instance.lock`。"""
    data = Path(instance_root) / "data"
    return Endpoint(socket_path=data / "run" / "api.sock", lock_path=data / "instance.lock")


_Client = TypeVar("_Client")


def client_for(isolated: Isolated, client_cls: type[_Client]) -> _Client:
    """機能の固定 principal の client だけを、受け取った鍵で作る。"""
    if getattr(client_cls, "principal", None) != PRINCIPALS[isolated.feature]:
        raise ValueError("client principal does not match the feature")
    return client_cls.from_token(isolated.endpoint, isolated.api_token)


# --- 純関数 (text の解釈) ------------------------------------------------------

def parse_status(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def cgroup_unit(text: str) -> str | None:
    """`/proc/<pid>/cgroup` (cgroup v2) の末端の名前。"""
    for line in text.splitlines():
        if line.startswith("0::"):
            path = line[3:].rstrip("/")
            return path.rsplit("/", 1)[-1] or None
    return None


def expected_unit(feature: str) -> str:
    return f"afx-ops@{feature}.service"


# --- 各段 --------------------------------------------------------------------

def _read(path: str) -> str:
    with open(path, encoding="ascii", errors="replace") as f:
        return f.read()


def _task_count(proc: str) -> int:
    return len(os.listdir(f"{proc}/task"))


def check_preconditions(plan: StartupPlan, proc: str) -> None:
    try:
        tasks = _task_count(proc)
        status = parse_status(_read(f"{proc}/status"))
        cgroup = _read(f"{proc}/cgroup") if plan.check_unit else ""
    except OSError as exc:
        raise StartupError("self_check_unreadable", str(exc.errno)) from None
    if tasks != 1:
        raise StartupError("multithreaded")
    if status.get("NoNewPrivs") != "1":
        raise StartupError("unit_no_new_privs")
    if plan.check_unit:
        if cgroup_unit(cgroup) != expected_unit(plan.feature):
            raise StartupError("unit_cgroup")
        try:
            parent = _read(f"/proc/{os.getppid()}/comm").strip()
        except OSError:
            parent = ""
        if parent != "systemd":
            raise StartupError("unit_parent")


_SYS_GETDENTS64 = 217


def open_fds(proc: str) -> set[int]:
    """`/proc/<pid>/fd` の fd 集合。一覧のために開いた dir fd 自身は除く。

    `os.listdir(fd)` は内部で fd を dup して数を増やすので、getdents64 を直接読む。
    """
    dirfd = os.open(f"{proc}/fd", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        names = _getdents_names(dirfd)
    finally:
        os.close(dirfd)
    return {int(n) for n in names if n not in (".", "..")} - {dirfd}


def _getdents_names(dirfd: int) -> list[str]:
    libc = _libc()
    buf = ctypes.create_string_buffer(32768)
    names: list[str] = []
    while True:
        n = libc.syscall(ctypes.c_long(_SYS_GETDENTS64), ctypes.c_int(dirfd), buf,
                         ctypes.c_uint(len(buf)))
        if n < 0:
            raise OSError(ctypes.get_errno(), "getdents64")
        if n == 0:
            return names
        raw = buf.raw[:n]
        pos = 0
        while pos < n:
            # struct linux_dirent64: ino u64, off s64, reclen u16, type u8, name
            reclen = int.from_bytes(raw[pos + 16:pos + 18], "little")
            name = raw[pos + 19:pos + reclen].split(b"\0", 1)[0]
            names.append(name.decode("ascii"))
            pos += reclen


def close_unlisted_fds(proc: str, keep: Iterable[int]) -> None:
    keep = set(keep)
    try:
        listed = open_fds(proc)
    except OSError as exc:
        raise StartupError("fd_close_failed", f"list {exc.errno}") from None
    for fd in sorted(listed - keep):
        try:
            os.close(fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise StartupError("fd_close_failed", str(fd)) from None


def runtime_rules() -> list[landlock.PathRule]:
    """interpreter・stdlib・venv・この package の read + execute。"""
    paths = sysconfig.get_paths()
    dirs = {os.path.realpath(paths[k]) for k in ("stdlib", "platstdlib", "purelib", "platlib")}
    dirs.add(os.path.realpath(sys.prefix))
    dirs.add(os.path.realpath(os.path.dirname(afx_ops.__file__)))
    rules = [landlock.PathRule(d, landlock.RUNTIME) for d in sorted(dirs)]
    rules.append(landlock.PathRule(os.path.realpath(sys.executable), landlock.RUNTIME))
    # os.urandom / secrets / uuid4 は隔離後に /dev/urandom を開く
    rules.append(landlock.PathRule("/dev/urandom", landlock.ACCESS_FS_READ_FILE))
    return rules


def dns_tls_rules() -> list[landlock.PathRule]:
    """bot / notify の名前解決と TLS に要る file の読取り。

    resolv.conf は file 単位の規則なので、起動後に実体が rename で置き換わると読めなくなる。
    そのときは名前解決が失敗して機能が非零終了し、systemd の再起動で張り直す (fail closed)。
    """
    rules = [landlock.PathRule(p, landlock.ACCESS_FS_READ_FILE, required=False)
             for p in DNS_TLS_FILES]
    for path in dict.fromkeys((RESOLV_CONF, os.path.realpath(RESOLV_CONF))):
        rules.append(landlock.PathRule(path, landlock.ACCESS_FS_READ_FILE, required=False))
    rules += [landlock.PathRule(p, landlock.READ, required=False) for p in TLS_DIRS]
    seen = set()
    for pattern in NSS_MODULE_GLOBS:
        for path in sorted(glob.glob(pattern)):
            real = os.path.realpath(path)
            if real not in seen:
                seen.add(real)
                rules.append(landlock.PathRule(real, landlock.ACCESS_FS_READ_FILE))
    return rules


def landlock_plan(plan: StartupPlan, proc: str, dirs: PreparedDirs) -> landlock.LandlockPlan:
    rules = runtime_rules()
    # 領域の規則は path を開き直さず、検査済みの fd に付ける (検査後の symlink 差し替えで
    # 別の dir に許可が移らないように)
    rules.append(landlock.PathRule(str(plan.config_dir), landlock.READ, fd=dirs.config_fd))
    rules.append(landlock.PathRule(str(plan.state_dir), landlock.STATE, fd=dirs.state_fd))
    if plan.feature in ("bot", "notify"):
        rules += dns_tls_rules()
    # server 認証の /proc 規則: /proc/locks の leaf と、自 pid の dir だけ
    rules.append(landlock.PathRule("/proc/locks", landlock.ACCESS_FS_READ_FILE))
    rules.append(landlock.PathRule(proc, landlock.READ))
    ports = plan.connect_ports if plan.feature in ("bot", "notify") else ()
    return landlock.LandlockPlan(path_rules=tuple(rules), connect_ports=tuple(ports))


def apply_landlock(plan: StartupPlan, proc: str, dirs: PreparedDirs) -> None:
    try:
        landlock.apply(landlock_plan(plan, proc, dirs))
    except landlock.LandlockError as exc:
        raise StartupError(exc.reason, str(exc)) from None
    except OSError as exc:
        # 規則の path の解決 (realpath 等) の失敗。Landlock は掛かっていない
        raise StartupError("landlock_failed", str(exc.errno)) from None


def apply_seccomp(compiled: bpf.CompiledPolicy) -> None:
    try:
        bpf.install(compiled)
    except bpf.SeccompInstallError as exc:
        raise StartupError("seccomp_failed", str(exc)) from None


def set_dumpable_zero() -> None:
    if _libc().prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise StartupError("dumpable_failed")


def filter_count(proc: str) -> int:
    return int(parse_status(_read(f"{proc}/status")).get("Seccomp_filters", "0"))


def self_check(proc: str, *, filters_before: int, keep: Iterable[int]) -> None:
    """隔離が効いていることを自分で確かめる。外れたら固定 reason。"""
    try:
        status = parse_status(_read(f"{proc}/status"))
        fds = open_fds(proc)
        tasks = _task_count(proc)
    except OSError as exc:
        raise StartupError("self_check_unreadable", str(exc.errno)) from None
    if status.get("Seccomp") != "2" or status.get("Seccomp_filters") != str(filters_before + 1):
        raise StartupError("self_check_seccomp")
    if status.get("NoNewPrivs") != "1":
        raise StartupError("self_check_no_new_privs")
    if _libc().prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise StartupError("self_check_dumpable")
    try:
        fd = os.open(LANDLOCK_PROBE_PATH, os.O_RDONLY | os.O_CLOEXEC)
    except PermissionError:
        pass
    except OSError as exc:
        raise StartupError("self_check_landlock", str(exc.errno)) from None
    else:
        os.close(fd)
        raise StartupError("self_check_landlock", "probe readable")
    if fds != set(keep):
        raise StartupError("self_check_fds", f"{sorted(fds)}")
    if tasks != 1:
        raise StartupError("self_check_threads", str(tasks))


@dataclass
class PreparedDirs:
    """検査済みの config dir と state dir の fd。Landlock の規則を付け終えたら閉じる。"""

    config_fd: int
    state_fd: int

    def fds(self) -> set[int]:
        return {fd for fd in (self.config_fd, self.state_fd) if fd >= 0}

    def close(self) -> None:
        for fd in self.fds():
            os.close(fd)
        self.config_fd = self.state_fd = -1


class _ConfigDirOwner:
    """起動口から受け取った config dir fd を一度だけ閉じる。"""

    def __init__(self, fd: int | None) -> None:
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


def _open_leaf(parent_fd: int, name: str, reason: str) -> int:
    """親 dir の fd から leaf を symlink を辿らずに dir として開く。"""
    try:
        return os.open(name, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        # symlink は ELOOP、dir でなければ ENOTDIR
        raise StartupError(reason, str(exc.errno)) from None


def _check_owned_dir(fd: int, reason: str, *, exact_mode: int | None) -> None:
    st = os.fstat(fd)
    mode = stat.S_IMODE(st.st_mode)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise StartupError(reason, "owner or type")
    if exact_mode is not None and mode != exact_mode:
        raise StartupError(reason, "mode")
    if mode & 0o022:
        raise StartupError(reason, "writable by others")


def open_config_dir(path: Path) -> int:
    """設定 dir を親 fd から開き、設定を読む側と隔離準備側で同じ検査を使う。"""
    try:
        parent = os.open(path.parent, _DIR_FLAGS | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise StartupError("config_dir_missing", "parent") from None
    except OSError as exc:
        raise StartupError("config_dir_invalid", str(exc.errno)) from None
    try:
        try:
            fd = os.open(path.name, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            raise StartupError("config_dir_missing") from None
        except OSError as exc:
            raise StartupError("config_dir_invalid", str(exc.errno)) from None
    finally:
        os.close(parent)
    try:
        # 隔離後も読む設定なので、他者が書き換えられない dir だけを受け入れる
        _check_owned_dir(fd, "config_dir_invalid", exact_mode=None)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_state_dir(path: Path) -> int:
    try:
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
        parent = os.open(path.parent, _DIR_FLAGS)
    except OSError as exc:
        raise StartupError("state_dir_failed", str(exc.errno)) from None
    try:
        created = False
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise StartupError("state_dir_failed", str(exc.errno)) from None
        fd = _open_leaf(parent, path.name, "state_dir_invalid")
    finally:
        os.close(parent)
    try:
        if created:
            # umask で削られた分を戻す。既存の dir の mode は直さず、違えば止める
            os.fchmod(fd, 0o700)
        _check_owned_dir(fd, "state_dir_invalid", exact_mode=0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _duplicate_checked_config_dir(config_dir_fd: int) -> int:
    try:
        fd = os.dup(config_dir_fd)
    except OSError as exc:
        raise StartupError("config_dir_invalid", str(exc.errno)) from None
    try:
        _check_owned_dir(fd, "config_dir_invalid", exact_mode=None)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_secret_dir(path: Path) -> int:
    try:
        parent = os.open(path.parent, _DIR_FLAGS | os.O_NOFOLLOW)
    except OSError as exc:
        raise StartupError("secrets_failed", f"dir {exc.errno}") from None
    try:
        fd = _open_leaf(parent, path.name, "secrets_failed")
    finally:
        os.close(parent)
    try:
        _check_owned_dir(fd, "secrets_failed", exact_mode=0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def prepare_dirs(plan: StartupPlan, *, config_dir_fd: int | None = None,
                 key_dir: Path | None = None) -> PreparedDirs:
    """config dir と state dir を検査して開く。鍵を読む前・隔離前に呼ぶ。

    どちらも親 dir の fd から `O_NOFOLLOW|O_DIRECTORY` で開き、fstat で自 uid の dir で
    あることを確かめる (symlink・他 uid・緩い mode は固定 reason で止まる)。config は
    利用者が置く設定の領域なので作らず (無いのは設置の誤り)、他者書込み不可を求める。
    state は機能自身が書く領域なので無ければ 0700 で作り、0700 ちょうどを求める。
    """
    config_fd = (open_config_dir(plan.config_dir) if config_dir_fd is None
                 else _duplicate_checked_config_dir(config_dir_fd))
    try:
        state_fd = _open_state_dir(plan.state_dir)
    except BaseException:
        os.close(config_fd)
        raise
    if key_dir is not None:
        # 領域の検査が通った直後、差し替えの余地が出る前に鍵 dir の境界を見る
        # (以後に差し替わっても Landlock は検査済みの fd に従う)
        from afx_ops.config import validate_key_dir
        try:
            checked_dir_fds = {"config_dir": config_fd, "state_dir": state_fd}
            secret_fd = _open_secret_dir(plan.secret_dir) if plan.secret_files else -1
            try:
                if secret_fd >= 0:
                    checked_dir_fds["secret_dir"] = secret_fd
                validate_key_dir(key_dir, config_dir=plan.config_dir, state_dir=plan.state_dir,
                                 secret_dir=plan.secret_dir, checked_dir_fds=checked_dir_fds)
            finally:
                if secret_fd >= 0:
                    os.close(secret_fd)
        except BaseException:
            os.close(config_fd)
            os.close(state_fd)
            raise
    return PreparedDirs(config_fd=config_fd, state_fd=state_fd)


_SECRET_MAX = 65536


def read_api_token(feature: str, key_dir: Path) -> bytes:
    """機能の固定 principal の鍵を、manifest の state と digest を照合して読む。隔離前に呼ぶ。

    feature secret の reader は使わない (manifest の照合を欠くため)。失敗は `key_*` の固定
    reason で、例外文に path や鍵を載せない。
    """
    principal = PRINCIPALS[feature]
    key_dir = Path(key_dir)
    try:
        token = keys.read_token(principal, key_file=key_dir / keys.key_file_name(principal),
                                ready_file=key_dir / keys.READY_NAME)
    except keys.KeyReadError as exc:
        raise StartupError(key_reason(exc.code)) from None
    return token.encode("ascii")


def read_secrets(plan: StartupPlan) -> Mapping[str, bytes]:
    """plan の secret dir から plan の secret_files だけを読む。隔離前に呼ぶ。

    dir は親 dir の fd から `O_NOFOLLOW|O_DIRECTORY` で開き、自 uid・0700 を確かめる。
    file は dir の fd から `O_NOFOLLOW` で開き、通常 file・自 uid・0600 を確かめる。
    読む名前が無い機能は dir に触れない。失敗は `secrets_failed` で、例外文に path や
    中身を載せない。
    """
    if not plan.secret_files:
        return MappingProxyType({})
    for name in plan.secret_files:
        if not name or "/" in name or name in (".", ".."):
            raise StartupError("secrets_failed", "name")
    dir_fd = _open_secret_dir(plan.secret_dir)
    try:
        return MappingProxyType({name: _read_secret_file(dir_fd, name)
                                 for name in plan.secret_files})
    finally:
        os.close(dir_fd)


def _read_secret_file(dir_fd: int, name: str) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOCTTY | os.O_NONBLOCK
                     | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError as exc:
        raise StartupError("secrets_failed", f"file {exc.errno}") from None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise StartupError("secrets_failed", "file check")
        chunks = []
        size = 0
        while chunk := os.read(fd, 4096):
            size += len(chunk)
            if size > _SECRET_MAX:
                raise StartupError("secrets_failed", "size")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise StartupError("secrets_failed", f"read {exc.errno}") from None
    finally:
        os.close(fd)


def connect_notify(value: str | None, *, required: bool) -> socket.socket | None:
    """`NOTIFY_SOCKET` に AF_UNIX DGRAM で connect した socket。隔離前に呼ぶ。

    隔離後は AF_UNIX DGRAM の socket を作れず (seccomp)、abstract socket への接続も
    Landlock の scope で拒否されるので、ここで繋いだ fd だけを保持する。
    """
    if not value:
        if required:
            raise StartupError("unit_notify_socket", "unset")
        return None
    if value.startswith("@"):
        address = "\0" + value[1:]
    elif value.startswith("/"):
        address = value
    else:
        raise StartupError("unit_notify_socket", "address")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
    try:
        sock.connect(address)
    except OSError as exc:
        sock.close()
        raise StartupError("unit_notify_socket", str(exc.errno)) from None
    return sock


def send_ready(sock: socket.socket) -> None:
    """`READY=1` を送り、通知 fd を閉じる (以後は使わないので、侵害時の送信路に残さない)。"""
    try:
        # send は宛先 NULL の sendto になり、web の「接続済み socket だけ」の条件を満たす
        sock.send(READY_MESSAGE)
    except OSError as exc:
        raise StartupError("notify_failed", str(exc.errno)) from None
    finally:
        sock.close()


def announce_ready(notify: socket.socket | None, endpoint: Endpoint, *,
                   wait: Callable[[Endpoint], None] = wait_ready) -> None:
    """server 認証が通ったときだけ `READY=1` を送る。通知 fd は成否に依らずここで閉じる。

    認証の失敗 (`ApiError`) はそのまま上げる。READY=1 が届かなければ unit は active に
    ならず、systemd の restart が新しい process で隔離からやり直す。
    """
    try:
        wait(endpoint)
    except BaseException:
        if notify is not None:
            notify.close()
        raise
    if notify is not None:
        send_ready(notify)


def isolate(plan: StartupPlan, config: OpsConfig, *,
            plan_check: Callable[[StartupPlan], None] = check_plan,
            release_config_dir: Callable[[], None] | None = None,
            ) -> tuple[Isolated, socket.socket | None]:
    """0〜5 段を順に行う。失敗は `StartupError`。

    通知 fd は `Isolated` に入れず別に返す (body へ渡さず、READY=1 の後に閉じるため)。
    """
    with contextlib.ExitStack() as cleanup:
        plan_check(plan)
        pid = os.getpid()
        proc = f"/proc/{pid}"
        check_preconditions(plan, proc)
        notify = connect_notify(os.environ.get(NOTIFY_ENV), required=plan.require_notify)
        if notify is not None:
            cleanup.callback(notify.close)
        try:
            compiled = feature_filter(plan.feature).compiled
        except PolicyBuildError as exc:
            raise StartupError("seccomp_table_invalid", str(exc)) from None
        dirs = prepare_dirs(plan, config_dir_fd=config.config_dir_fd, key_dir=config.key_dir)
        cleanup.callback(dirs.close)
        if release_config_dir is not None:
            release_config_dir()
        try:
            api_token = read_api_token(plan.feature, config.key_dir)
            secrets = read_secrets(plan)
            listener = None
            if PLANS[plan.feature].listener:
                if plan.web_port is None:
                    raise StartupError("listener_invalid", "no port source until web config")
                try:
                    listener = prebind_listener(plan.web_port)
                except (ListenerError, OSError) as exc:
                    raise StartupError("listener_invalid", str(exc)) from None
                cleanup.callback(listener.close)
            keep = {0, 1, 2}
            if listener is not None:
                keep.add(listener.fileno())
            if notify is not None:
                keep.add(notify.fileno())
            close_unlisted_fds(proc, keep | dirs.fds())
            try:
                filters_before = filter_count(proc)
            except (OSError, ValueError) as exc:
                raise StartupError("self_check_unreadable", type(exc).__name__) from None
            apply_landlock(plan, proc, dirs)
        finally:
            dirs.close()
        apply_seccomp(compiled)
        set_dumpable_zero()
        self_check(proc, filters_before=filters_before, keep=keep)
        if notify is not None:
            keep.discard(notify.fileno())
        isolated = Isolated(feature=plan.feature, pid=pid, proc_dir=proc, secrets=secrets,
                            listener=listener, kept_fds=frozenset(keep),
                            config=dataclasses.replace(config, config_dir_fd=None),
                            endpoint=endpoint_for(config.instance_root), api_token=api_token,
                            state_dir=plan.state_dir)
        cleanup.pop_all()
        return isolated, notify


def _no_preload() -> None:
    return None


def run(plan: StartupPlan, config: OpsConfig, body: Callable[[Isolated], int], *,
        plan_check: Callable[[StartupPlan], None] = check_plan,
        wait: Callable[[Endpoint], None] = wait_ready,
        preload: Callable[[], None] = _no_preload) -> int:
    """隔離して自己検査と server 認証が通ったときだけ body を呼ぶ。失敗は固定 reason で非零。

    `plan_check` と `wait` は試験だけが差し替える (unit の外・一時領域・短い待ちで起動する
    ため)。production の起動口は渡さず、設定からも変えられない。server 認証の失敗は要求経路
    の失敗と同じ終了コード (`RC_UNREACHABLE`) で、process 内では再試行しない。
    """
    owner = _ConfigDirOwner(config.config_dir_fd)
    try:
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(owner.close)
            try:
                preload()
            except Exception:
                raise StartupError("runtime_import_failed") from None
            isolated, notify = isolate(plan, config, plan_check=plan_check,
                                      release_config_dir=owner.close)
            announce_ready(notify, isolated.endpoint, wait=wait)
    except StartupError as exc:
        reason = exc.reason
        rc = RC_RUNTIME_IMPORT_FAILED if reason == "runtime_import_failed" else RC_ISOLATION_FAILED
    except ApiError as exc:
        reason, rc = exc.reason, RC_UNREACHABLE
    except Exception as exc:  # 想定外の失敗も traceback (path や値を含み得る) にせず固定語で閉じる
        reason = "config_invalid" if getattr(exc, "reason", None) == "config_invalid" else \
            "startup_internal_error"
        rc = RC_ISOLATION_FAILED
    else:
        try:
            return body(isolated)
        except Exception:  # 機能本体の想定外の失敗も、path・URL・token を含み得る traceback にしない
            reason, rc = "body_failed", RC_BODY_FAILED
    print(f"afx-ops: {reason}", file=sys.stderr, flush=True)
    return rc
