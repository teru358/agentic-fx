"""起動前検査の分類と `afx check`。

fatal (サービスが起動できない条件) と warning (LLM が使えるか確かめられていない
だけの条件) の分類はこの module の `classify` だけが持つ。サービスの起動前検査と
`afx check` は `service.STARTUP_CHECKS` の同じ一覧を回す。`check` は読み取りのみで、
lock を作らず、DB は読み取り専用で開いて承認済み plugin の一覧を得るだけ。
`check` は LLM の疎通を試さない。記録の deferred は init の再実行まで残る。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from agentic_fx.config import ConfigError, load_settings
from agentic_fx.setup.checks import CheckReason
from agentic_fx.store.setup_record import read_setup_record

FATAL_CODES: dict[str, str] = {
    "settings_invalid": "settings.yaml を読み込めません (構文・型・必須キーを確認してください)",
    "pairs_empty": "settings.pairs が空です",
    "datafeed_primary_unknown": "datafeed.primary が未知の価格源です",
    "db_unreadable": "data/agentic.db を読めないため、起動前検査を完了できません (壊れているか権限がありません)",
    "plugin_retention_short": "承認済み plugin が要求する履歴の保持日数を満たせません",
    "cache_retention_inconsistent": "datafeed.cache_retention_days が判断足の要求日数を下回っています",
    "mission_ceiling_violation": "判断足と mission の上限が両立しません",
    "cli_not_elf": "CLI backend の実行ファイルが ELF ではありません",
    "opencode_unverified": "opencode backend は improve.llama_swap_verified が必要です",
    "secret_env_present": "サービスの初期 env に秘密らしい変数名があります (.env に置いてください)",
    "startup_check_failed": "起動前検査に失敗しました",
}

WARNING_CODES: dict[str, str] = {
    "plugin_list_unavailable": "承認済み plugin の一覧を読めませんでした (旧 schema の DB は起動時に migration されます)",
    "watch_symbol_unknown": "datafeed.watch_symbols に bridge の銘柄一覧に無い名前があります (取得は失敗します。綴りを確かめてください)",
    "watch_symbols_unchecked": "bridge に問い合わせられず、datafeed.watch_symbols を確かめていません",
    CheckReason.BIN_NOT_FOUND.value: "CLI backend の実行ファイルが見つかりません (LLM は未検証のままです)",
    CheckReason.CREDENTIALS_MISSING.value: "CLI backend の認証ファイルがありません (ログインしてください)",
    CheckReason.VERSION_CHECK_FAILED.value: "CLI backend の --version が通りません (LLM は未検証のままです)",
    CheckReason.DEFERRED_BY_USER.value: "LLM は未確認です (後で設定する、を選択)。`afx init` の再実行で確認します",
    CheckReason.USER_DECLINED_PROBE.value: "LLM の疎通確認を行っていません",
    CheckReason.MODEL_NOT_FOUND.value: "モデルが確認できていません",
    CheckReason.CONNECT_ERROR.value: "LLM に接続できていません",
    CheckReason.AUTH_REQUIRED.value: "LLM の認証が通っていません",
    CheckReason.AUTH_UNSUPPORTED.value: "LLM の認証方式が対応外です",
}


class PreflightFailure(RuntimeError):
    """起動前検査の失敗。`code` で fatal / warning を分類する。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Finding:
    code: str
    message: str = ""


def is_warning(code: str) -> bool:
    """code が warning (起動を止めない条件) か。未知の code は warning ではない。"""
    return code in WARNING_CODES


def classify(findings: Iterable[Finding]) -> tuple[list[str], list[str]]:
    """findings を (fatal, warning) の code 一覧に分ける。未知の code は fatal。"""
    fatal: list[str] = []
    warning: list[str] = []
    for finding in findings:
        bucket = warning if is_warning(finding.code) else fatal
        if finding.code not in bucket:
            bucket.append(finding.code)
    return fatal, warning


@dataclass(frozen=True)
class CheckReport:
    fatal: list[str] = field(default_factory=list)
    warning: list[str] = field(default_factory=list)
    messages: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fatal


class _NullActivity:
    def write(self, *args, **kwargs) -> None:
        return None


# 利用者の選択として記録に残る語だけを、確認記録から warning に写す。それ以外の理由は
# 過去の環境での失敗なので、現在の検査結果だけから出す。
_RECORDED_CHOICE_CODES = (CheckReason.DEFERRED_BY_USER.value, CheckReason.USER_DECLINED_PROBE.value)


def _record_findings(root: Path, verified: set[str]) -> list[Finding]:
    read = read_setup_record(root)
    if read.state != "ok" or read.record is None:
        return []
    found: list[Finding] = []
    for target, which in (("llm_trade", "trade"), ("llm_improve", "improve")):
        if which in verified:
            continue
        entry = read.record.data.get(target)
        if not isinstance(entry, dict) or entry.get("result") == "ok":
            continue
        reason = entry.get("reason_code")
        if isinstance(reason, str) and reason in _RECORDED_CHOICE_CODES:
            found.append(Finding(reason))
    return found


def _report(findings: list[Finding], notes: list[str] | None = None) -> CheckReport:
    fatal, warning = classify(findings)
    messages: dict[str, str] = {}
    for finding in findings:
        base = FATAL_CODES.get(finding.code) or WARNING_CODES.get(finding.code) or finding.code
        messages.setdefault(finding.code, f"{base}: {finding.message}" if finding.message else base)
    return CheckReport(fatal, warning, messages, list(notes or []))


