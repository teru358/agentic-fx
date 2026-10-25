"""`afx ops web reconfigure` (別名 `reconf`): 有効化済みの web の設定を変える。

afx の unit には stop / start / restart / ensure_active を送らず、daemon-reload もしない。
鍵 dir・enable の stage file・enroll marker・instance flock には触れない。ops-web の unit に
stop → (設定の書換えと probe) → stop → reset-failed → start を送るだけで、durable な段は作らない。
途中で止まったときは同じ reconfigure の再実行で完結する。

入口の照合は読み取りだけで、どれも失敗しても何も書かず、stage にも failure を記録しない。
"""
from __future__ import annotations

import fcntl
import secrets
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from agentic_fx.ops.contracts import Principal
from agentic_fx.ops.keys import KeySetError, enrolled_key_matches, key_dir
from agentic_fx.ops_setup import interfaces as interface_listing
from agentic_fx.ops_setup.feature_config import web_defaults, web_secret_state
from agentic_fx.ops_setup.stages import (InvalidState, OpsSetup, _ENABLE_TERMINAL, _FEATURE_PRINCIPALS,
                                         _KEY_MISMATCH_CODES, read_marker, read_stage)
from agentic_fx.ops_setup.systemd import SystemdError
from agentic_fx.ops_setup.units import OpsExecNotFound, resolve_ops_exec, units_match
from agentic_fx.ops_setup.web_setup import (SECRET_GUIDANCE_TEMPLATE, WebOptions, WebSetupError, cleanup_temporaries,
                                            generated_password_line, options_problem, plan_for_input, read_config_file, read_password_file,
                                            target_from_config_file)
from agentic_fx.ops_setup.web_wizard import FULL, Session

FEATURE = "web"
RERUN = "afx ops web reconfigure を再実行する"
RECOVER = "afx ops web enable で状態を回復してから再実行する"
RELOAD = "systemctl --user daemon-reload の後に再実行する"
KEYS = "afx を止めて afx keys rotate ops-web を実行し、afx を起動してから afx ops web reconfigure を再実行する"
SECRET_GUIDANCE = SECRET_GUIDANCE_TEMPLATE.format(verb="reconfigure")
_AFX_RUNNING = frozenset({"active", "activating", "reloading"})
_AFX_DOWN = frozenset({"inactive", "failed"})


class Refusal(Exception):
    """固定 reason と案内で止まる。rc は既定 1。"""

    def __init__(self, reason: str, guidance: str = "", rc: int = 1) -> None:
        super().__init__(reason)
        self.reason, self.guidance, self.rc = reason, guidance, rc


def _unit(setup: OpsSetup) -> str:
    return setup._unit_name(FEATURE)


def _check_stage(root: Path) -> None:
    saved = read_stage(root, FEATURE, missing_ok=True)
    if saved is None or (saved["operation"] == "disable" and saved["stage"] == "disabled"):
        raise Refusal("not_enabled")
    if not (saved["operation"] == "enable" and saved["stage"] == _ENABLE_TERMINAL):
        raise Refusal("invalid_state")


def _check_others(setup: OpsSetup) -> None:
    """他 feature の未終端操作と enroll marker。enable と同じ規則で、書かず回収もしない。"""
    for other in _FEATURE_PRINCIPALS:
        if other == FEATURE:
            continue
        saved = read_stage(setup.root, other, missing_ok=True)
        if saved is None:
            continue
        terminal = "enabled" if saved["operation"] == "enable" else "disabled"
        if saved["stage"] != terminal:
            raise Refusal("invalid_state",
                          f"afx ops {other} {saved['operation']} を再実行して完了させてから再実行する")
    try:
        setup._check_marker_owner(FEATURE)
    except InvalidState as error:
        raise Refusal(error.reason, error.detail) from None
    marker = read_marker(setup.root)
    if marker is not None:
        raise Refusal("invalid_state", RECOVER)


def _check_principal(setup: OpsSetup) -> None:
    try:
        matches = enrolled_key_matches(key_dir(setup.root), Principal.OPS_WEB)
    except KeySetError as error:
        if error.code in _KEY_MISMATCH_CODES:
            raise Refusal("principal_key_mismatch", KEYS) from None
        raise Refusal("invalid_state") from None
    except OSError:
        raise Refusal("invalid_state") from None
    if not matches:
        raise Refusal("invalid_state")


def _check_units(setup: OpsSetup) -> None:
    """unit 資材・読込み・Wants の照合。manager は読むだけで、daemon-reload は呼ばない。"""
    try:
        if not units_match(setup.paths, setup._active_features() | {FEATURE}):
            raise Refusal("invalid_state", RECOVER)
    except OpsExecNotFound:
        raise Refusal("ops_exec_not_found") from None
    unit, afx = _unit(setup), setup.paths.afx_unit
    reload_state = (setup.systemd.show(afx, "NeedDaemonReload")["NeedDaemonReload"],
                    setup.systemd.show(unit, "NeedDaemonReload")["NeedDaemonReload"])
    if any(value != "no" for value in reload_state):
        raise Refusal("invalid_state", RELOAD)
    if setup.systemd.show(unit, "LoadState")["LoadState"] != "loaded":
        raise Refusal("invalid_state", RECOVER)
    if unit not in setup.systemd.show(afx, "Wants")["Wants"].split():
        raise Refusal("invalid_state", RECOVER)


def _report(refusal: Refusal, *, rerun: bool) -> None:
    guidance = refusal.guidance or (RERUN if rerun else "")
    print(f"afx ops: {refusal.reason}" + (f" ({guidance})" if guidance else ""), file=sys.stderr)


