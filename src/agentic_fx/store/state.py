"""稼働モード・autopilot 等の状態。config でなくここに置く (設計書 §3 の構造的担保)。"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import uuid
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


class DurabilityUncertain(Exception):
    """os.replace は済んだが、その後の親 dir の fsync 系が失敗した。

    新しい内容が書かれたが、耐久性が未確認の状態。元の例外を __cause__ に持つ。
    """


class ResetNotApplied(Exception):
    """解除の保存に失敗したが、旧状態 (ラッチ中) を書き戻せた。解除は適用されていない。"""


class StateUncertain(Exception):
    """解除の保存に失敗し、旧状態の書き戻しにも失敗した。ラッチの状態が不明。"""


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


# 状態が不明になったパスの集合 (プロセス内)。service はコマンドごとに
# StateStore を作り直すので、インスタンスでなくパスで覚える。再起動まで解けない。
_UNCERTAIN_PATHS: set[str] = set()
_UNCERTAIN_GUARD = threading.Lock()


def _path_key(path: Path) -> str:
    return os.path.abspath(str(path))


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

    def _is_uncertain(self) -> bool:
        with _UNCERTAIN_GUARD:
            return _path_key(self._path) in _UNCERTAIN_PATHS

    def _mark_uncertain(self) -> None:
        with _UNCERTAIN_GUARD:
            _UNCERTAIN_PATHS.add(_path_key(self._path))

    @contextmanager
    def _shared(self):
        """writer の復旧または fail-closed 化が終わるまで読者を待たせる共有 lock。

        lock ファイルを作れない (読み取り専用領域等) ときは開けるなら読み取りで、
        存在しなければ writer が居ないので lock なしで読む。
        """
        if not self._path.parent.exists():
            yield
            return
        lock = str(self._path) + ".lock"
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            try:
                fd = os.open(lock, os.O_RDONLY)
            except OSError:
                yield
                return
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    def load(self) -> AppState:
        """共有 lock を取って読む。状態不明になったパスは常にラッチ済みとして返す。"""
        with self._shared():
            state = self._load_unlocked()
        if self._is_uncertain():
            return replace(state, kill_switch_latched=True)
        return state

    def _load_unlocked(self) -> AppState:
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
        self._purge_orphan_temps(parent)
        try:
            mode = self._path.stat().st_mode & 0o7777
        except FileNotFoundError:
            mode = None
        tmp = str(parent / f"{self._path.name}.{os.getpid()}."
                           f"{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            with os.fdopen(fd, "wb") as f:
                if mode is not None:
                    os.fchmod(f.fileno(), mode)
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
        try:
            dfd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as e:
            raise DurabilityUncertain(
                "state file was replaced but directory fsync failed") from e

    def _purge_orphan_temps(self, parent: Path) -> None:
        """kill で残った一時ファイルを消す。save は排他中にしか呼ばれない。"""
        for orphan in parent.glob(self._path.name + ".*.tmp"):
            try:
                orphan.unlink()
            except OSError:
                pass

    def update(self, **changes) -> AppState:
        valid = {f.name for f in fields(AppState)}
        unknown = set(changes) - valid
        if unknown:
            raise TypeError(f"unknown state fields: {unknown}")
        with self._exclusive():
            current = self._load_unlocked()
            if changes == {"kill_switch_latched": True} \
                    and current.kill_switch_latched:
                return current  # 既にラッチ済み。書かない
            if changes.get("kill_switch_latched") is False:
                changes = {**changes, "kill_switch_latched_at": None}
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
            if self._is_uncertain():
                raise StateUncertain(
                    "kill switch state is unknown; restart after checking it")
            current = self._load_unlocked()
            if not current.kill_switch_latched:
                raise NotLatched()
            if current.kill_switch_generation != expected_generation:
                raise GenerationMismatch(current.kill_switch_generation,
                                         current.kill_switch_latched_at)
            state = replace(current, kill_switch_latched=False)
            try:
                self.save(state)
            except DurabilityUncertain as first:
                # 解除が書かれたかもしれない。ラッチ中の旧状態を書き直し、
                # 解除は適用されなかったことに揃える。
                try:
                    self.save(current)
                except Exception as second:
                    self._mark_uncertain()
                    raise StateUncertain(
                        "kill switch state is unknown after a failed save"
                    ) from second
                raise ResetNotApplied(
                    "kill switch reset was not applied") from first
            return state
