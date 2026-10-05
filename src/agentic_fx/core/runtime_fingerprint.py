"""plugin worker を動かす runtime の fingerprint と、対応集合外での代表自己試験。

seccomp allowlist は実測した runtime の syscall 集合に合わせてある。runtime が変われば
正常な計算でも表の外の syscall を呼び得るので、対応済みの組 (`supported_runtimes()`)
以外では、隔離を掛けた使い捨ての子 process で代表的な計算を一度走らせ、成功したときだけ
WARNING 付きで許可する。結果は `RuntimeAdmission` が fingerprint ごとに process 内で覚える。

このモジュールは import 時に numpy / pandas を読まない (fingerprint は dist-info から作る)。
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC
from email.parser import Parser
from pathlib import Path

from agentic_fx.core import seccomp
from agentic_fx.core.plugin_files import write_all as _write_all

#: worker の隔離 profile の版。seccomp・Landlock・protocol のどれかを変えたら上げる
SANDBOX_PROFILE_VERSION = "plugin-worker/6"

#: 集合外で自己試験に失敗したときの固定 reason
SELFTEST_FAILED_REASON = "runtime_fingerprint_selftest_failed"

#: 集合外で自己試験に成功したときの WARNING
OUT_OF_SET_WARNING = ("runtime fingerprint is outside the supported set; the representative "
                      "selftest passed under isolation. 安全性は維持するが可用性・計算互換性は未保証")


# --- fingerprint ---------------------------------------------------------------

@dataclass(frozen=True)
class DistIdentity:
    """インストール済み distribution の識別。`record_sha256` は dist-info の RECORD の bytes の hash。"""
    name: str
    version: str
    generator: str
    tags: tuple[str, ...]
    record_sha256: str


@dataclass(frozen=True)
class RuntimeFingerprint:
    kernel_release: str
    machine: str
    landlock_abi: int
    glibc_version: str
    glibc_build: str
    python_version: str
    python_build: tuple[str, str]
    python_executable: str
    numpy: DistIdentity
    pandas: DistIdentity
    sandbox_profile: str
    seccomp_profile: str

    def normalized(self) -> str:
        """比較と記録に使う正規形 (key を整列した JSON)。"""
        return json.dumps(dataclasses.asdict(self), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True)


# 対応集合との一致判定に使う field。実行ファイルの場所は venv ごとに違うので含めない
_PINNED_FIELDS = tuple(f.name for f in dataclasses.fields(RuntimeFingerprint)
                       if f.name != "python_executable")


def normalized_record_sha256(record: bytes) -> str:
    """RECORD から site-packages の外を指す行 (`../` で始まる行) を除き、残りの行を
    並び順・行末のまま連結した bytes の SHA-256。

    console script の行は shebang に venv の絶対 path が入るため venv ごとに hash が変わる。
    """
    kept = b"".join(line for line in record.splitlines(keepends=True)
                    if not line.startswith(b"../"))
    return hashlib.sha256(kept).hexdigest()


def dist_identity(dist_info_dir: Path) -> DistIdentity:
    """`<name>-<version>.dist-info` の METADATA・WHEEL・RECORD から識別を作る。

    `record_sha256` は `normalized_record_sha256` の値 (venv の場所に依存しない)。
    """
    dist_info_dir = Path(dist_info_dir)
    meta = Parser().parsestr((dist_info_dir / "METADATA").read_text(encoding="utf-8"),
                             headersonly=True)
    wheel = Parser().parsestr((dist_info_dir / "WHEEL").read_text(encoding="utf-8"),
                              headersonly=True)
    record = (dist_info_dir / "RECORD").read_bytes()
    return DistIdentity(
        name=str(meta["Name"]).strip().lower(),
        version=str(meta["Version"]).strip(),
        generator=str(wheel.get("Generator", "")).strip(),
        tags=tuple(sorted(t.strip() for t in wheel.get_all("Tag", []))),
        record_sha256=normalized_record_sha256(record),
    )


def _installed_dist_info(name: str) -> Path:
    import importlib.metadata as md
    dist = md.distribution(name)
    for f in dist.files or ():
        if str(f).endswith(".dist-info/RECORD"):
            return Path(dist.locate_file(f)).parent
    raise FileNotFoundError(f"{name}: dist-info RECORD not found")


_GLIBC_BANNER = re.compile(rb"GNU C Library \(([^)\n]{1,128})\) (?:stable )?release version "
                           rb"([0-9][0-9.]*[0-9])")


@functools.lru_cache(maxsize=1)
def _glibc_identity() -> tuple[str, str]:
    """(version, package/build の識別)。build は libc 本体の banner から読む (ldd を起動しない)。"""
    conf = os.confstr("CS_GNU_LIBC_VERSION") if hasattr(os, "confstr") else None
    version = conf.split()[-1] if conf else "unknown"
    build = "unknown"
    try:
        with open("/proc/self/maps", encoding="ascii", errors="replace") as f:
            libc_path = next((line.split()[-1] for line in f
                              if re.search(r"/libc[.-][^/]*\.so(\.\d+)*$", line.strip())
                              or line.rstrip().endswith("/libc.so.6")), None)
        if libc_path:
            m = _GLIBC_BANNER.search(Path(libc_path).read_bytes())
            if m:
                build = m.group(1).decode("ascii", "replace")
    except OSError:
        pass
    return version, build


def _python_executable_identity() -> str:
    base = getattr(sys, "_base_executable", None) or sys.executable
    return os.path.realpath(base)


def compute_fingerprint(*, landlock_abi: int) -> RuntimeFingerprint:
    """この process の runtime fingerprint。Landlock ABI は呼び出し元が測った値を渡す。"""
    glibc_version, glibc_build = _glibc_identity()
    uname = os.uname()
    return RuntimeFingerprint(
        kernel_release=uname.release,
        machine=uname.machine,
        landlock_abi=int(landlock_abi),
        glibc_version=glibc_version,
        glibc_build=glibc_build,
        python_version=platform.python_version(),
        python_build=tuple(platform.python_build()),
        python_executable=_python_executable_identity(),
        numpy=dist_identity(_installed_dist_info("numpy")),
        pandas=dist_identity(_installed_dist_info("pandas")),
        sandbox_profile=SANDBOX_PROFILE_VERSION,
        seccomp_profile=seccomp.SECCOMP_PROFILE,
    )


# 2026-10-03 に正常 workload を完走した 1 組
_SUPPORTED = (
    RuntimeFingerprint(
        kernel_release="7.0.0-38-generic",
        machine="x86_64",
        landlock_abi=8,
        glibc_version="2.43",
        glibc_build="Ubuntu GLIBC 2.43-2ubuntu2.4",
        python_version="3.13.14",
        python_build=("main", "Jun 11 2026 04:03:13"),
        python_executable="",
        numpy=DistIdentity(
            name="numpy", version="2.5.1", generator="meson",
            tags=("cp313-cp313-manylinux_2_27_x86_64", "cp313-cp313-manylinux_2_28_x86_64"),
            record_sha256="1302287028025a50047b0a8ca45e78195209fdc9a53efb0d25c3a7a86a2e792b"),
        pandas=DistIdentity(
            name="pandas", version="3.0.5", generator="meson",
            tags=("cp313-cp313-manylinux_2_24_x86_64", "cp313-cp313-manylinux_2_28_x86_64"),
            record_sha256="e9ba5610142346491f808af3ed84b371105e227ddb43d5b761bff583c2c990f3"),
        sandbox_profile="plugin-worker/6",
        seccomp_profile="allow/6",
    ),
)


def supported_runtimes() -> tuple[RuntimeFingerprint, ...]:
    """対応済み集合。`python_executable` は判定に使わないので空にしてある。"""
    return _SUPPORTED


def _pinned(fp: RuntimeFingerprint) -> tuple:
    return tuple(getattr(fp, name) for name in _PINNED_FIELDS)


def is_supported(fp: RuntimeFingerprint,
                 supported: tuple[RuntimeFingerprint, ...] | None = None) -> bool:
    pinned = _pinned(fp)
    return any(pinned == _pinned(s) for s in (supported if supported is not None else _SUPPORTED))


# --- 代表計算と oracle ------------------------------------------------------------

_N_BARS = 300
_SMA_WINDOW = 20
_RSI_PERIOD = 14
_BLAS_N = 256
_THREADS = 4
_TZ = "America/New_York"
# UTC 起点。途中で米国の夏時間開始 (2026-03-08 07:00 UTC) をまたぐ
_START_EPOCH = 1772841600  # 2026-03-07T00:00:00Z
_TZ_PROBES = (0, 30, 31, 32, _N_BARS - 1)


def _close(i: int) -> float:
    return 1.10 + 0.01 * math.sin(i * 0.07) + 0.0003 * (i % 17)


def _blas_entry(i: int, j: int) -> float:
    return float(((i * 7 + j * 3) % 11) - 5)


def representative_workload() -> dict:
    """隔離下で走らせる代表計算。numpy / pandas はここで初めて import する。"""
    import numpy as np
    import pandas as pd

    import agentic_fx.core.plugin_contract  # noqa: F401  (plugin が使う契約 module の import)

    idx = pd.date_range(pd.Timestamp(_START_EPOCH, unit="s", tz="UTC"), periods=_N_BARS,
                        freq="h")
    close = pd.Series([_close(i) for i in range(_N_BARS)], index=idx, dtype="float64")
    frame = pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "close": close,
                          "high": close + 0.0005}, index=idx)

    sma = frame["close"].rolling(_SMA_WINDOW).mean()

    delta = frame["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / _RSI_PERIOD, adjust=False, min_periods=_RSI_PERIOD).mean()
    avg_loss = loss.ewm(alpha=1.0 / _RSI_PERIOD, adjust=False, min_periods=_RSI_PERIOD).mean()
    rsi = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    local = frame.index.tz_convert(_TZ)
    offsets = [int(local[i].utcoffset().total_seconds()) for i in _TZ_PROBES]
    dst_hours = int(sum(1 for ts in local if ts.utcoffset().total_seconds() == -4 * 3600))

    a = np.fromfunction(lambda i, j: ((i * 7 + j * 3) % 11) - 5, (_BLAS_N, _BLAS_N),
                        dtype=np.int64).astype(np.float64)
    c = a @ a.T

    parts: list[int] = []
    idents: set[int] = set()
    lock = threading.Lock()
    main_ident = threading.get_ident()

    def work(t: int) -> None:
        s = sum(range(t * 1000, (t + 1) * 1000))
        with lock:
            parts.append(s)
            idents.add(threading.get_ident())

    threads = [threading.Thread(target=work, args=(t,)) for t in range(_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return {
        "frame_shape": [int(frame.shape[0]), int(frame.shape[1])],
        "close_sum": float(frame["close"].sum()),
        "sma_last": float(sma.iloc[-1]),
        "sma_valid": int(sma.notna().sum()),
        "rsi_last": float(rsi.iloc[-1]),
        "rsi_valid": int(rsi.notna().sum()),
        "tz_offsets": offsets,
        "tz_dst_hours": dst_hours,
        "blas_trace": float(np.trace(c)),
        "blas_corner": float(c[0, _BLAS_N - 1]),
        "thread_parts": sorted(parts),
        "threads_off_main": bool(idents) and main_ident not in idents,
    }


def _oracle() -> dict:
    """numpy / pandas を使わない独立実装。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    n, window, period, k = 300, 20, 14, 256
    closes = [_close(i) for i in range(n)]
    sma_last = math.fsum(closes[-window:]) / window

    alpha = 1.0 / period
    g = lo = None
    seen = 0
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gi, li = max(d, 0.0), max(-d, 0.0)
        if g is None:
            g, lo = gi, li
        else:
            g = (1.0 - alpha) * g + alpha * gi
            lo = (1.0 - alpha) * lo + alpha * li
        seen += 1
    rsi_last = 100.0 - 100.0 / (1.0 + g / lo)
    rsi_valid = seen - period + 1

    zone = ZoneInfo(_TZ)
    offs = [int(datetime.fromtimestamp(_START_EPOCH + 3600 * i, tz=UTC)
                .astimezone(zone).utcoffset().total_seconds()) for i in range(n)]

    rows = [[_blas_entry(i, j) for j in range(k)] for i in range(k)]
    trace = math.fsum(v * v for row in rows for v in row)
    corner = math.fsum(x * y for x, y in zip(rows[0], rows[k - 1]))

    return {
        "frame_shape": [n, 3],
        "close_sum": math.fsum(closes),
        "sma_last": sma_last,
        "sma_valid": n - window + 1,
        "rsi_last": rsi_last,
        "rsi_valid": rsi_valid,
        "tz_offsets": [offs[i] for i in _TZ_PROBES],
        "tz_dst_hours": sum(1 for o in offs if o == -4 * 3600),
        "blas_trace": trace,
        "blas_corner": corner,
        "thread_parts": sorted(sum(range(t * 1000, (t + 1) * 1000)) for t in range(4)),
        "threads_off_main": True,
    }


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return False
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def compare_with_oracle(result: Mapping) -> str | None:
    """一致なら None、不一致なら最初の key を `oracle_mismatch:<key>` で返す。"""
    expected = _oracle()
    for key in sorted(set(expected) | set(result)):
        if key not in expected or key not in result or not _same(result[key], expected[key]):
            return f"oracle_mismatch:{key}"
    return None


