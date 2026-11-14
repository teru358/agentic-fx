"""非配備の旧承認版を、配備せずに現在の条件で 1 回評価する (人間専用の一回限りの入口)。

入口は `audit_non_deployed_version`。保存版 (`plugins/.versions/<name>/<artifact_hash>/`) を
lock の下で symlink を追わずに検査して専用領域へ複製し、共有 gate (`switch._run_full_gate`) を
`floor_mode=warn` / 比較なしで 1 回だけ通す。成績は DB の成績表へ書かず、人間専用の report
(`<report_dir>/<試行 id>.json`) にだけ残す。approval・staging・archive・live・deploy op は
変更しない。改善 loop・agent・daemon からは呼ばない。

report は 4 軸 (実行完了 / 床合否 / 履歴検証 / 未実行 scope) を別々に持つ。warn の完走は
合格ではなく、評価できなかったものは成功に数えない。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agentic_fx.backtest import history_evidence
from agentic_fx.backtest import history_health as hh
from agentic_fx.backtest.history_migration import UNEVALUABLE_REASONS_V2
from agentic_fx.core.history_epoch import history_epoch
from agentic_fx.plugin import loader, strategy_gate, switch
from agentic_fx.plugin.gate_pytest import (
    CandidateSnapshotError, _is_ignored_entry, check_candidate_snapshot, hashes_of)
from agentic_fx.plugin.loader import _MAX_FILE_BYTES, REQUIRED_FILES
from agentic_fx.plugin.resolve import IndicatorResolutionError, resolve_indicator_deps
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import backtest_runs as backtest_runs_store
from agentic_fx.tools import plugin_loader as tools_plugin_loader

REPORT_FORMAT = 1
LOCK_WAIT_SEC = 30.0
_HEX64 = re.compile(r"[0-9a-f]{64}")
_UTC = timezone.utc

# 総合結果。
PASS, FAIL, UNEVALUABLE = "pass", "fail", "unevaluable"
# 実行完了 (軸 1)。
EXECUTION_STATUSES = ("not_started", "running", "completed", "interrupted")
# 床合否 (軸 2) の scope 別。
FLOOR_PASS, FLOOR_FAIL, FLOOR_INDETERMINATE, FLOOR_NOT_RUN = (
    "pass", "fail", "indeterminate", "not_run")
SCOPES = ("in_sample", "holdout")
_ROW_SCOPE = {"in_sample": "in_sample", "holdout_gate": "holdout"}
_METRIC_KEYS = ("trades", "pf", "win_rate", "avg_r", "max_drawdown", "evaluable", "eval_bars")


class VersionCopyError(Exception):
    """保存版のコピーを拒否した。`code` は固定の評価不能理由。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AuditTarget:
    """評価する旧承認 1 件。値は manifest / DB の完全長 hash と旧承認 id。"""
    old_approval_id: int
    name: str
    artifact_hash: str
    content_hash: str
    dependency_pins: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AuditResult:
    report: dict
    report_path: Path | None
    write_error: str | None = None


# ---- 保存版の安全なコピー -----------------------------------------------------------