def _config_findings(exc: ConfigError) -> list[Finding]:
    """設定の検証失敗を、直す場所が分かる fatal に写す。入力値は文言に含めない。"""
    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return [Finding("settings_invalid", str(exc).splitlines()[0] if str(exc) else "")]
    found: list[Finding] = []
    for error in cause.errors()[:8]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "settings"
        message = str(error.get("msg", ""))
        if location == "pairs" and error.get("type") in ("too_short", "missing"):
            code = "pairs_empty"
        elif "unknown_primary_source" in message:
            code = "datafeed_primary_unknown"
            location = "datafeed.primary"
        else:
            code = "settings_invalid"
        found.append(Finding(code, f"{location}: {message}"))
    return found or [Finding("settings_invalid", "")]


def _approved_plugins(root: Path, settings) -> list:
    """service と同じ入力 (DB 上の承認済み plugin) を読み取り専用で得る。

    DB が無い (初回) ときだけ空。旧 schema で読めないときは warning の
    `plugin_list_unavailable`、壊れている・開けないときは fatal の `db_unreadable`。"""
    from agentic_fx.store.db import connect_readonly
    from agentic_fx.tools import plugin_loader

    db_path = root / "data" / "agentic.db"
    if not db_path.exists():
        return []
    try:
        conn = connect_readonly(db_path)
        try:
            return list(plugin_loader.approved_plugins(
                conn, root / "plugins", settings=settings).inventory.metas)
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        # 旧 schema (表や列が無い) だけが service の起動時 migration で直るので止めない。
        # ロック・開けない・I/O 障害などは直らないので fatal にする。
        text = str(exc).lower()
        if "no such table" in text or "no such column" in text:
            raise PreflightFailure("plugin_list_unavailable",
                                   "起動時に migration されます") from exc
        raise PreflightFailure("db_unreadable", type(exc).__name__) from exc
    except (sqlite3.DatabaseError, OSError) as exc:
        raise PreflightFailure("db_unreadable", type(exc).__name__) from exc
    except Exception as exc:  # noqa: BLE001 - 例外文は出さず型名だけを残す
        raise PreflightFailure("startup_check_failed", type(exc).__name__) from exc


_SHELL_SECRET_NOTE = ("現在のシェルに秘密らしい名前の環境変数がありますが、unit には渡りません "
                      "(afx init が unit の環境で検査します)")


def _shell_secret_note(settings) -> list[str]:
    """手動実行のときだけ、シェルの env に秘密らしい名前があることを fatal にせず知らせる。"""
    from agentic_fx import service

    for which in ("trade", "improve"):
        backend = getattr(settings.runner, which).backend
        if backend == "local":
            continue
        try:
            service._check_service_initial_env_has_no_secrets(
                settings, which=which, backend=backend)
        except PreflightFailure:
            return ["注意: " + _SHELL_SECRET_NOTE]
        except Exception:  # noqa: BLE001 - 注意書きのための読み取りなので失敗しても検査は続ける
            continue
    return []


def _bridge_api_key(root: Path) -> str | None:
    """環境変数、無ければ root の `.env` から bridge の鍵を読む (環境は書き換えない)。"""
    value = os.environ.get("MT5_BRIDGE_API_KEY")
    if value:
        return value
    try:
        from dotenv import dotenv_values
        return dotenv_values(root / ".env").get("MT5_BRIDGE_API_KEY") or None
    except Exception:  # noqa: BLE001 - 鍵が読めなければ鍵なしで問い合わせる
        return None


def run_check(root: Path, *, unit_env: bool = False) -> CheckReport:
    """設定と実行環境だけで決まる起動前検査を、書き込みなしで実行する。

    サービスの初期 env の検査 (秘密らしい変数名) は、unit の環境で呼ばれたとき
    (`unit_env`) だけ fatal に数える。手動実行では自分のシェルの env を見て注意を出す。"""
    from agentic_fx import service

    root = Path(root)
    try:
        settings = load_settings(root / "config" / "settings.yaml")
    except ConfigError as exc:
        return _report(_config_findings(exc))
    except Exception as exc:  # noqa: BLE001 - 読めない設定は fatal として一覧に出す
        return _report([Finding("settings_invalid", type(exc).__name__)])
    findings: list[Finding] = []
    try:
        plugins = _approved_plugins(root, settings)
    except PreflightFailure as exc:
        findings.append(Finding(exc.code, str(exc)))
        plugins = []
    context = service.StartupContext(_NullActivity(), plugins, findings, check_secret_env=unit_env,
                                     bridge_api_key=_bridge_api_key(root))
    for check in service.STARTUP_CHECKS:
        try:
            settings = check(settings, context)
        except PreflightFailure as exc:
            findings.append(Finding(exc.code, str(exc)))
        except Exception as exc:  # noqa: BLE001 - 例外文は出さず型名だけを残す
            findings.append(Finding("startup_check_failed", type(exc).__name__))
    findings.extend(_record_findings(root, context.cli_ok))
    return _report(findings, [] if unit_env else _shell_secret_note(settings))


def format_report(report: CheckReport) -> str:
    lines = [f"fatal: {code}: {report.messages.get(code, code)}" for code in report.fatal]
    lines += [f"warning: {code}: {report.messages.get(code, code)}" for code in report.warning]
    lines += [f"info: {note}" for note in report.notes]
    if not report.fatal and not report.warning:
        lines.append("ok: 問題は見つかりませんでした")
    return "\n".join(lines)


def report_json(report: CheckReport) -> str:
    return json.dumps({"fatal": list(report.fatal), "warning": list(report.warning)},
                      ensure_ascii=False)


def main(root: Path, *, as_json: bool = False, unit_env: bool = False) -> int:
    report = run_check(root, unit_env=unit_env)
    print(report_json(report) if as_json else format_report(report))
    return 0 if report.ok else 1
