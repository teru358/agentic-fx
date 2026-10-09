"""afx 本体 (unit の main PID) が持つ cgroup の仕事: topology、前回 mission cgroup の回収、
mission 後の rmdir、次 mission を禁止する gate。

topology は次の形だけを supported とする::

    <afx unit>/        Delegate=pids memory。root に process を置かない
      ├─ supervisor/   afx 本体と mission worker
      └─ m-<nonce>/    CLI 部分木だけ (作成・上限・join・kill は worker 側)

cgroup v2 の「process の居る cgroup は子に controller を配れない」規則のため、afx は root に自分
だけが居ることを確かめてから `supervisor/` へ自己移動し、その後で root の `cgroup.subtree_control`
に `+pids +memory` を書く。screen や shell wrapper を挟むと root に他の process が居て、それを
勝手に動かすと wrapper の終了処理を壊すので、移さずに unsupported とする。

mission cgroup の後始末 (起動時の前回分と mission 後) は、保険の `cgroup.kill` → `populated 0`
を absolute deadline (monotonic) まで待つ → `cgroup.procs` が空 → rmdir の順。失敗は固定 reason で
gate を閉じ、process 内では開かない (afx の再起動で admission と回収をやり直す)。admission の
失敗も同じ gate の「不可」として読める。

配線前は隔離の保証なし (未有効): この module は afx の起動経路と改善 backend の選択には
まだ繋がっていない。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from agentic_fx.core import unit_admission as ua

logger = logging.getLogger(__name__)

REASON_CLEANUP_TIMEOUT = "cgroup_cleanup_timeout"
REASON_NOT_EMPTY = "cgroup_not_empty"
REASON_RMDIR_FAILED = "cgroup_rmdir_failed"
REASON_WORKER_NOT_REAPED = "worker_not_reaped"
GATE_REASONS = frozenset({
    REASON_CLEANUP_TIMEOUT, REASON_NOT_EMPTY, REASON_RMDIR_FAILED, REASON_WORKER_NOT_REAPED})

CGROUP_MOUNT = "/sys/fs/cgroup"
SUPERVISOR = "supervisor"
MISSION_PREFIX = "m-"
_MISSION_NAME = re.compile(r"m-[A-Za-z0-9_-]{1,64}")
_REQUIRED_CONTROLLERS = frozenset({"pids", "memory"})
#: populated の待ちの poll 間隔 (cgroup.kill から populated 0 は実測 23〜33 ms)
_POLL_SEC = 0.005


class CgroupHostError(Exception):
    def __init__(self, reason: str, detail: str):
        assert reason in GATE_REASONS or reason in ua.ADMISSION_REASONS
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _topology(detail: str) -> CgroupHostError:
    return CgroupHostError(ua.REASON_TOPOLOGY_UNSUPPORTED, detail)


def _read(path: Path) -> str:
    with open(path) as f:
        return f.read()


def _write(path: Path, value: str) -> None:
    with open(path, "w") as f:
        f.write(value)


# ------------------------------------------------------------------ topology


def _read_self_cgroup() -> str:
    try:
        return _read(Path("/proc/self/cgroup"))
    except OSError as e:
        raise _topology(f"/proc/self/cgroup を読めない ({e.strerror})") from None


def _parse_self_cgroup(unit: str, proc_self_cgroup: str) -> tuple[Path, bool]:
    """(unit の cgroup root, 自分が `supervisor/` に居るか) を一度の解釈で返す。"""
    lines = [line for line in proc_self_cgroup.split("\n") if line]
    if len(lines) != 1 or not lines[0].startswith("0::/"):
        raise _topology("cgroup v2 の単一階層でない")
    rel = lines[0][len("0::"):]
    parts = rel.split("/")
    if parts[-1] == unit:
        return Path(CGROUP_MOUNT + rel), False
    if len(parts) >= 3 and parts[-1] == SUPERVISOR and parts[-2] == unit:
        return Path(CGROUP_MOUNT + rel[: -len("/" + SUPERVISOR)]), True
    raise _topology(f"自分の cgroup {rel!r} が unit {unit!r} の root でも supervisor/ でもない")


def unit_cgroup_root(unit: str, proc_self_cgroup: str | None = None) -> Path:
    """`/proc/self/cgroup` (v2 の 1 行だけ) から unit の cgroup root を求める。

    自己移動の後 (`<unit>/supervisor`) も同じ root を返す。それ以外の位置は unsupported。
    """
    if proc_self_cgroup is None:
        proc_self_cgroup = _read_self_cgroup()
    return _parse_self_cgroup(unit, proc_self_cgroup)[0]


def _procs(cg: Path) -> list[str]:
    return _read(cg / "cgroup.procs").split()


def _enable_controllers(root: Path) -> None:
    try:
        available = set(_read(root / "cgroup.controllers").split())
    except OSError as e:
        raise _topology(f"cgroup.controllers を読めない ({e.strerror})") from None
    if not _REQUIRED_CONTROLLERS <= available:
        raise CgroupHostError(ua.REASON_DELEGATION_MISSING,
                              f"root に pids / memory が委任されていない ({sorted(available)})")
    try:
        enabled = set(_read(root / "cgroup.subtree_control").split())
        if not _REQUIRED_CONTROLLERS <= enabled:
            _write(root / "cgroup.subtree_control", "+pids +memory")
        enabled = set(_read(root / "cgroup.subtree_control").split())
    except OSError as e:
        raise CgroupHostError(ua.REASON_DELEGATION_MISSING,
                              f"+pids +memory を有効化できない ({e.strerror})") from None
    if not _REQUIRED_CONTROLLERS <= enabled:
        raise CgroupHostError(ua.REASON_DELEGATION_MISSING,
                              f"subtree_control に pids / memory が無い ({sorted(enabled)})")


def adopt_topology(unit: str) -> Path:
    """自分 (unit の main PID) を `supervisor/` へ移し、root で pids / memory を有効化する。

    冪等性: 同じ process で既に `supervisor/` に居れば、root が空であることと controller を
    確かめるだけ。root に居て `supervisor/` が既にある (前回の残り) 場合は、それが空 (process も
    子 cgroup も無い) なら使い回し、そうでなければ unsupported。
    """
    pid = str(os.getpid())
    root, already = _parse_self_cgroup(unit, _read_self_cgroup())
    sup = root / SUPERVISOR
    try:
        procs = _procs(root)
    except OSError as e:
        raise _topology(f"{root} を読めない ({e.strerror})") from None
    if already:
        if procs:
            raise _topology(f"root に process が居る ({len(procs)} 個)")
        _enable_controllers(root)
        return root
    if procs != [pid]:
        raise _topology(f"root の cgroup.procs が自分だけでない ({len(procs)} 個)")
    try:
        sup.mkdir()
    except FileExistsError:
        try:
            leftover = _procs(sup)
            children = [e.name for e in os.scandir(sup) if e.is_dir(follow_symlinks=False)]
        except OSError as e:
            raise _topology(f"既存の supervisor/ を読めない ({e.strerror})") from None
        if leftover or children:
            raise _topology("既存の supervisor/ が空でない") from None
    except OSError as e:
        raise _topology(f"supervisor/ を作れない ({e.strerror})") from None
    try:
        _write(sup / "cgroup.procs", pid)
        if _procs(root) or pid not in _procs(sup):
            raise _topology("supervisor/ への自己移動を読み戻せない")
    except OSError as e:
        raise _topology(f"supervisor/ へ自己移動できない ({e.strerror})") from None
    _enable_controllers(root)
    return root


# ------------------------------------------------------------------ mission cgroup の後始末


def _populated(cg: Path) -> bool:
    for line in _read(cg / "cgroup.events").split("\n"):
        key, _, value = line.partition(" ")
        if key == "populated":
            return value.strip() != "0"
    raise OSError(0, "cgroup.events に populated が無い")


def remove_mission_cgroup(cg: Path, deadline: float) -> None:
    """保険の kill → populated 0 を deadline まで待つ → procs が空 → rmdir。既に無ければ成功。"""
    try:
        _write(cg / "cgroup.kill", "1")
    except FileNotFoundError:
        if not cg.exists():
            return
    except OSError:
        # kill が書けなくても、空になったかどうかは populated の待ちで判定する
        pass
    if time.monotonic() >= deadline:
        raise CgroupHostError(REASON_CLEANUP_TIMEOUT, f"{cg.name}: deadline を過ぎている")
    while True:
        try:
            populated = _populated(cg)
        except OSError as e:
            if not cg.exists():
                return
            raise CgroupHostError(REASON_CLEANUP_TIMEOUT,
                                  f"{cg.name}: cgroup.events を読めない ({e.strerror})") from None
        if not populated:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CgroupHostError(REASON_CLEANUP_TIMEOUT, f"{cg.name}: populated 0 にならない")
        time.sleep(min(_POLL_SEC, remaining))
    try:
        procs = _procs(cg)
    except OSError as e:
        raise CgroupHostError(REASON_NOT_EMPTY,
                              f"{cg.name}: cgroup.procs を読めない ({e.strerror})") from None
    if procs:
        raise CgroupHostError(REASON_NOT_EMPTY, f"{cg.name}: cgroup.procs が空でない")
    try:
        os.rmdir(cg)
    except OSError as e:
        raise CgroupHostError(REASON_RMDIR_FAILED, f"{cg.name}: rmdir 失敗 ({e.strerror})") from None


def reclaim_stale_mission_cgroups(root: Path, deadline: float) -> list[str]:
    """root 直下の前回の `m-*` を全部回収する。最初の失敗で止まる (gate が閉じるので先は不要)。"""
    names = sorted(e.name for e in os.scandir(root)
                   if e.is_dir(follow_symlinks=False) and e.name.startswith(MISSION_PREFIX))
    for name in names:
        remove_mission_cgroup(root / name, deadline)
    return names


# ------------------------------------------------------------------ gate


class MissionCgroupHost:
    """起動時の admission・topology・前回回収と、次 mission の可否を一箇所で持つ。

    `mission_allowed()` は起動が成功し、かつ一度も gate が閉じていないときだけ真。閉じる経路は
    あっても開く経路は無い。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = False
        self._ready = False
        self._reasons: list[str] = []
        self.admission: ua.UnitAdmission | None = None
        self.root: Path | None = None
        self.reclaimed: tuple[str, ...] = ()

    def _add_reason(self, reason: str) -> None:
        with self._lock:
            if reason not in self._reasons:
                self._reasons.append(reason)

    def start(self, *, deadline: float,
              admit: Callable[[], ua.UnitAdmission] = ua.admit_unit) -> None:
        """process ごとに一度だけ。失敗は例外にせず、固定 reason として gate を閉じる。"""
        with self._lock:
            if self._started:
                raise RuntimeError("MissionCgroupHost.start は process ごとに一度だけ")
            self._started = True
        try:
            adm = admit()
            self.admission = adm
            self.root = adopt_topology(adm.unit)
            self.reclaimed = tuple(reclaim_stale_mission_cgroups(self.root, deadline))
        except (ua.AdmissionError, CgroupHostError) as e:
            self._add_reason(e.reason)
            return
        except OSError:
            # root の列挙などの想定外の I/O 失敗も、topology を確かめられないものとして閉じる
            self._add_reason(ua.REASON_TOPOLOGY_UNSUPPORTED)
            return
        except Exception:
            # 想定外の例外 (UnicodeDecodeError など) でも起動経路を落とさず fail closed にする。
            # reason は固定語しか持てないので、診断は例外名と traceback をログに残す
            logger.exception("mission cgroup host start failed")
            self._add_reason(ua.REASON_TOPOLOGY_UNSUPPORTED)
            return
        with self._lock:
            self._ready = True

    def mission_allowed(self) -> bool:
        with self._lock:
            return self._ready and not self._reasons

    def blocking_reasons(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._reasons)

    def close_gate(self, reason: str) -> None:
        if reason not in GATE_REASONS:
            raise ValueError(f"gate を閉じる固定 reason ではない: {reason!r}")
        self._add_reason(reason)

    def report_worker_not_reaped(self) -> None:
        self.close_gate(REASON_WORKER_NOT_REAPED)

    def cleanup_mission(self, name: str, deadline: float) -> str | None:
        """worker の reap 後に mission cgroup を片付ける。失敗の reason を返し、gate を閉じる。"""
        if self.root is None:
            raise RuntimeError("topology が成立していない")
        if _MISSION_NAME.fullmatch(name) is None:
            # 片付けるべき cgroup を特定できないので、残っているものとして扱う
            self.close_gate(REASON_RMDIR_FAILED)
            return REASON_RMDIR_FAILED
        try:
            remove_mission_cgroup(self.root / name, deadline)
        except CgroupHostError as e:
            self.close_gate(e.reason)
            return e.reason
        return None


__all__ = [
    "CGROUP_MOUNT", "CgroupHostError", "GATE_REASONS", "MissionCgroupHost",
    "REASON_CLEANUP_TIMEOUT", "REASON_NOT_EMPTY", "REASON_RMDIR_FAILED",
    "REASON_WORKER_NOT_REAPED", "adopt_topology", "reclaim_stale_mission_cgroups",
    "remove_mission_cgroup", "unit_cgroup_root",
]