# --- 自己試験 harness (子 process) ------------------------------------------------

@dataclass(frozen=True)
class SelftestOutcome:
    """`ok` と診断用の detail。detail は技術ログ向けで、公開 reason には使わない。"""
    ok: bool
    detail: str


#: 子の PYTHONPATH に入れる、このパッケージの置き場 (src/)
_SRC_DIR = Path(__file__).resolve().parents[2]

_CHILD_HOOK_FAILED = 3
_CHILD_NOT_ISOLATED = 4
_CHILD_WORKLOAD_FAILED = 5

#: hook が返す隔離の証跡 dict に必ず含まれる key
ATTESTATION_KEYS = ("landlock_fs_abi", "landlock_tsync", "network", "scope",
                    "seccomp_profile", "sandbox_profile")

# Landlock の効きを子自身が確かめる対象。allowlist (runtime・system・device) の外にある
# 常在 file で、隔離前なら開ける path を使う
_LANDLOCK_PROBE_PATHS = ("/etc/hostname", "/etc/passwd", "/etc/os-release",
                         "/proc/self/environ")

_CHILD_ENTRY = ("from agentic_fx.core.runtime_fingerprint import _selftest_child_main; "
                "_selftest_child_main()")


def _resolve_hook(spec: str) -> Callable[[], object]:
    import importlib
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"isolation hook must be 'module:function': {spec!r}")
    return getattr(importlib.import_module(module_name), attr)