def run_ops_reconfigure(root: Path, feature: str, *, options: WebOptions | None = None, io=None,
                        setup_factory: Callable[[Path], OpsSetup] = OpsSetup,
                        probe_runner: Callable[[str], tuple[int, str]] | None = None,
                        candidates: Callable[[], Sequence[interface_listing.Candidate]] | None = None,
                        hook: Callable[[str], None] | None = None,
                        token_bytes: Callable[[int], bytes] = secrets.token_bytes) -> int:
    """reconfigure の入口。rc を返す (0 成功、1 拒否・失敗、2 入力不足)。"""
    from agentic_fx.ops_setup.interactive import (ConsoleIO, _default_probe, _probe_reason_code,
                                                  _setup_lock, default_candidates)
    root = Path(root).resolve()
    if feature != FEATURE:
        print("afx ops: unknown_feature", file=sys.stderr)
        return 2
    options = options if options is not None else WebOptions()
    io = io if io is not None else ConsoleIO()
    emit = hook if hook is not None else (lambda _name: None)
    problem = options_problem(options, reconfigure=True)
    if problem:
        print(f"afx ops: {problem}", file=sys.stderr)
        return 2
    lock = _setup_lock(root)
    if isinstance(lock, str):
        print(f"afx ops: {lock}", file=sys.stderr)
        return 1
    try:
        setup = setup_factory(root)
        try:
            _check_stage(root)
            _check_others(setup)
            try:
                setup.paths = resolve_ops_exec(setup.paths)
            except OpsExecNotFound:
                raise Refusal("ops_exec_not_found") from None
            _check_principal(setup)
            _check_units(setup)
            if options.flagless and not io.isatty():
                raise Refusal("setup_input_required", rc=2)
        except Refusal as refusal:
            _report(refusal, rerun=False)
            return refusal.rc
        except (InvalidState, SystemdError) as error:
            print(f"afx ops: {error.reason}", file=sys.stderr)
            return 1
        unit = _unit(setup)
        if probe_runner is not None:
            probe_one = probe_runner
        else:
            def probe_one(feature: str) -> tuple[int, str]:
                return _default_probe(feature, root)
        interactive = options.flagless

        def ensure_stopped() -> None:
            if not setup.systemd.is_stopped(unit):
                setup.systemd.stop(unit)
                emit("ops_restopped")
                if not setup.systemd.is_stopped(unit):
                    raise Refusal("ops_still_active")

        session = Session(root, io, lambda: probe_one(FEATURE),
                          candidates if candidates is not None else default_candidates(root),
                          hook=hook, token_bytes=token_bytes, before_probe=ensure_stopped,
                          echo_each=interactive)
        try:
            cleanup_temporaries(root, hook=hook)
            if interactive:
                secret_state = web_secret_state(root)
                if secret_state == "unsafe":
                    raise WebSetupError("secret_invalid")
                values, plan = session.collect(web_defaults(root), secret_state, FULL)
            else:
                values = target_from_config_file(read_config_file(options.config_file or ""))
                password = read_password_file(options.password_file) if options.password_file else None
                plan = plan_for_input(values, web_secret_state(root), options.secret_action or "", password)
        except WebSetupError as error:
            guidance = SECRET_GUIDANCE if error.reason == "secret_invalid" else ""
            _report(Refusal(error.reason, guidance), rerun=False)
            return error.rc
        except OSError:
            _report(Refusal("material_write_failed"), rerun=False)
            return 1
        except (EOFError, KeyboardInterrupt):
            print("afx ops: setup_aborted", file=sys.stderr)
            return 1
        try:
            setup.systemd.stop(unit)
            if not setup.systemd.is_stopped(unit):
                raise Refusal("ops_still_active")
            emit("ops_stopped")

            def show_password(password: str, user: str) -> None:
                io.stdout(generated_password_line(user, password))

            failure = session.converge(values, plan, interactive=interactive, show_password=show_password,
                                       reason_code=_probe_reason_code)
            if failure is not None:
                raise Refusal(failure[1])
            emit("ops_probed")
            return _restart(setup, unit, io, emit)
        except Refusal as refusal:
            _report(refusal, rerun=True)
            return refusal.rc
        except WebSetupError as error:
            _report(Refusal(error.reason, SECRET_GUIDANCE if error.reason == "secret_invalid" else RERUN),
                    rerun=True)
            return error.rc
        except SystemdError as error:
            _report(Refusal(error.reason), rerun=True)
            return 1
        except OSError:
            _report(Refusal("material_write_failed"), rerun=True)
            return 1
        except (EOFError, KeyboardInterrupt):
            print("afx ops: setup_aborted", file=sys.stderr)
            return 1
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _restart(setup: OpsSetup, unit: str, io, emit: Callable[[str], None]) -> int:
    """手順 (4): afx が起動中か起動途中のときだけ ops-web を stop → reset-failed → start し、READY を確かめる。"""
    afx = setup.paths.afx_unit
    state = setup.systemd.show(afx, "ActiveState")["ActiveState"]
    if state not in _AFX_RUNNING:
        io.stdout("web: reconfigured ops=starts_with_afx")
        return 0
    setup.systemd.stop(unit)
    if not setup.systemd.is_stopped(unit):
        raise Refusal("ops_still_active")
    emit("ops_restopped")
    setup.systemd.reset_failed(unit)
    emit("ops_reset_failed")
    try:
        setup.systemd.start(unit)
    except SystemdError as error:
        try:
            again = setup.systemd.show(afx, "ActiveState")["ActiveState"]
        except SystemdError:
            raise error from None
        if again in _AFX_DOWN:
            io.stdout("web: reconfigured ops=starts_with_afx")
            return 0
        raise
    emit("ops_started")
    if not setup.systemd.is_active(unit) or not setup.ready(FEATURE):
        raise Refusal("ops_not_ready")
    io.stdout("web: reconfigured")
    return 0
