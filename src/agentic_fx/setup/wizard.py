"""対話ウィザード (`afx init`) の本体。

段 0〜9 は何も書かない「集める層」(書くのは lock ファイルだけ)。段 10 は回復可能な
commit protocol (marker → settings → DB → 確認記録 → 状態 → marker 除去) で、その後の
段 11〜14 は 1 つずつ独立に確定する。外部への呼び出し (HTTP・subprocess・systemd・
`afx ops enable`) は全て `WizardDeps` から注入され、既定が本物。

ウィザードは秘密を受け取らず、書かず、表示しない。`WizardIO` が秘密用の入口を
持たないのはそのため。
"""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import yaml

from agentic_fx import service
from agentic_fx._safe_error import safe_error_text, safe_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.commands import add_backlog_idea, append_policy_directive
from agentic_fx.config import MAX_STORED_DECISION_INTERVAL_MIN, Settings, load_settings_text
from agentic_fx.core import improve_admission, landlock
from agentic_fx.core.contracts import Mode
from agentic_fx.core.idea_check import idea_looks_unintended, normalize_idea_display
from agentic_fx.core.mission_ceiling import worker_ceiling
from agentic_fx.core.scheduler import (
    DAILY_AT_RE, WEEKLY_AT_RE, latest_scheduled_occurrence, scheduled_period,
)
from agentic_fx.datafeed import sources
from agentic_fx.datafeed.news_collector import seed_default_sources
from agentic_fx.logging_setup import setup_technical_logging
from agentic_fx.ops_setup.interactive import EnableResult, interactive_enable
from agentic_fx.ops_setup.setup_lease import (
    SetupLease, SetupLeaseBusy, SetupLeaseUnsafe, acquire_setup_lease,
)
from agentic_fx.policy import directives_path
from agentic_fx.setup import checks, post_start, unit as unit_module
from agentic_fx.setup.checks import CheckResult
from agentic_fx.setup.indicators import (
    deployed_names, indicators_record_values, run_indicator_stage,
)
from agentic_fx.setup.pending_steps import pending_steps
from agentic_fx.setup.pending_facts import collect_pending_facts
from agentic_fx.setup.pending_text import CLI_DOC_SECTION, render_pending_step
from agentic_fx.setup.preflight import _NullActivity
from agentic_fx.setup.settings_edit import atomic_write_settings, edit_settings, find_duplicate_keys
from agentic_fx.setup.settings_migrate import CURRENT_SCHEMA_VERSION, migrate
from agentic_fx.store.db import connect, connect_readonly, init_db
from agentic_fx.store.enroll_marker import enroll_marker_present
from agentic_fx.store.improve_waves import consume_scheduled_period
from agentic_fx.store.instance_lock import InstanceAlreadyRunning
from agentic_fx.store.setup_marker import (
    SetupMarker, format_setup_marker, read_setup_marker, remove_setup_marker,
    setup_marker_present, write_setup_marker,
)
from agentic_fx.store.setup_record import SetupRecord, read_setup_record, write_setup_record

Hook = Callable[[str], None]

RC_OK = 0
RC_STOPPED = 1
RC_USAGE = 2
RC_INTERRUPT = 130

NON_TTY_MESSAGE = ("対話が必要です (標準入力が端末ではありません)。"
                   "自動構築では `afx init --non-interactive` を使ってください。")
_NOT_REPO_MESSAGE = ("ここは agentic-fx のリポジトリ直下ではありません "
                     "(config/settings.yaml.example が見つかりません)。"
                     "clone したディレクトリで実行してください。")
_SETUP_BUSY_MESSAGE = ("別の `afx init` または `afx ops enable` が実行中です。"
                       "終了してから再実行してください。設定・DB は変更していません。")
_INSTANCE_BUSY_MESSAGE = ("別の agentic-fx が動いています。`systemctl --user stop afx` で止めてから"
                          "再実行してください (`uv run afx` で手動起動しているなら、"
                          "その端末で終了してください)。設定・DB は変更していません。")
_ENROLL_MESSAGE = ("`afx ops enable <feature>` が途中で止まっています。同じコマンドを再実行して"
                   "完了させてください。その間 afx は起動できません。")
_NOTHING_CHANGED = "確定していないので何も変えていません。"
_AFTER_COMMIT_STOP = ("ここまでの確定は残っています。続きは `afx init` の再実行で行えます "
                      "(同じ回答で収束し、確定済みの所は繰り返しません)。")

_PRICE_FAILURE_TEXT = {
    "connect_error": "MT5 bridge に接続できません。bridge が起動しているか、URL が正しいか確認してください。",
    "auth_required": "鍵が一致しません。`.env` の MT5_BRIDGE_API_KEY を確かめてください。",
    "server_time_unknown": ("bridge は動いていますが、MT5 のサーバ時刻がまだ確定していません。"
                            "MT5 端末がログイン済みで、市場が開いているときに再試行してください。"),
    "bridge_not_connected": ("bridge は動いていますが MT5 端末とつながっていません。"
                             "端末のログイン・自動売買の許可を確認してください。"),
    "invalid_response": "bridge の応答が想定した形ではありません。bridge のバージョンを確認してください。",
}
_BRIDGE_GUIDE = (
    "bridge を配置して afx とつなげるまでの手順 (Wine・MT5 本体の導入はここでは扱いません):",
    "  1. MT5 端末が動いてログイン済みの Windows (または Wine) 環境を用意する。",
    "  2. そこにリポジトリの `mt5_bridge/` を置き、`mt5_bridge/.env.example` を `.env` に写して MT5 のログイン情報を書く。"
    " `BRIDGE_API_KEY` を設定することを勧めます (キー無しでは同じネットワークの誰でも発注系 endpoint を叩けます)。",
    "  3. bridge の環境で `uv sync` → `uv run python server.py`。",
    "  4. afx を動かすマシンから `curl <URL>/health` で `\"mt5_connected\": true` を確認する。",
    "  5. bridge にキーを設定したなら、afx 側の `.env` に `MT5_BRIDGE_API_KEY=<同じ値>` を書く。",
    "  6. `afx init` を再実行し、価格源で「使う・動いている」を選ぶ (接続試験が記録される)。",
    "  7. 履歴を取り込む: `uv run afx history import --source mt5 --symbol USDJPY --from <開始日> --to <終了日> --interval 5m`。",
    "この手順は `afx ctl status` と起動画面にも出し続けます。",
)
_ATTENTION = (
    ("paper.starting_balance", "初期残高を変えると、過去の残高・HWM・kill switch 判定の基準が動きます。"),
    ("account_currency", "口座通貨を変えると、過去の損益と通貨が混ざります。"),
    ("datafeed.yfinance.enabled", "価格源を変えると、過去の足と新しい足の出どころが混ざります。"),
    ("datafeed.mt5.enabled", "価格源を変えると、過去の足と新しい足の出どころが混ざります。"),
    ("datafeed.twelvedata.enabled", "価格源を変えると、過去の足と新しい足の出どころが混ざります。"),
    ("datafeed.decision_timeframes", "判断足を変えると、配備済みの strategy の足と食い違うと signal が効かなくなります。"),
    ("backtest.", "backtest の設定を変えると、過去の審査結果と新しい審査結果が比べられなくなります。"),
)


# ---------------------------------------------------------------------------
# 入出力と注入口
# ---------------------------------------------------------------------------


class WizardIO(Protocol):
    """ウィザードの入出力。秘密を扱う口は持たない。"""

    def isatty(self) -> bool: ...
    def ask(self, prompt: str) -> str: ...
    def say(self, text: str) -> None: ...


class ConsoleWizardIO:
    def isatty(self) -> bool:
        try:
            return sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    def ask(self, prompt: str) -> str:
        return input(prompt)

    def say(self, text: str) -> None:
        print(text)


@dataclass(frozen=True)
class IndicatorOutcome:
    installed: int


@dataclass(frozen=True)
class WizardContext:
    root: Path
    settings: Settings
    io: WizardIO
    deps: "WizardDeps"
    lease: SetupLease
    now: datetime


class _IndicatorIOAdapter:
    def __init__(self, io: WizardIO) -> None:
        self._io = io

    def ask(self, label: str, default: str | None = None) -> str | None:
        prompt = f"{label} [{default}]: " if default is not None else label
        try:
            answer = self._io.ask(prompt)
        except EOFError:
            raise _Interrupted(RC_STOPPED) from None
        return answer or default

    def stdout(self, text: str) -> None:
        self._io.say(text)


def default_indicator_stage(ctx: WizardContext) -> IndicatorOutcome:
    db_path = ctx.root / "data" / "agentic.db"
    if not db_path.is_file():
        return IndicatorOutcome(0)
    conn = connect(db_path)
    try:
        outcomes = run_indicator_stage(
            _IndicatorIOAdapter(ctx.io), ctx.root, conn, settings=ctx.settings,
            landlock_available=ctx.deps.landlock_available, now=ctx.now)
        values = indicators_record_values(outcomes, deployed=deployed_names(ctx.root))
        return IndicatorOutcome(values["installed"])
    finally:
        conn.close()


def _local_timezone_name() -> str | None:
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    if marker in target:
        return target.split(marker, 1)[1] or None
    return None


def _default_deployed_plugins(root: Path, conn: sqlite3.Connection, settings: Settings):
    from agentic_fx.tools import plugin_loader
    plugins_dir = root / "plugins"
    if not plugins_dir.is_dir():
        return ()
    try:
        return plugin_loader.approved_plugins(conn, plugins_dir, settings=settings).inventory.metas
    except Exception:  # noqa: BLE001 — 表示専用。読めなければ「無い」として扱う
        return ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WizardDeps:
    """外部との境界。既定は本物で、テストが差し替える。"""

    fetch: Callable[..., Any] = httpx.get
    run: Callable[..., Any] = subprocess.run
    which: Callable[[str], str | None] = shutil.which
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    now: Callable[[], datetime] = _utc_now
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    unit_dir: Path | None = None
    landlock_available: Callable[[], bool] = landlock.is_available
    landlock_abi: Callable[[], int] = landlock.landlock_abi
    find_spec: Callable[[str], Any] | None = None
    local_timezone: Callable[[], str | None] = _local_timezone_name
    yf_quote: Callable[[str], Any] = sources.yf_quote
    resolve_ops: Callable[[Path], post_start.OpsPrecheck] = post_start.precheck_ops
    place_unit: Callable[..., unit_module.PlacementResult] = unit_module.place_unit
    enable_linger: Callable[..., unit_module.LingerResult] = unit_module.enable_linger
    handoff_and_enable: Callable[..., list[post_start.FeatureOutcome]] = post_start.handoff_and_enable
    enable_and_start: Callable[..., post_start.StartOutcome] = post_start.enable_and_start
    read_admission_line: Callable[..., Any] = post_start.read_admission_line
    enable: Callable[..., EnableResult] = interactive_enable
    indicator_stage: Callable[[WizardContext], IndicatorOutcome] | None = default_indicator_stage
    deployed_plugins: Callable[..., Any] = _default_deployed_plugins
    hook: Hook | None = None
    api_status: Callable[[], Any] | None = None
    ops_ready: Callable[[str], bool] | None = None


# ---------------------------------------------------------------------------
# 中断と対話の部品
# ---------------------------------------------------------------------------


class _Exit(Exception):
    """表示済みの理由で終了する。"""

    def __init__(self, rc: int) -> None:
        super().__init__(rc)
        self.rc = rc


class _Interrupted(Exception):
    def __init__(self, rc: int) -> None:
        super().__init__(rc)
        self.rc = rc


class _Back(Exception):
    """段 9 の検査が失敗し、回答のやり直しが要る段 (質問段の添字) へ戻る。"""

    def __init__(self, stage: int) -> None:
        super().__init__(stage)
        self.stage = stage