def _pick_probe_path() -> str | None:
    """隔離前に開ける、allowlist 外の path を 1 つ選ぶ。"""
    for path in _LANDLOCK_PROBE_PATHS:
        try:
            os.close(os.open(path, os.O_RDONLY | os.O_CLOEXEC))
        except OSError:
            continue
        return path
    return None


def _attestation_ok(att: object) -> bool:
    if not isinstance(att, dict) or any(k not in att for k in ATTESTATION_KEYS):
        return False
    abi = att["landlock_fs_abi"]
    return (isinstance(abi, int) and not isinstance(abi, bool) and abi >= 3
            and att["seccomp_profile"] == seccomp.SECCOMP_PROFILE
            and att["sandbox_profile"] == SANDBOX_PROFILE_VERSION)


def _landlock_blocks(path: str) -> bool:
    """隔離後に allowlist 外の path が実際に開けなくなっているか。"""
    try:
        os.close(os.open(path, os.O_RDONLY | os.O_CLOEXEC))
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def _selftest_child_main() -> None:
    """自己試験の子。隔離を掛けてから代表計算を走らせ、結果を 1 行の JSON で返す。

    通常の interpreter 終了処理は表の外の syscall を呼び得るので、必ず `os._exit` で終える。
    """
    import resource
    import traceback

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    hook = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
    if hook is None:
        os._exit(_CHILD_HOOK_FAILED)
    # 隔離後は /proc を開けない profile もあるので、検査用の fd を先に開いておく
    status_fd = os.open("/proc/self/status", os.O_RDONLY | os.O_CLOEXEC)
    probe_path = _pick_probe_path()
    try:
        attestation = _resolve_hook(hook)()
    except BaseException:
        _write_all(2, traceback.format_exc().encode("utf-8", "replace"))
        os._exit(_CHILD_HOOK_FAILED)
    status = os.pread(status_fd, 65536, 0).decode("ascii", "replace")
    os.close(status_fd)
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    observed = tuple(fields.get(k, "").strip() for k in ("NoNewPrivs", "Seccomp",
                                                         "Seccomp_filters"))
    if observed != ("1", "2", "1"):
        os._exit(_CHILD_NOT_ISOLATED)
    # 証跡は自己申告なので、Landlock が効いていることを子自身が確かめる
    if probe_path is None or not _attestation_ok(attestation) \
            or not _landlock_blocks(probe_path):
        os._exit(_CHILD_NOT_ISOLATED)
    try:
        result = representative_workload()
        payload = (json.dumps(result, sort_keys=True) + "\n").encode("ascii")
    except BaseException:
        _write_all(2, traceback.format_exc().encode("utf-8", "replace"))
        os._exit(_CHILD_WORKLOAD_FAILED)
    _write_all(1, payload)
    os._exit(0)


