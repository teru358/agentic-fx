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
    """解除は完了しなかった。完了前の失敗なので新規 OPEN は止まったまま。

    marker が残っているときは load() がラッチ中として返し続ける (`reconcile`)。
    """


class StateUncertain(Exception):
    """kill switch の状態が不確定のまま (marker が残っている、または消せない)。

    人が `killswitch reconcile` で確認するまで解除を受け付けない。
    """


class LockUnavailable(StateError):
    """状態ファイルの親 dir があるのに、読み取り用の lock を取れない。

    writer が居ないことの証拠にならないので、読み取りを続けず fail closed にする。
    """


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


_MARKER_SUFFIX = ".reset-in-progress"


class StateStore:
    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        # symlink 経由の別名でも lock・marker・一時ファイルが同じ場所になるよう、
        # 実体のパスに揃える。
        self._path = Path(os.path.realpath(path))
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

    @property
    def _marker(self) -> Path:
        return Path(str(self._path) + _MARKER_SUFFIX)

    def reconcile_marker(self) -> dict | None:
        """解除の途中で止まった印。無ければ None。壊れていても「有る」として返す。"""
        try:
            raw = self._marker.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            return {"unreadable": f"{type(e).__name__}"}
        try:
            data = json.loads(raw)
        except ValueError:
            return {"unreadable": "invalid json"}
        return data if isinstance(data, dict) else {"unreadable": "not an object"}

    def _fsync_dir(self) -> None:
        dfd = os.open(str(self._path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    @contextmanager
    def _shared(self):
        """writer の replace 途中を読者に見せない共有 lock。

        親 dir が無ければ初期化前で writer も居ないので lock なしで読む。親 dir が
        あるのに lock を開けないときは writer の不在を言えないので LockUnavailable。
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
            except OSError as e:
                raise LockUnavailable(
                    f"cannot open state lock: {type(e).__name__}") from e
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    def load(self) -> AppState:
        """共有 lock を取って読む。解除の途中 marker があればラッチ済みとして返す。"""
        with self._shared():
            state = self._load_unlocked()
            pending = self.reconcile_marker() is not None
        if pending:
            return replace(state, kill_switch_latched=True)
        return state

    def file_value(self) -> AppState:
        """marker を無視した、ファイルそのままの値 (reconcile の表示用)。"""
        with self._shared():
            return self._load_unlocked()

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
            self._fsync_dir()
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

    def _write_marker(self, expected_generation: int) -> None:
        """marker を作って fsync し、親 dir も fsync する。作れなければ何も残さない。"""
        body = json.dumps({
            "kind": "kill_switch_reset",
            "requested_generation": expected_generation,
            "started_at": self._clock.now().astimezone(timezone.utc).isoformat(),
        }, ensure_ascii=False).encode("utf-8")
        created = False
        try:
            fd = os.open(str(self._marker),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            created = True
            with os.fdopen(fd, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            self._fsync_dir()
        except BaseException as e:
            try:
                if created:
                    self._marker.unlink()
            except FileNotFoundError:
                pass
            except OSError as unlink_error:
                # 作りかけの印が残った。ラッチ側に倒れたまま人の確認を待つ。
                raise StateUncertain(
                    "reset marker could not be created or removed") \
                    from unlink_error
            if isinstance(e, OSError):
                raise ResetNotApplied(
                    "reset marker could not be written; nothing changed") from e
            raise

    def _remove_marker(self) -> None:
        """marker を消す。消えたあとの親 dir fsync の失敗は伝えない。

        消えた marker が crash で戻っても、ラッチ側に倒れるだけで安全側。
        """
        self._marker.unlink()
        try:
            self._fsync_dir()
        except OSError:
            pass

    def reset_kill_switch(self, expected_generation: int) -> AppState:
        """ラッチ中かつ世代が一致するときだけ解除する。それ以外は何も書かない。

        write-ahead: replace の前に marker を永続化し、全工程が済んでから消す。
        途中で止まれば marker が残り、load() は(別プロセスでも)ラッチ中を返す。
        """
        with self._exclusive():
            if self.reconcile_marker() is not None:
                raise StateUncertain(
                    "a previous reset did not finish; run `killswitch reconcile`")
            current = self._load_unlocked()
            if not current.kill_switch_latched:
                raise NotLatched()
            if current.kill_switch_generation != expected_generation:
                raise GenerationMismatch(current.kill_switch_generation,
                                         current.kill_switch_latched_at)
            state = replace(current, kill_switch_latched=False)
            self._write_marker(expected_generation)
            try:
                self.save(state)
            except Exception as e:
                # marker は残す。ファイルが解除済みかどうかを復旧で確かめない。
                raise ResetNotApplied(
                    "kill switch reset did not finish; marker kept") from e
            try:
                self._remove_marker()
            except OSError as e:
                raise ResetNotApplied(
                    "kill switch reset did not finish; marker kept") from e
            return state

    def confirm_latched(self) -> AppState:
        """marker を「ラッチ中として確定」して消す (解除側には倒さない)。

        ファイルをラッチ状態で書き直し、成功してから marker を消す。marker が無ければ
        何も書かず現在値を返す。
        """
        with self._exclusive():
            if self.reconcile_marker() is None:
                return self._load_unlocked()
            current = self._load_unlocked()
            if current.kill_switch_latched:
                state = current
            else:
                state = replace(
                    current, kill_switch_latched=True,
                    kill_switch_generation=current.kill_switch_generation + 1,
                    kill_switch_latched_at=self._clock.now().astimezone(
                        timezone.utc).isoformat())
            self.save(state)
            self._remove_marker()
            return state
