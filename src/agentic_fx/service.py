"""起動シーケンス (init ウィザード・起動ガード) とサービス本体の配線 — 設計書 §8。

`build_app` が全部品 (決定論的コア + 2 つの loop + Commands) を配線し、
`run_service` が scheduler スレッド・watchdog スレッド・対話シェル/daemon 待機・
graceful shutdown を担う (Phase 1 プラン 5)。
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Clock, Mode, SystemClock
from agentic_fx.core.executor import Executor
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.core.supervisor import MissionSupervisor
from agentic_fx.datafeed import cache_window, sources
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.news_collector import NewsCollector, seed_default_sources
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.logging_setup import setup_technical_logging
from agentic_fx.loops import reflection_cycle
from agentic_fx.loops.mission_watch import MissionWatch
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.loops.summary import ANSWER_SCHEMA, trade_intent_schema
from agentic_fx.loops.trade_loop import _TRADE_TOOLS, TradeLoop
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.policy import Policy
from agentic_fx.runners.base import AgentRunner
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.runners.worker_runner import WorkerRunner
from agentic_fx.store import approvals, missions, orders, signals, ohlcv
from agentic_fx.store.db import connect, connect_readonly, init_db
from agentic_fx.store.instance_lock import acquire_instance_lock
from agentic_fx.store.rag import Rag
from agentic_fx.store.state import StateStore
from agentic_fx.tools import (
    plugin_loader, signal_tools,
)
from agentic_fx.tools.mission_registry import build_mission_registry
from agentic_fx.tools.registry import ToolRegistry

_log = logging.getLogger("agentic_fx.service")

# プラン 9 Task 16: 1 回の DELETE が SL/TP 監視 (_process_exits) の許容
# 遅延を超えないことを基準にした有界バッチ上限。実測は Task 16 Step 43
# のコメントを参照。実測 2026-08-12: 5000 行の DELETE が 0.0049 秒
# (ローカル SQLite、WAL)。
_OHLCV_PRUNE_BATCH_LIMIT = 5000


def _state_store(root: Path) -> StateStore:
    return StateStore(root / "data" / "state" / "app_state.json")


def ensure_initialized(root: Path) -> None:
    if not _state_store(root).load().initialized:
        print("初期化が完了していません。先に `uv run main.py init` を実行してください。",
              file=sys.stderr)
        raise SystemExit(2)


def _model_load_order(settings) -> list[str]:
    """trade/improve の重複除去済み順序付きリスト。異なる場合は
    improve→trade (trade を最後に置くのは意図的 — 設計書 §4.4: llama-swap
    の常駐数/VRAM/TTL 次第では後発ロードが先発を unload しうるため、
    init 終了時に取引判断で使うモデルを hot な状態で終わらせる)。"""
    trade = settings.runner.trade.model
    improve = settings.runner.improve.model
    return [trade] if trade == improve else [improve, trade]


# `/props` は**モデルをロードさせる** (llama-swap が swap/spawn する)。
# したがって呼び出し側の状況で必要な予算が 4 桁違う — 単一の timeout では
# 必ずどちらかが壊れる。実測 (2026-08-12、:8080):
#   nomic-embed-text (137M) cold 4.26s / hot 0.0003s
#   qwen3.6-35b-a3b_Q4 (35B) cold 13.86s / hot 0.0005s
# 2 周目レビューの指摘: 全経路 timeout=5 だったため improve 側 (常に cold)
# が必ず ReadTimeout → None となり、improve の ctx 行は**この機能が作られた
# 唯一の構成 (trade != improve) で永久に出なかった**。テストは MockTransport
# が即答するので 1802 passed はこの行の証拠にならない。
# 短い予算で中断すると llama-swap を swap 途中に置き去りにし、後続の trade
# smoke がそれを待つ (llama-swap-environment の TTL/incoming-request race)。
# 3 周目レビュー: cold load を跨ぎうる要求は **smoke 自身も含めて** この 1 つの
# 予算に束ねる。分離した当初は `/props` の 2 箇所だけを対象にしたため、最も
# cold-load 耐性が要る smoke の `timeout=120` が pin から漏れ、5 へ縮める変異が
# 1808 passed のまま生存した。
_COLD_LOAD_TIMEOUT = 120   # smoke 本体と、smoke 前の improve `/props`
_PROPS_TIMEOUT_HOT = 5     # smoke 直後の trade `/props`。既にロード済み


def _fetch_model_ctx(base: str, model: str, timeout: float) -> int | None:
    """`GET /props?model=<model>` から `default_generation_settings.n_ctx`
    を取得する。**例外境界を限定する** (codex I2) — 取得・形状のいずれかの
    失敗でも None を返し、想定外例外は伝播させて init を落とす。

    `timeout` に既定値は置かない。呼び出し側が cold / hot のどちらを踏むか
    を必ず意識させるため (既定値を置くと第 3 の呼び出し点が黙って cold に
    5 秒を割り当てて同じ欠陥が再発する)。
    """
    import httpx
    try:
        api_root = base.rstrip("/")
        if api_root.endswith("/v1"):
            api_root = api_root[:-3]
        r = httpx.get(f"{api_root}/props", params={"model": model},
                      timeout=timeout)
        r.raise_for_status()
        n_ctx = r.json()["default_generation_settings"]["n_ctx"]
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError,
            TypeError, KeyError):
        return None
    if not isinstance(n_ctx, int) or isinstance(n_ctx, bool) or n_ctx <= 0:
        return None
    return n_ctx


def _check_llama_swap(settings) -> None:
    """llama-swap 接続確認 (上書き 2、プラン 9 Task 5)。一覧取得不能 /
    モデル不在 / cold-load smoke 失敗の 3 種を区別して警告する。init から
    呼ばれる (失敗は警告のみ — 取引判断 Mission は実行時に fail closed で
    保護される)。

    **対象モデルは重複除去した順序付きリスト** (`_model_load_order`)。
    trade == improve なら存在確認→smoke→/props を各 1 回。異なるなら
    improve の /props を先に (表示のみ)、trade は従来どおり最後に
    存在確認→smoke→/props (設計書 §4.4 — trade を最後に置くのは意図的。
    improve が trade と異なる場合、init に cold load 1 回分の時間が
    増えることを既知コストとして許容する)。`n_ctx` は表示のみで
    保存しない (§3 のドリフト回避 — llama-swap 側の --ctx-size 変更で
    陳腐化するため)。
    """
    import httpx
    base = settings.llama_swap.base_url
    trade_model = settings.runner.trade.model

    try:
        r = httpx.get(f"{base}/models", timeout=5)
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", [])
               if isinstance(m, dict)]
    except (httpx.RequestError, httpx.HTTPStatusError,
            ValueError, TypeError) as e:
        print(f"警告: llama-swap のモデル一覧を取得できません ({e})。"
              "取引判断 Mission は失敗として記録されます。")
        return

    if trade_model not in ids:
        print(f"警告: モデル '{trade_model}' が llama-swap の /models に存在しません。"
              f"alias 設定を確認してください (存在: {ids})")
        return

    order = _model_load_order(settings)
    if len(order) == 2:
        # improve はここが初回接触なので**必ず cold**。smoke と同額を積む
        improve_ctx = _fetch_model_ctx(base, order[0], _COLD_LOAD_TIMEOUT)
        if improve_ctx is not None:
            print(f"improve model '{order[0]}' ctx {improve_ctx}")

    try:
        # cold-load smoke: TTL unload 後の初回 Mission がロード時間で
        # timeout しないよう、1 トークン生成でロードを促す
        r = httpx.post(f"{base}/chat/completions",
                       json={"model": trade_model, "max_tokens": 1,
                             "messages": [{"role": "user", "content": "ping"}]},
                       timeout=_COLD_LOAD_TIMEOUT)
        r.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        print(f"警告: モデル '{trade_model}' の cold-load smoke に失敗しました ({e})。"
              "初回 Mission が timeout する可能性があります。")
        return

    # trade は直前の smoke でロード済みなので hot
    trade_ctx = _fetch_model_ctx(base, trade_model, _PROPS_TIMEOUT_HOT)
    if trade_ctx is not None:
        print(f"llama-swap OK (model '{trade_model}' loaded, ctx {trade_ctx})")
    else:
        print(f"llama-swap OK (model '{trade_model}' loaded)")


_SECRET_ENV_PATTERNS = ("_API_KEY", "TOKEN", "SECRET", "WEBHOOK",
                        "ANTHROPIC_", "OPENAI_")


def _resolve_cli_bin(bin_value: str, *, require_elf: bool) -> Path:
    import shutil

    resolved = shutil.which(bin_value) or (
        bin_value if Path(bin_value).is_absolute() else None)
    if resolved is None:
        raise RuntimeError(
            f"runner CLI bin {bin_value!r} not found on PATH nor an "
            "absolute path")
    path = Path(resolved).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"runner CLI bin {path} is not an executable file")
    if require_elf:
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic != b"\x7fELF":
            raise RuntimeError(
                f"runner.codex.bin {path} is not an ELF binary (vendor "
                "native required — node wrappers like 'codex.js' are "
                "rejected; find the vendor bin under "
                "'@openai/codex-linux-x64/vendor/.../bin/codex')")
    return path


def _check_cli_version(bin_path: Path) -> None:
    import subprocess

    try:
        r = subprocess.run([str(bin_path), "--version"],
                           capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"runner CLI --version check failed: {e}") from e
    if r.returncode != 0:
        raise RuntimeError(
            f"runner CLI --version exited {r.returncode} for {bin_path}")


def _check_credentials_file(path_str: str, *, label: str) -> None:
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise RuntimeError(
            f"{label} credentials file not found: {path} "
            "(run the CLI's login flow first)")


def _read_proc_self_environ_names() -> set[str]:
    """`/proc/self/environ` (このプロセスの exec 時点の初期 env) のキー名
    集合を返す (設計書 §1.4-⑤: `python-dotenv` の `load_dotenv()` は
    `os.environ` に setenv するだけで `/proc/self/environ` には現れない —
    B1 の再発防止。`monkeypatch.setenv`/`os.environ[...] = ...` もこの
    ブロックを書き換えない、Linux 前提)。"""
    with open("/proc/self/environ", "rb") as f:
        raw = f.read()
    names: set[str] = set()
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        key, _sep, _value = chunk.partition(b"=")
        names.add(key.decode("utf-8", errors="replace"))
    return names


def _check_service_initial_env_has_no_secrets(
        settings, *,
        read_initial_env_names=_read_proc_self_environ_names) -> None:
    """検査⑤ (設計書 §1.4、裁定 R3): サービス自身の**初期 env**
    (`/proc/self/environ` 相当。既定 seam = `_read_proc_self_environ_names`) に
    秘密名パターンがあれば起動拒否する。`.env`→`load_dotenv()` で
    `os.environ` にのみ現れるキーは対象外 (設計が明示的に許容している —
    exported shell env にだけ秘密を置くな、という検査)。`settings` は
    呼び出し規約を他の `_check_*` 検査と揃えるために受け取るのみで、
    現状は未使用。"""
    names = read_initial_env_names()
    leaked = [k for k in names
              if any(pat in k for pat in _SECRET_ENV_PATTERNS)]
    if leaked:
        raise RuntimeError(
            "improve+claude backend refuses to start: service initial env "
            f"contains secret-like variable name(s) {leaked!r} — improve "
            "worker can read /proc/self/environ of same-UID processes "
            "(R10). Put secrets in .env, not exported shell env.")


def _check_codex_subscription_expiry(auth_file: str, *, clock=None) -> None:
    """検査④ (設計書 §1.4、裁定 R4): codex+chatgpt の `auth_file` 内
    `chatgpt_subscription_active_until` を読み、期限切れなら起動拒否
    (ERROR)、7 日以内なら WARNING ログのみで起動は継続する。キー欠落・
    読み取り不能・形式不正は WARNING に留める (fail closed にしない —
    `auth.json` の形式は実測できていないため、裁定 R4 に従い誤検出で
    起動不能にしない)。`clock` はテスト注入用 (既定 `datetime.now(UTC)`)。"""
    import json as _json

    _logger = logging.getLogger("agentic_fx.service")
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    path = Path(auth_file).expanduser()
    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _logger.warning(
            "codex auth.json (%s) を読めない: %s — chatgpt_subscription_active_until "
            "を検査できない (fail closed にしない、裁定 R4)", path, e)
        return
    value = raw.get("chatgpt_subscription_active_until") if isinstance(raw, dict) else None
    if value is None:
        _logger.warning(
            "codex auth.json (%s) に chatgpt_subscription_active_until が無い "
            "— サブスク期限を検査できない (形式未実測、裁定 R4)", path)
        return
    try:
        active_until = datetime.fromisoformat(value)
        if active_until.tzinfo is None:
            active_until = active_until.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as e:
        _logger.warning(
            "chatgpt_subscription_active_until の形式が不正: %r (%s)", value, e)
        return
    if active_until <= now:
        raise RuntimeError(
            f"codex chatgpt subscription expired at {active_until.isoformat()} "
            "— renew before starting improve+codex")
    if active_until - now <= timedelta(days=7):
        _logger.warning(
            "codex chatgpt subscription expires soon: %s", active_until.isoformat())


def _check_cli_backend(settings, *, which: str) -> None:
    """<!-- precheck 2026-08-22: T1-M14 --> CLI backend 起動時検査 ①②③⑤
    (設計書 §1.4)。`which` は `"trade"` か `"improve"` — `getattr(settings.runner,
    which)` で対象の `RunnerChoice` を選ぶ。backend=local の環境では一切
    走らない。④ (codex サブスク期限) は improve+codex+chatgpt のみ発火する
    (trade+codex は `RunnerSettings._trade_backend_not_codex` が `Settings`
    構築時点で拒否するため、trade 側でこの分岐に到達しない)。

    **Minor 14 の再発防止**: 旧実装 (`_check_improve_backend`) は
    `settings.runner.improve` しか見なかったため、`runner.trade.backend=claude`
    構成では bin 解決も `--version` も認証ファイルもどれも未検査のまま
    Mission 実行時に初めて失敗していた (config は `trade.backend: claude`
    を許容する — trade+codex のみ拒否)。"""
    choice = getattr(settings.runner, which)
    backend = choice.backend
    if backend == "local":
        return
    if backend == "claude":
        bin_path = _resolve_cli_bin(settings.runner.claude.bin, require_elf=False)
        _check_cli_version(bin_path)
        _check_credentials_file(settings.runner.claude.credentials_file,
                                label="claude")
        _check_service_initial_env_has_no_secrets(settings)
    elif backend == "codex":
        bin_path = _resolve_cli_bin(settings.runner.codex.bin, require_elf=True)
        _check_cli_version(bin_path)
        if settings.runner.codex.provider == "chatgpt":
            _check_credentials_file(settings.runner.codex.auth_file,
                                    label="codex")
            _check_codex_subscription_expiry(settings.runner.codex.auth_file)
        elif settings.runner.codex.provider == "llama_swap":
            if not settings.improve.llama_swap_verified:
                raise RuntimeError(
                    "runner.codex.provider='llama_swap' requires "
                    "improve.llama_swap_verified=true (set only after "
                    "`afx improve verify-backend` passes — Task 13)")


def run_init(root: Path) -> int:
    cfg_dir = root / "config"
    example = cfg_dir / "settings.yaml.example"
    target = cfg_dir / "settings.yaml"
    if not example.exists():
        print(f"settings.yaml.example が見つかりません: {example}", file=sys.stderr)
        return 1
    if not target.exists():
        shutil.copy(example, target)
        print(f"作成: {target} (必要に応じて編集してください)")
    settings = load_settings(target)  # 検証を兼ねる

    (root / "logs").mkdir(parents=True, exist_ok=True)
    setup_technical_logging(root / "logs", settings.logging.level)

    # init_db は既存 DB のスキーマが旧形式なら移行 (例: ohlcv v1→v2 の table
    # rebuild) を実行する。移行が必要な場合は init_db が自動で
    # agentic.db.bak-ohlcv-v2 を作ってから rebuild する (db._migrate_ohlcv_v2
    # 参照) が、サービスは停止した状態で実行すること (WAL 越しの同時書き込み
    # は想定していない)。
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)

    clock = SystemClock()
    seeded = seed_default_sources(conn, clock.now())
    if seeded:
        print(f"基本ニュースソースを {seeded} 件登録しました")

    # 価格ソースの接続確認。**DataUnhealthy は警告に留めて init は成功させる**
    # (オフライン環境でも初期化を完了できるようにする。データ不健全時に取引を
    # 止める本防御線はサービス起動後の fail closed — 設計書 §5)。
    # ただし DataUnhealthy 以外は握り潰さない: 設定ミスや実装バグまで警告に
    # 落とすと init が「常に成功する」だけのコマンドになり、確認の意味が
    # 無くなる。この確認は state 更新より前に置いてあるので、想定外の例外で
    # 落ちた場合は未初期化のまま残り、起動ガードが引き続き止める。
    try:
        source = PriceProvider(conn, settings, clock).healthcheck(
            settings.pairs[0])   # pairs は空を config が弾く (min_length=1)
    except DataUnhealthy as e:
        # safe_error_text を再度通す (多層防御)。init の標準出力は人が見て
        # コピペする場所で、技術ログより秘密が漏れたときの帰結が重い
        print(f"警告: 価格ソースに接続できません ({safe_error_text(e)})。"
              "サービス起動後は fail closed で保護されます。")
    else:
        # 確認したペアを明示する。config は複数ペアを許すが healthcheck は
        # 先頭 1 ペアしか見ないため、無限定の「OK」は 2 ペア目以降が壊れて
        # いても OK に見える (レビュー指摘 Minor-3)。
        print(f"価格ソース OK ({settings.pairs[0]}, source={source})")

    # llama-swap 接続確認 (プラン 5 — 上書き 2)。失敗は警告のみ (init は成功させる)。
    _check_llama_swap(settings)

    # learning への切替はモード遷移ガード (§3) を通る mode コマンドのみ。
    # init はガードの迂回路にしない: trading 中は mode/autopilot に触れない。
    store = _state_store(root)
    if store.load().mode is Mode.TRADING:
        state = store.update(initialized=True)
        print("警告: 現在 trading モードです。init は mode/autopilot を変更しません "
              "(切替は mode コマンドを使用)。")
    else:
        state = store.update(initialized=True, mode=Mode.LEARNING,
                             autopilot=False)

    ActivityLog(root / "logs" / "activity.log").write(
        Category.SYSTEM, "init_completed",
        f"pairs={settings.pairs} mode={state.mode.value} "
        f"autopilot={'on' if state.autopilot else 'off'}")
    print("初期化が完了しました。`uv run main.py` でサービスを起動できます。")
    return 0


# ---- サービス本体配線 (プラン 5) -------------------------------------------


@dataclass
class App:
    conn_core: object
    conn_shell: object
    settings: object
    state: object
    activity: object
    broker: object
    executor: object
    provider: object
    econ: object
    collector: object
    rag: object
    trade_loop: object
    reflection: object
    scheduler: object
    commands: object
    registry: object
    core_lock: threading.RLock
    mission_watch: MissionWatch
    notifier: object
    runner: object
    owns_runner: bool
    clock: object
    instance_lock: object
    supervisor: object
    conn_supervisor: object
    stop_event: threading.Event = field(default_factory=threading.Event)
    health_latch: HealthLatch = field(default_factory=HealthLatch)
    watchdog_heartbeat: float = field(default_factory=time.monotonic)
    fatal_reason: str | None = None

    def close(self, *, busy_resources: frozenset[str] = frozenset()) -> list[str]:
        skipped: list[str] = []
        resources = [
            ("runner", lambda: self.runner.close()
             if self.owns_runner and hasattr(self.runner, "close") else None),
            ("rag", self.rag.close),
            ("conn_supervisor", self.conn_supervisor.close),
            ("conn_core", self.conn_core.close),
            ("conn_shell", self.conn_shell.close),
            ("instance_lock", self.instance_lock.close),
        ]
        for name, closer in resources:
            if name in busy_resources:
                skipped.append(name)
                continue
            try:
                closer()
            except Exception as e:  # noqa: BLE001 — continue closing owned resources
                _log.warning("App.close: %s failed: %s", name, safe_error_text(e))
        return skipped


# service.py:get_bars の既定引数 (datafeed/price_provider.py:get_bars) と
# 同じ値。ここが乖離すると保持期間検証が実際の要求と食い違う。
_DEFAULT_LOOKBACK_DAYS = 5


def _validate_cache_retention(settings) -> None:
    """起動時ガード: datafeed.cache_retention_days が構成済み intervals の
    live_window_days 最大値を下回っていないか (設計書 D2)。

    enabled に関わらず全既知チェーン source (mt5/twelvedata/yfinance) で
    検証する — config の enabled は運用中いつでも切り替わりうるため、
    「今 enabled な source だけ」を基準にすると、後から別 source を有効化
    した瞬間にキャッシュフォールバックが黙って壊れる余地を残す。
    """
    d = settings.datafeed
    for interval in d.intervals:
        for source in sources.NATIVE_INTERVALS:
            try:
                needed = cache_window.live_window_days(
                    source, interval, _DEFAULT_LOOKBACK_DAYS)
            except DataUnhealthy:
                continue  # この source は interval を提供も導出もできない
            if needed > d.cache_retention_days:
                raise RuntimeError(
                    f"datafeed.cache_retention_days={d.cache_retention_days} "
                    f"日は interval={interval!r} (source={source!r}) が要求 "
                    f"する {needed} 日を下回っています — "
                    "datafeed.cache_retention_days を "
                    f"{needed} 以上に増やすか、datafeed.intervals から "
                    f"{interval!r} を外してください")


def _validate_startup(settings) -> None:
    """起動時ガード (上書き 4): pairs 非空 + 実使用 schema の構文検証。

    配線ミス (例: 壊れた schema 定義) を起動時の RuntimeError で殺す —
    サイレントに Mission が全滅する事態を避ける。
    """
    if not settings.pairs:
        raise RuntimeError("settings.pairs is empty — cannot start service")
    schemas = (trade_intent_schema(settings.pairs), ANSWER_SCHEMA,
              reflection_cycle._SCHEMA)
    for schema in schemas:
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.exceptions.SchemaError as e:
            raise RuntimeError(f"invalid tool/output schema: {e}") from e
    if settings.plugin.producer_source not in ohlcv.KNOWN_OHLCV_SOURCES:
        raise RuntimeError(
            f"settings.plugin.producer_source={settings.plugin.producer_source!r} "
            f"is not a known source (known: {sorted(ohlcv.KNOWN_OHLCV_SOURCES)})")
    _validate_cache_retention(settings)


def _assert_tools_registered(registry: ToolRegistry, names: list[str]) -> None:
    """配線ミスの即時検出 (上書き 5): 必要なツールが登録されていることを確認する。"""
    missing = set(names) - set(registry.names())
    if missing:
        raise RuntimeError(f"tools not registered: {sorted(missing)}")


class _SupervisorAsk:
    """ask を supervisor 経由で実行する薄いラッパー (`_LockedAsk` の後継 —
    設計書 §3.3「ask の統一」。core_lock を直接掴まない)。"""

    def __init__(self, supervisor: MissionSupervisor,
                wait_timeout_sec: float) -> None:
        self._supervisor = supervisor
        self._wait_timeout_sec = wait_timeout_sec

    def ask_once(self, question: str) -> str:
        future = self._supervisor.try_submit("ask", question=question)
        if future is None:
            return ("(現在 Mission 実行中のため質問を受け付けられません。"
                    "しばらくして再試行してください)")
        try:
            return future.result(timeout=self._wait_timeout_sec)
        except TimeoutError:
            return "(Mission 失敗: ask がタイムアウトしました)"
        except Exception as e:  # noqa: BLE001 — 元の ask_once の service
            # boundary (trade_loop.ask_once 内) が normalize 済みの文字列を
            # 返す設計だが、supervisor 経由の Future.exception() 化で
            # 二重に例外化され得るため、ここでも最終防波堤を置く。
            return f"(Mission 失敗: {e})"


def _run_signal_maintenance(*, conn, signal_producer, approved, settings,
                            now: datetime) -> None:
    """`on_signal_maintenance` の実体 (裁定書 F-16/IM-10 — module レベル
    関数として抽出し、`build_app()` 全体を構築せずに単体テスト可能に
    する)。

    Task 7 申し送り → プラン 8 B 束で順序入替 (codex M⑤): lease 切れの
    claimed 行を先に reclaim_expired で pending へ戻し、その後に
    expire_stale で鮮度切れの pending を abandoned 化する。この順序により、
    reclaim で pending に戻った行が鮮度切れなら同じ tick 内で abandoned
    という終端状態に落ちる。旧順序 (expire → reclaim) では、その行は
    expire の時点でまだ claimed のため対象外となり、鮮度切れで claim され得
    ない pending のまま次の maintenance まで居残った。なお鮮度ゲート有効時
    (`freshness_bars is not None`) は `claim_oldest` の WHERE が
    `_FRESH_CONDITION` を含むため、stale な pending が mission に拾われる
    ことはない — 本順序の利得は「無駄な mission の実行の回避」ではなく、
    終端状態への即時収束と `expire_stale` の戻り値 (呼び出し側が通知件数に
    使う) の正確さである。呼び出し元 (Scheduler._run_data_hook) が
    fail-open で包む。
    """
    signals.reclaim_expired(conn, now=now,
                            lease_min=settings.plugin.signal_lease_min,
                            max_requeue=settings.plugin.signal_requeue_max)
    signals.expire_stale(conn, now=now,
                         freshness_bars=settings.plugin.signal_freshness_bars)
    signal_producer.evaluate_due_plugins(
        conn=conn, plugins=approved, now=now,
        source=settings.plugin.producer_source, settings=settings)


def build_app(root: Path, *, runner: AgentRunner | None = None,
              clock: Clock | None = None, quote_fn=None, spec_fn=None,
              bars_fn=None, embedding_fn=None,
              provider: PriceProvider | None = None,
              stop_event: threading.Event | None = None) -> App:
    """全部品を配線して `App` を返す。

    quote_fn / spec_fn / bars_fn / embedding_fn は E2E テストの注入点 (None なら
    各部品の実装を使う — build 後の patch では bound 済みクロージャに届かないため
    注入で解決する)。

    `provider` (プラン 8 park 返済): 非 None の場合、`PriceProvider` の内部構築と
    `quote_fn`/`spec_fn`/`bars_fn` の bound-method 差し替えを丸ごとスキップし、
    渡されたインスタンスをそのまま使う。`quote_fn`/`spec_fn`/`bars_fn` と併用した
    場合は `ValueError` を送出して排他を強制する (provider が全挙動を握る seam
    のため、併用は static な設定ミスとして即座に検出する)。

    **注入対象外**: `healthcheck()` は個別関数注入 (`quote_fn`/`spec_fn`/`bars_fn`)
    では到達不能である (fix round 1 F2)。`PriceProvider.healthcheck` は
    `self.get_quote(...)` に加えて `self.get_bars(...)` を呼ぶが、`get_bars`
    は quote_fn/spec_fn/bars_fn のどれにもマップされていない。

    一方 mission registry のツールは以下の 2 つの経路で provider を束縛する:
    (a) `provider=` 注入した場合: 同じインスタンスが registry に透通される
    ため、mission registry のツールも注入 provider を使う。
    (b) `provider=` 注入しない場合: registry が内部で新規構築した
    PriceProvider インスタンスを束縛する。

    `quote_fn`/`spec_fn`/`bars_fn` (個別関数注入) は (a)(b) いずれの場合でも
    mission registry のツールに到達しない。決定論的な E2E テストで mission
    registry のツール挙動を注入制御する場合、`provider=` パラメータで
    カスタム PriceProvider を渡すこと (本 E2E テスト `tests/test_e2e_phase1.py`
    参照)。
    """
    clock = clock or SystemClock()
    stop_event = stop_event if stop_event is not None else threading.Event()
    settings = load_settings(root / "config" / "settings.yaml")

    state = _state_store(root)
    health_latch = HealthLatch()
    activity = ActivityLog(
        root / "logs" / "activity.log",
        on_write_failure=lambda e: health_latch.record_failure(
            f"activity write failed: {safe_error_text(e)}"))
    watchdog_heartbeat = time.monotonic()

    # FC-2 (裁定書): recover_interrupted は起動時に status='running' の
    # 全 mission を無条件に interrupted 化する。二重起動があると、後発
    # プロセスが先発の稼働中 Mission を誤って終端し claim 済み signal を
    # 横取り requeue しかねないため、DB 接続・回収より**前**にプロセス
    # 排他 flock を取得する。取得できなければ起動を中止する (fail
    # closed — InstanceAlreadyRunning は呼び出し元の run_service/CLI
    # エントリまで伝播させ、非ゼロ終了させる)。
    instance_lock = acquire_instance_lock(root / "data")

    try:
        conn_core = connect(root / "data" / "agentic.db")
        init_db(conn_core)
        # プラン 8 (codex C-5): 前回停止時に running のまま残った mission と、
        # それが claim していた signal を同一トランザクションで回収する。
        # 既存の signals.reclaim_expired (403-407 行付近、lease ベースの
        # 一般的な回収) より前に置く — running mission の signal は claimed_at
        # が直近であり得るため lease ベースでは長時間拾われない。
        missions.recover_interrupted(
            conn_core, now=clock.now(),
            max_requeue=settings.plugin.signal_requeue_max)
        conn_shell = connect(root / "data" / "agentic.db")

        # プラン 8 (Task 13, レビュー反映1回目 IM-1/P8-02): commit-pre 相
        # (lock 外) が使う読取専用の lock 外接続。core_lock は取らない
        # (Task 15 で使用開始)。Global Constraints「読取専用接続」の性質を
        # connect_readonly (URI mode=ro) で構造的に強制する — query_only
        # PRAGMA は使わない (プロセス内の別接続からは無効化されうるため)。
        conn_supervisor = connect_readonly(root / "data" / "agentic.db")

        if provider is not None:
            # provider と quote_fn/spec_fn/bars_fn は排他的: provider が指定されたら
            # 他の注入点は併用不可 (fail closed: 混合による silent の誤動作を防止)。
            if any([quote_fn is not None, spec_fn is not None, bars_fn is not None]):
                raise ValueError(
                    "provider と quote_fn/spec_fn/bars_fn は併用不可 — "
                    "provider が全挙動を握る seam であり、個別関数との混合は "
                    "設定ミス (provider 側の挙動が一部無視される)。"
                    "provider を使わない場合のみ個別関数を指定してください。")
            # プラン 8 park 返済: 呼び出し側が provider の全挙動を握る
            # (quote_fn/spec_fn/bars_fn の bound-method 差し替えは行わない)。
            # 上の ValueError で併用を弾いた後なので 3 つとも必ず None —
            # 無条件に provider の束縛メソッドを採る (`x if x is not None else`
            # の形は到達しない分岐を残し「併用可能」と誤読させる)。
            quote_fn = provider.get_quote
            spec_fn = provider.spec
            bars_fn = provider.latest_1m_bar
        else:
            provider = PriceProvider(conn_core, settings, clock)
            # 注入された quote_fn/spec_fn/bars_fn は provider 自身の束縛メソッドにも
            # 反映する (Task 8 E2E で実測)。実際に内部 self-call が存在するのは
            # `self.get_quote` だけ (`PriceProvider._rate_of` および `healthcheck`
            # から呼ばれる — `to_account_rate` の換算レート解決がここを経由する)。
            # `self.spec` / `self.latest_1m_bar` は現時点で provider 内部からは
            # 一切呼ばれていない (呼び出し元は Executor/Scheduler にローカル変数
            # 経由で渡した spec_fn/bars_fn のみ) — 以下 2 行は今の挙動には効いて
            # いない。それでも残しているのは予防的措置 (fix round 1 F1): 将来
            # provider 内部に `self.spec(...)` / `self.latest_1m_bar(...)` の
            # 自己呼び出しが追加されたとき、ここが無いと `get_quote` と同じ
            # 「注入がローカル変数にしか反映されず内部呼び出しをすり抜ける」バグを
            # 無音で再発させる (quote_fn 側の実例がまさにそれだった)。
            # 注意: `self.get_bars(...)` (`latest_1m_bar`/`healthcheck` から既に
            # 呼ばれている) はこの 2 行では捕捉できない — 別メソッド名なので
            # `provider.latest_1m_bar = bars_fn` は届かない。恒久的に注入対象外
            # (docstring の fix round 1 F2 注記を参照)。
            if quote_fn is not None:
                provider.get_quote = quote_fn
            else:
                quote_fn = provider.get_quote
            if spec_fn is not None:
                provider.spec = spec_fn
            else:
                spec_fn = provider.spec
            if bars_fn is not None:
                provider.latest_1m_bar = bars_fn
            else:
                bars_fn = provider.latest_1m_bar

        def rate_fn(ccy: str, account_ccy: str, now: datetime, *,
                    deadline_check: Callable[[str], None] | None = None):
            return provider.to_account_rate(
                ccy, account_ccy, reference_ts=now,
                max_skew_min=settings.datafeed.conversion_skew_max_min,
                deadline_check=deadline_check)

        econ = EconCalendar(conn_core, activity, clock,
                            timeout_sec=settings.worker.data_hook_timeout_sec)
        rag = Rag(root / "data" / "rag", embedding_function=embedding_fn,
                 lock_timeout_sec=settings.worker.rpc_timeout_sec)
        collector = NewsCollector(conn_core, rag, activity, clock,
                                  timeout_sec=settings.worker.data_hook_timeout_sec)
        broker = PaperBroker(conn_core, settings, clock)
        notifier = Notifier(enabled=settings.discord.enabled,
                            webhook_url=os.environ.get("DISCORD_WEBHOOK_URL"))
        executor = Executor(conn=conn_core, broker=broker, settings=settings,
                            state_store=state, activity=activity,
                            notifier=notifier, clock=clock,
                            quote_fn=quote_fn, spec_fn=spec_fn, rate_fn=rate_fn)

        # プラン 7 Task 3: plugins/ 直下の承認済み plugin をロードする。反映は
        # 次回起動時のみ (hot reload しない — YAGNI)。plugins/ が存在しない環境
        # (未使用のデフォルト) でも approved_plugins は [] を返し起動を妨げない。
        plugins_dir = root / "plugins"
        approved = plugin_loader.approved_plugins(conn_core, plugins_dir)

        # プラン 7 Task 8: signal producer (承認済み signal/strategy plugin の
        # 評価 → signals キュー投入)。producer は評価 cursor をメモリに持つ
        # ため App 寿命で 1 個だけ生成する (再生成 = cursor 喪失)。
        signal_producer = SignalProducer()

        # プラン 8 worker 基盤: 親 (ここ) と子 (mission_worker.py) が同一関数
        # (build_mission_registry) でツール配線を組み立てる。親は注入された provider
        # (あるいは内部構築した provider) を registry に渡し、同じインスタンスを
        # registry のツールが束縛する (テスト注入 seam)。子は provider を渡さず、
        # readonly=True で内部構築する。
        registry = build_mission_registry(
            "trade", conn_core, settings, clock, rag, activity=activity,
            indicator_plugins=approved, provider=provider)
        # 上書き 4/5: 配線ミスは起動時 RuntimeError で殺す (registry 組み立て後)
        _validate_startup(settings)
        _check_cli_backend(settings, which="trade")
        _check_cli_backend(settings, which="improve")
        _assert_tools_registered(registry, _TRADE_TOOLS)

        owns_runner = runner is None
        def _on_rpc_leak() -> None:
            health_latch.record_failure(
                "RAG RPC dispatcher leaked past rpc_timeout_sec")
            stop_event.set()
        if runner is None:
            runner = WorkerRunner(root=root, settings=settings, clock=clock,
                                  rag=rag, worker_profile="trade",
                                  stop_event=stop_event,
                                  on_rpc_leak=_on_rpc_leak)

        policy = Policy(root / "policy" / "directives.md")
        # 上書き 3: MissionWatch は 1 インスタンスを trade_loop / reflection に共有注入
        mission_watch = MissionWatch()

        core_lock = threading.RLock()

        # プラン 8 (Task 15, レビュー反映1回目 裁定書 F-6/CR-5): TradeLoop の
        # provider は healthcheck 専用 (lock 外で呼ばれる) — 書込可能な
        # conn_core 版を渡すと healthcheck 内の get_bars が無保護で conn_core
        # を書き込む (Global Constraints 違反)。conn_supervisor (RO) +
        # readonly=True で構築した専用インスタンスを**常に**渡す (レビュー
        # 3 周目 codex E1 — レビュー2周目 codex D2 の「注入時は provider を
        # そのまま流用する」は RO 制約を打ち消しており指揮者の指示ミスだった:
        # quote_fn/spec_fn/bars_fn だけを注入し provider= は注入しない
        # 呼び出し元では、392 行の else 分岐で書込可能な conn_core 版
        # provider が構築されるため、それを healthcheck_provider として
        # そのまま使うと F-6/CR-5 が塞いだ違反が supervisor スレッドから
        # core_lock 非保持で再発する)。
        # 注意: readonly=True のため healthcheck はもう ohlcv キャッシュを
        # 温めない (get_bars/derive の upsert_cache_bars がスキップされる) —
        # これは意図した挙動であり退行ではない。CR-4 と同じ理由でキャッシュの
        # 一次的な書き手は scheduler tick (mark-to-market 等、既存の conn_core
        # 版 provider) であり続けるため、healthcheck が書かなくてもキャッシュ
        # 鮮度は保たれる。
        # scheduler tick の processed-bar marking が書き込み可能 provider 経由で
        # 1m cache を継続的に温める。1h は live 1h が検証を通れば直接保存され、
        # 通らない場合は保存済み 1m から cache(1m→1h derived) として復元される。
        # readonly Mission provider はこの二段構えの書き手ではない。
        healthcheck_provider = PriceProvider(conn_supervisor, settings,
                                             clock, readonly=True)
        # RO と stub 尊重を両立させる: `provider` (388-420 行) に適用した
        # のと同じ注入バインドを healthcheck_provider にも適用する。この
        # 時点で quote_fn/spec_fn/bars_fn は (注入されていれば呼び出し元の
        # 関数、されていなければ `provider` の束縛メソッド) のいずれかで
        # 必ず非 None。`get_bars` はどの注入点にもマップされない (docstring
        # 313-318 行、fix round 1 F2) ため意図的に上書きしない —
        # healthcheck_provider 自身の (readonly=True・conn_supervisor 版)
        # get_bars がそのまま使われ、F-6/CR-5 の保護は保たれる。
        healthcheck_provider.get_quote = quote_fn
        healthcheck_provider.spec = spec_fn
        healthcheck_provider.latest_1m_bar = bars_fn
        trade_loop = TradeLoop(conn=conn_core, runner=runner, settings=settings,
                               executor=executor, provider=healthcheck_provider,
                               econ=econ, policy=policy, activity=activity,
                               notifier=notifier, clock=clock,
                               core_lock=core_lock, conn_supervisor=conn_supervisor,
                               watch=mission_watch)
        reflection = ReflectionCycle(conn=conn_core, runner=runner, rag=rag,
                                     settings=settings, activity=activity,
                                     clock=clock, core_lock=core_lock,
                                     watch=mission_watch)

        def _trade_fn(trigger: str):
            # プラン 8 (Task 15): TradeLoop 自身が prepare/commit-core で
            # core_lock を保持する五相構造になったため、ここでは lock を
            # 掴まない (二重取得は RLock で技術的には安全だが、run 相の
            # 間ずっと lock を保持したままになり Task 15 の目的を無効化する)。
            return trade_loop.run_once(trigger)

        def _reflection_fn():
            # プラン 8 (Task 16): ReflectionCycle 自身が prepare/commit-core で
            # core_lock を保持する三相構造になったため、ここでは lock を掴まない。
            return reflection.run_pending()

        def _ask_fn(question: str) -> str:
            # プラン 8 (Task 15): TradeLoop.ask_once も三相構造になったため
            # ここでは lock を掴まない。
            return trade_loop.ask_once(question)

        supervisor = MissionSupervisor(
            trade_fn=_trade_fn, reflection_fn=_reflection_fn, ask_fn=_ask_fn)

        def on_trade_mission(trigger: str) -> bool:
            # trigger は scheduler._trade_mission_due() が返した起動理由。
            # supervisor.try_submit が受理すれば True (scheduler 側が cron
            # 締切を前進させる判断材料になる — 設計書 §3.3)。
            return supervisor.try_submit("trade", trigger=trigger) is not None

        # プラン 7 Task 8: signal 起動の判定・保守処理を Scheduler へ配線する。
        def on_signal_maintenance(now: datetime) -> None:
            _run_signal_maintenance(conn=conn_core, signal_producer=signal_producer,
                                    approved=approved, settings=settings, now=now)

        def on_cache_maintenance(now: datetime) -> None:
            # プラン 9 Task 16: ohlcv_cache の保持ポリシー。有界バッチ
            # (_OHLCV_PRUNE_BATCH_LIMIT) × 毎 maintenance 実行で、初回の
            # 大量削除 (既存蓄積分) の lock 保持窓を抑えつつ、定常状態では
            # 1 回の呼び出しで日次増分に追いつく (設計書 D2)。
            cutoff = now - timedelta(days=settings.datafeed.cache_retention_days)
            ohlcv.prune_cache(conn_core, cutoff=cutoff,
                              limit=_OHLCV_PRUNE_BATCH_LIMIT)

        def signal_due_fn(now: datetime) -> bool:
            # D2: オープンポジション or pending_fill の注文が無いなら signal
            # 起動は無意味 (新規建玉を提案しても executor が gate で弾くだけ
            # ではなく、そもそも判断 Mission を起こす価値が薄い運用判断)。
            if not orders.list_by_status(conn_core, "open", "pending_fill"):
                return False
            if not signals.pending_exists(conn_core):
                return False
            return missions.signals_rate_ok(conn_core, now, settings)

        scheduler = Scheduler(conn=conn_core, executor=executor,
                              settings=settings, state_store=state,
                              activity=activity, bars_fn=bars_fn,
                              on_trade_mission=on_trade_mission,
                              on_news_cycle=collector.collect,
                              on_econ_cycle=econ.refresh,
                              on_signal_maintenance=on_signal_maintenance,
                              on_cache_maintenance=on_cache_maintenance,
                              signal_due_fn=signal_due_fn,
                              stop_event=stop_event)

        # 起動時 reclaim 1 回 (コントローラ裁定): 前回停止時に claimed のまま
        # 残った signal を、次の tick を待たずに起動直後から回収対象にする。
        signals.reclaim_expired(conn_core, now=clock.now(),
                                lease_min=settings.plugin.signal_lease_min,
                                max_requeue=settings.plugin.signal_requeue_max)

        # Commands は conn_shell 束縛の broker を持つ (conn_core をシェルスレッドから触らない)
        shell_broker = PaperBroker(conn_shell, settings, clock)
        ask_wait_timeout_sec = (settings.llama_swap.timeout_sec
                                + settings.worker.worker_grace_sec
                                + settings.worker.worker_terminate_grace_sec + 10.0)
        commands = Commands(conn=conn_shell, state_store=state,
                            broker=shell_broker,
                            trade_loop=_SupervisorAsk(supervisor, ask_wait_timeout_sec),
                            activity=activity, log_dir=root / "logs", clock=clock,
                            health_latch=health_latch)
        return App(conn_core=conn_core, conn_shell=conn_shell, settings=settings,
                   state=state, activity=activity, broker=broker,
                   executor=executor, provider=provider, econ=econ,
                   collector=collector, rag=rag, trade_loop=trade_loop,
                   reflection=reflection, scheduler=scheduler, commands=commands,
                   registry=registry, core_lock=core_lock,
                   mission_watch=mission_watch, notifier=notifier,
                   runner=runner, owns_runner=owns_runner, clock=clock,
                   instance_lock=instance_lock, supervisor=supervisor,
                   conn_supervisor=conn_supervisor, stop_event=stop_event,
                   health_latch=health_latch,
                   watchdog_heartbeat=watchdog_heartbeat,
                   fatal_reason=None)
    except BaseException:
        # 2 周目レビュー (sonnet Minor / KAT-Coder Critical): 素の
        # `instance_lock.close()` だと close 自身が送出した例外が伝播し、
        # **元の失敗原因が呼び出し元から見えなくなる** (元の例外は __context__
        # に退避されるだけで、except 節やエントリの終了コード判定は新しい例外を
        # 見る)。解放の失敗より原因の伝播を優先する — ロックは fd なので
        # プロセス終了時に OS が回収する。
        try:
            instance_lock.close()
        except Exception:  # noqa: BLE001 — 元の例外を握り潰さないための抑制
            pass
        raise


def build_splash(app: App) -> str:
    """起動スプラッシュ。項目は最小でよい (運用しながら調整 — 設計書 §8)。

    transcript_json は機微データなので出さない (grep で自己確認済み)。
    """
    s = app.state.load()
    balance, equity = app.commands.broker.equity()  # conn_shell 側
    pending = len(approvals.pending(app.conn_shell))
    limits = len(orders.list_by_status(app.conn_shell, "pending_fill"))
    return (
        "=== agentic-fx ===\n"
        f"mode: {s.mode.value} / autopilot: {'on' if s.autopilot else 'off'}"
        f" / kill switch: {'LATCHED' if s.kill_switch_latched else 'ok'}\n"
        f"pairs: {', '.join(app.settings.pairs)}\n"
        f"runner: {app.settings.runner.trade.backend}"
        f" ({app.settings.runner.trade.model})\n"
        f"risk: {app.settings.risk.risk_per_trade_pct}%/trade,"
        f" DD kill {app.settings.risk.drawdown_kill_pct}%,"
        f" daily {app.settings.risk.daily_loss_limit_pct}%\n"
        f"残高: {balance:,.0f} / 承認待ち: {pending} / 未約定指値: {limits}\n"
        "コマンドは help を参照。stop で終了。")


def _scheduler_tick_once(app: App) -> None:
    """scheduler_thread の 1 tick 分 (抽出 — 単体テスト用シーム)。

    app.clock.now() を読んで scheduler に渡すことを固定する。
    """
    with app.core_lock:
        app.scheduler.tick(app.clock.now())


def _watchdog_tick(app: App) -> None:
    """1 回分の watchdog 監視 (上書き 3)。activity/notifier の失敗はスレッドを
    殺さない — 呼び出し元 (watchdog スレッド) 側も広い try で包む。

    missions 行には書かない (finalize の所有者は
    `agentic_fx.loops.mission_finalize.finalize_mission` のみ — TradeLoop /
    ReflectionCycle の commit-core 相と、各ループの外側 `finally` の
    fail-closed 経路から呼ばれる。二重終端は CAS が防ぐ)。
    **`_run_recorded` はプラン 8 Task 15/16 で廃止済み** — 旧名で grep しても
    見つからない (レビュー 2 周目の指摘)。
    """
    entry = app.mission_watch.breached(grace_sec=60)
    if entry is None:
        return
    elapsed = app.mission_watch.time_fn() - entry.started
    try:
        app.activity.write(
            Category.SYSTEM, "mission_watchdog_breach",
            f"mission_id={entry.mission_id} loop={entry.loop} "
            f"elapsed={elapsed:.0f}s (timeout={entry.timeout_sec:.0f}s)")
    except Exception:  # noqa: BLE001
        _log.exception("watchdog activity write failed")
    try:
        app.notifier.send(
            f"[agentic-fx] Mission #{entry.mission_id} ({entry.loop}) が"
            f" 想定時間 ({entry.timeout_sec:.0f}s) を超過しています"
            f" (経過 {elapsed:.0f}s)")
    except Exception:  # noqa: BLE001
        _log.exception("watchdog notifier send failed")
    try:
        # Mission あたり 1 回だけ通知する (breached() は notified=True 以降 None を返す)
        app.mission_watch.mark_notified(entry.mission_id)
    except Exception:  # noqa: BLE001
        _log.exception("watchdog mark_notified failed")


def _thread_has_died(thread_obj) -> bool:
    """**起動前**と**死亡後**を区別する (レビュー2周目 codex Important)。

    `is_alive()` は「まだ start していないスレッド」でも `False` を返すため、
    起動順に依存した誤検出が起きる — 実測で両方向が発生した:
    ①`wd.start()` が先だと watchdog が未起動の scheduler を死亡と誤判定
    ②`th.start()` が先だと scheduler の初回 tick (`last = 0.0` なので待ち
    無しで走る) が未起動の watchdog を死亡と誤判定。**起動順を入れ替える
    だけでは原理的に片方しか塞げない。**

    `Thread.ident` は start 前は `None`、start 後は**死亡後も値が残る**ので、
    「ident が付いていて、かつ生きていない」= 本当に死んだ、と判定できる。
    """
    return getattr(thread_obj, "ident", 1) is not None and not thread_obj.is_alive()


def _record_fatal(app: App, stop_event: threading.Event, reason: str) -> None:
    if app.fatal_reason is None:
        app.fatal_reason = reason
    # レビュー1周目 C-1: 記録・通知の**どちらの失敗でも** stop_event.set()
    # へ到達させる。ここが無防備だと、呼び出し元 (watchdog/scheduler) は
    # `except Exception` で握って周期的に同じ経路を再実行し、毎回同じ行で
    # 落ちるため**停止が永久にトリガーされない** (fatal_reason だけが立つ)。
    try:
        app.activity.write(Category.SYSTEM, "fatal_thread_death", reason)
    except Exception:  # noqa: BLE001
        _log.exception("failed to record fatal_thread_death")
    try:
        app.notifier.send(f"[agentic-fx] 致命的エラー: {reason} — 停止します")
    except Exception:  # noqa: BLE001
        _log.exception("failed to notify fatal_thread_death")
    stop_event.set()


def _default_dispatch_ceiling_sec(app: App) -> float:
    w = app.settings.worker
    per_mission = (app.settings.llama_swap.timeout_sec
                   + w.worker_grace_sec + w.worker_terminate_grace_sec)
    return per_mission * 4 + 60.0


def _watchdog_check(app: App, scheduler_thread_obj: threading.Thread,
                    stop_event: threading.Event, *,
                    heartbeat_grace_sec: float = 30.0,
                    dispatch_ceiling_sec: float | None = None) -> None:
    # レビュー1周目 I-3 に伴う追加: **停止シーケンスが始まっていたら何も
    # 判定しない。** 停止中は main が supervisor を shutdown しスレッドが
    # 順に終了していくため、その終了を「死亡」と誤認して fatal をラッチ
    # すると graceful な停止が終了コード 1 になる。停止の実行主体は常に
    # main であり、停止中の監視は不要 (scheduler→watchdog 方向にも同じ
    # ガードが既にある)。
    if stop_event.is_set():
        return
    if _thread_has_died(scheduler_thread_obj):
        _record_fatal(app, stop_event, "scheduler thread is dead")
        return
    if not app.supervisor.is_alive():
        _record_fatal(app, stop_event, "supervisor thread is dead")
        app.supervisor.fail_pending(exc=RuntimeError("supervisor thread died"))
        return
    if time.monotonic() - app.supervisor.heartbeat > heartbeat_grace_sec:
        _record_fatal(app, stop_event, "supervisor heartbeat stale")
        return
    ceiling = (dispatch_ceiling_sec if dispatch_ceiling_sec is not None
               else _default_dispatch_ceiling_sec(app))
    busy_since = app.supervisor.busy_since
    if busy_since is not None and time.monotonic() - busy_since > ceiling:
        _record_fatal(app, stop_event,
                      f"supervisor dispatch exceeded ceiling ({ceiling:.0f}s)")
        app.supervisor.fail_pending(exc=RuntimeError("supervisor dispatch hung"))
        return
    _watchdog_tick(app)


def _check_watchdog_health(app: App, watchdog_thread_obj: threading.Thread,
                           stop_event: threading.Event, *,
                           wd_heartbeat_grace_sec: float = 90.0) -> None:
    # レビュー3周目 F0: `_watchdog_check` (watchdog→scheduler 方向) と**対称**
    # にする。1周目 I-3 のガードは片方向にしか転写されておらず、こちらは
    # 呼び出し元 (`scheduler_thread`) の外側ガードだけに頼っていた。外側
    # ガードは check-then-act であり、通過した直後に main が
    # `stop_event.set()` すると、graceful に終了した watchdog を「死亡」と
    # 誤認して fatal をラッチし、正常停止が終了コード 1 になる。停止の実行
    # 主体は常に main なので、停止中の監視はどちらの方向にも不要。
    if stop_event.is_set():
        return
    if _thread_has_died(watchdog_thread_obj):
        _record_fatal(app, stop_event, "watchdog thread is dead")
        return
    if time.monotonic() - app.watchdog_heartbeat > wd_heartbeat_grace_sec:
        _record_fatal(app, stop_event, "watchdog heartbeat stale")


def _busy_resources_after_join(scheduler_still_busy: bool,
                               supervisor_still_busy: bool) -> frozenset[str]:
    busy: set[str] = set()
    if scheduler_still_busy or supervisor_still_busy:
        busy.add("conn_core")
        # レビュー2周目 codex Critical: `instance_lock` は単なる close 対象
        # ではなく**残存 App 全体の単一起動所有権**を表す。残存スレッドが
        # 旧 App の DB/broker/runner を使っているのに flock を解放すると、
        # 別プロセス (または同一プロセスの再 build_app) が起動でき、後発の
        # `recover_interrupted` が**旧 supervisor がまだ処理している
        # `running` mission を `interrupted` に書き換える**。設計書 §5.6 の
        # 「使用中資源は閉じず leak を選ぶ」はこの資源にも適用される。
        #
        # (レビュー3周目 F1) **この leak は現実的にはプロセス終了でしか
        # 回収されない。** flock は open file description に紐づくので、
        # 解放されるのは lock を保持する file object が close された時
        # (明示 close または参照喪失) — だが busy skip した以上その file
        # object は残存 App が握ったままであり、close される契機が無い。
        # 同一プロセス内で `run_service` を再度呼ぶ経路を将来足すと、
        # **別の file object で開き直しても競合し** (これは同一プロセス内でも
        # 成立する。`store/instance_lock.py` のテストが前提にしている挙動)、
        # 以降ずっと `InstanceAlreadyRunning` になる。
        # 現状の本番エントリ (console_script / `__main__`) はどちらも
        # `run_service` の戻り値を終了コードにしてプロセスを終えるため成立
        # している。**「run_service は 1 プロセスにつき 1 回」が契約である。**
        busy.add("instance_lock")
    if supervisor_still_busy:
        busy.add("conn_supervisor")
    return frozenset(busy)


def _exit_code(app: App, scheduler_alive: bool,
               supervisor_alive: bool) -> int:
    if app.fatal_reason is not None:
        return 1
    return 1 if scheduler_alive or supervisor_alive else 0


def run_service(root: Path, *, daemon: bool = False,
               _stop_event: threading.Event | None = None) -> int:
    """`_stop_event` はテスト用のシーム (fix round 1 F4)。省略時は内部で
    `threading.Event()` を生成する (本番挙動は不変)。テストは事前に `.set()`
    済みのイベントや、`.wait()` から `KeyboardInterrupt` を送出するカスタム
    実装を注入することで、実スリープ・実シグナルなしに shutdown 経路を検証
    できる。"""
    ensure_initialized(root)
    settings = load_settings(root / "config" / "settings.yaml")
    setup_technical_logging(root / "logs", settings.logging.level,
                            daemon=daemon)
    stop_event = _stop_event if _stop_event is not None else threading.Event()
    app = build_app(root, stop_event=stop_event)

    warning = Policy(root / "policy" / "directives.md").size_warning()
    if warning:
        print(warning)
    print(build_splash(app))
    app.activity.write(Category.SYSTEM, "service_started",
                       f"daemon={daemon}")

    scheduler_busy = threading.Event()

    def scheduler_thread() -> None:
        last = 0.0
        while not stop_event.is_set():
            if time.monotonic() - last >= 60:
                last = time.monotonic()
                if stop_event.is_set():
                    break  # 停止フェーズ: 新しい tick を開始しない
                scheduler_busy.set()
                try:
                    _scheduler_tick_once(app)
                except Exception:  # noqa: BLE001
                    _log.exception("tick failed")
                finally:
                    scheduler_busy.clear()
                if not stop_event.is_set():
                    try:
                        _check_watchdog_health(app, wd, stop_event)
                    except Exception:  # noqa: BLE001
                        _log.exception("watchdog health check failed")
            stop_event.wait(1)

    # レビュー1周目 I-3: dispatch ceiling は**ここで 1 度だけ**求め、
    # watchdog のチェックと supervisor.join の budget が同じ値を共有する
    # 構造にする。両者が独立に同じ関数を呼ぶ形だと、片方だけを書き換える
    # 変異 (実測で全件緑のまま生存) を構造的に防げない。**join budget が
    # ceiling より小さいと main が先に join を諦め、watchdog の
    # `busy_since` 軸が構造的に到達不能になり `fatal_reason` がその経路で
    # 永久にラッチしない** (裁定 B の決め手)。
    dispatch_ceiling_sec = _default_dispatch_ceiling_sec(app)

    def watchdog_thread() -> None:
        # レビュー1周目 I-3: **チェックを待ちより先に行う。** 以前は
        # `wait(30)` が先だったため、起動直後 30 秒はスレッド死亡を一切
        # 検出できない盲窓があった (資金保護が止まっていても気付けない)。
        while not stop_event.is_set():
            app.watchdog_heartbeat = time.monotonic()
            try:
                _watchdog_check(app, th, stop_event,
                                dispatch_ceiling_sec=dispatch_ceiling_sec)
            except Exception:  # noqa: BLE001 — スレッドを殺さない
                _log.exception("watchdog tick failed")
            stop_event.wait(30)

    # F2 (fix round 1): シグナルハンドラはスレッド起動より**前**に登録する。
    # 以前はスレッド起動後に登録しており、その間に SIGTERM が届くとデフォルト
    # 動作 (即時終了) で graceful shutdown 経路を経ずにプロセスが死ぬ窓が
    # あった。
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    if daemon:
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    app.supervisor.start()
    # **起動順に依存しない。** 監視側は `_thread_has_died` (ident で
    # 起動前/死亡後を区別) を使うため、どちらを先に起動しても
    # 「構築済みだが未起動」を死亡と誤判定しない。1 周目で起動順の
    # 入れ替えを試みたが、それでは逆方向のレース (scheduler の初回 tick が
    # 未起動の watchdog を見る) が開くだけだった (レビュー2周目 codex)。
    # 構築を両方先に済ませるのは `scheduler_thread` が参照する `wd` を
    # 束縛しておくため。
    th = threading.Thread(target=scheduler_thread, daemon=True)
    wd = threading.Thread(target=watchdog_thread, daemon=True)
    th.start()
    wd.start()

    try:
        if daemon:
            while not stop_event.is_set():
                try:
                    stop_event.wait(1)
                except KeyboardInterrupt:
                    # F2: シグナルハンドラを経ずに KeyboardInterrupt が
                    # 素通りすると graceful shutdown 経路 (finally) を丸ごと
                    # 飛ばして例外がそのまま伝播していた。ここで捕捉して
                    # stop_event を立てるだけにし、後続の finally に処理を
                    # 委ねる。
                    stop_event.set()
        else:
            from agentic_fx.shell import run_shell
            run_shell(app.commands, stop_event)
            if stop_event.is_set() and app.fatal_reason is not None:
                print("警告: システムスレッドの異常を検出したため停止します。")
    finally:
        # F2: 待機部で想定外の例外 (KeyboardInterrupt 含む) が起きても、
        # shutdown 手順 (stop・join・close・記録) は必ず実行する。ここでは
        # 例外を握りつぶさない (return を置かない) — 記録後、元の例外があれば
        # そのまま再送出される。
        stop_event.set()
        # **(レビュー 3 周目 codex E3)** supervisor.shutdown() を
        # th.join() より前に呼ぶ — stop_event.set() の時点で既に走って
        # いた scheduler tick は on_trade_mission → supervisor.try_submit
        # まで到達しうる。shutdown() (新規受付停止) が後回しだと、停止
        # 処理の最中に新しい trade+reflection Mission が受理されてしまう。
        # shutdown() 自体はブロックしない (queue 内の未着手ジョブを
        # fail_pending するだけ) ので、位置を早めても th.join() の意味は
        # 変わらない。
        # レビュー1周目 (指揮者所見): `shutdown` → `fail_pending` は
        # `if not future.done()` の直後に `future.set_exception()` を呼ぶ
        # ため、supervisor スレッドが間で完了させると `InvalidStateError`
        # が伝播する。裸で呼ぶと **th.join / app.close / 記録が全て飛ぶ**
        # (資源リーク + 元の例外が置き換わる)。同ファイルの
        # `instance_lock.close()` と同じ扱いに揃える。
        try:
            app.supervisor.shutdown(
                drain_exc=RuntimeError("service shutting down"))
        except Exception:  # noqa: BLE001 — 停止シーケンスは必ず最後まで走らせる
            _log.exception("supervisor.shutdown() failed during shutdown")
        # scheduler スレッドの終了を確認する。
        # **(レビュー 2 周目 codex D1 — この task が壊した前提の修復)**
        # 旧コメント「tick は core_lock 下で走るため join 完了 = 実行中
        # Mission も完了」はプラン8 Task 15 (五相再構成) でもう成立しない
        # — run 相 (WorkerRunner.run) と commit-pre/commit-post は
        # core_lock を保持しないため、scheduler スレッドは Mission が
        # supervisor スレッドで実行中でも即座に tick を終えて th.join が
        # 成功しうる。実際に Mission (commit-core 含む) を実行しているのは
        # supervisor スレッドなので、以下で supervisor の join も判定に
        # 加える (完全な停止状態機械は Task 19 の担当 — ここでは
        # 「join 完了 = 実行中 Mission も完了」という壊れた不変条件の
        # 最小修復に留める)。
        th.join(timeout=30)
        scheduler_still_busy = scheduler_busy.is_set() and th.is_alive()
        # **(レビュー 3 周目 codex E2)** `shutdown_join_timeout_sec`
        # (settings.yaml.example 既定 30 秒) は trade Mission の
        # `llama_swap.timeout_sec` (既定 300 秒) と釣り合っておらず、
        # run 相にいる最中に停止すると必ずタイムアウトしていた。
        # `ask_wait_timeout_sec` (569 行付近) と同じ導出で、Mission が
        # WorkerRunner の preemption エスカレーション (worker_grace_sec →
        # worker_terminate_grace_sec) で確実に終端されるまでの上限を budget
        # にする。**Task 19 で完全な導出に置き換えた**: budget は
        # `_default_dispatch_ceiling_sec` (trade 1 回 + reflection 最大
        # 3 件の連鎖を含む) から導出し、watchdog の dispatch ceiling と
        # **同じローカル変数を共有する**。join budget がそれより小さいと
        # main が先に join を諦め、watchdog の `busy_since` 軸が構造的に
        # 到達不能になり `fatal_reason` がその経路で永久にラッチしない。
        supervisor_join_timeout_sec = dispatch_ceiling_sec
        app.supervisor.join(timeout=supervisor_join_timeout_sec)
        supervisor_still_busy = app.supervisor.is_alive()
        # F3 (fix round 1): watchdog の join を service_stopped 記録より前に
        # 行う。notifier は最大 10 秒ブロックしうるため、記録を先にすると
        # 「graceful」記録の後に watchdog がまだ activity へ書き込める窓が
        # 生じる。判定権威は th.join(30) + supervisor.join(...) — wd は
        # ここで待つだけで graceful/timeout の判定には関与しない。
        wd.join(timeout=15)
        # **(レビュー 3 周目 codex E2)** タイムアウトでも runner.close()
        # は呼ぶ — 子プロセス/接続の leak は停止の目的に反する
        # (以前は join 成功時のみ close していた「上書き 7」の判断を、
        # leak 防止を優先する形に変更する)。close 自体の失敗で shutdown
        # シーケンスを止めない。
        skipped = app.close(busy_resources=_busy_resources_after_join(
            scheduler_still_busy, supervisor_still_busy))
        if skipped:
            app.activity.write(Category.SYSTEM, "close_skipped_resources",
                               f"{skipped} (join timeout — used-in-flight)")
        if th.is_alive() or app.supervisor.is_alive():
            app.activity.write(Category.SYSTEM, "service_stopped",
                               "shutdown_timeout (Mission 継続中の可能性)")
        else:
            app.activity.write(Category.SYSTEM, "service_stopped", "graceful")

    if _exit_code(app, th.is_alive(), app.supervisor.is_alive()):
        print("警告: 停止タイムアウト。実行中の処理が残っている可能性があります。")
        return 1
    print("停止しました。")
    return 0
