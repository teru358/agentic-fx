"""`afx ops web enable --write` / `--no-write`: web の書込みの有効化と無効化。

書込みを有効にした web は `ops-web-write` の鍵だけを読み、無効の web は `ops-web` の鍵だけを読む。
切替は、目標 (write の真偽と使う principal) を web の stage file に記録してから、
afx の停止 → 鍵 → ops.toml の `write` → 起動し直し、の順に進める。各段の後で止まっても、
同じコマンドの再実行が記録した段から続けて目標へ収束する (別の目標で再実行すれば目標を差し替える)。

構成の検査 (loopback と `auth = basic`) は鍵に触れる前に行う。起動前に afx が止まっていたなら、
afx は起動しない (web は afx の次の起動で一緒に起きる)。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import (KeySetError, enroll, enrolled_key_matches, key_dir, principal_entry,
                                 revoke, rotate)
from agentic_fx.ops_setup.feature_config import (_WEB_ACCESS, is_loopback, web_access_check, web_live_config,
                                                 write_web_config)
from agentic_fx.ops_setup.stages import (_AFX_RUNNING, _ENABLE_TERMINAL, _KEY_MISMATCH_CODES, _WRITE_PRINCIPALS,
                                         InvalidState, OpsSetup, StageResult, _remove_marker, _describe,
                                         _write_stage, read_marker, read_stage)
from agentic_fx.ops_setup.systemd import SystemdError
from agentic_fx.ops_setup.write_key import (CONFIG_PATH, RECOVERY, Consistent, Inconsistent,
                                            check_write_key_consistency, stage_mode)

WRITE_PRINCIPAL = Principal.OPS_WEB_WRITE
READ_PRINCIPAL = Principal.OPS_WEB
NOT_ENABLED_GUIDANCE = "afx ops web enable で web を有効にしてから実行する"
CONFIG_GUIDANCE = ("loopback の bind と Basic 認証にする: afx ops web reconfigure --non-interactive "
                   "--config-file <port と auth = \"basic\" だけの TOML> --secret-action install "
                   "--password-file <file>")
UNREADABLE_GUIDANCE = f"ops.toml が構文か schema の検査を通らない。対象は {CONFIG_PATH}。{RECOVERY}"
_MAX_KEY_PASSES = 4
_AFX_DOWN = frozenset({"inactive", "failed"})


def _mismatch(setup: OpsSetup, state: dict[str, Any], principal: Principal, code: str) -> InvalidState:
    detail = (f"code={code} 回復: afx を止めて afx keys rotate {principal.value} を実行し、"
              "afx ops web enable --write (又は --no-write) を再実行する")
    setup._failure(state, "principal_key_mismatch", detail)
    return InvalidState("principal_key_mismatch", detail)


def _active_and_matching(setup: OpsSetup, state: dict[str, Any], principal: Principal) -> bool:
    """manifest が active で鍵 file が一致すれば True。active なのに一致しなければ止める。"""
    try:
        return enrolled_key_matches(key_dir(setup.root), principal)
    except KeySetError as error:
        if error.code in _KEY_MISMATCH_CODES:
            raise _mismatch(setup, state, principal, error.code) from None
        raise


def _complete(setup: OpsSetup, principal: Principal, op: str) -> None:
    directory = key_dir(setup.root)
    {"enroll": enroll, "rotate": rotate, "revoke": revoke}[op](directory, principal)


def align_keys(setup: OpsSetup, state: dict[str, Any], target: bool, *, require_read: bool = True) -> None:
    """鍵を目標に合わせる。どの途中状態からでも収束する (停止中・instance lock 保持中に呼ぶ)。

    有効: `ops-web-write` が未登録なら enroll、active なら照合、revoked なら rotate。
    無効: `ops-web` が active であることを確かめてから `ops-web-write` を revoke する (未登録は何もしない)。
    """
    directory = key_dir(setup.root)
    if not target and require_read:
        if not _active_and_matching(setup, state, READ_PRINCIPAL):
            setup._failure(state, "principal_key_mismatch", f"{READ_PRINCIPAL.value} の鍵が active でない")
            raise InvalidState("principal_key_mismatch",
                               f"{READ_PRINCIPAL.value} の鍵が active でない。afx keys enroll / rotate "
                               f"{READ_PRINCIPAL.value} で直してから再実行する")
    for _ in range(_MAX_KEY_PASSES):
        entry = principal_entry(directory, WRITE_PRINCIPAL)
        if entry is None:
            if not target:
                return
            enroll(directory, WRITE_PRINCIPAL, hook=setup.hook)
            continue
        state_name, op = entry
        if state_name == "preparing":
            _complete(setup, WRITE_PRINCIPAL, str(op))
        elif target and state_name == "revoked":
            rotate(directory, WRITE_PRINCIPAL, hook=setup.hook)
        elif target:
            if _active_and_matching(setup, state, WRITE_PRINCIPAL):
                return
            raise InvalidState("invalid_state")
        elif state_name == "active":
            revoke(directory, WRITE_PRINCIPAL, hook=setup.hook)
        else:
            return
    raise InvalidState("invalid_state")


def target_problem(root, live: dict[str, Any]) -> str | None:
    """`--write` へ進めない構成の固定 reason。起動時の検査と同じ順 (auth → loopback → その他)。"""
    if live.get("auth", "none") != "basic":
        return "write_requires_auth"
    if not is_loopback(live):
        return "write_requires_loopback"
    return web_access_check(root).reason


def _check_target(root, live: dict[str, Any]) -> None:
    problem = target_problem(root, live)
    if problem is not None:
        raise InvalidState(problem, CONFIG_GUIDANCE if problem.startswith("write_requires") else "")


def _recover_idle(setup: OpsSetup, saved: dict[str, Any] | None, target: bool,
                  confirm: Callable[[], bool] | None) -> StageResult:
    """有効でない web (一度も enable していない・disabled) の write と鍵の不整合を、停止と lock の下で整合させる。

    stage file は作らない。途中で止まっても鍵・設定の操作は冪等なので、同じコマンドの再実行で収束する。
    整合しているなら何もせず not_enabled。afx が動いていたなら最後に afx を起こす (web は有効でないので起きない)。
    """
    root = setup.root
    setup._unfinished("web", "enable", switching_write=True)
    setup._check_marker_owner("web")
    verdict = check_write_key_consistency(root, mode="idle")
    if isinstance(verdict, Consistent):
        raise InvalidState("not_enabled", NOT_ENABLED_GUIDANCE)
    live = web_live_config(root)
    if isinstance(live, str):
        if target or live != "config_dir_missing":
            raise InvalidState(live, UNREADABLE_GUIDANCE)
        live = None
    if target:
        _check_target(root, live)
        if verdict.reason == "principal_key_mismatch":
            raise InvalidState(verdict.reason, verdict.guidance)
    afx_now = setup.systemd.show(setup.paths.afx_unit, "ActiveState")["ActiveState"]
    if afx_now in _AFX_RUNNING and confirm is not None and not confirm():
        raise InvalidState("setup_aborted")
    state: dict[str, Any] = {"feature": "web", "operation": "disable", "stage": "disabled",
                             "afx_before_stop": afx_now, "unrecorded": True}
    lock = None
    try:
        setup._check_key_dir()
        lock = setup._quiesce_afx(state, reset_stage=False)
        setup._before("keys")
        align_keys(setup, state, target, require_read=saved is not None)
        setup._after("keys")
        if live is not None:
            setup._before("config")
            values = {name: live[name] for name in ("port", *_WEB_ACCESS) if name in live and name != "write"}
            if target:
                values["write"] = True
            write_web_config(root, values)
            setup._after("config")
        lock.close()
        lock = None
        _unmark(setup, state)
        if afx_now in _AFX_RUNNING:
            setup.systemd.start(setup.paths.afx_unit)
        return StageResult("web", "recover", "disabled")
    finally:
        if lock is not None:
            lock.close()


def _set_step(setup: OpsSetup, state: dict[str, Any], step: str) -> None:
    setup.hook(f"before_stage_write_{step}")
    state["write_step"] = step
    state.pop("failure", None)
    state.pop("failure_detail", None)
    _write_stage(setup.root, state)
    setup.hook(f"after_stage_write_{step}")


def switch_write(setup: OpsSetup, target: bool, *, confirm: Callable[[], bool] | None = None) -> StageResult:
    """書込みを目標 (`target`) の状態にする。既にその状態で途中の記録も無ければ何もしない。

    `confirm` は afx が動いているときだけ、構成の検査の後・何かを書く前に呼ぶ (偽なら止まる)。
    """
    return setup._guarded(lambda: _switch(setup, target, confirm))


def _switch(setup: OpsSetup, target: bool, confirm: Callable[[], bool] | None) -> StageResult:
    root = setup.root
    done = StageResult("web", "enable", _ENABLE_TERMINAL)
    saved = read_stage(root, "web", missing_ok=True)
    if stage_mode(saved) == "idle":
        return _recover_idle(setup, saved, target, confirm)
    if not (saved["operation"] == "enable" and saved["stage"] == _ENABLE_TERMINAL):
        raise InvalidState("invalid_state")
    setup._unfinished("web", "enable", switching_write=True)
    setup._check_marker_owner("web")
    live = web_live_config(root)
    if isinstance(live, str):
        raise InvalidState(live, UNREADABLE_GUIDANCE)
    pending = "write_target" in saved
    if target:
        _check_target(root, live)
    verdict = check_write_key_consistency(root, mode="enabled")
    if target and isinstance(verdict, Inconsistent) and verdict.reason == "principal_key_mismatch":
        raise InvalidState(verdict.reason, verdict.guidance)
    if not pending and verdict == Consistent(target):
        return done
    state = saved
    setup._current = state
    afx_now = setup.systemd.show(setup.paths.afx_unit, "ActiveState")["ActiveState"]
    if afx_now in _AFX_RUNNING and confirm is not None and not confirm():
        raise InvalidState("setup_aborted")
    if not pending:
        state["afx_before_stop"] = afx_now
    if not pending or state["write_target"] != target:
        state.update(write_target=target, write_principal=_WRITE_PRINCIPALS[target], write_step="staged")
        state.pop("failure", None)
        state.pop("failure_detail", None)
        _write_stage(root, state)
    if state["write_step"] == "configured" and not _settled(setup, state, target):
        _set_step(setup, state, "stopped")
    lock = None
    try:
        if state["write_step"] in ("staged", "stopped", "keyed"):
            setup._check_key_dir()
            lock = setup._quiesce_afx(state, reset_stage=False)
            if state["write_step"] == "staged":
                _set_step(setup, state, "stopped")
            setup._before("keys")
            align_keys(setup, state, target)
            setup._after("keys")
            if state["write_step"] == "stopped":
                _set_step(setup, state, "keyed")
            setup._before("config")
            values = {name: live[name] for name in ("port", *_WEB_ACCESS) if name in live and name != "write"}
            if target:
                values["write"] = True
            write_web_config(root, values)
            setup._after("config")
            _set_step(setup, state, "configured")
            lock.close()
            lock = None
        if state["write_step"] == "configured":
            _unmark(setup, state)
            _restart(setup, state)
            for key in ("write_target", "write_principal", "write_step"):
                state.pop(key)
            state.pop("failure", None)
            state.pop("failure_detail", None)
            _write_stage(root, state)
        return done
    except KeySetError as error:
        setup._failure(state, error.code)
        raise
    finally:
        if lock is not None:
            lock.close()


def _settled(setup: OpsSetup, state: dict[str, Any], target: bool) -> bool:
    """`configured` の記録を実物と照合する。afx の停止 (marker が残る間)・鍵・設定のどれかが崩れていれば偽。"""
    live = web_live_config(setup.root)
    if isinstance(live, str) or bool(live.get("write", False)) != target:
        return False
    if read_marker(setup.root) is not None:
        seen = setup.systemd.show(setup.paths.afx_unit, "ActiveState")["ActiveState"]
        if seen not in _AFX_DOWN:
            return False
    directory = key_dir(setup.root)
    try:
        if not enrolled_key_matches(directory, READ_PRINCIPAL):
            return False
        entry = principal_entry(directory, WRITE_PRINCIPAL)
        if target:
            return entry is not None and entry[0] == "active" and enrolled_key_matches(directory, WRITE_PRINCIPAL)
    except KeySetError:
        return False
    return entry is None or entry[0] == "revoked"


def _unmark(setup: OpsSetup, state: dict[str, Any]) -> None:
    if read_marker(setup.root) is None:
        return
    setup._before("unmark")
    try:
        _remove_marker(setup.root)
    except OSError as error:
        detail = _describe(error)
        setup._failure(state, "marker_remove_failed", detail)
        raise SystemdError("marker_remove_failed", detail) from error
    setup._after("unmark")


def _restart(setup: OpsSetup, state: dict[str, Any]) -> None:
    """afx が元々動いていた (今動いている) ときは afx を起こし、web を止め直してから起動して READY を確かめる。

    afx が止まっていて元々も止まっていたなら何も起こさない (web は afx の次の起動で一緒に起きる)。
    """
    unit = setup._unit_name("web")
    now = setup.systemd.show(setup.paths.afx_unit, "ActiveState")["ActiveState"]
    if not setup._afx_was_running(state) and now not in _AFX_RUNNING:
        state["ops_active"] = False
        return
    try:
        setup._before("start")
        if not setup.systemd.is_stopped(unit):
            setup.systemd.stop(unit)
            if not setup.systemd.is_stopped(unit):
                raise SystemdError("ops_still_active")
        setup.systemd.reset_failed(unit)
        setup.systemd.start(setup.paths.afx_unit)
        setup.systemd.ensure_active(unit)
        setup._after("start")
        setup._before("check")
        if not setup.systemd.is_active(unit) or not setup.ready("web"):
            raise SystemdError("ops_not_ready")
        setup._after("check")
    except SystemdError as error:
        setup._failure(state, error.reason, error.detail)
        setup._stop_afx_after_failure(state, error)
        raise
    state["ops_active"] = True