def run_representative_selftest(*, isolation_hook: str, timeout: float = 120.0,
                                env: Mapping[str, str] | None = None,
                                python: str | None = None) -> SelftestOutcome:
    """代表計算を隔離した子 process で 1 回走らせる。

    `isolation_hook` は必須の `"module:function"`。子はそれを引数なしで呼んで実 sandbox
    (匿名 keyring → Landlock → seccomp) を掛け、隔離の証跡 dict (`ATTESTATION_KEYS`) を
    返す契約。呼んだ後に filter 状態 (NoNewPrivs 1、Seccomp 2、Seccomp_filters 1)、証跡の
    中身 (`landlock_fs_abi` 3 以上、現行の seccomp・sandbox profile)、allowlist 外の file が
    実際に開けないことを子が確かめる。どれか欠けると `isolation_not_applied`。
    `PluginSession` は経由しない。
    """
    if not isolation_hook:
        raise ValueError("isolation_hook is required: 'module:function'")
    # 親の環境変数 (.env 由来の秘密を含み得る) は渡さず、子が動くのに要る最小限を組み立てる
    child_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_SRC_DIR)}
    child_env.update(env or {})
    child_env["OPENBLAS_NUM_THREADS"] = "4"
    # -P: cwd を sys.path に入れない (filter 後の import 探索が getcwd を呼ぶ)
    # -B: __pycache__ を書かない (filter 後の mkdir は表の外)
    argv = [python or sys.executable, "-P", "-B", "-c", _CHILD_ENTRY, isolation_hook]
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=child_env, close_fds=True)
    except OSError:
        return SelftestOutcome(False, "spawn_failed")
    try:
        stdout, _stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return SelftestOutcome(False, "timeout")
    rc = proc.returncode
    if rc < 0:
        return SelftestOutcome(False, f"signaled:{-rc}")
    if rc == _CHILD_NOT_ISOLATED:
        return SelftestOutcome(False, "isolation_not_applied")
    if rc == _CHILD_WORKLOAD_FAILED:
        return SelftestOutcome(False, "workload_failed")
    if rc != 0:
        return SelftestOutcome(False, f"exit:{rc}")
    try:
        result = json.loads(stdout.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return SelftestOutcome(False, "no_result")
    if not isinstance(result, dict):
        return SelftestOutcome(False, "no_result")
    mismatch = compare_with_oracle(result)
    if mismatch is not None:
        return SelftestOutcome(False, mismatch)
    return SelftestOutcome(True, "ok")


# --- admission (process 内 cache) ------------------------------------------------

#: 環境の一時要因による selftest 失敗 (負荷・資源逼迫)。恒久要因と違い、間隔を空けて
#: 再試行する。spawn_failed = fork/exec 不能 (fd・メモリ逼迫)、timeout = 負荷で遅い。
#: 計算不一致・隔離不成立・signaled などは恒久要因 (runtime が正しく動けない) とする。
_TRANSIENT_SELFTEST_DETAILS = frozenset({"spawn_failed", "timeout"})

#: 一時失敗の再試行間隔。selftest は最大 120 秒かかるので、評価ごとには再試行しない。
SELFTEST_RETRY_INTERVAL_SEC = 60.0


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    supported: bool
    reason: str | None
    warning: str | None
    #: True のとき、この失敗は一時要因なので恒久 cache せず間隔を空けて再試行する
    retryable: bool = False


class RuntimeAdmission:
    """fingerprint ごとの admission 判定を process 内で覚える。

    対応集合内なら自己試験なしで許可。集合外なら `selftest` を呼び、成功で WARNING 付き
    許可、恒久失敗で `runtime_fingerprint_selftest_failed` を cache、一時失敗 (timeout・
    spawn_failed) は cache せず `retry_interval_sec` を空けて再試行する。同じ fingerprint の
    同時の初回呼び出しは自己試験 1 回にまとまる。
    """

    def __init__(self, *, selftest: Callable[[], SelftestOutcome],
                 supported: tuple[RuntimeFingerprint, ...] | None = None,
                 retry_interval_sec: float = SELFTEST_RETRY_INTERVAL_SEC,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.selftest = selftest
        self._supported = supported if supported is not None else _SUPPORTED
        self._cache: dict[RuntimeFingerprint, tuple[AdmissionDecision, float]] = {}
        self._lock = threading.Lock()
        self._retry_interval = retry_interval_sec
        self._clock = clock

    def admit(self, fingerprint: RuntimeFingerprint) -> AdmissionDecision:
        with self._lock:
            now = self._clock()
            cached = self._cache.get(fingerprint)
            if cached is not None:
                decision, ts = cached
                # 恒久判定は常に、一時失敗は間隔内だけ、cache を返す
                if not decision.retryable or now - ts < self._retry_interval:
                    return decision
            decision = self._decide(fingerprint)
            self._cache[fingerprint] = (decision, now)
            return decision

    def _decide(self, fingerprint: RuntimeFingerprint) -> AdmissionDecision:
        if is_supported(fingerprint, self._supported):
            return AdmissionDecision(admitted=True, supported=True, reason=None, warning=None)
        try:
            outcome = self.selftest()
        except Exception:
            outcome = None
        if isinstance(outcome, SelftestOutcome) and outcome.ok is True:
            return AdmissionDecision(admitted=True, supported=False, reason=None,
                                     warning=OUT_OF_SET_WARNING)
        detail = outcome.detail if isinstance(outcome, SelftestOutcome) else None
        retryable = detail in _TRANSIENT_SELFTEST_DETAILS
        return AdmissionDecision(admitted=False, supported=False, reason=SELFTEST_FAILED_REASON,
                                 warning=None, retryable=retryable)


__all__ = [
    "ATTESTATION_KEYS",
    "OUT_OF_SET_WARNING",
    "SANDBOX_PROFILE_VERSION",
    "SELFTEST_FAILED_REASON",
    "AdmissionDecision",
    "DistIdentity",
    "RuntimeAdmission",
    "RuntimeFingerprint",
    "SelftestOutcome",
    "compare_with_oracle",
    "compute_fingerprint",
    "dist_identity",
    "is_supported",
    "normalized_record_sha256",
    "representative_workload",
    "run_representative_selftest",
]
