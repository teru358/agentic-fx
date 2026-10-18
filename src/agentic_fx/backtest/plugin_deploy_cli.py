"""``afx plugin disable / rollback / retire / revoke / versions`` の CLI 面。

どの outcome も (rc, code, 文言) の 1 組に決まる。rc は 0 = 完了または no-op、
1 = 拒否・失敗・未完 (open のまま)・例外、2 = ``--revoke`` で live が無く
``revoke --version`` へ誘導するとき。非 0 のときは stderr の 1 行目を
``エラー: <説明> (code=<code>)`` に揃え、2 行目以降に op_id / 理由 / 案内を置く。
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog
from agentic_fx.plugin import deploy_disable, deploy_ops, deploy_retire, deploy_rollback
from agentic_fx.plugin import deploy_versions
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.store import plugin_deploy_ops as deploy_store

LOCK_WAIT_SECONDS = 30.0

RC_OK = 0
RC_FAILED = 1
RC_USE_REVOKE = 2

UNRESOLVED_JOURNAL_RECOVERY_HINT = (
    "  収束手順: サービスの対話シェルで `approval list` → "
    "`approval retry <approval_id>`")

OPEN_DEPLOY_OP_RECOVERY_HINT = (
    "  配備操作が未完です (op_id は上記)。daemon の起動時 reconcile を待つか、"
    "同じ指定で CLI を再実行してください。別の操作はその op が閉じた後に行ってください。")

SIGNATURE_MISMATCH_HINT = (
    "  未完の op と指定が違うため引き継げません。reconcile の完了を待つか daemon を再起動して、"
    "その op が終端してから打ち直してください (--revoke / --force / --to を後から付け足せません)。")

RETRY_HINT = "  同じコマンドを再実行すると未完の op を引き継いで続きから完遂します。"

NOOP_OUTCOMES = frozenset({"already_disabled", "already_retired", "already_revoked"})


@dataclass
class Report:
    rc: int
    code: str | None = None
    out: list[str] = field(default_factory=list)
    err: list[str] = field(default_factory=list)


def _failure(message: str, code: str, *detail: str, rc: int = RC_FAILED) -> Report:
    return Report(rc, code, err=[f"エラー: {message} (code={code})", *detail])


def _emit(report: Report) -> int:
    for line in report.out:
        print(line)
    for line in report.err:
        print(line, file=sys.stderr)
    return report.rc


def _op_lines(conn, op_id) -> list[str]:
    if op_id is None:
        return []
    lines = [f"  op_id={op_id}"]
    row = deploy_store.get_by_id(conn, op_id) if conn is not None else None
    if row is not None:
        lines.append(f"  phase={row['phase']}"
                     + (f" fail_reason={row['fail_reason']}" if row["fail_reason"] else ""))
    return lines


def _short(value: str | None) -> str:
    if not value:
        return "-"
    return value.rsplit("/", 1)[-1][:12]


def _cascade_text(names) -> str:
    return ", ".join(names) if names else "なし"


def _dependents_text(dependents) -> str:
    return ", ".join(f"{dep[0]} ({dep[1]}: {dep[2]})" for dep in dependents)


def _notified_lines(outcome) -> list[str]:
    ids = getattr(outcome, "notified_op_ids", ())
    return [f"未送信だった通知をやり直しました: op_id={', '.join(str(i) for i in ids)}"] if ids else []


def _stuck(outcome, conn, name: str, kind: str) -> Report | None:
    """Shared rendering of the outcomes that leave an op unfinished or foreign."""
    if outcome.outcome == "foreign":
        return _failure(
            f"plugin {name!r} の live が op の記録と一致しませんでした。op は foreign で終端しました",
            "foreign", *_op_lines(conn, outcome.op_id),
            f"  `afx plugin versions {name}` で現在の live を確認してください。")
    if outcome.outcome == "needs_retry":
        extra = f" 退避先: {outcome.retired_path}" if getattr(outcome, "retired_path", None) else ""
        return _failure(
            f"plugin {name!r} の {kind} は完了していません (needs_retry、op は未完のまま)。{extra}".rstrip(),
            "needs_retry", *_op_lines(conn, outcome.op_id), RETRY_HINT)
    return None


def render_disable(outcome, conn, name: str, *, revoke: bool, verb: str = "disable") -> Report:
    kind = outcome.outcome
    if kind == "dry_run":
        return Report(RC_OK, out=preview_disable(outcome, name, revoke=revoke, verb=verb))
    if kind == "disabled":
        out = [f"plugin {name!r} を {verb} しました: filesystem_switched=yes "
               "runtime_deactivation=effective_on_next_signal_insert"]
        out.append(f"cascade した strategy: {_cascade_text(outcome.cascade_strategies)}")
        if revoke:
            out.append("--revoke: 外した版を rejected にしました")
        return Report(RC_OK, out=out)
    if kind == "already_disabled":
        return Report(RC_OK, out=[f"plugin {name!r} は既に disable 済みです (変更なし)",
                                  *_notified_lines(outcome)])
    if kind == "revoke_requires_version":
        candidate = outcome.version_candidate
        guide = (f"  `afx plugin revoke {name} --version {candidate}` を使ってください"
                 if candidate else
                 f"  `afx plugin versions {name}` で版を確認し "
                 f"`afx plugin revoke {name} --version <hash>` を使ってください")
        return _failure(
            f"plugin {name!r} は live が無いため --revoke を適用できません",
            "revoke_requires_version", guide,
            *(["  (最後に外した版: " + candidate + ")"] if candidate else []),
            rc=RC_USE_REVOKE)
    if kind == "dependents_blocked":
        return _failure(
            f"plugin {name!r} を外すと解決できなくなる strategy があります",
            "dependents_blocked", "  依存: " + _dependents_text(outcome.dependents),
            "  --force を付けると、その strategy を止めて続行できます。")
    stuck = _stuck(outcome, conn, name, verb)
    if stuck is not None:
        return stuck
    return _failure(f"想定外の結果です ({kind})", "unexpected_outcome")


def preview_disable(outcome, name: str, *, revoke: bool, verb: str = "disable",
                    live_hash: str | None = None) -> list[str]:
    lines = [f"dry-run: plugin {name!r} の live (symlink) を外す予定です ({verb})"]
    if getattr(outcome, "op_id", None) is not None:
        lines.append(f"  未完の op_id={outcome.op_id} を引き継いで完遂します")
    lines.append(f"  cascade する strategy: {_cascade_text(outcome.cascade_strategies)}")
    if outcome.dependents:
        lines.append("  --force で止める strategy: " + _dependents_text(outcome.dependents))
    if revoke:
        lines.append("  --revoke: 外す版を rejected にします"
                     + (f" (版 {live_hash})" if live_hash else ""))
    lines.append("  完了後: filesystem_switched=yes runtime_deactivation=effective_on_next_signal_insert")
    return lines


def render_rollback(outcome, conn, name: str, *, revoke: bool) -> Report:
    kind = outcome.outcome
    if kind == "dry_run":
        return Report(RC_OK, out=preview_rollback(outcome, name, revoke=revoke))
    if kind == "rolled_back":
        out = [f"plugin {name!r} を {_short(outcome.to_target)} へ戻しました: "
               "filesystem_switched=yes runtime_activation=pending_restart",
               "  稼働中の daemon が戻した版を使い始めるのは再起動後です。",
               f"  cascade した strategy: {_cascade_text(outcome.cascade_strategies)}"]
        if revoke:
            out.append("  --revoke: 外した版を rejected にしました")
        return Report(RC_OK, out=out)
    if kind == "refused":
        code = outcome.refusal_code or "refused"
        reason = "; ".join(outcome.reasons)
        if code == "already_live":
            return Report(RC_OK, out=[f"plugin {name!r}: {reason} (変更なし)",
                                      *_notified_lines(outcome)])
        return _failure(f"plugin {name!r} の rollback を拒否しました: {reason}", code,
                        *_notified_lines(outcome))
    if kind == "dependents_blocked":
        return _failure(
            f"戻り先を置くと解決できなくなる strategy があります (plugin {name!r})",
            outcome.refusal_code or "dependents_unresolvable",
            "  依存: " + _dependents_text(outcome.dependents),
            "  --force を付けると、その strategy を止めて続行できます。")
    if kind == "verify_failed":
        if outcome.live_removed:
            message = ("戻り先の検証に失敗し、元の版 (symlink) は外した。live は不在です"
                       f" (plugin {name!r})")
        else:
            message = f"戻り先の検証に失敗しました (plugin {name!r}、op は未完のまま)"
        return _failure(
            message, "target_verification_failed", *_op_lines(conn, outcome.op_id),
            *[f"  理由: {reason}" for reason in outcome.reasons],
            f"  `afx plugin versions {name}` で版を確認し、`afx plugin rollback {name} "
            "--to <hash>` で戻し直せます。")
    stuck = _stuck(outcome, conn, name, "rollback")
    if stuck is not None:
        return stuck
    return _failure(f"想定外の結果です ({kind})", "unexpected_outcome")


def preview_rollback(outcome, name: str, *, revoke: bool, live_text: str | None = None,
                     live_hash: str | None = None) -> list[str]:
    lines = [f"dry-run: plugin {name!r} の live を "
             f"{live_text or '(現在の live)'} から {_short(outcome.to_target)} へ戻す予定です"]
    if getattr(outcome, "op_id", None) is not None:
        lines.append(f"  未完の op_id={outcome.op_id} を引き継いで完遂します")
    lines.append(f"  cascade する strategy: {_cascade_text(outcome.cascade_strategies)}")
    if outcome.dependents:
        lines.append("  --force で止める strategy: " + _dependents_text(outcome.dependents))
    if revoke:
        lines.append("  --revoke: 外す版を rejected にします"
                     + (f" (版 {live_hash})" if live_hash else ""))
    lines.append("  完了後: filesystem_switched=yes runtime_activation=pending_restart "
                 "(daemon が使い始めるのは再起動後)")
    return lines


def render_retire(outcome, conn, name: str) -> Report:
    if isinstance(outcome, deploy_disable.DisableOutcome):
        return render_disable(outcome, conn, name, revoke=False, verb="retire")
    kind = outcome.outcome
    if kind == "dry_run":
        return Report(RC_OK, out=preview_retire(outcome, name))
    if kind == "done":
        return Report(RC_OK, out=[f"plugin {name!r} を retire しました (done)",
                                  f"退避先: {outcome.retired_path}",
                                  f"cascade した strategy: {_cascade_text(outcome.cascade_strategies)}"])
    if kind == "already_retired":
        return Report(RC_OK, out=[f"plugin {name!r} は既に retire 済みです (変更なし)",
                                  *_notified_lines(outcome)])
    if kind == "dependents_blocked":
        return _failure(
            f"plugin {name!r} を外すと解決できなくなる plugin があります (dependents_blocked)",
            "dependents_blocked", "  依存: " + _dependents_text(outcome.dependents),
            "  --force を付けると続行できます。")
    if kind == "refused":
        return _failure(
            f"plugin {name!r} の retire は完了していません (refused)", "retire_refused",
            *([f"  {outcome.reason}"] if outcome.reason else []),
            *([f"  退避先: {outcome.retired_path}"] if outcome.retired_path else []),
            *_op_lines(conn, outcome.op_id))
    stuck = _stuck(outcome, conn, name, "retire")
    if stuck is not None:
        return stuck
    return _failure(f"想定外の結果です ({kind})", "unexpected_outcome")


def preview_retire(outcome, name: str) -> list[str]:
    where = outcome.retired_path or "plugins/_retired/ 配下"
    lines = [f"dry-run: plugin {name!r} (legacy plain) を {where} へ退避する予定です"]
    if getattr(outcome, "op_id", None) is not None:
        lines.append(f"  未完の op_id={outcome.op_id} を引き継いで完遂します")
    lines.append(f"  cascade する strategy: {_cascade_text(outcome.cascade_strategies)}")
    if outcome.dependents:
        lines.append("  --force で止める plugin: " + _dependents_text(outcome.dependents))
    return lines


def render_revoke(outcome, name: str, content_hash: str) -> Report:
    kind = outcome.outcome
    if kind == "dry_run":
        return Report(RC_OK, out=[
            f"dry-run: plugin {name!r} の版 {content_hash} を rejected にする予定です",
            "  live ではない版です。FS は変更しません。この版の pending signal は abandoned になります"])
    if kind == "revoked":
        return Report(RC_OK, out=[f"plugin {name!r} の版 {content_hash} を revoke しました",
                                  f"  op_id={outcome.op_id}"])
    if kind == "already_revoked":
        return Report(RC_OK, out=[
            f"plugin {name!r} の版 {content_hash} は既に revoke 済みです (変更なし)",
            *_notified_lines(outcome)])
    return _failure(f"想定外の結果です ({kind})", "unexpected_outcome")


def report_exception(exc: BaseException) -> Report | None:
    """Map the errors a deploy op raises to a report; ``None`` for unrelated errors."""
    if isinstance(exc, deploy_ops.SignatureMismatchError):
        return _failure("未完の配備操作と指定が一致しません", "signature_mismatch",
                        f"  {exc}", SIGNATURE_MISMATCH_HINT)
    if isinstance(exc, (deploy_ops.DeployOpBlockedError, plugin_switch.OpenDeployOpError)):
        return _failure(str(exc), "deploy_op_blocked", OPEN_DEPLOY_OP_RECOVERY_HINT)
    if isinstance(exc, plugin_switch.UnresolvedJournalError):
        return _failure(str(exc), "unresolved_journal", UNRESOLVED_JOURNAL_RECOVERY_HINT)
    if isinstance(exc, plugin_switch.PluginBusyError):
        return _failure(
            f"他の操作が {LOCK_WAIT_SECONDS:g} 秒以内に完了しませんでした。何も変更していません",
            "lock_timeout", "  少し待ってから再実行してください。")
    if isinstance(exc, deploy_ops.StateChangedError):
        return _failure("予告時と状態が変わりました。何も変更していません", "state_changed",
                        "  もう一度実行して予告を確認してください。")
    if isinstance(exc, deploy_ops.DependentsChangedError):
        return _failure(str(exc), "dependents_changed", "  何も変更していません。再実行してください。")
    if isinstance(exc, ValueError):
        return _failure(str(exc), "invalid_request")
    if isinstance(exc, OSError):
        return _failure(str(exc), "os_error")
    if isinstance(exc, sqlite3.Error):
        return _failure(safe_error_text(exc), "db_error")
    return None


def guarded(command):
    """Wrap one subcommand so that no exception leaves without a code and a one-line text."""
    @functools.wraps(command)
    def run(*args, **kwargs) -> int:
        try:
            return command(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — every failure is reported with a code
            report = report_exception(exc)
            if report is None:
                report = _failure(safe_error_text(exc), "internal_error")
            return _emit(report)
    return run


def _isatty() -> bool:
    return sys.stdin.isatty()


def _confirm() -> bool:
    try:
        answer = input("続行しますか? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


@contextlib.contextmanager
def _lock_wait_notice(enabled: bool):
    if not enabled:
        yield
        return

    def notice() -> None:
        print(f"他の操作の完了を待っています (最大 {LOCK_WAIT_SECONDS:g} 秒)", file=sys.stderr)

    with plugin_switch.lock_wait_notice(notice):
        yield


def run_op(conn, *, call, render, preview_lines=None, args, recover=None) -> int:
    """Run one deploy op with its confirmation flow.

    ``call(dry_run, deadline, expect)`` performs the op; ``render(outcome)`` turns an
    outcome into a report. The preview shown before asking comes from a real
    ``dry_run=True`` call, so the question and the action cannot disagree, and the
    real run receives the state the preview showed so it can refuse, changing nothing,
    if that state moved while the operator was deciding. A no-op preview never reaches
    the real run unless ``--yes`` was given; ``recover(outcome)`` only re-sends missed
    notifications for it (``None`` when the state moved).
    """
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    tty = _isatty()

    def attempt(flag: bool, expect=None):
        return call(flag, time.monotonic() + LOCK_WAIT_SECONDS, expect)

    try:
        with _lock_wait_notice(tty):
            if dry_run:
                outcome = attempt(True)
                if outcome.outcome == "dry_run" and preview_lines is not None:
                    return _emit(Report(RC_OK, out=preview_lines(outcome)))
                return _emit(render(outcome))
            if not yes:
                preview = attempt(True)
                if preview.outcome in NOOP_OUTCOMES:
                    notified = recover(preview) if recover is not None else ()
                    if notified is None:
                        raise deploy_ops.StateChangedError("state changed after the preview")
                    return _emit(render(dataclasses.replace(preview, notified_op_ids=notified)))
                if preview.outcome != "dry_run":
                    return _emit(render(preview))
                lines = (preview_lines(preview) if preview_lines is not None
                         else render(preview).out)
                for line in lines:
                    print(line)
                if not tty:
                    return _emit(_failure(
                        "確認が必要です。端末で実行するか --yes を付けてください。何も変更していません",
                        "confirmation_required"))
                if not _confirm():
                    return _emit(_failure("中止しました。何も変更していません", "aborted"))
                return _emit(render(attempt(False, getattr(preview, "expectation", None))))
            return _emit(render(attempt(False)))
    except Exception as exc:  # noqa: BLE001 — mapped to a coded report below
        report = report_exception(exc)
        if report is None:
            raise
        return _emit(report)


def _live_text(plugins_root: Path, name: str) -> str:
    live = plugins_root / name
    if live.is_symlink():
        with contextlib.suppress(OSError):
            return _short(os.readlink(live))
    return "不在" if not os.path.lexists(live) else "plain"


def _live_hash(plugins_root: Path, name: str) -> str | None:
    try:
        identity = deploy_store.live_identity(plugins_root, name)
    except (ValueError, OSError):
        return None
    return identity.content_hash if identity is not None else None


def _common(root: Path):
    plugins_dir = root / "plugins"
    return plugins_dir, ActivityLog(root / "logs" / "activity.log")


def _recover_noop(conn, plugins_dir, name, activity):
    def recover(outcome):
        return deploy_ops.recover_noop_notifications(
            conn, plugins_dir, name, activity, now=datetime.now(timezone.utc),
            expect=outcome.expectation,
            deadline=time.monotonic() + LOCK_WAIT_SECONDS)
    return recover


@guarded
def plugin_disable(conn, settings, args, root: Path) -> int:
    plugins_dir, activity = _common(root)
    revoke = bool(getattr(args, "revoke", False))

    def call(dry_run: bool, deadline: float, expect):
        return deploy_disable.disable_plugin(
            conn, plugins_dir, args.name, revoke=revoke, force=bool(getattr(args, "force", False)),
            now=datetime.now(timezone.utc), settings=settings, activity=activity,
            deadline=deadline, dry_run=dry_run, expect=expect)

    return run_op(
        conn, call=call, args=args, recover=_recover_noop(conn, plugins_dir, args.name, activity),
        render=lambda outcome: render_disable(outcome, conn, args.name, revoke=revoke),
        preview_lines=lambda outcome: preview_disable(
            outcome, args.name, revoke=revoke,
            live_hash=_live_hash(plugins_dir, args.name) if revoke else None))


@guarded
def plugin_rollback(conn, settings, args, root: Path) -> int:
    plugins_dir, activity = _common(root)
    revoke = bool(getattr(args, "revoke", False))

    def call(dry_run: bool, deadline: float, expect):
        return deploy_rollback.rollback_plugin(
            conn, plugins_dir, args.name, to=getattr(args, "to", None), revoke=revoke,
            force=bool(getattr(args, "force", False)), now=datetime.now(timezone.utc),
            settings=settings, activity=activity, deadline=deadline, dry_run=dry_run,
            expect=expect)

    return run_op(
        conn, call=call, args=args,
        render=lambda outcome: render_rollback(outcome, conn, args.name, revoke=revoke),
        preview_lines=lambda outcome: preview_rollback(
            outcome, args.name, revoke=revoke, live_text=_live_text(plugins_dir, args.name),
            live_hash=_live_hash(plugins_dir, args.name) if revoke else None))


@guarded
def plugin_retire(conn, settings, args, root: Path) -> int:
    plugins_dir, activity = _common(root)

    def call(dry_run: bool, deadline: float, expect):
        return deploy_retire.retire_plugin(
            conn, plugins_dir, args.name, force=bool(getattr(args, "force", False)),
            now=datetime.now(timezone.utc), settings=settings, activity=activity,
            deadline=deadline, dry_run=dry_run, expect=expect)

    def preview(outcome):
        if isinstance(outcome, deploy_disable.DisableOutcome):
            return preview_disable(outcome, args.name, revoke=False, verb="retire")
        return preview_retire(outcome, args.name)

    return run_op(conn, call=call, args=args, preview_lines=preview,
                  recover=_recover_noop(conn, plugins_dir, args.name, activity),
                  render=lambda outcome: render_retire(outcome, conn, args.name))


@guarded
def plugin_revoke(conn, settings, args, root: Path) -> int:
    plugins_dir, activity = _common(root)
    content_hash = args.version
    if len(content_hash) < 64:
        try:
            data = deploy_versions.collect_versions(conn, plugins_dir, args.name)
        except ValueError as exc:
            return _emit(_failure(str(exc), "invalid_request"))
        matches = sorted({v["content_hash"] for v in data["versions"]
                          if v["content_hash"] and v["content_hash"].startswith(content_hash)})
        if len(matches) != 1:
            if not matches:
                return _emit(_failure(
                    f"plugin {args.name!r} に {content_hash!r} に一致する版がありません",
                    "target_not_found", f"  `afx plugin versions {args.name}` で版を確認してください。"))
            return _emit(_failure(
                f"{content_hash!r} に一致する版が複数あります", "target_ambiguous",
                "  " + ", ".join(matches)))
        content_hash = matches[0]

    def call(dry_run: bool, deadline: float, expect):
        return deploy_disable.revoke_version(
            conn, plugins_dir, args.name, content_hash=content_hash,
            now=datetime.now(timezone.utc), activity=activity, deadline=deadline,
            dry_run=dry_run, expect=expect)

    def recover(outcome):
        return deploy_disable.recover_revoke_notifications(
            conn, plugins_dir, args.name, content_hash, activity,
            now=datetime.now(timezone.utc), expect=outcome.expectation,
            deadline=time.monotonic() + LOCK_WAIT_SECONDS)

    return run_op(conn, call=call, args=args, recover=recover,
                  render=lambda outcome: render_revoke(outcome, args.name, content_hash))


@guarded
def plugin_versions(settings, args, root: Path) -> int:
    """Show one plugin's versions without taking a lock or writing anything."""
    conn = deploy_store.open_readonly(root / "data" / "agentic.db")
    if conn is None:
        return _emit(_failure(
            "データベースがまだ初期化されていません。何も変更していません", "db_missing",
            "  先に `afx init` を実行してください。"))
    try:
        data = deploy_versions.collect_versions(
            conn, root / "plugins", args.name, settings=settings)
    except ValueError as exc:
        return _emit(_failure(str(exc), "invalid_request"))
    finally:
        conn.close()
    for line in deploy_versions.render_versions(data):
        print(line)
    return RC_OK
