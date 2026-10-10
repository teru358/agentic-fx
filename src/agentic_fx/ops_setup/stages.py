"""機能 enable / disable を durable stage file で再開する。"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from agentic_fx._safe_error import safe_text
from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import KeySetError, check_key_dir_usable, enroll, enrolled_key_matches, key_dir
from agentic_fx.store.enroll_marker import ENROLL_IN_PROGRESS, EXIT_ENROLL_IN_PROGRESS, enroll_marker_path
from agentic_fx.store.instance_lock import InstanceAlreadyRunning, acquire_instance_lock

from .systemd import Systemd, SystemdError
from .units import (OpsExecNotFound, UnitPaths, atomic_write, install_units, remove_durably,
                    resolve_ops_exec, uninstall_feature, units_match)


_FEATURE_PRINCIPALS = {
    "bot": Principal.OPS_BOT,
    "web": Principal.OPS_WEB,
    "notify": Principal.OPS_NOTIFY,
}
_ENABLE_TERMINAL = "enabled"
_DISABLE_TERMINAL = "disabled"
_ENABLE_ORDER = ("staged", "stopped", "enrolled", "installed", "started", "checked", _ENABLE_TERMINAL)
_DISABLE_ORDER = (_ENABLE_TERMINAL, "stopping-ops", "uninstalled", _DISABLE_TERMINAL)
_STAGES = {"enable": _ENABLE_ORDER, "disable": _DISABLE_ORDER}
Hook = Callable[[str], None]
# manifest は active なのに鍵 file が使えない・一致しない。
_KEY_MISMATCH_CODES = frozenset({"key_missing", "key_file_invalid", "digest_mismatch"})
# enable 前に afx がこの状態だったときだけ、enable の最後に afx を起こす。
_AFX_RUNNING = frozenset({"active", "activating", "reloading"})


class SystemdPort(Protocol):
    def stop(self, unit: str) -> None: ...
    def start(self, unit: str) -> None: ...
    def ensure_active(self, unit: str) -> None: ...
    def is_active(self, unit: str) -> bool: ...
    def is_stopped(self, unit: str) -> bool: ...
    def daemon_reload(self) -> None: ...
    def show(self, unit: str, *props: str) -> dict[str, str]: ...


class InvalidState(RuntimeError):
    """別の未終端操作、又は他機能・持ち主不明の marker が存在する。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


@dataclass(frozen=True)
class StageResult:
    feature: str
    operation: str
    stage: str


def _stage_path(root: Path, feature: str) -> Path:
    return root / "data" / "ops" / f"{feature}.stage.json"