def _open_dir(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise VersionCopyError("artifact_missing", "version directory") from exc
    except OSError as exc:
        raise VersionCopyError("invalid_snapshot", "version directory is not a plain directory") from exc


def _identity(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino)


def _read_checked(dir_fd: int, name: str, pre: os.stat_result) -> bytes:
    """検査済みの元を、同じ fd から上限つきで読む。パスを開き直さない。"""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise VersionCopyError("changed_during_evaluation", f"{name} could not be opened") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise VersionCopyError("changed_during_evaluation", f"{name} is no longer a regular file")
        if st.st_nlink != 1 or st.st_size > _MAX_FILE_BYTES:
            raise VersionCopyError("changed_during_evaluation", f"{name} attributes changed")
        if _identity(st) != _identity(pre) or st.st_size != pre.st_size:
            raise VersionCopyError("changed_during_evaluation", f"{name} was replaced")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_FILE_BYTES:
                raise VersionCopyError("changed_during_evaluation", f"{name} grew past the limit")
        after = os.fstat(fd)
        if (total != pre.st_size or after.st_size != pre.st_size
                or after.st_mtime_ns != st.st_mtime_ns or _identity(after) != _identity(pre)):
            raise VersionCopyError("changed_during_evaluation", f"{name} changed while read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def copy_exact_version(plugins_root: Path, name: str, artifact_hash: str, dest: Path, *,
                       content_hash: str, between_checks_and_reads: Callable[[], None] | None = None
                       ) -> None:
    """正規の保存版を `dest` (存在しない dir) へ bytes コピーし、両 hash を照合する。

    呼び出し側が plugin lock を持った状態で呼ぶ。元は dirfd で固定した正規 dir の 3 ファイルを
    `stat(follow_symlinks=False)` で全部検査 (通常ファイル・リンク数 1・サイズ上限) してから、
    検査した fd から読む。コピー先は排他的に新規作成し、既存の snapshot 検査と
    `hashes_of` の hash 照合を通す。失敗では `dest` を片付けて `VersionCopyError`。
    `between_checks_and_reads` は検査と読取りの間に呼ばれる (差替えの test 用の差込口)。"""
    if not loader.is_canonical_plugin_name(name) or not _HEX64.fullmatch(artifact_hash):
        raise VersionCopyError("invalid_snapshot", "not a canonical version locator")
    fds: list[int] = []
    try:
        try:
            current = os.open(str(plugins_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise VersionCopyError("artifact_missing", "plugins root") from exc
        fds.append(current)
        for component in (".versions", name, artifact_hash):
            current = _open_dir(current, component)
            fds.append(current)
        dir_fd = current
        names = os.listdir(dir_fd)
        unexpected = sorted(n for n in names
                            if n not in REQUIRED_FILES and not _is_ignored_entry(n, dir_fd))
        if unexpected:
            raise VersionCopyError("invalid_snapshot", "unexpected entries in the version")
        pre: dict[str, os.stat_result] = {}
        for filename in REQUIRED_FILES:
            try:
                st = os.stat(filename, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise VersionCopyError("artifact_missing", f"{filename} is missing") from exc
            except OSError as exc:
                raise VersionCopyError("invalid_snapshot", f"{filename} cannot be inspected") from exc
            if not stat.S_ISREG(st.st_mode):
                raise VersionCopyError("invalid_snapshot", f"{filename} is not a regular file")
            if st.st_nlink != 1:
                raise VersionCopyError("invalid_snapshot", f"{filename} has extra links")
            if st.st_size > _MAX_FILE_BYTES:
                raise VersionCopyError("invalid_snapshot", f"{filename} exceeds the size limit")
            pre[filename] = st
        if between_checks_and_reads is not None:
            between_checks_and_reads()
        data = {filename: _read_checked(dir_fd, filename, pre[filename])
                for filename in REQUIRED_FILES}
    finally:
        for fd in reversed(fds):
            os.close(fd)

    try:
        os.mkdir(dest, 0o700)
    except OSError as exc:
        raise VersionCopyError("resource_unavailable", "the copy area cannot be created") from exc
    try:
        for filename in REQUIRED_FILES:
            out = os.open(dest / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(out, data[filename])
                os.fsync(out)
            finally:
                os.close(out)
        try:
            check_candidate_snapshot(dest)
        except CandidateSnapshotError as exc:
            raise VersionCopyError("invalid_snapshot", "the copy failed the snapshot check") from exc
        if hashes_of(dest) != (content_hash, artifact_hash):
            raise VersionCopyError("hash_mismatch", "the copy does not match the recorded hashes")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise


# ---- 失敗の収集 ---------------------------------------------------------------------

class FailureCollector:
    """共有 gate の失敗の記録先。DB へは書かず、report の材料としてだけ保持する。"""

    def __init__(self) -> None:
        self.failures: list[switch.GateFailure] = []

    def __call__(self, failure: switch.GateFailure) -> None:
        self.failures.append(failure)


def classify_failure(failure: "switch.GateFailure") -> tuple[str, str]:
    """(総合結果, 固定理由)。構造化された段階・判別子だけで決め、文言は見ない。"""
    stage, detail, kind = failure.stage, failure.detail, failure.verdict_kind
    if stage == "pytest":
        if detail == "pytest":
            return (UNEVALUABLE, "timeout") if failure.returncode == -1 else (FAIL, "pytest")
        if failure.error_type == "RuntimeError":
            return UNEVALUABLE, "sandbox_unavailable"
        if failure.error_type in ("OSError", "MemoryError"):
            return UNEVALUABLE, "resource_unavailable"
        return UNEVALUABLE, "failed"
    if stage == "snapshot":
        if failure.error_type == "CandidateSnapshotError":
            return UNEVALUABLE, "invalid_snapshot"
        return UNEVALUABLE, "failed"
    if stage in ("source", "discovery", "contract", "indicator_static"):
        return UNEVALUABLE, "unsupported_version"
    if stage == "hash":
        return UNEVALUABLE, "changed_during_evaluation"
    if stage == "kind_gate":
        if detail == "db_busy":
            return UNEVALUABLE, "db_busy"
        if detail == "resource":
            return UNEVALUABLE, "resource_unavailable"
        if detail == "no_history":
            return UNEVALUABLE, hh.HISTORY_UNAVAILABLE
        if detail == "sandbox_unavailable":
            return UNEVALUABLE, "sandbox_unavailable"
        if detail == "timeout":
            return UNEVALUABLE, "timeout"
        if failure.error_type == "SandboxError":
            return UNEVALUABLE, "worker_failed"
        return UNEVALUABLE, "failed"
    if stage == "verdict":
        if kind == "indicator_unresolved":
            return UNEVALUABLE, "dependency_changed"
        if kind in ("insufficient_trades", "reference_history_missing",
                    "history_source_unconfigured", hh.HISTORY_STALE, hh.HISTORY_GAP,
                    hh.HISTORY_UNAVAILABLE):
            return UNEVALUABLE, kind
    return UNEVALUABLE, "failed"


# ---- 成績の整理 (許可した項目だけ) ----------------------------------------------------

def _number(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return None


def _metrics_entry(row: dict) -> dict:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    entry = {key: _number(metrics.get(key)) for key in _METRIC_KEYS}
    period = row.get("period")
    if isinstance(period, (tuple, list)) and len(period) == 2:
        entry["period"] = [p.isoformat() if isinstance(p, datetime) else None for p in period]
    return entry


def collect_scope_rows(rows) -> dict:
    """gate の行から、候補の scope 別・pair 別の測定結果を取り出す (raw 値は持ち帰らない)。"""
    out: dict[str, dict] = {scope: {} for scope in SCOPES}
    for row in rows:
        if not isinstance(row, dict) or row.get("variant", "candidate") != "candidate":
            continue
        scope = _ROW_SCOPE.get(row.get("scope"))
        pair = row.get("pair")
        if scope is not None and isinstance(pair, str):
            out[scope][pair] = {"raw": row.get("metrics"), "entry": _metrics_entry(row)}
    return out


def judge_scope(scope: str, pairs: list[str], measured: dict, *, settings) -> dict:
    """測定済みの pair へ共有の床判定を適用し、pair 別と scope 全体の合否を返す。"""
    per_pair: dict[str, dict] = {}
    for pair in pairs:
        item = measured.get(pair)
        if item is None:
            per_pair[pair] = {"result": FLOOR_NOT_RUN}
            continue
        try:
            fails = strategy_gate.floor_fail_items(
                {pair: item["raw"]}, settings=settings, scope=scope)
            result, reasons = (FLOOR_FAIL if fails else FLOOR_PASS), [f.kind for f in fails]
        except (KeyError, TypeError, ValueError, AttributeError):
            result, reasons = FLOOR_INDETERMINATE, []
        per_pair[pair] = {"result": result, "reasons": reasons, "metrics": item["entry"]}
    results = {p["result"] for p in per_pair.values()}
    if not pairs or results == {FLOOR_NOT_RUN}:
        overall = FLOOR_NOT_RUN
    elif FLOOR_FAIL in results:
        overall = FLOOR_FAIL
    elif FLOOR_NOT_RUN in results or FLOOR_INDETERMINATE in results:
        overall = FLOOR_INDETERMINATE
    else:
        overall = FLOOR_PASS
    return {"result": overall, "pairs": per_pair}


# ---- 試行 ----------------------------------------------------------------------------

# 検査を終えたうえで評価を確定できなかった理由。それ以外の評価不能 (資源・worker・DB・
# 中断・不明) は、実行が途中で止まったものとして「中断」に数える。
_COMPLETED_UNEVALUABLE = frozenset({
    "insufficient_trades", "reference_history_missing", "history_source_unconfigured",
    hh.HISTORY_STALE, hh.HISTORY_GAP, hh.HISTORY_UNAVAILABLE, "dependency_changed",
    "hash_mismatch", "artifact_missing", "invalid_snapshot", "unsupported_version",
    "changed_during_evaluation"})


def _digest(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat()


def _approval_row_state(conn: sqlite3.Connection, target: AuditTarget) -> dict:
    row = conn.execute("SELECT status, payload_json FROM approval_requests WHERE id=?",
                       (target.old_approval_id,)).fetchone()
    if row is None:
        return {"status": "missing"}
    try:
        payload = json.loads(row[1])
    except (TypeError, ValueError):
        return {"status": "unreadable"}
    if not isinstance(payload, dict):
        return {"status": "unreadable"}
    deps = payload.get("indicator_deps")
    identity_ok = ((payload.get("name"), payload.get("artifact_hash"), payload.get("content_hash"))
                   == (target.name, target.artifact_hash, target.content_hash))
    pins_ok = (deps if deps is not None else {}) == target.dependency_pins
    return {"status": "ok" if identity_ok and pins_ok else "mismatch",
            "identity_ok": identity_ok, "pins_ok": pins_ok}


def _latest_decision(conn: sqlite3.Connection, target: AuditTarget) -> dict | None:
    row = approvals_store.latest_decision_for_identity(conn, target.name, target.content_hash)
    return None if row is None else {"id": row["id"], "status": row["status"]}


def _check_dependencies(conn, plugins_root: Path, settings, meta, target: AuditTarget
                        ) -> tuple[bool, str]:
    """現在の承認済み inventory で、保存 pin と完全に同じ依存が live で承認されているか。"""
    inventory = tools_plugin_loader.approved_plugins(conn, plugins_root, settings=settings)
    try:
        resolved = resolve_indicator_deps(
            meta, inventory.inventory, settings=settings, pin_mode="require")
    except IndicatorResolutionError as exc:
        return False, f"{exc.alias}:{exc.reason}"
    pins = resolved.pin_object()
    if pins != target.dependency_pins:
        return False, "pins_differ"
    if not switch._reevaluation_pins_are_live(conn, plugins_root, {"indicator_deps": pins}):
        return False, "not_live"
    return True, ""


def _lock_names(target: AuditTarget) -> list[str]:
    names = {target.name}
    for ref in target.dependency_pins.values():
        if isinstance(ref, dict) and isinstance(ref.get("plugin"), str):
            names.add(ref["plugin"])
    return sorted(names)


def _write_report(report_dir: Path, trial_id: str, report: dict) -> Path:
    """一時ファイルへ書いて fsync し、既存を上書きしない形で公開する。"""
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = report_dir / f"{trial_id}.json"
    tmp = report_dir / f".{trial_id}.{uuid.uuid4().hex}.tmp"
    data = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(tmp, final)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    dir_fd = os.open(report_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return final


class _Trial:
    """1 回の試行の途中経過。report はこれだけから作る。"""

    def __init__(self) -> None:
        self.stage = "precheck"
        self.input_checks: dict = {}
        self.meta = None
        self.copied = False
        self.failure: "switch.GateFailure | None" = None
        self.outcome = None
        self.scope_rows: dict = {scope: {} for scope in SCOPES}
        self.overall: str | None = None
        self.reason: str | None = None
        self.interrupted = False
        self.epoch: str | None = None
        self.policy = None
        self.decision = None
        self.changed: list[str] = []

    def stop(self, overall: str, reason: str) -> None:
        self.overall, self.reason = overall, reason


def audit_non_deployed_version(
        conn: sqlite3.Connection, target: AuditTarget, *, plugins_root: Path, settings,
        report_dir: Path, work_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(_UTC),
        lock_wait_sec: float = LOCK_WAIT_SEC,
        gate_fn: Callable | None = None,
        between_checks_and_reads: Callable[[], None] | None = None) -> AuditResult:
    """`target` の保存版を現在の条件で 1 回評価し、report を書く。配備は変えない。

    `report_dir` は人間専用の領域、`work_root` はコピーを置く専用領域 (どちらも `plugins/` の
    外)。評価不能・中断・出力失敗でも、完成していない report を pass として公開しない。"""
    started = clock().astimezone(_UTC)
    trial_id = (f"{target.old_approval_id}-{started.strftime('%Y%m%dT%H%M%SZ')}"
                f"-{uuid.uuid4().hex[:8]}")
    trial = _Trial()
    copy_dir = Path(work_root) / trial_id
    collector = FailureCollector()
    pending: BaseException | None = None
    try:
        try:
            trial.epoch = history_epoch(conn)
            trial.policy = history_evidence.current_policy_key()
            _precheck_and_copy(conn, target, trial, plugins_root=plugins_root, settings=settings,
                               copy_dir=copy_dir, lock_wait_sec=lock_wait_sec,
                               between=between_checks_and_reads)
            if trial.overall is None:
                trial.stage = "gate"
                _run_gate(conn, target, trial, copy_dir, gate_fn or switch._run_full_gate,
                          collector, plugins_root, settings, started)
            if trial.copied:
                if trial.failure is None and trial.overall is None:
                    trial.stage = "recheck"
                _end_recheck(conn, target, trial, copy_dir, plugins_root, settings,
                             lock_wait_sec)
        except (KeyboardInterrupt, SystemExit) as exc:
            trial.interrupted = True
            trial.stop(UNEVALUABLE, "interrupted")
            pending = exc
        except Exception as exc:  # noqa: BLE001 - classified into a fixed reason
            detail = switch._failure_detail(exc)
            trial.stop(UNEVALUABLE, {"db_busy": "db_busy", "resource": "resource_unavailable",
                                     "no_history": hh.HISTORY_UNAVAILABLE}.get(detail, "failed"))
        report = _build_report(target, trial_id, started, clock(), trial, settings, plugins_root)
        written, error = None, None
        try:
            written = _write_report(Path(report_dir), trial_id, report)
        except Exception as exc:  # noqa: BLE001 - reported on its own, never turned into a pass
            error = type(exc).__name__
    finally:
        shutil.rmtree(copy_dir, ignore_errors=True)
    if pending is not None:
        raise pending
    return AuditResult(report, written, error)


def _precheck_and_copy(conn, target, trial, *, plugins_root, settings, copy_dir, lock_wait_sec,
                       between) -> None:
    checks = trial.input_checks
    row = _approval_row_state(conn, target)
    checks["approval_row"] = row["status"]
    trial.decision = checks["latest_decision"] = _latest_decision(conn, target)
    if row["status"] != "ok":
        trial.stop(UNEVALUABLE, "dependency_changed"
                   if row.get("identity_ok") and not row.get("pins_ok") else "hash_mismatch")
        return
    Path(copy_dir).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with switch._plugin_locks(plugins_root, _lock_names(target),
                                  deadline=time.monotonic() + lock_wait_sec, stop_event=None):
            try:
                copy_exact_version(plugins_root, target.name, target.artifact_hash, copy_dir,
                                   content_hash=target.content_hash,
                                   between_checks_and_reads=between)
            except VersionCopyError as exc:
                checks["stored_version"] = exc.code
                trial.stop(UNEVALUABLE, exc.code)
                return
            trial.copied = True
            checks["stored_version"] = "ok"
            trial.meta = loader._discover_one(copy_dir, target.name)
            if trial.meta is None:
                checks["dependencies"] = "not_checked"
                return
            ok, why = _check_dependencies(conn, plugins_root, settings, trial.meta, target)
            checks["dependencies"] = "ok" if ok else why
            if not ok:
                trial.stop(UNEVALUABLE, "dependency_changed")
    except switch.PluginBusyError:
        checks.setdefault("stored_version", "not_checked")
        trial.stop(UNEVALUABLE, "resource_unavailable")


def _run_gate(conn, target, trial, copy_dir, gate, collector, plugins_root, settings,
              started) -> None:
    try:
        _meta, _content, _artifact, outcome = gate(
            conn, copy_dir, name=target.name, settings=settings, now=started,
            floor_mode="warn", plugins_root=plugins_root, activity=None,
            failure_sink=collector)
    except Exception as exc:  # noqa: BLE001 - every gate failure is classified below
        failure = collector.failures[-1] if collector.failures else switch.GateFailure(
            stage="unexpected", error_type=type(exc).__name__,
            detail=switch._failure_detail(exc))
        trial.failure = failure
        trial.scope_rows = collect_scope_rows(failure.rows)
        trial.stop(*classify_failure(failure))
        trial.stage = "pytest" if (failure.stage, failure.detail) == ("pytest", "pytest") \
            else failure.stage
        return
    trial.outcome = outcome
    trial.scope_rows = collect_scope_rows(outcome.gate_rows)


def _end_recheck(conn, target, trial, copy_dir, plugins_root, settings, lock_wait_sec) -> None:
    """終了時に、評価の入力が開始から変わっていないことを lock の下で確かめる。"""
    changed: list[str] = []
    try:
        with switch._plugin_locks(plugins_root, _lock_names(target),
                                  deadline=time.monotonic() + lock_wait_sec, stop_event=None):
            if _approval_row_state(conn, target).get("status") != "ok":
                changed.append("approval_row")
            if _latest_decision(conn, target) != trial.decision:
                changed.append("latest_decision")
            try:
                if hashes_of(copy_dir) != (target.content_hash, target.artifact_hash):
                    changed.append("copy_hash")
            except OSError:
                changed.append("copy_hash")
            if trial.meta is not None:
                ok, _why = _check_dependencies(conn, plugins_root, settings, trial.meta, target)
                if not ok:
                    changed.append("dependencies")
            if history_epoch(conn) != trial.epoch:
                changed.append("history_epoch")
            if history_evidence.current_policy_key() != trial.policy:
                changed.append("history_policy")
    except switch.PluginBusyError:
        changed.append("lock_unavailable")
    trial.changed = changed


def _judge_success(trial: _Trial, scopes: dict, pairs: list[str], history: dict, settings,
                   plugins_root) -> tuple[str, str | None]:
    """gate が最後まで進んだ結果を 4 軸から総合結果へ。warn の完走は合格ではない。"""
    outcome = trial.outcome
    dataset = settings.backtest.dataset()
    history["evidence"] = outcome.history_guard
    if outcome.history_guard is None:
        history["result"] = "unverifiable"
        return UNEVALUABLE, hh.HISTORY_UNAVAILABLE
    check = history_evidence.verify_evidence(
        outcome.history_guard, eval_source=dataset.source, base_interval=dataset.base_interval,
        pairs=pairs, no_bars_path=history_evidence.no_bars_path(Path(plugins_root).parent))
    history["evidence_check"] = check.status
    if not check.ok:
        history["result"] = ("unverifiable" if check.status == history_evidence.CHECK_UNAVAILABLE
                             else "failed")
        return UNEVALUABLE, "changed_during_evaluation"
    history["result"] = "ready_verified"
    if not outcome.evaluable:
        return UNEVALUABLE, "failed"
    results = [scopes[s]["result"] for s in SCOPES]
    if any(r in (FLOOR_NOT_RUN, FLOOR_INDETERMINATE) for r in results):
        return UNEVALUABLE, "failed"
    floor_failed = FLOOR_FAIL in results
    if (outcome.verdict_kind == "floor") != floor_failed:
        return UNEVALUABLE, "failed"
    return (FAIL, "floor") if floor_failed else (PASS, None)


def _build_report(target, trial_id, started, finished, trial: _Trial, settings,
                  plugins_root) -> dict:
    meta = trial.meta
    pairs = list(meta.pairs) if meta is not None else []
    dataset = settings.backtest.dataset()
    scopes = {scope: judge_scope(scope, pairs, trial.scope_rows[scope], settings=settings)
              for scope in SCOPES}
    history = {"result": "not_run", "evidence": None, "evidence_check": None,
               "epoch": trial.epoch}
    failure = trial.failure
    if failure is not None and failure.verdict_kind in (
            hh.HISTORY_STALE, hh.HISTORY_GAP, hh.HISTORY_UNAVAILABLE):
        history.update(result="failed", reason=failure.verdict_kind)
    overall, reason = trial.overall, trial.reason
    if overall is None and trial.outcome is not None:
        overall, reason = _judge_success(trial, scopes, pairs, history, settings, plugins_root)
    if trial.changed and overall in (PASS, FAIL):
        overall, reason = UNEVALUABLE, "changed_during_evaluation"
    if overall not in (PASS, FAIL, UNEVALUABLE) or (
            overall == UNEVALUABLE and reason not in UNEVALUABLE_REASONS_V2):
        overall, reason = UNEVALUABLE, "failed"
    if trial.interrupted:
        status = "interrupted"
    elif overall in (PASS, FAIL) or reason in _COMPLETED_UNEVALUABLE:
        status = "completed"
    else:
        status = "interrupted"
    stop_reason = reason if overall == UNEVALUABLE else None
    if failure is not None:
        stop_reason = failure.verdict_kind or failure.stage
    unexecuted: list[dict] = []
    for scope in SCOPES:
        for pair in pairs:
            if scopes[scope]["pairs"][pair]["result"] == FLOOR_NOT_RUN:
                unexecuted.append({"item": f"{scope}:{pair}",
                                   "reason": stop_reason or "not_measured"})
    if not pairs:
        unexecuted.append({"item": "scopes", "reason": stop_reason or "pairs_unknown"})
    if history["result"] == "not_run":
        unexecuted.append({"item": "history_check", "reason": stop_reason or "not_reached"})
    if failure is not None and (failure.stage, failure.detail) == ("pytest", "pytest"):
        unexecuted.append({"item": "floor_and_history", "reason": "pytest_stopped"})
    rows, local_id = [], 0
    for scope in SCOPES:
        for pair in pairs:
            item = trial.scope_rows[scope].get(pair)
            if item is not None:
                local_id += 1
                rows.append({"local_row_id": local_id, "db_run_id": None, "scope": scope,
                             "pair": pair, **item["entry"]})
    condition = {
        "dataset": {"source": dataset.source, "base_interval": dataset.base_interval},
        "holdout_months": settings.backtest.holdout_months,
        "floor": settings.improve.gate.snapshot(),
        "guard_version": hh.GUARD_VERSION, "calendar_version": hh.CALENDAR_VERSION,
        "thresholds": hh.threshold_snapshot(),
        "core_commit": backtest_runs_store.core_commit(),
        "pairs": pairs, "timeframe": None if meta is None else meta.timeframe}
    return {
        "report_format": REPORT_FORMAT,
        "trial_id": trial_id,
        "old_approval_id": target.old_approval_id,
        "name": target.name,
        "artifact_hash": target.artifact_hash,
        "content_hash": target.content_hash,
        "dependencies": [
            {"alias": alias, "plugin": ref.get("plugin"), "content_hash": ref.get("content_hash")}
            for alias, ref in sorted(target.dependency_pins.items()) if isinstance(ref, dict)],
        "input_checks": trial.input_checks,
        "evaluation": {
            "started_at": _iso(started), "finished_at": _iso(finished),
            "eval_now": _iso(started), "core_commit": condition["core_commit"],
            "dataset": condition["dataset"], "pairs": pairs, "timeframe": condition["timeframe"],
            "holdout_months": condition["holdout_months"], "history_epoch": trial.epoch,
            "conditions_digest": _digest(condition),
            "mode": {"floor_mode": "warn", "compare_mode": "off"}},
        "axes": {
            "execution": {"status": status, "stage": trial.stage},
            "floor": scopes, "history": history, "unexecuted": unexecuted},
        "changed_during_evaluation": trial.changed,
        "result": {"overall": overall, "reason": reason,
                   "result_status": "evaluated" if overall in (PASS, FAIL) else "unevaluable"},
        "rows": rows,
    }
