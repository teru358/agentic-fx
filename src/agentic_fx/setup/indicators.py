"""Standard indicator adoption stage of the first-run wizard.

Each selected example is copied into the human candidate area and handed to the
existing bless path one at a time; the gate, journal and ledger are never
bypassed and nothing is copied straight into ``plugins/``.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol, runtime_checkable

from agentic_fx.core import landlock
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.store import plugin_switch_journal as journal_store

DECIDED_BY = "human_cli"

STANDARD_INDICATORS: tuple[tuple[str, str], ...] = (
    ("sma", "単純移動平均 (終値の期間平均)"),
    ("ema", "指数移動平均 (直近を重く見る平均)"),
    ("rsi", "RSI (上昇と下落の勢いの比、0〜100)"),
    ("macd", "MACD (2 本の EMA の差とそのシグナル線・ヒストグラム)"),
    ("bollinger", "ボリンジャーバンド (移動平均と標準偏差の上下帯)"),
    ("atr", "ATR (値動きの幅の平均、ボラティリティ)"),
    ("adx", "ADX (トレンドの強さと +DI / -DI)"),
    ("stochastic", "ストキャスティクス (直近レンジ内の終値位置の %K / %D)"),
    ("ichimoku", "一目均衡表 (転換線・基準線・先行スパン・遅行スパン)"),
)

_NO_LANDLOCK_TEXT = (
    "この環境では Landlock が使えないため、標準指標の導入は飛ばします。"
    "指標だけでは取引しません。")
_INTRO_TEXT = "指標だけでは取引しません。改善ループが戦略を作るときの材料になります。"
_DEFAULT_GATE_SECONDS = 300
_DEPLOY_DOC = "docs/operations/indicator-initial-set-deploy-2026-09-19.md"


@runtime_checkable
class IndicatorIO(Protocol):
    """The subset of the wizard IO (``ops_setup.interactive.IO``) this stage uses."""

    def ask(self, label: str, default: str | None = None) -> str | None: ...
    def stdout(self, text: str) -> None: ...


@dataclass(frozen=True)
class IndicatorRow:
    name: str
    summary: str
    deployed: bool


@dataclass(frozen=True)
class AdoptOutcome:
    name: str
    result: str  # adopted | gate_failed | stalled | legacy_present | error | skipped
    approval_id: int | None = None
    reason_code: str = ""
    error_type: str = ""


def deployed_names(root: Path) -> set[str]:
    """Names already present under ``plugins/`` (candidate area and dot dirs excluded)."""
    plugins = Path(root) / "plugins"
    try:
        entries = list(plugins.iterdir())
    except OSError:
        return set()
    return {entry.name for entry in entries
            if not entry.name.startswith((".", "_"))}


def list_standard_indicators(examples_dir: Path, deployed: Iterable[str]) -> list[IndicatorRow]:
    """The standard rows in fixed order, limited to names whose bundled example exists."""
    present = set(deployed)
    return [IndicatorRow(name, summary, name in present)
            for name, summary in STANDARD_INDICATORS
            if (Path(examples_dir) / name).is_dir()]


def _parse_numbers(text: str, count: int) -> set[int] | None:
    numbers: set[int] = set()
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        if re.fullmatch(r"\d+", token):
            low = high = int(token)
        elif (match := re.fullmatch(r"(\d+)-(\d+)", token)):
            low, high = int(match.group(1)), int(match.group(2))
        else:
            return None
        if low < 1 or high > count or low > high:
            return None
        numbers.update(range(low, high + 1))
    return numbers


def ask_selection(io: IndicatorIO, rows: list[IndicatorRow]) -> list[str]:
    """Show the list and accept all by default, with an optional exclusion menu."""
    io.stdout(_INTRO_TEXT)
    if rows and all(row.deployed for row in rows):
        io.stdout(f"標準指標 {len(rows)} 本は配備済みです (変更なし)")
        return []
    for index, row in enumerate(rows, 1):
        mark = "  (配備済み・既定で外れています)" if row.deployed else ""
        io.stdout(f"{index}. {row.name}: {row.summary}{mark}")
    default_chosen = [row.name for row in rows if not row.deployed]
    io.stdout("登録はあなたの確認として記録されます")
    while True:
        answer = io.ask(f"標準指標 {len(default_chosen)} 本を登録します [Y/n] (e = 外す番号を選ぶ)", "Y")
        normalized = (answer or "Y").strip().lower()
        if normalized in ("", "y", "yes"):
            return default_chosen
        if normalized in ("n", "no"):
            io.stdout("採用しませんでした。")
            return []
        if normalized == "e":
            break
        io.stdout("y、n、e のどれかで答えてください。")
    toggled: set[int] = set()
    while True:
        answer = io.ask("外したい番号 (複数はカンマか空白、範囲は 2-4。配備済みの番号は採用に戻します。空 Enter = この一覧で進む)", "")
        parsed = _parse_numbers(answer or "", len(rows))
        if parsed is not None:
            toggled = parsed
            break
        io.stdout(f"番号は 1〜{len(rows)} の整数 (カンマ・空白区切り、範囲は 2-4) で入力してください。")
    chosen = [row.name for index, row in enumerate(rows, 1)
              if (index in toggled) == row.deployed]
    if not chosen:
        io.stdout("採用する指標がありません。")
        return []
    return chosen


def _copy_candidate(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))


def _gate_kind(error: BaseException) -> str | None:
    """The failed check as ``pytest`` / ``ast`` / ``hash``, from structure only."""
    if isinstance(error, plugin_switch.GateCheckError):
        return error.kind if error.kind in ("pytest", "hash") else None
    if isinstance(error, SandboxError) or isinstance(error.__cause__, SandboxError):
        return "ast"
    return None


def _open_journal_approval(conn: sqlite3.Connection, name: str) -> tuple[bool, int | None]:
    row = journal_store.get_open_by_name(conn, name)
    if row is None:
        return False, None
    value = row.get("approval_id")
    return True, (int(value) if value is not None else None)


def _pending_ids(conn: sqlite3.Connection, name: str) -> set[int]:
    rows = conn.execute(
        "SELECT id FROM approval_requests WHERE status='pending' AND kind='plugin' "
        "AND json_extract(payload_json, '$.name') = ?", (name,)).fetchall()
    return {int(row[0]) for row in rows}


def _approval_state(conn: sqlite3.Connection, approval_id: int) -> tuple[str, str | None]:
    """(status, reason) of the approval row; unreadable or missing counts as pending."""
    try:
        row = conn.execute("SELECT status, reason FROM approval_requests WHERE id=?",
                           (approval_id,)).fetchone()
    except Exception:
        return "pending", None
    if row is None:
        return "pending", None
    return row[0], row[1]


def _is_gate_failure(error: BaseException) -> bool:
    return (isinstance(error, (plugin_switch.GateCheckError, SandboxError))
            or isinstance(error.__cause__, SandboxError))


def _gate_seconds(settings) -> str:
    value = getattr(getattr(settings, "plugin", None), "pytest_timeout_sec", None)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        value = _DEFAULT_GATE_SECONDS
    return f"{int(value)}" if float(value).is_integer() else f"{value:g}"


def _gate_failed_text(name: str, kind: str | None) -> str:
    label = f"審査に通りませんでした ({kind} 不合格)" if kind else "審査に通りませんでした"
    return (f"{name}: {label}。何も配備されていません。"
            f"後で `uv run afx plugin bless {name} --from _human` でやり直せます。")


def _stalled_text(name: str, approval_id: int | None) -> str:
    if approval_id is None:
        return (f"{name}: 配備の途中で止まりました。サービス起動後に `afx ctl status` で"
                "未完の手順を確かめてください。")
    return (f"承認 #{approval_id} ({name}) が途中で止まりました。サービス起動後に "
            f"`afx ctl --as approver approval retry {approval_id}` を実行してください "
            "(端末での確認があります)。")


def _open_deploy_op_text(name: str, error: Exception) -> str:
    return (f"{name}: 配備操作が未完のため中断しました ({error})。"
            f"候補は plugins/_human/{name} に残っています。daemon の起動時 reconcile を待つか、"
            "同じ指定で CLI を再実行してから `afx init` の採用をやり直してください。")


def _legacy_text(name: str) -> str:
    return (f"{name} は旧形式で配備済みです。切り替えは手順書 (0) を参照してください "
            f"({_DEPLOY_DOC})。")


def _error_text(name: str, error_type: str) -> str:
    return (f"{name}: 採用処理でエラー ({error_type})。"
            "候補は plugins/_human に残っています。")


def _announce_interrupt(io: IndicatorIO, conn: sqlite3.Connection, name: str) -> None:
    try:
        pending = sorted(_pending_ids(conn, name))
    except Exception:
        pending = None
    if pending is None:
        left = "未決の承認は確かめられませんでした"
    elif pending:
        left = "未決の承認: " + ", ".join(f"#{i}" for i in pending)
    else:
        left = "未決の承認はありません"
    io.stdout(f"{name}: 中断しました。候補は plugins/_human/{name} に残っています。{left}。")


def _classify_failure(io: IndicatorIO, conn: sqlite3.Connection, name: str, candidate: Path,
                      error: Exception, before_pending: set[int]) -> AdoptOutcome:
    if isinstance(error, plugin_switch.OpenDeployOpError):
        io.stdout(_open_deploy_op_text(name, error))
        return AdoptOutcome(name, "stalled", None, "open_deploy_op")
    try:
        has_journal, found = _open_journal_approval(conn, name)
        if not has_journal:
            new_pending = sorted(_pending_ids(conn, name) - before_pending)
            if new_pending:
                has_journal, found = True, new_pending[-1]
    except Exception:
        io.stdout(_error_text(name, type(error).__name__))
        return AdoptOutcome(name, "error", None, "adopt_error", type(error).__name__)
    if has_journal:
        io.stdout(_stalled_text(name, found))
        if isinstance(error, plugin_switch.UnresolvedJournalError):
            code = "unresolved_journal"
        else:
            code = "switch_incomplete"
        return AdoptOutcome(name, "stalled", found, code)
    if _is_gate_failure(error):
        shutil.rmtree(candidate, ignore_errors=True)
        io.stdout(_gate_failed_text(name, _gate_kind(error)))
        return AdoptOutcome(name, "gate_failed", None, "gate_rejected")
    io.stdout(_error_text(name, type(error).__name__))
    return AdoptOutcome(name, "error", None, "adopt_error", type(error).__name__)


def adopt(io: IndicatorIO, root: Path, conn: sqlite3.Connection, names: list[str], *,
          settings, bless: Callable[..., int] = plugin_switch.bless_candidate,
          now: Callable[[], datetime] | datetime | None = None) -> list[AdoptOutcome]:
    """Bless the chosen examples one by one; a failure never stops the others."""
    root = Path(root)
    examples = root / "docs" / "examples" / "plugins"
    staging = root / "plugins" / "_human"
    outcomes: list[AdoptOutcome] = []
    if not isinstance(io, IndicatorIO):
        raise TypeError("io must provide ask() and stdout()")
    total = len(names)
    seconds = _gate_seconds(settings)
    for position, name in enumerate(names, 1):
        io.stdout(f"{position}/{total} {name}: 審査中 (pytest、最長 {seconds} 秒)")
        source, candidate = examples / name, staging / name
        if not source.is_dir():
            io.stdout(f"{name}: 同梱の見本が見つからないので飛ばします。")
            outcomes.append(AdoptOutcome(name, "skipped", None, "example_missing"))
            continue
        if candidate.exists() or candidate.is_symlink():
            io.stdout(f"{name}: 候補置き場 plugins/_human/{name} に既に何かあるので飛ばします。"
                      "中身を確かめてから `uv run afx plugin bless "
                      f"{name} --from _human` を実行してください。")
            outcomes.append(AdoptOutcome(name, "skipped", None, "candidate_exists"))
            continue
        try:
            staging.mkdir(parents=True, exist_ok=True)
            _copy_candidate(source, candidate)
        except OSError:
            shutil.rmtree(candidate, ignore_errors=True)
            io.stdout(f"{name}: 候補置き場に置けなかったので飛ばします。")
            outcomes.append(AdoptOutcome(name, "skipped", None, "candidate_copy_failed"))
            continue
        stamp = now() if callable(now) else (now or datetime.now(timezone.utc))
        before_pending: set[int] = set()
        try:
            before_pending = _pending_ids(conn, name)
            approval_id = int(bless(conn, name=name, human_dir=candidate, settings=settings,
                                    now=stamp, decided_by=DECIDED_BY))
        except KeyboardInterrupt:
            _announce_interrupt(io, conn, name)
            raise
        except Exception as error:
            outcome = _classify_failure(io, conn, name, candidate, error, before_pending)
            outcomes.append(outcome)
            continue
        status, reason = _approval_state(conn, approval_id)
        if status == "pending" and reason == "legacy_plain_present":
            io.stdout(_legacy_text(name))
            outcomes.append(AdoptOutcome(name, "legacy_present", approval_id, "legacy_plain_present"))
            continue
        if status == "pending":
            io.stdout(_stalled_text(name, approval_id))
            outcomes.append(AdoptOutcome(name, "stalled", approval_id, "approval_pending"))
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        io.stdout(f"{name}: 採用しました (承認 #{approval_id})。")
        outcomes.append(AdoptOutcome(name, "adopted", approval_id, ""))
    if any(o.result == "adopted" for o in outcomes):
        io.stdout("反映は次回のサービス起動時です。")
    return outcomes


def run_indicator_stage(io: IndicatorIO, root: Path, conn: sqlite3.Connection, *, settings,
                        landlock_available: Callable[[], bool] = landlock.is_available,
                        bless: Callable[..., int] = plugin_switch.bless_candidate,
                        now: Callable[[], datetime] | datetime | None = None) -> list[AdoptOutcome]:
    """List, select and adopt; without Landlock only explain and return nothing."""
    if not landlock_available():
        io.stdout(_NO_LANDLOCK_TEXT)
        return []
    if not isinstance(io, IndicatorIO):
        raise TypeError("io must provide ask() and stdout()")
    root = Path(root)
    examples = root / "docs" / "examples" / "plugins"
    rows = list_standard_indicators(examples, deployed_names(root))
    listed = {row.name for row in rows}
    for name, _ in STANDARD_INDICATORS:
        if name not in listed:
            io.stdout(f"{name}: 雛形が見つかりません。")
    if not rows:
        return []
    names = ask_selection(io, rows)
    if not names:
        return []
    return adopt(io, root, conn, names, settings=settings, bless=bless, now=now)


def indicators_record_values(outcomes: Iterable[AdoptOutcome],
                             deployed: Iterable[str] = ()) -> dict[str, int]:
    """The ``indicators`` section: standard indicators deployed once this run is done."""
    standard = {name for name, _ in STANDARD_INDICATORS}
    present = set(deployed) | {outcome.name for outcome in outcomes if outcome.result == "adopted"}
    return {"installed": len(present & standard)}