def read_stage(root: Path, feature: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
    path = _stage_path(root, feature)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise InvalidState("invalid_stage", f"path={path} JSON として読めない ({type(error).__name__})") from None
    problem = _stage_problem(data, feature)
    if problem:
        raise InvalidState("invalid_stage", f"path={path} {problem}")
    return data


# 型を確かめる任意の key。手編集や破損で型が変わると、再開の判断が TypeError や誤判定になる。
_STAGE_FIELD_TYPES: dict[str, type] = {
    "principal": str, "registered": bool, "ops_active": bool, "committed": bool,
    "afx_before_stop": str, "failure": str, "failure_detail": str, "warn": str,
}
_STAGE_LIST_FIELDS = ("artifacts", "wants")


def _stage_problem(data: object, feature: str) -> str:
    """stage file の形の崩れを 1 つ短く返す。問題が無ければ空文字。"""
    if not isinstance(data, dict):
        return "JSON object でない"
    operation = data.get("operation")
    if not isinstance(operation, str) or operation not in _STAGES:
        return "operation が enable / disable でない"
    stage = data.get("stage")
    if not isinstance(stage, str) or stage not in _STAGES[operation]:
        return f"stage が {operation} の段でない"
    if data.get("feature") != feature:
        return f"feature が {feature} でない"
    for key, kind in _STAGE_FIELD_TYPES.items():
        if key in data and type(data[key]) is not kind:
            return f"{key} の型が {kind.__name__} でない"
    for key in _STAGE_LIST_FIELDS:
        if key in data and (not isinstance(data[key], list)
                            or not all(isinstance(item, str) for item in data[key])):
            return f"{key} が文字列の list でない"
    return ""


def _write_stage(root: Path, state: dict[str, Any]) -> None:
    path = _stage_path(root, str(state["feature"]))
    raw = (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    atomic_write(path, raw, 0o600)


def read_marker(root: Path) -> dict[str, Any] | None:
    """enroll marker を読む。無ければ None、読めない・形が違えば空 dict (持ち主不明)。

    feature が既知の機能名、started_at が空でない文字列の両方を満たすものだけを持ち主付きとする。
    unhashable な feature を集合と比べて TypeError にしないよう、型を先に見る。
    """
    path = enroll_marker_path(root)
    if not os.path.lexists(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    feature, started = data.get("feature"), data.get("started_at")
    if not (isinstance(feature, str) and feature in _FEATURE_PRINCIPALS
            and isinstance(started, str) and started):
        return {}
    return data


def _remove_marker(root: Path) -> None:
    # 削除も durable にする。crash 後に marker が蘇ると afx が起動できない。
    remove_durably(enroll_marker_path(root))


def _describe(error: BaseException) -> str:
    """失敗の原因を stage file に残す短い 1 行にする。"""
    if isinstance(error, SystemdError):
        return f"{error.reason}: {error.detail}" if error.detail else error.reason
    return safe_text(" ".join(f"{type(error).__name__}: {error}".split()))


def _noop(_name: str) -> None:
    return


class OpsSetup:
    """stage file と実物を照合しながら一機能ずつ遷移する。"""

    def __init__(self, root: Path, *, systemd: SystemdPort | None = None,
                 paths: UnitPaths | None = None, ready: Callable[[str], bool] | None = None,
                 hook: Hook = _noop) -> None:
        self.root = Path(root)
        self.systemd = Systemd() if systemd is None else systemd
        self.paths = UnitPaths(Path.home() / ".config" / "systemd" / "user") if paths is None else paths
        self.ready = (lambda _feature: True) if ready is None else ready
        self.hook = hook
        # 書込み失敗を操作の境界で記録するため、実行中の操作の state と、既に固有の reason で
        # 記録済みの OSError を持つ。
        self._current: dict[str, Any] | None = None
        self._recorded: BaseException | None = None

    def _before(self, name: str) -> None:
        self.hook(f"before_{name}")

    def _after(self, name: str) -> None:
        self.hook(f"after_{name}")

    def _set_stage(self, state: dict[str, Any], stage: str, **extra: Any) -> None:
        self.hook(f"before_stage_{stage}")
        state["stage"] = stage
        state.update(extra)
        state.pop("failure", None)
        state.pop("failure_detail", None)
        _write_stage(self.root, state)
        self.hook(f"after_stage_{stage}")

    def _failure(self, state: dict[str, Any], reason: str, detail: str = "") -> None:
        state["failure"] = reason
        if detail:
            state["failure_detail"] = detail
        else:
            state.pop("failure_detail", None)
        _write_stage(self.root, state)

    def _unit_name(self, feature: str) -> str:
        return self.paths.feature_unit(feature)

    def _unfinished(self, feature: str, operation: str) -> None:
        for other in _FEATURE_PRINCIPALS:
            saved = read_stage(self.root, other, missing_ok=True)
            if saved is None:
                continue
            terminal = _ENABLE_TERMINAL if saved["operation"] == "enable" else _DISABLE_TERMINAL
            if saved["stage"] != terminal and (other != feature or saved["operation"] != operation):
                raise InvalidState("invalid_state")

    def _active_features(self) -> set[str]:
        features: set[str] = set()
        for feature in _FEATURE_PRINCIPALS:
            saved = read_stage(self.root, feature, missing_ok=True)
            if saved is None:
                continue
            if saved["operation"] == "enable" and saved["stage"] == _ENABLE_TERMINAL:
                features.add(feature)
            if saved["operation"] == "disable" and saved["stage"] != _DISABLE_TERMINAL:
                features.add(feature)
        return features

    def _new_state(self, feature: str, operation: str, stage: str) -> dict[str, Any]:
        return {"operation": operation, "stage": stage, "feature": feature,
                "principal": _FEATURE_PRINCIPALS[feature].value, "registered": False,
                "artifacts": [], "wants": [], "ops_active": False, "committed": False}

    def _guarded(self, run: Callable[[], StageResult]) -> StageResult:
        """操作中の OSError を、固有の reason が無ければ material_write_failed として stage file に残す。"""
        self._current = None
        self._recorded = None
        try:
            return run()
        except OSError as error:
            if error is self._recorded or self._current is None:
                raise
            detail = _describe(error)
            try:
                self._failure(self._current, "material_write_failed", detail)
            except OSError:
                # stage file 自体が書けない。記録はできないが元の失敗は返す。
                pass
            raise SystemdError("material_write_failed", detail) from error
        finally:
            self._current = None
            self._recorded = None

    def _rollback_install(self, state: dict[str, Any], error: BaseException) -> None:
        """途中まで書いた資材を戻し、manager にも読み直させる。戻しの失敗も install_failed に含める。"""
        notes = [_describe(error)]
        try:
            uninstall_feature(self.paths, self._active_features())
        except OSError as rollback_error:
            notes.append(f"資材の戻しに失敗 ({_describe(rollback_error)})")
        try:
            self.systemd.daemon_reload()
        except SystemdError as reload_error:
            notes.append(f"戻し後の daemon-reload に失敗 ({_describe(reload_error)})")
        self._failure(state, "install_failed", "; ".join(notes))
        self._recorded = error

    def _stop_afx_after_failure(self, state: dict[str, Any], error: SystemdError) -> None:
        """失敗を記録した後で afx を止める。止められなくても元の失敗理由は残す。"""
        try:
            self.systemd.stop(self.paths.afx_unit)
        except SystemdError as stop_error:
            note = f"afx の停止にも失敗 ({_describe(stop_error)})"
            self._failure(state, error.reason, f"{error.detail}; {note}" if error.detail else note)

    def _resolve_exec(self, state: dict[str, Any]) -> None:
        # unit 資材の照合も設置も解決後の起動コマンドで行うので、外部変更より前に決める。
        try:
            self.paths = resolve_ops_exec(self.paths)
        except OpsExecNotFound as error:
            detail = safe_text(" ".join(str(error).split()))
            self._failure(state, "ops_exec_not_found", detail)
            raise SystemdError("ops_exec_not_found", detail) from error

    def _check_key_dir(self) -> None:
        # afx keys と同じ検査を、afx を止める前に通す。鍵 dir が使えないなら外部変更をしない。
        check_key_dir_usable(key_dir(self.root), root=self.root)

    def _principal_active(self, feature: str, state: dict[str, Any]) -> bool:
        """principal が active で鍵 file が manifest と一致するか。

        active なのに鍵が一致しなければ、enroll は無変更で成功してしまうので止める。
        鍵は再 enroll も revoke もせず、利用者に回復手順を示す。
        """
        principal = _FEATURE_PRINCIPALS[feature]
        try:
            return enrolled_key_matches(key_dir(self.root), principal)
        except KeySetError as error:
            if error.code not in _KEY_MISMATCH_CODES:
                # 鍵 dir 自体の問題は enroll / 鍵 dir 検査が固定 reason で止める。
                return False
            detail = (f"code={error.code} 回復: afx を止めて afx keys rotate {principal.value}"
                      f" を実行し、afx ops enable {feature} を再実行する")
            self._failure(state, "principal_key_mismatch", detail)
            raise InvalidState("principal_key_mismatch", detail) from None

    def _units_in_place(self, feature: str) -> bool:
        # manager に問い合わせられないことは「資材が崩れた」ではない。崩れたと読んで設置を
        # やり直すと、失敗時の戻しで動いている資材まで外し得るので、SystemdError は呼び出し側へ返す。
        if not units_match(self.paths, self._active_features() | {feature}):
            return False
        self.systemd.daemon_reload()
        unit = self._unit_name(feature)
        if self.systemd.show(unit, "LoadState")["LoadState"] != "loaded":
            return False
        return unit in self.systemd.show(self.paths.afx_unit, "Wants")["Wants"].split()

    @staticmethod
    def _afx_was_running(state: dict[str, Any]) -> bool:
        # 記録が無い stage file は、afx が動いていた前提で扱う。
        return state.get("afx_before_stop", "active") in _AFX_RUNNING

    def _verified_stage(self, feature: str, stage: str, state: dict[str, Any]) -> str:
        """記録された stage の事後条件を前から順に実物と照合し、崩れていない最後の stage を返す。"""
        reached = _ENABLE_ORDER.index(stage)
        if reached >= _ENABLE_ORDER.index("enrolled") and (
                not self._principal_active(feature, state) or read_marker(self.root) is not None):
            # marker が残っていれば enrolled は完了していない。stopped から marker 削除を続ける。
            return "stopped"
        if reached >= _ENABLE_ORDER.index("installed") and not self._units_in_place(feature):
            return "enrolled"
        if stage == _ENABLE_TERMINAL:
            # 有効化後は、今 afx が動いているなら ops も動いているはず。afx を後から起こした
            # 場合もあるので、enable 前の記録ではなく今の afx の姿で決め、記録もそれに合わせる。
            afx_now = self.systemd.show(self.paths.afx_unit, "ActiveState")["ActiveState"]
            if afx_now in _AFX_RUNNING and not self.systemd.is_active(self._unit_name(feature)):
                state["afx_before_stop"] = afx_now
                return "installed"
            return stage
        if (reached >= _ENABLE_ORDER.index("started") and self._afx_was_running(state)
                and not self.systemd.is_active(self._unit_name(feature))):
            return "installed"
        return stage

    def _verify_resumed(self, feature: str, state: dict[str, Any]) -> None:
        """記録された stage を実物と照合し、崩れていれば stage_mismatch でその段へ戻す。"""
        try:
            verified = self._verified_stage(feature, str(state["stage"]), state)
        except SystemdError as error:
            # 照合できないまま進めも戻しもしない。理由を残して止まる。
            self._failure(state, error.reason, error.detail)
            raise
        if verified != state["stage"]:
            state["stage"] = verified
            state["committed"] = False
            self._failure(state, "stage_mismatch")

    def _restart_prevent_warning(self) -> str:
        """afx の unit が終了コード 75 を再起動の対象から外しているかを見る。

        外していないと、marker が残った間 afx は即終了と再起動を繰り返す。unit は利用者の持ち物
        なので書き換えず、確かめられなければ (問い合わせ失敗も含め) 警告だけを残す。
        """
        try:
            values = self.systemd.show(self.paths.afx_unit, "RestartPreventExitStatus")
        except SystemdError:
            return "afx_restart_prevent_missing"
        if str(EXIT_ENROLL_IN_PROGRESS) in values["RestartPreventExitStatus"].split():
            return ""
        return "afx_restart_prevent_missing"

    def _lock(self):
        try:
            return acquire_instance_lock(self.root / "data")
        except InstanceAlreadyRunning as error:
            raise SystemdError("afx_still_running") from error

    def _check_marker_owner(self, feature: str) -> None:
        # 別機能 (又は持ち主不明) の marker を勝手に上書き・回収しない。
        # 自動では消さない。利用者が持ち主の enable を再実行するか、手で消す。
        marker = read_marker(self.root)
        if marker is None:
            return
        path = enroll_marker_path(self.root)
        owner = marker.get("feature")
        if owner not in _FEATURE_PRINCIPALS:
            raise InvalidState("marker_corrupt", f"path={path} 回復: 中身を確かめて marker を手で削除する")
        if owner != feature:
            raise InvalidState("marker_foreign", f"path={path} feature={owner} 回復: "
                               f"afx ops enable {owner} を再実行するか、marker を手で削除する")

    def _place_marker(self, feature: str) -> None:
        # 既に在る自機能の marker は書き換えない (開始時刻は最初の enable のまま)。
        if read_marker(self.root) is not None:
            return
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = (json.dumps({"feature": feature, "started_at": started}, sort_keys=True) + "\n").encode()
        atomic_write(enroll_marker_path(self.root), raw, 0o600)

    def _quiesce_afx(self, state: dict[str, Any]):
        """marker を置き、afx を止めて止まったことを manager で確かめ、instance flock を取る。

        失敗したら staged に戻す。marker は残し、同じ enable の再実行が回収する。
        """
        try:
            if "afx_before_stop" not in state:
                # 止める前の状態は最初の一回だけ記録する。再開時の観測は自分が止めた後の姿になる。
                state["afx_before_stop"] = self.systemd.show(self.paths.afx_unit, "ActiveState")["ActiveState"]
                _write_stage(self.root, state)
            self._before("marker")
            try:
                self._place_marker(str(state["feature"]))
            except OSError as error:
                raise SystemdError("marker_write_failed", _describe(error)) from error
            self._after("marker")
            self._before("stop")
            self.systemd.stop(self.paths.afx_unit)
            self._after("stop")
            self._before("show")
            seen = self.systemd.show(self.paths.afx_unit, "ActiveState")["ActiveState"]
            self._after("show")
            if seen not in {"inactive", "failed"}:
                raise SystemdError("afx_still_running")
            self._before("lock")
            lock = self._lock()
        except SystemdError as error:
            state["stage"] = "staged"
            self._failure(state, error.reason, error.detail)
            raise
        try:
            self._after("lock")
        except BaseException:
            lock.close()
            raise
        return lock

    def enable(self, feature: str) -> StageResult:
        if feature not in _FEATURE_PRINCIPALS:
            raise ValueError("unknown_feature")
        return self._guarded(lambda: self._enable(feature))

    def _enable(self, feature: str) -> StageResult:
        saved = read_stage(self.root, feature, missing_ok=True)
        self._unfinished(feature, "enable")
        self._check_marker_owner(feature)
        fresh = saved is None or saved["stage"] == _DISABLE_TERMINAL
        state = self._new_state(feature, "enable", "staged") if fresh else saved
        self._current = state
        if fresh:
            _write_stage(self.root, state)
        self._resolve_exec(state)
        # afx を止める前に鍵の不一致を見つける。不一致は enroll では直らない。
        self._principal_active(feature, state)
        if not fresh:
            # stage file の記録だけを信じない (enabled も含む)。実物が崩れていれば、その段からやり直す。
            self._verify_resumed(feature, state)
            if state["stage"] == _ENABLE_TERMINAL:
                return StageResult(feature, "enable", _ENABLE_TERMINAL)
        lock = None
        try:
            if state["stage"] in {"staged", "stopped"}:
                self._check_key_dir()
            if state["stage"] == "staged":
                lock = self._quiesce_afx(state)
                self._set_stage(state, "stopped")
            elif state["stage"] == "stopped" and not self._principal_active(feature, state):
                # enroll 前に落ちた。flock は process と共に外れているので、止めて取り直す。
                lock = self._quiesce_afx(state)
                self._set_stage(state, "stopped")

            if state["stage"] == "stopped":
                if lock is not None:
                    self._before("enroll")
                    enroll(key_dir(self.root), _FEATURE_PRINCIPALS[feature])
                    self._after("enroll")
                    self._before("unlock")
                    lock.close()
                    lock = None
                    self._after("unlock")
                # enroll 済みで再開した場合は marker 削除から続ける。
                if read_marker(self.root) is not None:
                    self._before("unmark")
                    try:
                        _remove_marker(self.root)
                    except OSError as error:
                        # stopped に留まる。同じ enable の再実行が marker 削除から続ける。
                        detail = _describe(error)
                        self._failure(state, "marker_remove_failed", detail)
                        raise SystemdError("marker_remove_failed", detail) from error
                    self._after("unmark")
                self._set_stage(state, "enrolled", registered=True)

            if state["stage"] == "enrolled":
                try:
                    self._before("install")
                    active = self._active_features() | {feature}
                    install_units(self.paths, active)
                    self.systemd.daemon_reload()
                    self._after("install")
                except (OSError, SystemdError) as error:
                    self._rollback_install(state, error)
                    raise
                self._set_stage(state, "installed", artifacts=[str(self.paths.template_path), str(self.paths.dropin_path)],
                                wants=sorted(active))

            # afx が元々止まっていれば起こさない。ops は afx の Wants で引かれ BindsTo で縛られるので、
            # ops だけを start しても afx が起きてしまう。ops は afx の次の起動で一緒に起きる。
            running = self._afx_was_running(state)
            if state["stage"] == "installed" and not running:
                self._set_stage(state, "started", ops_active=False)
            if state["stage"] == "installed":
                try:
                    self._before("start")
                    self.systemd.start(self.paths.afx_unit)
                    self.systemd.ensure_active(self._unit_name(feature))
                    self._after("start")
                except SystemdError as error:
                    self._failure(state, error.reason, error.detail)
                    self._stop_afx_after_failure(state, error)
                    raise
                self._set_stage(state, "started", ops_active=True)

            if state["stage"] == "started" and not running:
                self._set_stage(state, "checked")
            if state["stage"] == "started":
                try:
                    self._before("check")
                    if not self.systemd.is_active(self._unit_name(feature)) or not self.ready(feature):
                        raise SystemdError("ops_not_ready")
                    self._after("check")
                except SystemdError as error:
                    state["stage"] = "installed"
                    self._failure(state, error.reason, error.detail)
                    self._stop_afx_after_failure(state, error)
                    raise
                self._set_stage(state, "checked")

            if state["stage"] == "checked":
                warn = self._restart_prevent_warning()
                state.pop("warn", None)
                extra = {"warn": warn} if warn else {}
                self._set_stage(state, "enabled", committed=True, **extra)
            return StageResult(feature, "enable", str(state["stage"]))
        except KeySetError as error:
            self._failure(state, error.code)
            raise
        finally:
            if lock is not None:
                lock.close()

    def disable(self, feature: str) -> StageResult:
        if feature not in _FEATURE_PRINCIPALS:
            raise ValueError("unknown_feature")
        return self._guarded(lambda: self._disable(feature))

    def _disable(self, feature: str) -> StageResult:
        saved = read_stage(self.root, feature, missing_ok=True)
        if saved is None:
            # 一度も enable していない。照合する記録が無い。
            return StageResult(feature, "disable", _DISABLE_TERMINAL)
        if saved["operation"] == "enable" and saved["stage"] != _ENABLE_TERMINAL:
            raise InvalidState("invalid_state")
        self._unfinished(feature, "disable")
        state = self._new_state(feature, "disable", "enabled") if saved["operation"] == "enable" else saved
        self._current = state
        if saved["operation"] == "enable":
            _write_stage(self.root, state)
        if self._active_features() - {feature}:
            # 残る機能の template を書き直す・照合するときだけ起動コマンドが要る。
            self._resolve_exec(state)
        if state["stage"] == _DISABLE_TERMINAL:
            # stage file だけを信じない。unit が残って動いている・Wants が残っていれば戻る。
            back = self._checked_gap(feature, state)
            if back is None:
                return StageResult(feature, "disable", _DISABLE_TERMINAL)
            state["stage"] = back
            state["committed"] = False
            self._failure(state, "stage_mismatch")
        if state["stage"] == "enabled":
            try:
                self._before("stop_ops")
                self.systemd.stop(self._unit_name(feature))
                # 止まったと確かめられたときだけ先へ進む。問い合わせ失敗は unavailable で止まる。
                if not self.systemd.is_stopped(self._unit_name(feature)):
                    raise SystemdError("ops_still_active")
                self._after("stop_ops")
            except SystemdError as error:
                self._failure(state, error.reason, error.detail)
                raise
            self._set_stage(state, "stopping-ops")
        if state["stage"] == "stopping-ops":
            try:
                self._before("uninstall")
                remaining = self._active_features() - {feature}
                uninstall_feature(self.paths, remaining)
                self.systemd.daemon_reload()
                self._after("uninstall")
            except (OSError, SystemdError) as error:
                self._failure(state, "uninstall_failed", _describe(error))
                self._recorded = error
                raise
            self._set_stage(state, "uninstalled", artifacts=[], wants=sorted(remaining))
        if state["stage"] == "uninstalled":
            back = self._checked_gap(feature, state)
            if back is not None:
                state["stage"] = back
                self._failure(state, "uninstall_incomplete")
                raise SystemdError("uninstall_incomplete")
            self._set_stage(state, "disabled", committed=True)
        return StageResult(feature, "disable", str(state["stage"]))

    def _checked_gap(self, feature: str, state: dict[str, Any]) -> str | None:
        """_uninstall_gap を呼び、manager に問い合わせられなければ理由を残して止まる。"""
        try:
            return self._uninstall_gap(feature)
        except SystemdError as error:
            self._failure(state, error.reason, error.detail)
            raise

    def _uninstall_gap(self, feature: str) -> str | None:
        """uninstall の結果を manager の見え方と資材の両方で確かめ、崩れていれば戻る段を返す。

        manager に問い合わせられないときは SystemdError のまま返す (崩れたとは読まない)。
        """
        unit = self._unit_name(feature)
        remaining = self._active_features() - {feature}
        seen = self.systemd.show(unit, "LoadState", "ActiveState")
        wants = self.systemd.show(self.paths.afx_unit, "Wants")["Wants"].split()
        if seen["ActiveState"] not in {"inactive", "failed"}:
            # 動いている unit は uninstall では止まらない。stop からやり直す。
            return _ENABLE_TERMINAL
        if unit in wants:
            return "stopping-ops"
        if remaining:
            # 他の機能が残る間は template が残るので、instance は loaded に見えてよい。
            done = units_match(self.paths, remaining)
        else:
            done = (not self.paths.template_path.exists() and not self.paths.dropin_path.exists()
                    and seen["LoadState"] == "not-found")
        return None if done else "stopping-ops"

    def status_line(self, feature: str) -> str:
        result = self.status(feature)
        saved = read_stage(self.root, feature, missing_ok=True)
        failure = ""
        if saved and saved.get("failure"):
            detail = f"; {saved['failure_detail']}" if saved.get("failure_detail") else ""
            failure = f" ({saved['failure']}{detail})"
        try:
            unit = self.systemd.show(self._unit_name(feature), "ActiveState", "Result")
            observed = f"unit={unit['ActiveState'] or 'unknown'} result={unit['Result'] or 'unknown'}"
        except SystemdError:
            observed = "unit=unknown result=unknown"
        marker = read_marker(self.root)
        left = ""
        if marker is not None:
            # crash で残った marker は afx の起動を止め続ける。同じ enable の再実行で回収する。
            owner = marker.get("feature")
            owner = owner if owner in _FEATURE_PRINCIPALS else "unknown"
            since = marker.get("started_at")
            since = since if isinstance(since, str) and since else "unknown"
            left = (f" marker={ENROLL_IN_PROGRESS} feature={owner} since={since}"
                    f" path={enroll_marker_path(self.root)}")
        waiting = ""
        if saved and saved.get("operation") == "enable" and not self._afx_was_running(saved):
            waiting = f" afx_before_enable={saved.get('afx_before_stop') or 'unknown'} ops=starts_with_afx"
        warn = f" warn={saved['warn']}" if saved and saved.get("warn") else ""
        return f"{feature}: {result.operation} {result.stage}{failure} {observed}{waiting}{warn}{left}"

    def status(self, feature: str) -> StageResult:
        if feature not in _FEATURE_PRINCIPALS:
            raise ValueError("unknown_feature")
        saved = read_stage(self.root, feature, missing_ok=True)
        if saved is None:
            return StageResult(feature, "disable", _DISABLE_TERMINAL)
        return StageResult(feature, str(saved["operation"]), str(saved["stage"]))


def run_ops_command(root: Path, action: str, feature: str) -> int:
    """CLI 用の小さな表示層。状態の正本は stage file のままにする。"""
    setup = OpsSetup(root)
    try:
        result = getattr(setup, action)(feature)
        line = setup.status_line(feature) if action == "status" else None
    except (InvalidState, SystemdError, KeySetError, OSError, ValueError) as error:
        reason = getattr(error, "reason", None) or getattr(error, "code", None) or str(error)
        detail = getattr(error, "detail", "")
        print(f"afx ops: {reason}" + (f" ({detail})" if detail else ""), file=sys.stderr)
        return 1
    if action == "status":
        print(line)
    else:
        print(f"{feature}: {result.stage}")
    return 0