@dataclass
class _Session:
    root: Path
    io: WizardIO
    deps: WizardDeps
    lease: SetupLease | None = None
    phase: str = "collect"  # collect | commit | committed
    written: list[str] = field(default_factory=list)
    landlock_ok: bool = True

    def say(self, text: str = "") -> None:
        self.io.say(text)

    def now(self) -> datetime:
        return self.deps.now().astimezone(timezone.utc)

    def now_iso(self) -> str:
        return self.now().isoformat()

    def hook(self, name: str) -> None:
        if self.deps.hook is not None:
            self.deps.hook(name)

    def ask(self, prompt: str, *, help_text: str | None = None, allow_quit: bool = True) -> str:
        while True:
            try:
                raw = self.io.ask(prompt)
            except EOFError:
                raise _Interrupted(RC_STOPPED) from None
            except KeyboardInterrupt:
                raise _Interrupted(RC_INTERRUPT) from None
            text = raw.strip()
            if allow_quit and text.lower() == "q":
                raise _Interrupted(RC_STOPPED)
            if text == "?" and help_text:
                self.say(help_text)
                continue
            return text

    def text(self, prompt: str, default: str | None = None, *, help_text: str | None = None) -> str:
        suffix = f" [{default}]" if default not in (None, "") else ""
        answer = self.ask(f"{prompt}{suffix}: ", help_text=help_text)
        return answer or (default or "")

    def yesno(self, prompt: str, default: bool) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            answer = self.ask(f"{prompt} {suffix} ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("y か n で答えてください。")

    def choice(self, prompt: str, keys: Sequence[str], default: str | None,
               *, help_text: str | None = None) -> str:
        suffix = f" [既定: {default}]" if default else ""
        while True:
            answer = self.ask(f"{prompt}{suffix}: ", help_text=help_text).lower()
            if not answer and default:
                return default
            if answer in keys:
                return answer
            self.say(f"{' / '.join(keys)} のどれかで答えてください。")

    def typed_yes(self, prompt: str) -> None:
        while True:
            if self.ask(f"{prompt} ('yes' と打ち込んでください。やめるときは q): ") == "yes":
                return
            self.say("Enter や y では通りません。")


def _one_line(value: object, limit: int = 120) -> str:
    text = " ".join(safe_error_text(value).split()) if not isinstance(value, str) else " ".join(value.split())
    return text[:limit]


def _check_record(result: CheckResult) -> dict[str, str]:
    data = {"result": result.result, "at": result.at.isoformat()}
    if result.reason_code is not None:
        data["reason_code"] = result.reason_code.value
    return data


def _plain_record(result: str, at: str, reason: str | None = None) -> dict[str, str]:
    data = {"result": result, "at": at}
    if reason is not None:
        data["reason_code"] = reason
    return data


def _sha256(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(raw).hexdigest()


def _get_path(raw: Any, dotted: str) -> Any:
    value = raw
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


@contextlib.contextmanager
def _lenient_env():
    """設定の検証が見る環境変数のうち、キーの存在だけを要るもの (Twelve Data) を一時的に満たす。

    ダミー値を検証の間だけ置いて戻す。実際のキーの有無は価格源を選ぶ段で `.env` と環境から確かめ、
    起動時の検査が最終判断をする。子 process には渡さない (検証の間だけ)。"""
    name = "TWELVEDATA_API_KEY"
    if os.environ.get(name):
        yield
        return
    os.environ[name] = "-"
    try:
        yield
    finally:
        os.environ.pop(name, None)


def _load_settings(text: str) -> Settings:
    with _lenient_env():
        return load_settings_text(text)


def _edit(text: str, changes: dict[str, Any]):
    with _lenient_env():
        return edit_settings(text, changes)


def _url_problem(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "URL の形が正しくありません。"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return ("URL に認証情報を入れないでください。API キーは `.env` に書きます "
                "(値はここでは入力しません)。")
    if parts.scheme not in ("http", "https") or not host:
        return "http:// か https:// で始まる URL を入力してください。"
    return None


# ---------------------------------------------------------------------------
# 回答と、設定・記録への写し (純関数)
# ---------------------------------------------------------------------------


@dataclass
class Answers:
    display_timezone: str
    account_currency: str
    pairs: list[str]
    starting_balance: int | float
    source: str = "mt5"
    bridge_url: str | None = None
    bridge_key: str = "unknown"
    price: dict[str, str] = field(default_factory=dict)
    bridge_plan: str = "planned"
    eval_source: str = "none"
    base_interval: str | None = None
    trade_backend: str | None = None
    trade_model: str | None = None
    improve_backend: str | None = None
    improve_model: str | None = None
    llama_base_url: str | None = None
    context_limit: int | None = None
    codex_bin: str | None = None
    claude_bin: str | None = None
    landlock: dict[str, str] = field(default_factory=dict)
    llm_trade: dict[str, str] = field(default_factory=dict)
    llm_improve: dict[str, str] = field(default_factory=dict)
    run_verify_improve: bool = False
    timeframe: str = "1h"
    cadence: str | None = None
    improve_at: str | None = None
    wait_first: bool = False
    api_enabled: bool = True
    daemon: bool = True


_SOURCE_KEYS = ("yfinance", "mt5", "twelvedata")


def build_changes(raw: Mapping[str, Any], a: Answers) -> dict[str, Any]:
    """回答のうち現在の値と違うものだけを settings の変更にする。"""
    changes: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if _get_path(raw, key) != value or type(_get_path(raw, key)) is not type(value):
            changes[key] = value

    put("display_timezone", a.display_timezone)
    put("account_currency", a.account_currency)
    put("pairs", list(a.pairs))
    put("paper.starting_balance", a.starting_balance)
    for name in _SOURCE_KEYS:
        put(f"datafeed.{name}.enabled", name == a.source)
    if a.source == "mt5" and a.bridge_url is not None:
        put("datafeed.mt5.bridge_url", a.bridge_url)
    put("backtest.eval_source", a.eval_source)
    if a.base_interval is not None:
        put("backtest.base_interval", a.base_interval)
    if a.trade_backend is not None:
        put("runner.trade.backend", a.trade_backend)
        if a.trade_model is not None:
            put("runner.trade.model", a.trade_model)
    if a.improve_backend is not None:
        put("runner.improve.backend", a.improve_backend)
        if a.improve_model is not None:
            put("runner.improve.model", a.improve_model)
    if a.llama_base_url is not None:
        put("llama_swap.base_url", a.llama_base_url)
    if a.context_limit is not None:
        put("runner.opencode.context_limit", a.context_limit)
    if a.codex_bin is not None:
        put("runner.codex.bin", a.codex_bin)
    if a.claude_bin is not None:
        put("runner.claude.bin", a.claude_bin)
    intervals = _get_path(raw, "datafeed.intervals")
    if isinstance(intervals, list) and a.timeframe not in intervals:
        put("datafeed.intervals", [*intervals, a.timeframe])
    put("datafeed.decision_timeframes", [a.timeframe])
    if a.cadence is not None:
        put("schedule.improve", a.cadence)
    if a.improve_at is not None:
        put("schedule.improve_at", a.improve_at)
    put("api.enabled", a.api_enabled)
    return changes


_DEFAULT_RECORD_TAIL: dict[str, Any] = {
    "indicators": {"installed": 0},
    "ops": {},
    "isolation": {"admitted": "unknown"},
    "linger": {"state": "not_checked"},
    "start": {"result": "skipped"},
    "unit": "not_installed",
}


def build_record(a: Answers, *, now_iso: str, previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """確定 1 で書く確認記録。前回の記録がある再実行では、この実行が決めない項目 (unit・ops・
    linger・指標の本数) を引き継ぐ。"""
    carried = {key: (previous[key] if previous is not None and key in previous else value)
               for key, value in _DEFAULT_RECORD_TAIL.items()}
    carried["isolation"] = {"admitted": "unknown"}
    carried["start"] = {"result": "skipped"}
    prior_api = (previous or {}).get("api") if previous is not None else None
    api_result = "unchecked"
    if isinstance(prior_api, Mapping) and prior_api.get("enabled") == a.api_enabled:
        api_result = prior_api.get("result", "unchecked")
        if api_result not in ("ok", "failed", "disabled", "unchecked"):
            api_result = "unchecked"
    return {
        "version": 1,
        "completed_at": now_iso,
        "mode": "interactive",
        "settings_schema_version": CURRENT_SCHEMA_VERSION,
        "price": a.price,
        "llm_trade": a.llm_trade,
        "llm_improve": a.llm_improve,
        "landlock": a.landlock,
        "bridge_plan": a.bridge_plan,
        "history": {"source": a.eval_source},
        "api": {"enabled": a.api_enabled, "result": api_result},
        **carried,
    }


def next_improve_occurrence(settings: Settings, now: datetime) -> datetime:
    """`now` より後で最初の改善予定時刻 (供給側の関数だけで求める)。"""
    cadence = settings.schedule.improve
    for days in range(1, 9):
        occurrence = latest_scheduled_occurrence(
            now + timedelta(days=days), cadence=cadence, at=settings.schedule.improve_at,
            display_timezone=settings.display_timezone)
        if occurrence > now:
            return occurrence
    raise ValueError("next improve occurrence not found")


def _format_occurrence(occurrence: datetime) -> str:
    weekday = "月火水木金土日"[occurrence.weekday()]
    return f"{occurrence:%Y-%m-%d} ({weekday}) {occurrence:%H:%M} {occurrence.tzname()}"


def _cross_quote_pairs(pairs: Sequence[str], currency: str) -> list[str]:
    return [pair for pair in pairs if pair[3:] != currency]


# ---------------------------------------------------------------------------
# 段 0: 前提確認
# ---------------------------------------------------------------------------


@dataclass
class _Start:
    """段 0 の結果。"""

    mode: str  # fresh | rerun | handoff | commit_tail
    base_text: str
    base_settings: Settings
    original_text: str | None
    migrated: bool
    prior_marker: SetupMarker | None
    landlock_ok: bool
    improve_isolation_ok: bool
    landlock_record: dict[str, str]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None


def _db_has_schema(db_path: Path) -> bool:
    if not db_path.is_file():
        return False
    try:
        conn = connect_readonly(db_path)
    except Exception:  # noqa: BLE001
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_sources'").fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _commit_is_complete(root: Path, marker: SetupMarker) -> bool:
    """前回の確定が印を消す所だけを残して終わっているか。"""
    settings_text = _read_text(root / "config" / "settings.yaml")
    if settings_text is None or marker.settings_sha256 != _sha256(settings_text):
        return False
    if not _db_has_schema(root / "data" / "agentic.db"):
        return False
    try:
        if not service._state_store(root).load().initialized:
            return False
    except Exception:  # noqa: BLE001
        return False
    return read_setup_record(root).state == "ok"


def _stage0(sess: _Session) -> _Start:
    root, deps = sess.root, sess.deps
    example = root / "config" / "settings.yaml.example"
    if not example.is_file():
        sess.say(_NOT_REPO_MESSAGE)
        raise _Exit(RC_USAGE)
    if not sess.io.isatty():
        sess.say(NON_TTY_MESSAGE)
        raise _Exit(RC_USAGE)
    sess.say("agentic-fx の初期設定を始めます。途中でやめても、段 10 で確定するまでは何も書きません。")
    try:
        sess.lease = acquire_setup_lease(root, hook=deps.hook)
    except SetupLeaseBusy:
        sess.say(_SETUP_BUSY_MESSAGE)
        raise _Exit(RC_USAGE) from None
    except SetupLeaseUnsafe:
        sess.say("data/ops の権限が安全ではありません (自分以外が書ける、または他人の所有です)。"
                 "権限を直してから再実行してください。設定・DB は変更していません。")
        raise _Exit(RC_STOPPED) from None
    try:
        sess.lease.acquire_instance_lock(hook=deps.hook)
    except InstanceAlreadyRunning:
        sess.say(_INSTANCE_BUSY_MESSAGE)
        raise _Exit(RC_USAGE) from None
    if enroll_marker_present(root):
        sess.say(_ENROLL_MESSAGE)
        raise _Exit(RC_USAGE)
    landlock_ok = _safe_bool(deps.landlock_available)
    abi = _safe_int(deps.landlock_abi)
    improve_isolation_ok = landlock_ok and abi >= improve_admission.MIN_LANDLOCK_ABI
    landlock_record = _plain_record(
        "ok" if landlock_ok else "failed", sess.now_iso(),
        "landlock_abi_too_old" if landlock_ok and not improve_isolation_ok
        else ("ok" if landlock_ok else "sandbox_unavailable"))
    if not landlock_ok:
        sess.say("この環境では Landlock が使えません (x86_64 かつ ABI 3 以上が必要)。"
                 "標準指標の導入と改善ループは使えません。取引判断のペーパー運用だけ設定できます。")
        if not sess.yesno("続けますか", False):
            sess.say(_NOTHING_CHANGED)
            raise _Exit(RC_STOPPED)
    elif not improve_isolation_ok:
        sess.say(
            f"この kernel (Landlock ABI {abi}) では改善ループの隔離が使えません "
            f"(ABI {improve_admission.MIN_LANDLOCK_ABI} 以上、Linux 6.18 以降が必要)。"
            "取引判断と標準指標は設定できます。改善は kernel を更新するまで止まります。")
    pytest_check = checks.check_pytest(**({"find_spec": deps.find_spec} if deps.find_spec else {}),
                                       now=deps.now)
    if pytest_check.result != "ok":
        sess.say("plugin の採用審査 (gate) が pytest を使います。`uv sync` (dev 依存を含む既定) で入ります。")

    settings_path = root / "config" / "settings.yaml"
    original = _read_text_or_exit(sess, settings_path)
    migrated = False
    if original is None:
        base_text = example.read_text(encoding="utf-8")
        mode = "fresh"
        sess.say("初めての設定です。")
    else:
        mode = "rerun"
        sess.say("再実行です。現在の値を既定として表示します。")
        for key, lines in find_duplicate_keys(original).items():
            sess.say(f"settings.yaml の {key} が 2 回書かれています (行 {' と '.join(map(str, lines))})。"
                     "現在は後の行の値が効いています。どちらかを消してから再実行してください。")
            raise _Exit(RC_STOPPED)
        result = migrate(original)
        if not result.ok:
            sess.say(result.reason)
            raise _Exit(RC_STOPPED)
        base_text = result.text or original
        migrated = base_text != original
        if migrated:
            sess.say("settings.yaml の版が古いため、段 9 の要約に移行する行を出します。")
    try:
        base_settings = _load_settings(base_text)
    except Exception as error:  # noqa: BLE001 — 設定の検証失敗は理由を見せて止める
        sess.say(f"settings の検証に失敗しました: {_one_line(error, 400)}。手で直してから再実行してください。")
        raise _Exit(RC_STOPPED) from None
    sess.say(f"Python {sys.version_info.major}.{sys.version_info.minor} / {sys.platform}")

    prior: SetupMarker | None = None
    if setup_marker_present(root):
        prior = read_setup_marker(root)
        sess.say("前回の初期設定が途中で止まっています。このまま続けて収束させます "
                 "(サービスはこの印がある間、起動を拒否します)。")
        if (prior is not None and prior.phase in {"commit", "handoff"} and prior.settings_sha256
                and original is not None and _commit_is_complete(root, prior)):
            mode = "handoff" if prior.phase == "handoff" else "commit_tail"
        else:
            sess.say("前回の引き渡しの記録が不完全です。最初からやり直します (印は残します)。")
            prior = None
    return _Start(mode, base_text, base_settings, original, migrated, prior, landlock_ok,
                  improve_isolation_ok, landlock_record)


def _read_text_or_exit(sess: _Session, path: Path) -> str | None:
    try:
        return _read_text(path)
    except (OSError, UnicodeDecodeError):
        sess.say("config/settings.yaml を読めません (通常ファイルで UTF-8 のものだけ扱えます)。")
        raise _Exit(RC_STOPPED) from None


def _safe_bool(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001 — 検出に失敗したら使えない側に倒す
        return False


def _safe_int(fn: Callable[[], int]) -> int:
    try:
        value = fn()
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    except Exception:  # noqa: BLE001 — 検出に失敗したら使えない側に倒す
        return 0


# ---------------------------------------------------------------------------
# 段 1〜8: 集める層 (何も書かない)
# ---------------------------------------------------------------------------

_HELP_STAGE = {
    2: "表示タイムゾーンは改善の時刻 (improve_at) の解釈にも使われます。口座通貨と初期残高は後から変えると過去の損益と混ざります。",
    3: "取引判断と約定判定に使う価格の取得元です。どれを選んでも後の振る舞いが大きく違うので、既定は置いていません。",
    4: "取引判断と改善にそれぞれ LLM を割り当てます。claude / codex はサブスクの枠を使います (従量課金 API ではありません)。",
    5: "1 回の判断が判断足の幅より短い時間で終わらないと、1 足に 1 回の判断が成立しません。",
    7: "改善は予定時刻に 1 回ずつ走ります。最初の 1 本を待つ場合、方針文と最初の課題を先に入れられます。",
    8: "操作 API は同じマシンの UNIX ソケットだけで受け付けます。無効にすると `afx ctl` が使えません。",
}


def _initial_answers(sess: _Session, st: _Start) -> Answers:
    s = st.base_settings
    balance = s.paper.starting_balance
    zone = s.display_timezone
    if st.mode == "fresh":
        guess = sess.deps.local_timezone() or "UTC"
        try:
            ZoneInfo(guess)
            zone = guess
        except (ZoneInfoNotFoundError, ValueError, OSError):
            zone = "UTC"
    unit_dir = sess.deps.unit_dir or unit_module.default_unit_dir(sess.deps.environ)
    daemon = True if st.mode == "fresh" else (s.api.enabled or (unit_dir / "afx.service").is_file())
    return Answers(
        display_timezone=zone, account_currency=s.account_currency,
        pairs=list(s.pairs), starting_balance=balance, timeframe=s.datafeed.decision_timeframe,
        api_enabled=s.api.enabled, daemon=daemon, landlock=dict(st.landlock_record))


def _stage1(sess: _Session, st: _Start) -> None:
    if st.mode == "fresh":
        sess.say("")
        sess.say("[段 1 はじめに]")
        sess.say("これは「使う人が育てる」システムです。すぐに取引できる、すぐに勝てるものではありません。")
        sess.say("まずペーパー (実資金を動かさない) で動かします。発注・損切り・資金保護は LLM ではなく決められたコードが行い、"
                 "drawdown kill switch は設定で切れません。")
        sess.say("戦略や指標の採用は必ずあなたの明示確認を経ます。改善ループは LLM を使うので、"
                 "CLI backend (claude / codex) を選ぶとサブスクの枠を使います。")
        sess.ask("読みました [Enter]: ")
    else:
        sess.say("[段 1] 再実行です。各段の既定値は現在の settings.yaml の値です。")


def _parse_balance(text: str) -> int | float | None:
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return None
    if value <= 0 or value != value or value in (float("inf"),):
        return None
    return int(value) if value == int(value) else value


def _stage2(sess: _Session, st: _Start, a: Answers) -> None:
    sess.say("")
    sess.say("[段 2 基本] 表示・口座・対象ペア・初期残高 (まだ何も書きません)")
    help_text = _HELP_STAGE[2]
    while True:
        zone = sess.text("表示タイムゾーン (例 Asia/Tokyo)", a.display_timezone, help_text=help_text)
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            sess.say("'JST' のような略称は使えません。'Asia/Tokyo' のような地域名で入力してください。")
            continue
        a.display_timezone = zone
        break
    while True:
        currency = sess.text("口座通貨 (後から変えると過去の損益と通貨が混ざります)", a.account_currency,
                             help_text=help_text)
        if re.fullmatch(r"[A-Z]{3}", currency):
            a.account_currency = currency
            break
        sess.say("3 文字の大文字で入力してください (JPY, USD ...)。")
    allowed = sorted(st.base_settings.risk.pair_rules)
    while True:
        answer = sess.text(f"対象ペア (選べるもの: {', '.join(allowed)}。複数はカンマ区切り)",
                           ",".join(a.pairs), help_text=help_text)
        pairs = [part for part in re.split(r"[,\s]+", answer) if part]
        if pairs and all(pair in allowed for pair in pairs) and len(set(pairs)) == len(pairs):
            a.pairs = pairs
            break
        sess.say(f"{', '.join(allowed)} の中から重複なく選んでください。")
    if len(a.pairs) > 1:
        sess.say("複数ペアを選びました。運用実績は USDJPY のみです。")
    crossing = _cross_quote_pairs(a.pairs, a.account_currency)
    if crossing:
        sess.say(f"{', '.join(crossing)} はクォート通貨が口座通貨と違うため、換算を通ります。")
    while True:
        answer = sess.text("初期残高 (ペーパー。後から変えると過去の残高と HWM の基準が動きます)",
                           str(a.starting_balance), help_text=help_text)
        balance = _parse_balance(answer)
        if balance is not None:
            a.starting_balance = balance
            break
        sess.say("正の数で入力してください。")


def _env_value(root: Path, environ: Mapping[str, str], key: str) -> str | None:
    if environ.get(key):
        return environ[key]
    try:
        from dotenv import dotenv_values
        value = dotenv_values(root / ".env").get(key)
    except Exception:  # noqa: BLE001 — .env が読めなくても試験を続ける
        return None
    return value or None


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _may_send_bridge_key(url: str) -> bool:
    """API 鍵のヘッダを送ってよい宛先か (https か、この端末の http だけ)。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS)


def _bridge_check(sess: _Session, a: Answers, url: str) -> CheckResult:
    headers: dict[str, str] = {}
    if a.bridge_key == "yes":
        if _may_send_bridge_key(url):
            key = _env_value(sess.root, sess.deps.environ, "MT5_BRIDGE_API_KEY")
            if key:
                headers["X-Bridge-Api-Key"] = key
        else:
            sess.say("平文 http には鍵を送りません。鍵なしで /health を試します。")
    return checks.check_bridge(url, a.pairs[0], fetch=sess.deps.fetch, headers=headers,
                               now=sess.deps.now)


def _stage3(sess: _Session, st: _Start, a: Answers) -> None:
    sess.say("")
    sess.say("[段 3 価格源] 取引判断と約定判定に使う価格をどこから取るかを決めます。")
    sess.say("本線は MT5 bridge (MetaTrader 5 から価格を受け取る小さな HTTP サーバ) です。")
    sess.say("  1. 使う。bridge は動いている")
    sess.say("  2. 使う予定。まだ準備していない")
    sess.say("  3. 使わない (yfinance などを使う)")
    settings = st.base_settings
    default = None
    if st.mode != "fresh":
        default = "1" if settings.datafeed.mt5.enabled else "3"
    branch = sess.choice("価格源", ("1", "2", "3"), default, help_text=_HELP_STAGE[3])
    default_url = settings.datafeed.mt5.bridge_url or "http://localhost:8812"
    now_iso = sess.now_iso()
    if branch == "3":
        _stage3_without_bridge(sess, a)
        return
    a.source = "mt5"
    a.eval_source = "mt5"
    if branch == "2":
        for line in _BRIDGE_GUIDE:
            sess.say(line)
    while True:
        url = sess.text("bridge の URL", a.bridge_url or default_url)
        problem = _url_problem(url)
        if problem is None:
            break
        sess.say(problem)
    a.bridge_url = url
    if branch == "2":
        a.price = _plain_record("planned", now_iso)
        a.bridge_plan = "planned"
        if sess.yesno("今 bridge への接続を試しますか", False):
            a.bridge_key = _ask_bridge_key(sess)
            result = _bridge_check(sess, a, url)
            if result.result == "ok":
                sess.say("MT5 bridge OK")
                a.price = _check_record(result)
                a.bridge_plan = "connected"
            else:
                sess.say(_PRICE_FAILURE_TEXT.get(
                    result.reason_code.value if result.reason_code else "", "接続できませんでした。"))
        return
    a.bridge_key = _ask_bridge_key(sess)
    while True:
        result = _bridge_check(sess, a, url)
        if result.result == "ok":
            server_time = result.params.get("server_time", "")
            sess.say(f"MT5 bridge OK: {a.pairs[0]} (bridge の時刻 {server_time})")
            a.price = _check_record(result)
            a.bridge_plan = "connected"
            return
        reason = result.reason_code.value if result.reason_code else "connect_error"
        if (not _may_send_bridge_key(url) and reason == checks.CheckReason.AUTH_REQUIRED.value):
            a.price = _plain_record("unverified", sess.now_iso(), checks.CheckReason.KEY_WITHHELD_PLAIN_HTTP.value)
            a.bridge_plan = "connected"
            sess.say("bridge は応答しています。平文 http には鍵を送らないため接続の検査はここでは行いません。"
                     "daemon は `.env` の鍵で接続します。")
            if not _env_value(sess.root, sess.deps.environ, "MT5_BRIDGE_API_KEY"):
                sess.say("`.env` に MT5_BRIDGE_API_KEY がありません。段 8 で確認します")
            return
        sess.say(_PRICE_FAILURE_TEXT.get(reason, "接続できませんでした。"))
        action = sess.choice("[r] 再試行 / [2] 『使う予定・未準備』として進む / [u] URL を入れ直す",
                             ("r", "2", "u"), None)
        if action == "r":
            continue
        if action == "2":
            a.price = _plain_record("planned", now_iso)
            a.bridge_plan = "planned"
            return
        while True:
            url = sess.text("bridge の URL", url)
            problem = _url_problem(url)
            if problem is None:
                break
            sess.say(problem)
        a.bridge_url = url


def _ask_bridge_key(sess: _Session) -> str:
    answer = sess.choice("bridge 側で API キーを設定しましたか (1 はい / 2 いいえ / 3 分からない)",
                         ("1", "2", "3"), "3")
    return {"1": "yes", "2": "no", "3": "unknown"}[answer]


def _stage3_without_bridge(sess: _Session, a: Answers) -> None:
    a.bridge_plan = "unused"
    a.eval_source = "none"
    a.bridge_url = None
    now_iso = sess.now_iso()
    sess.say("  1. yfinance (既定)")
    sess.say("  2. Twelve Data (API キーが要ります)")
    while True:
        pick = sess.choice("価格源", ("1", "2"), "1")
        if pick == "2":
            env = checks.check_env_keys(sess.root / ".env", ["TWELVEDATA_API_KEY"],
                                        environ=sess.deps.environ, now=sess.deps.now)
            if env.result != "ok":
                sess.say("TWELVEDATA_API_KEY が `.env` にも環境にもありません。キーを `.env` に書いてから"
                         "選び直すか、yfinance を選んでください (値はここでは入力しません)。")
                continue
            sess.say("Twelve Data の無料枠の使い方 (外向きリクエストの予算) はまだ設計されていません。")
            a.source = "twelvedata"
            a.price = _plain_record("unverified", now_iso)
            return
        a.source = "yfinance"
        break
    sess.say("yfinance は週明けの開場直後 (日曜 21:00〜23:00 UTC ごろの足が来ない) に、毎週しばらく取引判断を止めます "
             "(degraded)。既知の問題です。")
    if not sess.yesno(f"yfinance に {a.pairs[0]} の価格を 1 回問い合わせて確認します。実行しますか", True):
        a.price = _plain_record("unverified", now_iso, "user_declined_probe")
        return
    try:
        sess.deps.yf_quote(a.pairs[0])
    except Exception as error:  # noqa: BLE001 — 外部取得の失敗は完了を妨げない
        sess.say(f"yfinance の確認に失敗しました ({_one_line(safe_error_text(error))})。後で再確認できます。")
        a.price = _plain_record("failed", sess.now_iso(), "connect_error")
        return
    sess.say("yfinance OK")
    a.price = _plain_record("ok", sess.now_iso())


class _LaterChosen(Exception):
    pass


def _safe_model_name(name: str) -> str:
    return _one_line(safe_text(name), 80)


# claude は一覧を取れる API が無いので別名の固定表 (別名, 一言)。値は `claude --model` がそのまま受ける。
CLAUDE_MODEL_ALIASES: tuple[tuple[str, str], ...] = (
    ("sonnet", "標準"),
    ("opus", "高性能"),
    ("fable", "別系統"),
    ("haiku", "軽量"),
)
CODEX_FALLBACK_MODEL = "gpt-5.6-terra"
_MODEL_PICK_LIMIT = 10
_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")


def _manual_model(sess: _Session, label: str, default: str | None) -> str:
    while True:
        name = sess.text(f"{label}のモデル名", default)
        if _MODEL_NAME_RE.fullmatch(name):
            return name
        sess.say("モデル名は英数字と . _ : / @ + - だけで入力してください。")


def _choose_model(sess: _Session, label: str, entries: Sequence[tuple[str, str]],
                  default: str | None, *, manual_default: str | None = None) -> str:
    """(識別子, 一言) の一覧から番号で選ばせる。一覧が空なら手入力だけ。`m` でも手入力できる。"""
    if not entries:
        return _manual_model(sess, label, manual_default)
    shown = list(entries)
    if len(shown) > _MODEL_PICK_LIMIT:
        while True:
            query = sess.text(f"{label}のモデルの絞り込み (部分一致。空 Enter で全件)").lower()
            if not query:
                break
            matched = [entry for entry in shown if query in entry[0].lower() or query in entry[1].lower()]
            if matched:
                shown = matched
                break
            sess.say("該当するモデルがありません。")
    if any("/" in name for name, _ in shown):
        groups: dict[str, list[tuple[str, str]]] = {}
        for entry in shown:
            groups.setdefault(entry[0].partition("/")[0] if "/" in entry[0] else "", []).append(entry)
        shown = [entry for group in groups.values() for entry in group]
    heading: str | None = None
    for index, (name, note) in enumerate(shown, 1):
        provider = name.partition("/")[0] if "/" in name else ""
        if provider and provider != heading:
            sess.say(f"  [{_safe_model_name(provider)}]")
        heading = provider
        sess.say(f"  {index}. {_safe_model_name(name)}" + (f" ({note})" if note else ""))
    names = [name for name, _ in shown]
    default_number = str(names.index(default) + 1) if default in names else None
    while True:
        answer = sess.text(f"{label}のモデル (番号 / m = 手入力)", default_number)
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        if answer.lower() == "m":
            return _manual_model(sess, label, manual_default)
        if answer in names:
            return answer
        sess.say("モデルを選んでください。" if not answer else "一覧の番号か m で答えてください。")


def _codex_model(sess: _Session, a: Answers, configured_bin: str, cli_ok: bool) -> bool:
    """改善に使う codex のモデルを決める。一覧を取れなかったときだけ False (手入力に回した)。"""
    deps = sess.deps
    ids: list[str] = []
    listed = True
    if cli_ok:
        bin_value = a.codex_bin or configured_bin
        listing = checks.list_codex_models(
            bin_path=checks.cli_candidate(bin_value, which=deps.which) or bin_value,
            run=deps.run, now=deps.now)
        ids = list(listing.params.get("models", ())) if listing.result == "ok" else []
        listed = listing.result == "ok"
        if not listed:
            sess.say("codex のモデル一覧を取得できませんでした (model_list_unavailable)。モデル名を手入力します。")
    default = next((name for name in ids if "terra" in name.lower()), None) if ids else CODEX_FALLBACK_MODEL
    a.improve_model = _choose_model(sess, "改善", [(name, "") for name in ids], default,
                                    manual_default=CODEX_FALLBACK_MODEL)
    return listed


def _opencode_model(sess: _Session, st: _Start, a: Answers) -> bool:
    """改善に使う opencode のモデルを決める (既定なし)。一覧を取れなかったときだけ False。"""
    deps = sess.deps
    bin_value = str(Path(st.base_settings.runner.opencode.bin).expanduser())
    listing = checks.list_opencode_models(
        bin_path=checks.cli_candidate(bin_value, which=deps.which) or bin_value,
        run=deps.run, now=deps.now)
    ids = list(listing.params.get("models", ())) if listing.result == "ok" else []
    if not ids:
        sess.say("opencode のモデル一覧を取得できませんでした (model_list_unavailable)。モデル名を手入力します。")
    a.improve_model = _choose_model(sess, "改善", [(name, "") for name in ids], None)
    return bool(ids)


def _select_model(sess: _Session, label: str, models: Sequence[str], current: str | None) -> str:
    default = str(models.index(current) + 1) if current in models else None
    while True:
        answer = sess.text(f"{label}のモデルの番号", default)
        if answer.isdigit() and 1 <= int(answer) <= len(models):
            return models[int(answer) - 1]
        if answer in models:
            return answer
        sess.say("一覧の番号で答えてください。")


def _query_context_limit(sess: _Session, base_url: str, model: str) -> int | None:
    api_root = base_url.rstrip("/")
    if api_root.endswith("/v1"):
        api_root = api_root[:-3]
    try:
        response = sess.deps.fetch(f"{api_root}/props", params={"model": model}, timeout=120)
        response.raise_for_status()
        value = response.json()["default_generation_settings"]["n_ctx"]
    except Exception:  # noqa: BLE001 — 取得できなければ手入力に回す
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _ask_positive_int(sess: _Session, prompt: str) -> int:
    while True:
        answer = sess.text(prompt)
        if answer.isdigit() and int(answer) > 0:
            return int(answer)
        sess.say("正の整数で入力してください。")


def _local_pair(sess: _Session, st: _Start, a: Answers, url: str, probe: CheckResult) -> None:
    models = list(probe.params.get("models", ()))
    settings = st.base_settings
    a.llama_base_url = url
    a.trade_backend = "local"
    a.improve_backend = "local"
    if models:
        sess.say("llama-swap のモデル一覧 (取得だけで、モデルはロードしません):")
        for index, name in enumerate(models, 1):
            sess.say(f"  {index}. {_safe_model_name(name)}")
        a.trade_model = _select_model(sess, "取引判断", models, settings.runner.trade.model)
    else:
        sess.say("モデル一覧を取得できませんでした。モデル名を手入力します (未確認のまま記録します)。")
        a.trade_model = sess.text("取引判断のモデル名", settings.runner.trade.model)
    record = (_check_record(probe) if models else
              _plain_record("unverified", probe.at.isoformat(),
                            probe.reason_code.value if probe.reason_code else "connect_error"))
    a.llm_trade = dict(record)
    _choose_improve_backend(sess, st, a, url, probe, default_model=a.trade_model)


def _choose_improve_backend(sess: _Session, st: _Start, a: Answers, url: str, probe: CheckResult,
                            *, default_model: str | None = None, default_backend: str | None = None) -> None:
    """改善用 backend とモデルを、取引判断とは別に決める。"""
    settings = st.base_settings
    models = list(probe.params.get("models", ()))
    mapping = {"local": "1", "opencode": "2", "claude": "3", "codex": "4"}
    default = mapping.get(default_backend or settings.runner.improve.backend)
    if st.mode == "fresh" and probe.result == "ok":
        default = "1"
    sess.say("改善の backend: ローカル LLM では opencode が本線で、local は最低線です。")
    sess.say("  1. local")
    sess.say("  2. opencode")
    sess.say("  3. claude")
    sess.say("  4. codex")
    pick = sess.choice("改善の backend", ("1", "2", "3", "4"), default)
    backend = {"1": "local", "2": "opencode", "3": "claude", "4": "codex"}[pick]
    a.improve_backend = backend
    if backend == "local":
        selected_default = (settings.runner.improve.model if settings.runner.improve.model in models
                            else default_model)
        if models:
            a.improve_model = _select_model(sess, "改善", models, selected_default)
        else:
            a.improve_model = sess.text("改善のモデル名", settings.runner.improve.model)
        a.llm_improve = (_check_record(probe) if models else _plain_record(
            "unverified", probe.at.isoformat(), probe.reason_code.value if probe.reason_code else "connect_error"))
        return
    if backend == "opencode":
        listed = _opencode_model(sess, st, a)
        a.llm_improve = (_check_record(probe) if listed else
                         _plain_record("unverified", sess.now_iso(), "model_list_unavailable"))
        sess.say("opencode の context_limit は llama-swap の --ctx-size と同じ値にします。")
        limit = None
        if sess.yesno("llama-swap に問い合わせて取得しますか (モデルがロードされ、数十秒〜数分かかることがあります)",
                      False):
            limit = _query_context_limit(sess, url, a.improve_model)
            if limit is None:
                sess.say("取得できませんでした。手入力してください。")
        a.context_limit = limit if limit is not None else _ask_positive_int(
            sess, "context_limit (llama-swap の --ctx-size と同じ値)")
        sess.say("improve.llama_swap_verified はここでは true にしません。"
                 "`afx check backend --backend opencode` に合格してから手で書いてください。")
        return
    _node_check(sess)
    _secret_env_warning(sess)
    if backend == "claude":
        cfg = settings.runner.claude
        result = _cli_check_loop(sess, "claude", lambda: checks.check_cli_backend(
            "claude", cfg.bin, cfg.credentials_file, which=sess.deps.which, run=sess.deps.run, now=sess.deps.now))
        a.improve_model = _choose_model(sess, "改善", CLAUDE_MODEL_ALIASES, "sonnet")
        a.llm_improve = _check_record(result)
        if result.result == "ok":
            _record_claude_binary(sess, cfg.bin, a)
        if result.result == "ok" and st.improve_isolation_ok:
            a.run_verify_improve = sess.yesno(
                "改善 backend の実往復 (`afx check backend`) を確定の後に試験しますか。サブスクの枠を少し使います", False)
        return
    _improve_cli(sess, st, a, "codex", None)


def _report_cli(sess: _Session, backend: str, result: CheckResult) -> None:
    if result.result == "ok":
        sess.say(f"{backend}: 導入とログインを確認しました。")
        return
    reason = result.reason_code.value if result.reason_code else "unknown"
    if reason == "bin_not_found":
        sess.say(f"{backend} が見つかりません ({reason})。導入手順は {CLI_DOC_SECTION} を見てください。")
    elif reason == "credentials_missing":
        sess.say(f"{backend} にログインしていません ({reason})。端末で `{backend}` を起動してログインしてから"
                 "再試行してください。ここでは入力しません。")
    else:
        sess.say(f"{backend} を確認できませんでした ({reason})。")


def _cli_check_loop(sess: _Session, backend: str, run_check: Callable[[], CheckResult]) -> CheckResult:
    while True:
        result = run_check()
        _report_cli(sess, backend, result)
        if result.result == "ok":
            return result
        action = sess.choice("[r] 導入・ログイン後に再試行 / [c] 未確認のまま進む / [d] 後で設定する",
                             ("r", "c", "d"), "c")
        if action == "r":
            continue
        if action == "d":
            raise _LaterChosen()
        return result


def _secret_env_warning(sess: _Session) -> None:
    names = sorted(name for name in sess.deps.environ
                   if any(pattern in name.upper() for pattern in service._SECRET_ENV_PATTERNS))
    if names:
        sess.say(f"この端末の環境変数 {', '.join(names)} は CLI backend の起動を拒否させます "
                 "(値は表示しません)。unset するか `.env` に移してください。"
                 "unit から起動する場合は unit の環境で判定されます。")


def _node_check(sess: _Session) -> None:
    try:
        completed = sess.deps.run(["node", "--version"], capture_output=True, text=True, timeout=15)
        found = getattr(completed, "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError):
        found = False
    if not found:
        sess.say(f"Node.js が見つかりません (bin_not_found)。claude / codex の導入に必要です。"
                 f"手順は {CLI_DOC_SECTION} を見てください。")


def _claude_trade(sess: _Session, st: _Start, a: Answers) -> CheckResult:
    deps = sess.deps
    cfg = st.base_settings.runner.claude
    result = _cli_check_loop(sess, "claude", lambda: checks.check_cli_backend(
        "claude", cfg.bin, cfg.credentials_file, which=deps.which, run=deps.run, now=deps.now))
    a.trade_backend = "claude"
    a.trade_model = _choose_model(sess, "取引判断", CLAUDE_MODEL_ALIASES, "sonnet")
    if result.result != "ok":
        a.llm_trade = _check_record(result)
        return result
    _record_claude_binary(sess, cfg.bin, a)
    sess.say("取引判断用の `claude -p` が 1 往復できるかを試験できます。サブスクの枠を少し使います。")
    if sess.yesno("trade 用の `claude -p` を 1 往復で試験します。サブスクの枠を少し使います。送りますか", False):
        bin_path = checks.cli_candidate(str(Path(cfg.bin).expanduser()), which=deps.which) or cfg.bin
        probe = checks.probe_claude_trade(bin_path, run=deps.run, now=deps.now)
        a.llm_trade = _check_record(probe)
        sess.say("claude -p の試験: " + ("成功" if probe.result == "ok" else "失敗"))
    else:
        a.llm_trade = _plain_record("unverified", sess.now_iso(), "user_declined_probe")
    return result


def _record_claude_binary(sess: _Session, configured_bin: str, a: Answers) -> None:
    configured = Path(configured_bin).expanduser()
    if configured_bin.startswith("/") and checks.is_executable_file(configured):
        return
    candidate = checks.cli_candidate(configured_bin, which=sess.deps.which)
    if candidate is not None:
        a.claude_bin = candidate
        sess.say(f"claude の実体: {candidate}")


def _ask_codex_bin(sess: _Session, current: str) -> str | None:
    """codex の実体の絶対パスを尋ねる。None は「このまま進む」(空 Enter または現在値と同じ)。"""
    while True:
        path = sess.text("codex の実体の絶対パス (Enter = 後で settings.yaml に自分で書く)", "")
        if not path:
            return None
        if not path.startswith("/"):
            sess.say("絶対パス (/ で始まる) で入力してください。相対パスや ~ は使えません。")
            continue
        if path == current:
            sess.say("現在の値と同じです。変更なしで進みます。")
            return None
        return path


def _improve_cli(sess: _Session, st: _Start, a: Answers, backend: str,
                 claude_result: CheckResult | None) -> None:
    deps = sess.deps
    cfg = st.base_settings.runner.codex
    a.improve_backend = backend
    listed = True
    if backend == "claude" and claude_result is not None:
        result = claude_result
        a.improve_model = _choose_model(sess, "改善", CLAUDE_MODEL_ALIASES, "sonnet")
    else:
        while True:
            bin_value = a.codex_bin or cfg.bin
            result = _cli_check_loop(sess, "codex", lambda: checks.check_cli_backend(
                "codex", a.codex_bin or cfg.bin,
                cfg.auth_file if cfg.provider == "chatgpt" else None,
                which=deps.which, run=deps.run, require_elf=True, now=deps.now))
            if result.result == "ok" or result.reason_code is not checks.CheckReason.BIN_NOT_FOUND:
                break
            sess.say("`runner.codex.bin` には codex の実体 (node のラッパーではなく ELF) の絶対パスを入れます。")
            path = _ask_codex_bin(sess, bin_value)
            if path is None:
                break
            a.codex_bin = path
        listed = _codex_model(sess, a, cfg.bin, result.result == "ok")
    a.llm_improve = _check_record(result)
    if not listed:
        a.llm_improve = _plain_record("unverified", sess.now_iso(), "model_list_unavailable")
    sess.say("1 回の改善 mission はサブスク枠を使います。")
    if result.result == "ok" and st.improve_isolation_ok:
        a.run_verify_improve = sess.yesno(
            "改善 backend の実往復 (`afx check backend`) を確定の後に試験しますか。"
            "サブスクの枠を少し使います", False)


def _reset_llm(a: Answers) -> None:
    a.trade_backend = a.trade_model = a.improve_backend = a.improve_model = None
    a.llama_base_url = a.context_limit = a.codex_bin = None
    a.run_verify_improve = False
    a.llm_trade, a.llm_improve = {}, {}


def _apply_later(sess: _Session, a: Answers) -> None:
    _reset_llm(a)
    at = sess.now_iso()
    a.llm_trade = _plain_record("unverified", at, "deferred_by_user")
    a.llm_improve = _plain_record("unverified", at, "deferred_by_user")
    sess.say("LLM は後で設定します。runner.* には触れません。取引判断 mission は LLM が使えるまで止まり、"
             "未完の手順に出し続けます。")


def _llm_default(st: _Start, local_up: bool) -> str | None:
    settings = st.base_settings
    trade, improve = settings.runner.trade.backend, settings.runner.improve.backend
    if st.mode == "fresh":
        return "local" if local_up else None
    if trade == "claude" and improve == "claude":
        return "1"
    if trade == "claude" and improve == "codex":
        return "2"
    if trade == "local" and local_up:
        return "local"
    return None


_REMOTE_FAILURE_TEXT = {
    "connect_error": "接続できません (connect_error)。",
    "auth_unsupported": ("この URL は認証を要求しています (auth_unsupported)。ウィザードは認証つきの llama-swap に"
                         "対応していません。認証不要の URL を使うか、後で設定してください。"),
    "model_not_found": "モデル一覧が空です (model_not_found)。",
}


def _stage4(sess: _Session, st: _Start, a: Answers) -> None:
    sess.say("")
    sess.say("[段 4 LLM] 取引判断 (1 足ごと) と改善 (週次など) にそれぞれ LLM を割り当てます。")
    sess.say("取引判断は local か claude、改善は local / opencode / claude / codex。claude と codex はサブスク "
             "(従量課金 API ではない) を使います。")
    base_url = st.base_settings.llama_swap.base_url
    probe = checks.list_llama_swap_models(base_url, fetch=sess.deps.fetch, now=sess.deps.now)
    local_up = probe.result == "ok"
    if local_up:
        sess.say(f"llama-swap が応答しました (モデル {probe.params.get('model_count', 0)} 件)。")
    else:
        sess.say("llama-swap が応答しません。local は既定にしません。")
    default = _llm_default(st, local_up)
    keys = ("1", "2", "3", "4") + (("local",) if local_up else ())
    while True:
        _reset_llm(a)
        if local_up:
            sess.say("  local. 取引判断に llama-swap を使う (Enter)")
        sess.say("  1. 取引判断に Claude を使う (`claude -p`、サブスクの枠を使う)")
        sess.say("  2. 取引判断に Claude を使う (改善の既定は Codex)")
        sess.say("  3. 別ホストの llama-swap の URL を入れる")
        sess.say("  4. 後で設定する")
        pick = sess.choice("LLM の構成", keys, default, help_text=_HELP_STAGE[4])
        try:
            if pick == "local":
                _local_pair(sess, st, a, base_url, probe)
                _report_improve_isolation(sess, st, a)
                return
            if pick == "4":
                raise _LaterChosen()
            if pick in ("1", "2"):
                _node_check(sess)
                _secret_env_warning(sess)
                _claude_trade(sess, st, a)
                _choose_improve_backend(sess, st, a, base_url, probe,
                                        default_backend="claude" if pick == "1" else "codex")
                _report_improve_isolation(sess, st, a)
                return
            if _stage4_remote(sess, st, a):
                _report_improve_isolation(sess, st, a)
                return
        except _LaterChosen:
            _apply_later(sess, a)
            _report_improve_isolation(sess, st, a)
            return


def _report_improve_isolation(sess: _Session, st: _Start, a: Answers) -> None:
    if (st.landlock_ok and not st.improve_isolation_ok
            and a.improve_backend in {"claude", "codex"}
            and isinstance(a.llm_improve, dict) and a.llm_improve.get("result") == "ok"):
        sess.say("改善の実往復の試験は、この kernel では隔離が使えないため行いません。")


def _stage4_remote(sess: _Session, st: _Start, a: Answers) -> bool:
    """別ホストの llama-swap。構成が決まれば True、メニューへ戻るなら False。"""
    sess.say("認証を要しない llama-swap だけ扱えます。")
    url = sess.text("llama-swap の URL", st.base_settings.llama_swap.base_url)
    problem = _url_problem(url)
    if problem is not None:
        sess.say(problem)
        return False
    probe = checks.list_llama_swap_models(url, fetch=sess.deps.fetch, now=sess.deps.now)
    if probe.result == "ok":
        _local_pair(sess, st, a, url, probe)
        return True
    reason = probe.reason_code.value if probe.reason_code else "connect_error"
    sess.say(_REMOTE_FAILURE_TEXT.get(reason, "確認できませんでした。"))
    action = sess.choice("[r] 構成の選び直し / [m] モデル名を手入力して未確認のまま進む / [4] 後で設定する",
                         ("r", "m", "4"), "r")
    if action == "m":
        _local_pair(sess, st, a, url, probe)
        return True
    if action == "4":
        raise _LaterChosen()
    return False


def _candidate_settings(st: _Start, a: Answers) -> Settings:
    """ここまでの回答を当てた設定 (当てられないときは現在の設定)。"""
    raw = yaml.safe_load(st.base_text) or {}
    result = _edit(st.base_text, build_changes(raw, a))
    if result.ok and result.text is not None:
        try:
            return _load_settings(result.text)
        except Exception:  # noqa: BLE001
            pass
    return st.base_settings


def _deployed_strategy_timeframes(sess: _Session) -> list[tuple[str, str | None]]:
    db_path = sess.root / "data" / "agentic.db"
    settings_path = sess.root / "config" / "settings.yaml"
    if not db_path.is_file() or not settings_path.is_file():
        return []
    try:
        settings = _load_settings(settings_path.read_text(encoding="utf-8"))
        conn = connect_readonly(db_path)
    except Exception:  # noqa: BLE001
        return []
    try:
        metas = sess.deps.deployed_plugins(sess.root, conn, settings)
        return [(meta.name, getattr(meta, "timeframe", None)) for meta in metas if meta.kind == "strategy"]
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def _stage5(sess: _Session, st: _Start, a: Answers) -> None:
    sess.say("")
    sess.say("[段 5 判断足] 判断を行う足です。")
    candidate = _candidate_settings(st, a)
    ceiling = worker_ceiling(candidate).total
    keys = tuple(key for key, minutes in sources.INTERVAL_MIN.items()
                 if minutes * 60 > ceiling and minutes < MAX_STORED_DECISION_INTERVAL_MIN)
    if not keys:
        sess.say(f"判断の上限時間 ({ceiling:.0f} 秒) が長すぎて選べる足がありません。"
                 "`llama_swap.timeout_sec` を下げてから `afx init` を再実行してください")
        if sess.choice("[r] 段 2 へ / [a] 中止", ("r", "a"), "a") == "r":
            raise _Back(0)
        raise _Exit(RC_STOPPED)
    default = a.timeframe if a.timeframe in keys else keys[0]
    for key, minutes in sources.INTERVAL_MIN.items():
        if key in keys:
            mark = " (推奨)" if key == "1h" else ""
            sess.say(f"  {keys.index(key) + 1}. {key}{mark}")
        elif minutes * 60 <= ceiling:
            sess.say(f"  {key}: 判断の上限時間より短いため選べません")
        else:
            sess.say(f"  {key}: 保存が 1h に正規化される派生専用の足で判断足にできない")
    while True:
        answer = sess.ask(f"判断足の番号 [既定: {keys.index(default) + 1}]: ", help_text=_HELP_STAGE[5])
        if not answer:
            timeframe = default
            break
        if answer.isdecimal() and 1 <= int(answer) <= len(keys):
            timeframe = keys[int(answer) - 1]
            break
        sess.say(f"1〜{len(keys)} の番号で選んでください。")
    a.timeframe = timeframe
    if st.mode != "fresh" and timeframe != st.base_settings.datafeed.decision_timeframe:
        sess.say("判断足を変えると、配備済みの strategy plugin の足と食い違うと signal が静かに効かなくなります。")
        mismatched = [(name, tf) for name, tf in _deployed_strategy_timeframes(sess) if tf != timeframe]
        for name, tf in mismatched:
            sess.say(f"  食い違い: {name} (足 {tf})")


def _stage6(sess: _Session, st: _Start, a: Answers) -> None:
    sess.say("")
    sess.say("[段 6 履歴と backtest] 戦略を採用するかは、過去の価格での試験 (backtest) の成績で判断します。")
    if a.eval_source == "mt5":
        current = st.base_settings.backtest.base_interval
        default = current if st.mode != "fresh" else "5m"
        sess.say("基底足の既定は 5m です (MT5 の 1m は約 3 ヶ月分で、holdout 3 ヶ月と合わせると試験期間が残りません)。")
        a.base_interval = sess.choice("backtest の基底足", ("1m", "5m", "15m"),
                                      a.base_interval or default)
        if a.base_interval == "1m":
            sess.say("警告: MT5 の 1m は約 3 ヶ月分で、holdout 3 ヶ月を除くと試験期間が残りません。")
        sess.say("履歴の取り込みはここでは実行しません。取り込みの手順は価格源の案内にあります。")
    else:
        a.base_interval = None
        sess.say("MT5 bridge が無いと、戦略の採用審査に使う履歴を取り込めません。この状態では改善ループは戦略を作れても"
                 "採用審査を通せません。指標の導入はできます (指標の審査は履歴を使いません)。")
        sess.say("backtest.eval_source は none にします。Dukascopy は相手に負荷をかけ遮断されることがあるため勧めません。")
    sess.say("holdout は 3 ヶ月です (変更は人の審査が要る項目なので尋ねません)。")


def _next_schedule_text(settings: Settings, now: datetime) -> str:
    occurrence = next_improve_occurrence(settings, now)
    return _format_occurrence(occurrence)


def _stage7(sess: _Session, st: _Start, a: Answers) -> None:
    sess.say("")
    sess.say("[段 7 改善周期と最初の 1 本]")
    if not st.landlock_ok:
        sess.say("Landlock が使えないため、改善ループは動きません。ここでは何も尋ねません。")
        a.cadence = a.improve_at = None
        a.wait_first = False
        return
    schedule = st.base_settings.schedule
    a.cadence = sess.choice("改善の周期 (weekly / daily)", ("weekly", "daily"), a.cadence or schedule.improve,
                            help_text=_HELP_STAGE[7])
    pattern = DAILY_AT_RE if a.cadence == "daily" else WEEKLY_AT_RE
    example = "03:00" if a.cadence == "daily" else "Sat 03:00"
    default_at = a.improve_at or schedule.improve_at
    if not pattern.fullmatch(default_at):
        default_at = example
    while True:
        a.improve_at = sess.text(f"改善の時刻 (表示タイムゾーン {a.display_timezone}、例 {example})", default_at)
        if pattern.fullmatch(a.improve_at):
            break
        sess.say(f"形式が違います。例: {example}")
    candidate = st.base_settings.model_copy(update={
        "display_timezone": a.display_timezone,
        "schedule": schedule.model_copy(update={"improve": a.cadence, "improve_at": a.improve_at})})
    now = sess.now()
    current = scheduled_period(candidate, now)
    sess.say(f"直近の予定は {_format_occurrence(current.occurrence)} (期 {current.period_key})、"
             f"次回は {_next_schedule_text(candidate, now)} です。")
    sess.say("  1. 次の予定まで待つ (既定) — 起動直後には走らない。その間に方針文と最初の課題を入れられる")
    sess.say("  2. サービス起動の直後に 1 本走らせる")
    a.wait_first = sess.choice("最初の 1 本", ("1", "2"), "1") == "1"
    if a.wait_first:
        sess.say(f"直近の予定の期 ({current.period_key}) は消費済みにします。")
    if (a.improve_backend or st.base_settings.runner.improve.backend) in ("claude", "codex"):
        sess.say("1 回の改善 mission はサブスク枠を使います。")
    if not st.improve_isolation_ok:
        sess.say("この kernel では改善は走りません (段 0 の表示)。")


def _stage8(sess: _Session, st: _Start, a: Answers) -> None:
    sess.say("")
    sess.say("[段 8 常駐サービスと .env]")
    sess.say("常駐なら `afx ctl` と bot / web / notify で操作し、承認もそこから行います。")
    sess.say("手動起動なら対話シェルで操作し、外からは見えません。")
    a.daemon = sess.yesno("afx を常駐サービス (systemd user unit) として動かしますか", a.daemon)
    a.api_enabled = a.daemon
    keys: list[str] = []
    if a.source == "twelvedata":
        keys.append("TWELVEDATA_API_KEY")
    if a.source == "mt5" and a.bridge_key == "yes":
        keys.append("MT5_BRIDGE_API_KEY")
    elif a.source == "mt5" and a.bridge_key == "unknown":
        sess.say("bridge に API キーを設定した場合だけ、`.env` に MT5_BRIDGE_API_KEY が要ります。")
    if st.base_settings.discord.enabled:
        keys.append("DISCORD_WEBHOOK_URL")
    if keys:
        env = checks.check_env_keys(sess.root / ".env", keys, environ=sess.deps.environ, now=sess.deps.now)
        missing = set(env.params.get("missing_keys", ()))
        listing = "、".join(f"{key} ({'なし' if key in missing else 'あり'})" for key in keys)
        sess.say(f".env に必要なキー: {listing}")
    else:
        sess.say(".env に必要なキーはありません。")
    sess.say(".env は作りません (.gitignore で除外済みです)。Discord の token と通知の webhook URL は "
             "`afx ops enable` が feature 専用の secret file に置きます。")


# ---------------------------------------------------------------------------
# 段 9: 要約と最終確認
# ---------------------------------------------------------------------------


@dataclass
class _Plan:
    new_text: str
    new_settings: Settings
    changes: dict[str, Any]


_STAGE_INDEX_TIMEFRAME = 3  # _QUESTION_STAGES の中の段 5
_STAGE_INDEX_IMPROVE_CYCLE = 5  # _QUESTION_STAGES の中の段 7


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_fmt_value(item) for item in value) + "]"
    return str(value)


def _open_order_count(root: Path) -> int | None:
    db_path = root / "data" / "agentic.db"
    if not db_path.is_file():
        return None
    try:
        conn = connect_readonly(db_path)
    except Exception:  # noqa: BLE001
        return None
    try:
        row = conn.execute("SELECT COUNT(*) FROM orders WHERE status IN "
                           "('open','pending_fill','protection_pending')").fetchone()
        return int(row[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _summary_lines(a: Answers, settings: Settings, now: datetime) -> tuple[str, ...]:
    key = "あり" if a.bridge_key == "yes" else "なし" if a.bridge_key == "no" else "不明"
    price_ok = a.price.get("result") == "ok"
    price = "確認済み" if price_ok else ("bridge の接続は未確認" if a.source == "mt5" else "価格の取得は未確認")
    source = (f"価格源: MT5 bridge（{a.bridge_url or 'URL 未設定'}、鍵 {key}、{price}）"
              if a.source == "mt5" else f"価格源: {a.source}（{price}）")
    trade = a.trade_backend or settings.runner.trade.backend
    improve = a.improve_backend or settings.runner.improve.backend
    trade_model = a.trade_model or settings.runner.trade.model
    improve_model = a.improve_model or settings.runner.improve.model
    cadence = a.cadence or settings.schedule.improve
    at = a.improve_at or settings.schedule.improve_at
    first = "次の予定まで待つ" if a.wait_first else "起動直後に 1 本走らせる"
    resident = "常駐サービスとして動かす" if a.daemon else "手動起動で使う"
    unverified: list[str] = []
    if not price_ok:
        label = "bridge の接続" if a.source == "mt5" else "価格の取得"
        reason = a.price.get("reason_code")
        unverified.append(f"{label} は未確認です" + (f" ({reason})" if reason else ""))
    for label, record in (("取引判断の LLM", a.llm_trade), ("改善の LLM", a.llm_improve)):
        if record.get("result") != "ok":
            reason = record.get("reason_code")
            unverified.append(f"{label} は未確認です" + (f" ({reason})" if reason else ""))
    return (
        source,
        f"取引判断と改善: {trade}（{trade_model}）/ {improve}（{improve_model}）",
        f"判断足: {a.timeframe}。backtest の基底足: {a.base_interval or settings.backtest.base_interval}、履歴源: {a.eval_source}",
        f"改善: {cadence} {at}。次回: {_next_schedule_text(settings, now)}。最初の 1 本: {first}",
        f"常駐: {resident}",
        "作るもの: settings.yaml、DB、状態、確認記録。",
        "このあと尋ねること: 標準指標、方針文と最初の課題、常駐化と外から見る手段。",
        *unverified,
    )


def _stage9(sess: _Session, st: _Start, a: Answers) -> _Plan:
    raw = yaml.safe_load(st.base_text) or {}
    changes = build_changes(raw, a)
    result = _edit(st.base_text, changes)
    if not result.ok or result.text is None:
        sess.say(result.reason or "settings.yaml を書き換えられませんでした。")
        sess.say("何も書いていません。案内に従って settings.yaml を手で直してから再実行してください。")
        raise _Exit(RC_STOPPED)
    new_text = result.text
    try:
        new_settings = _load_settings(new_text)
        service._validate_cache_retention(new_settings)
    except Exception as error:  # noqa: BLE001 — 設定だけで決まる起動前検査の失敗
        sess.say(f"設定の検査に失敗しました: {_one_line(error, 300)}")
        sess.say("何も書いていません。")
        raise _Exit(RC_STOPPED) from None
    try:
        service._check_mission_ceilings(new_settings, _NullActivity())
    except Exception as error:  # noqa: BLE001
        message = str(error)
        sess.say(f"設定の検査に失敗しました: {_one_line(error, 300)}")
        if "schedule." in message or "improve_at" in message:
            sess.say("改善周期の段へ戻ります。")
            raise _Back(_STAGE_INDEX_IMPROVE_CYCLE) from None
        if message.startswith("判断足"):
            sess.say("判断足の段へ戻ります。")
            raise _Back(_STAGE_INDEX_TIMEFRAME) from None
        sess.say("この値は質問への答えでは直せません (settings.yaml に書かれている値です)。")
        if sess.choice("[a] 中止して settings.yaml を直す / [r] 段 2 からやり直す", ("a", "r"), "a") == "r":
            raise _Back(0) from None
        sess.say("何も書いていません。上の理由に従って settings.yaml を直してから再実行してください。")
        raise _Exit(RC_STOPPED) from None

    sess.say("")
    sess.say("")
    sess.say("[段 9 要約]")
    for line in _summary_lines(a, new_settings, sess.now()):
        sess.say(line)
    if st.mode != "fresh":
        sentences: list[str] = []
        for key in changes:
            for prefix, sentence in _ATTENTION:
                if (key == prefix or (prefix.endswith(".") and key.startswith(prefix))) and sentence not in sentences:
                    sentences.append(sentence)
        if sentences:
            sess.say("注意が要る項目を変えます:")
            for sentence in sentences:
                sess.say(f"  - {sentence}")
            count = _open_order_count(sess.root)
            if count:
                sess.say(f"建玉・未約定が {count} 件あります。これらの項目を変える前に建玉を閉じることを勧めます。")
            sess.typed_yes("変えてよいときだけ")
    while True:
        action = sess.ask("この内容で書き込みます [Y/n] (d = settings.yaml の差分を見る): ").lower() or "y"
        if action not in ("y", "n", "d"):
            sess.say("y / n / d のどれかで答えてください。")
            continue
        if action == "y":
            break
        if action == "n":
            sess.say("何も書かずに終了します。")
            raise _Exit(RC_STOPPED)
        if changes:
            sess.say("settings.yaml の変更点 (キー: 現在値 → 新しい値):")
            for key, new in changes.items():
                sess.say(f"  {key}: {_fmt_value(_get_path(raw, key))} → {_fmt_value(new)}")
        else:
            sess.say("settings.yaml の変更点はありません。")
        if st.migrated and st.original_text is not None:
            sess.say("版の移行で挿入・変更する行:")
            for line in difflib.unified_diff(st.original_text.splitlines(), st.base_text.splitlines(),
                                             lineterm="", n=0):
                if line[:1] in "+-" and not line.startswith(("+++", "---")):
                    sess.say(f"  {line}")
    return _Plan(new_text, new_settings, changes)


_QUESTION_STAGES: tuple[Callable[[_Session, _Start, Answers], None], ...] = (
    _stage2, _stage3, _stage4, _stage5, _stage6, _stage7, _stage8)


def _collect(sess: _Session, st: _Start) -> tuple[Answers, _Plan]:
    answers = _initial_answers(sess, st)
    _stage1(sess, st)
    start = 0
    while True:
        try:
            for stage in _QUESTION_STAGES[start:]:
                stage(sess, st, answers)
            return answers, _stage9(sess, st, answers)
        except _Back as back:
            start = back.stage


# ---------------------------------------------------------------------------
# 段 10: 確定 1 (回復可能な commit protocol)
# ---------------------------------------------------------------------------

_COMMIT_STEPS = ("marker", "settings.yaml", "logs", "DB", "確認記録", "状態", "活動ログ", "marker の除去")


def _report_commit_failure(sess: _Session, error: BaseException) -> None:
    sess.say(f"確定の途中で失敗しました: {_one_line(error, 300)}")
    done = list(sess.written)
    sess.say("書かれたもの: " + (", ".join(done) if done else "なし"))
    sess.say("書かれていないもの: " + (", ".join(step for step in _COMMIT_STEPS if step not in done) or "なし"))
    sess.say("次にすること: `afx init` を再実行してください。印が残っている間、サービスは起動しません。")


def _backup_path(settings_path: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    candidate = settings_path.with_name(f"{settings_path.name}.bak-{stamp}")
    index = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = settings_path.with_name(f"{settings_path.name}.bak-{stamp}-{index}")
        index += 1
    return candidate


def _settings_unchanged_since_start(sess: _Session, st: _Start) -> None:
    """確定の直前に settings.yaml が段 0 で読んだときのままかを確かめ、違えば何も書かずに止める。"""
    try:
        current = _read_text(sess.root / "config" / "settings.yaml")
    except (OSError, UnicodeDecodeError):
        current = object()  # type: ignore[assignment]
    if current != st.original_text:
        sess.say("設定が別の process に変更されました。`afx init` を再実行してください。")
        sess.say("何も書いていません。")
        raise _Exit(RC_STOPPED)


def _commit(sess: _Session, st: _Start, a: Answers, plan: _Plan) -> None:
    root, deps = sess.root, sess.deps
    sess.phase = "commit"
    now = sess.now()
    sess.say("")
    sess.say("[段 10 確定] 設定・DB・確認記録・状態を順に書きます。")
    try:
        sess.hook("before_commit_marker")
        write_setup_marker(root, format_setup_marker("commit", now.isoformat(), _sha256(plan.new_text)),
                           hook=deps.hook)
        sess.written.append("marker")
        settings_path = root / "config" / "settings.yaml"
        sess.hook("before_settings_replace")
        current = _read_text(settings_path)
        if current != plan.new_text:
            prior = st.prior_marker
            continuing = (prior is not None and prior.phase == "commit"
                          and prior.settings_sha256 is not None
                          and current is not None and prior.settings_sha256 == _sha256(current))
            if current is not None and not continuing:
                atomic_write_settings(_backup_path(settings_path, now), current)
            atomic_write_settings(settings_path, plan.new_text)
        sess.written.append("settings.yaml")
        sess.hook("after_settings_replace")
        (root / "logs").mkdir(parents=True, exist_ok=True)
        setup_technical_logging(root / "logs", plan.new_settings.logging.level)
        sess.written.append("logs")
        _commit_database(sess, a, plan, now)
        sess.written.append("DB")
        previous = read_setup_record(root)
        data = build_record(a, now_iso=now.isoformat(),
                            previous=previous.record.data if previous.state == "ok" and previous.record else None)
        sess.hook("before_setup_record_write")
        write_setup_record(root, SetupRecord(data))
        sess.written.append("確認記録")
        sess.hook("after_setup_record_write")
        store = service._state_store(root)
        if store.load().initialized:
            store.update(initialized=True)
        else:
            store.update(initialized=True, mode=Mode.LEARNING, autopilot=False)
        sess.written.append("状態")
        sess.hook("after_app_state_write")
        try:
            ActivityLog(root / "logs" / "activity.log").write(
                Category.SYSTEM, "init_completed", f"pairs={plan.new_settings.pairs} interactive")
        except Exception as error:  # noqa: BLE001 — 活動ログの失敗は警告に留める
            sess.say(f"警告: 活動ログに書けませんでした ({_one_line(error)})。")
        sess.written.append("活動ログ")
        remove_setup_marker(root, hook=deps.hook)
        sess.written.append("marker の除去")
    except Exception as error:  # noqa: BLE001 — 失敗時は何が残ったかを見せる
        _report_commit_failure(sess, error)
        raise _Exit(RC_STOPPED) from None
    sess.phase = "committed"
    sess.say("設定を書き込みました。ここから先 (指標・方針文・常駐化) は 1 つずつ確定し、"
             "途中でやめても書いた所までが残ります。")


def _commit_database(sess: _Session, a: Answers, plan: _Plan, now: datetime) -> None:
    conn = connect(sess.root / "data" / "agentic.db")
    try:
        init_db(conn)
        sess.hook("after_init_db")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            seed_default_sources(conn, now, commit=False)
            if a.wait_first:
                consume_scheduled_period(conn, plan.new_settings, now=now, commit=False)
            sess.hook("before_seed_commit")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        sess.hook("after_seed_commit")
    finally:
        conn.close()


def _update_record(sess: _Session, **values: Any) -> bool:
    current = read_setup_record(sess.root)
    if current.state != "ok" or current.record is None:
        sess.say("確認記録を読めないため更新できません。`afx init` の再実行で作り直せます。")
        return False
    data = dict(current.record.data)
    data.update(values)
    try:
        write_setup_record(sess.root, SetupRecord(data))
    except (OSError, ValueError) as error:
        sess.say(f"確認記録を更新できませんでした ({_one_line(error)})。")
        return False
    return True


def _verify_improve_backend(sess: _Session, a: Answers) -> None:
    backend = a.improve_backend
    if not a.run_verify_improve or backend is None:
        return
    sess.say(f"改善 backend ({backend}) の実往復を試験します (子 process の `afx check backend`)。")
    reason: str | None = None
    try:
        completed = sess.deps.run(
            [sys.executable, "main.py", "check", "backend", "--backend", backend],
            cwd=str(sess.root), capture_output=True, text=True, timeout=1800)
        ok = getattr(completed, "returncode", 1) == 0
        reason = None if ok else "verify_failed"
    except subprocess.TimeoutExpired:
        ok, reason = False, "timeout"
    except (OSError, subprocess.SubprocessError):
        ok, reason = False, "verify_failed"
    sess.say("実往復: " + ("成功 (疎通のみ。隔離の適合は起動後の admission が判定します)" if ok else "失敗"))
    _update_record(sess, llm_improve=_plain_record("ok" if ok else "failed", sess.now_iso(), reason))


# ---------------------------------------------------------------------------
# 段 11〜12: 標準指標・方針文と最初の課題
# ---------------------------------------------------------------------------


def _stage11(sess: _Session, settings: Settings, lease: SetupLease) -> None:
    sess.say("")
    sess.say("[段 11 標準指標]")
    if not sess.landlock_ok:
        sess.say("Landlock が使えないため、標準指標の導入は飛ばします。")
        return
    stage = sess.deps.indicator_stage
    if stage is None:
        sess.say("標準指標は後で `afx plugin bless <名> --from _human` で採用できます "
                 "(docs/examples/plugins/ に 9 本あります)。")
        return
    outcome = stage(WizardContext(sess.root, settings, sess.io, sess.deps, lease, sess.now()))
    _update_record(sess, indicators={"installed": int(outcome.installed)})


def _text_length(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


def _backlog_count(root: Path) -> int | None:
    db_path = root / "data" / "agentic.db"
    if not db_path.is_file():
        return None
    try:
        conn = connect_readonly(db_path)
    except Exception:  # noqa: BLE001
        return None
    try:
        return int(conn.execute("SELECT COUNT(*) FROM improvement_backlog").fetchone()[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


_POLICY_EXAMPLES = (
    "ペーパー運用。実資金の発注は行わない。",
    "対象は {pair} の {timeframe} 足。指標発表の前後 30 分は新規に入らない。",
    "選別して勝つを優先し、取引回数より負けを減らす提案を評価する。",
)
_BACKLOG_EXAMPLE = "標準指標だけで組める {timeframe} 足の押し目買い戦略候補を 1 本作り、直近 3 ヶ月の履歴で PF と最大ドローダウンを測る"


def _stage12(sess: _Session, a: Answers | None) -> None:
    sess.say("")
    sess.say("[段 12 方針文と最初の課題]")
    path = directives_path(sess.root)
    count = _backlog_count(sess.root)
    sess.say(f"既存の方針文: {_text_length(path)} 文字 / 改善課題: {count if count is not None else '不明'} 件")
    sess.say("方針文は取引判断と改善の全 mission に注入される運用者の方針です。")
    if a is None:
        settings = _load_settings((sess.root / "config" / "settings.yaml").read_text(encoding="utf-8"))
        pair, timeframe = settings.pairs[0], settings.datafeed.decision_timeframe
    else:
        pair, timeframe = a.pairs[0], a.timeframe
    examples = tuple(example.format(pair=pair, timeframe=timeframe) for example in _POLICY_EXAMPLES)
    backlog_example = _BACKLOG_EXAMPLE.format(timeframe=timeframe)
    for example in examples:
        sess.say(f"例: 「{example}」")
    sess.say("後から `afx ctl policy add` / `afx ctl improve add` で足せます。空 Enter で飛ばせます。e で例文を入れます。")
    written = 0
    activity = ActivityLog(sess.root / "logs" / "activity.log")
    while True:
        raw = sess.ask("方針文: ", allow_quit=False)
        entries = examples if raw == "e" else (" ".join(raw.split()),)
        if not entries[0]:
            break
        for text in entries:
            if text in (_read_text(path) or ""):
                sess.say("方針文は追加済みです。")
                continue
            try:
                append_policy_directive(path, activity, text)
                written += 1
            except OSError as error:
                sess.say(f"警告: 方針文を書けませんでした ({_one_line(error)})。")
                return
    sess.say(f"方針文を {written} 行書きました。")
    sess.say("課題は改善ループが最初に取り組む 1 件です。")
    sess.say(f"例: 「{backlog_example}」")
    sess.say("後から `afx ctl policy add` / `afx ctl improve add` で足せます。空 Enter で飛ばせます。e で例文を入れます。")
    added = 0
    conn = None
    try:
        conn = connect(sess.root / "data" / "agentic.db")
        while True:
            raw = sess.ask("最初の課題: ", allow_quit=False)
            if raw == "e":
                raw = backlog_example
            display, _removed = normalize_idea_display(raw)
            if not display:
                break
            if idea_looks_unintended(display):
                sess.say("短い、またはプレースホルダのように見えます。")
                if not sess.yesno("このまま追加しますか", False):
                    continue
            try:
                if conn.execute("SELECT 1 FROM improvement_backlog WHERE idea_norm=? LIMIT 1",
                                (display.strip().lower(),)).fetchone() is not None:
                    sess.say("課題は追加済みです。")
                    continue
                add_backlog_idea(conn, activity, display, sess.now(), via="init")
                added += 1
            except (sqlite3.Error, OSError) as error:
                sess.say(f"警告: 課題を追加できませんでした ({_one_line(error)})。")
                break
    finally:
        if conn is not None:
            conn.close()
    sess.say(f"最初の課題を {added} 件追加しました。")


# ---------------------------------------------------------------------------
# 段 13: 常駐化と外から見る手段
# ---------------------------------------------------------------------------

_FEATURES = ("bot", "web", "notify")


def _ask_features(sess: _Session) -> list[str]:
    sess.say("外から見る / 操作する手段: bot (Discord bot) / web (読み取り専用 web) / notify (通知)。"
             "質問 (token・port・webhook URL など) は `afx ops enable` 側が端末から直接受けます。")
    if not sess.yesno("外から見る手段を今設定しますか (既定: あとで)", False):
        return []
    while True:
        answer = sess.text("有効にする手段 (カンマ区切り。空 Enter = あとで)", "")
        picked = [part for part in re.split(r"[,\s]+", answer) if part]
        if not picked:
            return []
        if all(part in _FEATURES for part in picked) and len(set(picked)) == len(picked):
            return picked
        sess.say(f"{', '.join(_FEATURES)} の中から選んでください。")


def _linger_step(sess: _Session) -> None:
    deps = sess.deps
    state = unit_module.linger_state(deps.run)
    at = sess.now_iso()
    if state == "yes":
        value = {"state": "already_enabled", "at": at}
    elif state == "unknown":
        sess.say("linger の状態を読めませんでした。ログアウトすると afx も止まる場合は "
                 "`loginctl enable-linger $USER` を実行してください。")
        value = {"state": "failed", "at": at, "reason_code": "unknown"}
    else:
        result = deps.enable_linger(deps.run, confirm=lambda: sess.yesno(
            "ログアウトすると afx も止まります。`loginctl enable-linger $USER` を実行しますか", False))
        value = {"state": result.state, "at": sess.now_iso()}
        if result.reason_code is not None:
            value["reason_code"] = result.reason_code
    _update_record(sess, linger=value)


def _start_step(sess: _Session, settings: Settings, enabled_features: list[str],
                outcomes: list[post_start.FeatureOutcome]) -> None:
    deps = sess.deps
    assert sess.lease is not None
    sess.lease.release_instance_lock()
    _update_record(sess, start=_plain_record("pending", sess.now_iso()))

    def confirm(name: str) -> bool:
        if name == "enable":
            return sess.yesno("ログイン時に自動起動するよう enable しますか", True)
        sess.say("今起動すると、価格の取り込みと (待つを選ばなかった場合) 改善 mission が始まります。")
        return sess.yesno("起動しますか", False)

    outcome = deps.enable_and_start(
        sess.root, run=deps.run, confirm=confirm, now=sess.now_iso, api_status=deps.api_status,
        api_enabled=settings.api.enabled, features=enabled_features, ops_ready=deps.ops_ready,
        clock=deps.clock, sleep=deps.sleep)
    values: dict[str, Any] = dict(post_start.start_record_values(outcome, api_enabled=settings.api.enabled))
    if outcomes:
        previous = read_setup_record(sess.root)
        prior_ops = dict(previous.record.data.get("ops", {})) if previous.state == "ok" and previous.record else {}
        values["ops"] = {**prior_ops, **post_start.ops_ready_record_values(outcomes, outcome)}
    sess.say("")
    if outcome.result == "started":
        sess.say("起動後の確認 (3 段階。それぞれ別に記録します):")
        sess.say(f"  1. サービス: {'active' if outcome.active == 'ok' else outcome.active}")
        api_text = outcome.api_result + (f" ({outcome.api_reason_code})" if outcome.api_reason_code else "")
        sess.say(f"  2. 操作 API (認証つき status): {api_text}")
        for feature, ready in (outcome.ops or {}).items():
            sess.say(f"  3. ops {feature}: ready {ready}")
        if outcome.api_result == "disabled":
            sess.say("操作 API が無効です。`afx ctl` は使えず、未完の手順を `afx ctl status` に出せません。"
                     "案内はこの表示と起動画面だけです。")
        elif outcome.api_result == "failed":
            sess.say("サービスは起動しましたが、操作 API に認証つきで接続できませんでした。"
                     "`journalctl --user -u afx -n 50` を見てください。")
        backend = settings.runner.improve.backend
        if backend == "local":
            observed = None
        else:
            observed = deps.read_admission_line(deps.run, outcome.invocation_id,
                                                cursor_fallback=outcome.journal_cursor, backend=backend)
        values["isolation"] = post_start.isolation_record_values(observed, at=sess.now_iso())
        if observed is not None:
            sess.say(f"改善の隔離 (admission): {observed.admitted} ({observed.reason_code})")
            if observed.guidance:
                sess.say(observed.guidance)
    else:
        sess.say(f"  サービスは起動していません ({outcome.reason_code})。"
                 "起動するには `systemctl --user start afx` を実行してください。")
    _update_record(sess, **values)


def _stage13(sess: _Session, settings: Settings, *, resume_handoff: bool, daemon: bool = True) -> None:
    deps = sess.deps
    root = sess.root
    assert sess.lease is not None
    sess.say("")
    sess.say("[段 13 常駐化と外から見る手段]")
    if not resume_handoff and not daemon:
        sess.say("手動起動: `uv run afx`。常駐に切り替えるときは `afx init` を再実行してください。")
        unit_dir = deps.unit_dir or unit_module.default_unit_dir(deps.environ)
        unit_value = "installed" if (unit_dir / "afx.service").is_file() else "not_installed"
        if unit_value == "installed":
            sess.say("既存の unit は残します。止めるには `systemctl --user disable --now afx`")
        _update_record(sess, unit=unit_value, start=_plain_record("skipped", sess.now_iso(), "declined"))
        return
    if resume_handoff:
        sess.say("前回は外から見る手段の設定の途中で止まりました。unit は設置済みです。手順 3 から続けます。")
        unit_value = "installed"
    else:
        unit_dir = deps.unit_dir or unit_module.default_unit_dir(deps.environ)

        def confirm_unit(text: str = "") -> bool:
            if text:
                sess.say(text)
            return sess.yesno("この内容で unit を置きますか", False)

        result = deps.place_unit(root, settings, unit_dir=unit_dir, confirm=confirm_unit, run=deps.run,
                                 check_run=deps.run, which=deps.which, env=deps.environ)
        unit_value = unit_module.unit_record_values(result)["unit"]
        if result.state == "unchanged":
            sess.say("unit は設置済みです (内容は同じです)。")
        elif result.state == "installed":
            sess.say("unit (afx.service) を設置しました。")
        elif result.message:
            sess.say(result.message)
        for note in result.notes:
            sess.say(note)
        report = getattr(result, "report", None)
        if report is not None and getattr(report, "warning", None):
            sess.say("afx check の warning (unit は置きます): " + ", ".join(report.warning))
        _update_record(sess, unit=unit_value)
        if unit_value != "installed":
            sess.say("unit を置けていないため、有効化・起動は行いません。起動のしかたは次の案内を見てください。")
            return
    features = _ask_features(sess)
    outcomes: list[post_start.FeatureOutcome] = []
    if features:
        settings_text = _read_text(root / "config" / "settings.yaml")
        outcomes = deps.handoff_and_enable(root, sess.lease, features, enable=deps.enable,
                                           precheck=deps.resolve_ops, now=sess.now_iso, hook=deps.hook,
                                           settings_sha256=_sha256(settings_text or ""))
        for item in outcomes:
            if item.result == "enabled":
                sess.say(f"{item.feature}: 有効化しました。")
            elif item.result == "later":
                sess.say(f"{item.feature}: あとで設定します ({item.reason_code})。"
                         "afx-ops が見つかりません。`afx-ops/` を PATH に置くか `afx_ops` を import できる環境にしてください。")
            else:
                sess.say(f"{item.feature}: 有効化が途中です ({item.reason_code})。"
                         f"`afx ops enable {item.feature}` を再実行してください。")
        _update_record(sess, ops={**_prior_ops(sess), **post_start.ops_record_values(outcomes)})
    if resume_handoff:
        remove_setup_marker(root, hook=deps.hook)
    _linger_step(sess)
    _start_step(sess, settings, [item.feature for item in outcomes if item.result == "enabled"], outcomes)
    sess.lease.close()


def _prior_ops(sess: _Session) -> dict[str, Any]:
    previous = read_setup_record(sess.root)
    if previous.state == "ok" and previous.record is not None:
        return dict(previous.record.data.get("ops", {}))
    return {}


# ---------------------------------------------------------------------------
# 段 14: 次の一歩
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now()


def _stage14(sess: _Session, settings: Settings) -> None:
    root = sess.root
    sess.say("")
    sess.say("[段 14 次の一歩]")
    sess.say("起動: unit なら `systemctl --user start afx` (状態は `systemctl --user status afx`)、手動なら `uv run afx`。")
    if settings.api.enabled:
        sess.say("操作は `afx ctl`: `afx ctl status` (稼働状態と未完の手順)、`afx ctl approval list`、"
                 "`afx ctl improve add <案>`、`afx ctl policy add <文>`。")
        sess.say("改善候補の承認は二通り: Discord bot を有効にしたなら承認依頼で二段操作 (claim → confirm)、"
                 "もう一つは `afx ctl --as approver approve <id>` (端末での確認あり)。")
    else:
        sess.say("操作 API が無効なので `afx ctl` は使えません。有効にするには settings.yaml の api.enabled を true にして再起動します。")
    sess.say("ログ: logs/agentic.log、logs/activity.log、unit なら `journalctl --user -u afx`。")
    sess.say("設定を変えるときは `afx init` の再実行です (稼働中は先に `systemctl --user stop afx`)。")
    db_path = root / "data" / "agentic.db"
    if not db_path.is_file():
        return
    try:
        conn = connect_readonly(db_path)
    except Exception:  # noqa: BLE001
        return
    try:
        deployed = sess.deps.deployed_plugins(root, conn, settings)
        facts = collect_pending_facts(root, settings, conn, deployed_plugins=deployed, outage=None,
                                      clock=_Clock(sess.now), landlock_available=sess.landlock_ok,
                                      data_state=None)
        record = read_setup_record(root)
        started = (record.state == "ok" and record.record is not None
                   and record.record.data.get("start", {}).get("result") == "started")
        facts = replace(facts, service_active=started)
        steps = pending_steps(facts)
    except Exception as error:  # noqa: BLE001 — 案内の表示だけなので失敗しても終了は妨げない
        sess.say(f"未完の手順を読めませんでした ({_one_line(error)})。`afx ctl status` で確認してください。")
        return
    finally:
        conn.close()
    if steps:
        sess.say("")
        sess.say("未完の手順:")
        for step in steps:
            sess.say(f"  - {render_pending_step(step)}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _interrupt_message(sess: _Session) -> str:
    if sess.phase == "collect":
        return _NOTHING_CHANGED
    if sess.phase == "commit":
        return ("確定の途中で止まりました。書かれた所までが残っています。`afx init` を再実行してください "
                "(印が残っている間、サービスは起動しません)。")
    return _AFTER_COMMIT_STOP


def _tail_removal(sess: _Session) -> None:
    sess.say("前回の確定は印を消す所だけが残っています。")
    if not sess.yesno("消しますか", True):
        sess.say("印を残したまま終了します。サービスは起動しません。")
        raise _Exit(RC_STOPPED)
    remove_setup_marker(sess.root, hook=sess.deps.hook)
    sess.phase = "committed"


def run_wizard(root: Path, io: WizardIO, deps: WizardDeps | None = None) -> int:
    """対話ウィザードの入口。戻り値は終了コード。"""
    deps = deps or WizardDeps()
    sess = _Session(Path(root).resolve(), io, deps)
    try:
        try:
            start = _stage0(sess)
            sess.landlock_ok = start.landlock_ok
            assert sess.lease is not None
            if start.mode == "handoff":
                settings = start.base_settings
                sess.phase = "committed"
                _stage13(sess, settings, resume_handoff=True)
                _stage14(sess, settings)
                return RC_OK
            answers: Answers | None = None
            if start.mode == "commit_tail":
                settings = start.base_settings
                _tail_removal(sess)
            else:
                answers, plan = _collect(sess, start)
                _settings_unchanged_since_start(sess, start)
                _commit(sess, start, answers, plan)
                settings = plan.new_settings
                _verify_improve_backend(sess, answers)
            _stage11(sess, settings, sess.lease)
            _stage12(sess, answers)
            _stage13(sess, settings, resume_handoff=False,
                     daemon=answers.daemon if answers else settings.api.enabled)
            _stage14(sess, settings)
            return RC_OK
        except _Interrupted as stop:
            sess.say("")
            sess.say(_interrupt_message(sess))
            return stop.rc
        except _Exit as stop:
            return stop.rc
        except KeyboardInterrupt:
            sess.say("")
            sess.say(_interrupt_message(sess))
            return RC_INTERRUPT
    finally:
        if sess.lease is not None:
            sess.lease.close()
