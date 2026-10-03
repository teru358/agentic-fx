"""稼働モード・autopilot 等の状態。config でなくここに置く (設計書 §3 の構造的担保)。"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import timezone
from pathlib import Path

from agentic_fx.core.contracts import Clock, Mode, SystemClock


class StateError(Exception):
    """state.json が資金安全上信頼できない内容だった場合に送出する。

    fail closed: 握りつぶしてデフォルトを返さず、サービスの起動を止める。
    """


class NotLatched(Exception):
    """kill switch がラッチされていないのに解除が要求された。"""


class GenerationMismatch(Exception):
    """解除が要求された世代と、現在のラッチの世代が違う (ラッチが更新された)。"""

    def __init__(self, current: int, latched_at: str | None) -> None:
        super().__init__(f"kill switch generation is {current}")
        self.current = current
        self.latched_at = latched_at


@dataclass(frozen=True, slots=True)
class AppState:
    initialized: bool = False
    mode: Mode = Mode.LEARNING
    autopilot: bool = False
    kill_switch_latched: bool = False
    kill_switch_generation: int = 0
    kill_switch_latched_at: str | None = None


_BOOL_FIELDS = ("initialized", "autopilot", "kill_switch_latched")
_REQUIRED_KEYS = ("initialized", "mode", "autopilot", "kill_switch_latched")


class StateStore:
    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        self._path = path
        self._clock = clock if clock is not None else SystemClock()
        self._thread_lock = threading.Lock()

    @contextmanager
    def _exclusive(self):
        """プロセス内 (threading.Lock) → プロセス間 (flock) の順で取る。

        flock は open file description 単位なので、同一プロセスの別スレッドは
        先に threading.Lock で直列化する。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            fd = os.open(str(self._path) + ".lock",
                         os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # close で flock も解放される

    def load(self) -> AppState:
        if not self._path.exists():
            return AppState()
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise StateError(
                f"state file must contain a JSON object, got {type(raw).__name__}")

        missing = [k for k in _REQUIRED_KEYS if k not in raw]
        if missing:
            raise StateError(f"state file missing required keys: {missing}")

        for key in _BOOL_FIELDS:
            v = raw[key]
            if type(v) is not bool:
                raise StateError(
                    f"state field {key!r} must be a JSON boolean, "
                    f"got {v!r} ({type(v).__name__})")

        mode_raw = raw["mode"]
        try:
            mode = Mode(mode_raw)
        except ValueError:
            raise StateError(
                f"state field 'mode' has invalid value {mode_raw!r} "
                f"(allowed: {[m.value for m in Mode]})") from None

        generation = raw.get("kill_switch_generation", 0)
        if type(generation) is not int:
            raise StateError(
                f"state field 'kill_switch_generation' must be a JSON integer, "
                f"got {generation!r} ({type(generation).__name__})")
        latched_at = raw.get("kill_switch_latched_at")
        if latched_at is not None and type(latched_at) is not str:
            raise StateError(
                f"state field 'kill_switch_latched_at' must be a string or "
                f"null, got {latched_at!r} ({type(latched_at).__name__})")

        return AppState(initialized=raw["initialized"],
                        mode=mode,
                        autopilot=raw["autopilot"],
                        kill_switch_latched=raw["kill_switch_latched"],
                        kill_switch_generation=generation,
                        kill_switch_latched_at=latched_at)

    def save(self, state: AppState) -> None:
        """一意名の一時ファイル → fsync → os.replace → 親 dir の fsync。"""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        d = asdict(state)
        d["mode"] = state.mode.value
        payload = json.dumps(d, ensure_ascii=False, indent=1).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=self._path.name + ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        dfd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def update(self, **changes) -> AppState:
        valid = {f.name for f in fields(AppState)}
        unknown = set(changes) - valid
        if unknown:
            raise TypeError(f"unknown state fields: {unknown}")
        with self._exclusive():
            current = self.load()
            if changes.get("kill_switch_latched") is True \
                    and not current.kill_switch_latched:
                changes = {
                    **changes,
                    "kill_switch_generation": current.kill_switch_generation + 1,
                    "kill_switch_latched_at":
                        self._clock.now().astimezone(timezone.utc).isoformat(),
                }
            state = replace(current, **changes)
            self.save(state)
            return state

    def reset_kill_switch(self, expected_generation: int) -> AppState:
        """ラッチ中かつ世代が一致するときだけ解除する。それ以外は何も書かない。"""
        with self._exclusive():
            current = self.load()
            if not current.kill_switch_latched:
                raise NotLatched()
            if current.kill_switch_generation != expected_generation:
                raise GenerationMismatch(current.kill_switch_generation,
                                         current.kill_switch_latched_at)
            state = replace(current, kill_switch_latched=False)
            self.save(state)
            return state
