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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

from agentic_fx._safe_error import safe_error_text
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Clock, Mode, SystemClock
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
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
from agentic_fx.store import approvals, missions, orders, signals, ohlcv
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from agentic_fx.store.state import StateStore
from agentic_fx.tools import (
    account_tools, market_tools, news_tools, plugin_loader, reflection_tools,
    signal_tools,
)
from agentic_fx.tools.registry import ToolRegistry

_log = logging.getLogger("agentic_fx.service")


def _state_store(root: Path) -> StateStore:
    return StateStore(root / "data" / "state" / "app_state.json")


def ensure_initialized(root: Path) -> None:
    if not _state_store(root).load().initialized:
        print("初期化が完了していません。先に `uv run main.py init` を実行してください。",
              file=sys.stderr)
        raise SystemExit(2)


def _check_llama_swap(settings) -> None:
    """llama-swap 接続確認 (上書き 2)。一覧取得不能 / モデル不在 / cold-load
    smoke 失敗の 3 種を区別して警告する。init から呼ばれる (失敗は警告のみ —
    取引判断 Mission は実行時に fail closed で保護される)。"""
    import httpx
    base = settings.llama_swap.base_url
    model = settings.runner.trade.model

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

    if model not in ids:
        print(f"警告: モデル '{model}' が llama-swap の /models に存在しません。"
              f"alias 設定を確認してください (存在: {ids})")
        return

    try:
        # cold-load smoke: TTL unload 後の初回 Mission がロード時間で
        # timeout しないよう、1 トークン生成でロードを促す
        r = httpx.post(f"{base}/chat/completions",
                       json={"model": model, "max_tokens": 1,
                             "messages": [{"role": "user", "content": "ping"}]},
                       timeout=120)
        r.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        print(f"警告: モデル '{model}' の cold-load smoke に失敗しました ({e})。"
              "初回 Mission が timeout する可能性があります。")
        return

    print(f"llama-swap OK (model '{model}' loaded)")


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


def _assert_tools_registered(registry: ToolRegistry, names: list[str]) -> None:
    """配線ミスの即時検出 (上書き 5): 必要なツールが登録されていることを確認する。"""
    missing = set(names) - set(registry.names())
    if missing:
        raise RuntimeError(f"tools not registered: {sorted(missing)}")


class _LockedAsk:
    """ask を Mission スロット (core_lock) 経由で実行する薄いラッパー。"""

    def __init__(self, trade_loop: TradeLoop, lock: threading.RLock) -> None:
        self._loop = trade_loop
        self._lock = lock

    def ask_once(self, question: str) -> str:
        with self._lock:
            return self._loop.ask_once(question)


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
              bars_fn=None, embedding_fn=None) -> App:
    """全部品を配線して `App` を返す。

    quote_fn / spec_fn / bars_fn / embedding_fn は E2E テストの注入点 (None なら
    各部品の実装を使う — build 後の patch では bound 済みクロージャに届かないため
    注入で解決する)。

    **`healthcheck()` は注入対象外** (fix round 1 F2): `PriceProvider.healthcheck`
    は `self.get_quote(...)` に加えて `self.get_bars(...)` を呼ぶが、
    `get_bars` は quote_fn/spec_fn/bars_fn のどれにもマップされていない。
    決定論的なテストで `build_app` を使う場合、`app.provider.healthcheck` を
    個別に patch すること (本 E2E テスト `tests/test_e2e_phase1.py` 参照)。
    """
    clock = clock or SystemClock()
    settings = load_settings(root / "config" / "settings.yaml")

    state = _state_store(root)
    activity = ActivityLog(root / "logs" / "activity.log")
    conn_core = connect(root / "data" / "agentic.db")
    init_db(conn_core)
    conn_shell = connect(root / "data" / "agentic.db")

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

    def rate_fn(ccy: str, account_ccy: str, now: datetime):
        return provider.to_account_rate(
            ccy, account_ccy, reference_ts=now,
            max_skew_min=settings.datafeed.conversion_skew_max_min)

    econ = EconCalendar(conn_core, activity, clock)
    rag = Rag(root / "data" / "rag", embedding_function=embedding_fn)
    collector = NewsCollector(conn_core, rag, activity, clock)
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

    registry = ToolRegistry()
    registry.register_all(market_tools.build(
        provider, econ, settings, indicator_plugins=approved))
    registry.register_all(news_tools.build(rag))
    registry.register_all(account_tools.build(conn_core, broker))
    # 上書き 6: reflection_tools.build は pairs が必須引数 (Task 0-8)
    registry.register_all(reflection_tools.build(conn_core, rag, settings.pairs))
    # プラン 7 Task 9: get_signals (取引判断 loop 専用。_TRADE_TOOLS 経由で
    # trade/ask 両 Mission に露出する)
    registry.register_all(signal_tools.build(conn_core, settings, clock))
    # 上書き 4/5: 配線ミスは起動時 RuntimeError で殺す (registry 組み立て後)
    _validate_startup(settings)
    _assert_tools_registered(registry, _TRADE_TOOLS)

    owns_runner = runner is None
    if runner is None:
        runner = LocalRunner(base_url=settings.llama_swap.base_url,
                             model=settings.runner.trade.model,
                             registry=registry)

    policy = Policy(root / "policy" / "directives.md")
    # 上書き 3: MissionWatch は 1 インスタンスを trade_loop / reflection に共有注入
    mission_watch = MissionWatch()
    trade_loop = TradeLoop(conn=conn_core, runner=runner, settings=settings,
                           executor=executor, provider=provider, econ=econ,
                           policy=policy, activity=activity,
                           notifier=notifier, clock=clock,
                           watch=mission_watch)
    reflection = ReflectionCycle(conn=conn_core, runner=runner, rag=rag,
                                 settings=settings, activity=activity,
                                 clock=clock, watch=mission_watch)

    core_lock = threading.RLock()

    def on_trade_mission(trigger: str) -> None:
        # tick 全体が core_lock 下で走る (RLock のため再取得も安全)。
        # trigger は scheduler._trade_mission_due() が返した起動理由。
        # ここで捨てると missions.trigger が常に既定値になり監査列が死ぬ
        # (上書き 1 参照)。
        with core_lock:
            trade_loop.run_once(trigger)
            reflection.run_pending()

    # プラン 7 Task 8: signal 起動の判定・保守処理を Scheduler へ配線する。
    def on_signal_maintenance(now: datetime) -> None:
        _run_signal_maintenance(conn=conn_core, signal_producer=signal_producer,
                                approved=approved, settings=settings, now=now)

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
                          signal_due_fn=signal_due_fn)

    # 起動時 reclaim 1 回 (コントローラ裁定): 前回停止時に claimed のまま
    # 残った signal を、次の tick を待たずに起動直後から回収対象にする。
    signals.reclaim_expired(conn_core, now=clock.now(),
                            lease_min=settings.plugin.signal_lease_min,
                            max_requeue=settings.plugin.signal_requeue_max)

    # Commands は conn_shell 束縛の broker を持つ (conn_core をシェルスレッドから触らない)
    shell_broker = PaperBroker(conn_shell, settings, clock)
    commands = Commands(conn=conn_shell, state_store=state,
                        broker=shell_broker,
                        trade_loop=_LockedAsk(trade_loop, core_lock),
                        activity=activity, log_dir=root / "logs", clock=clock)
    return App(conn_core=conn_core, conn_shell=conn_shell, settings=settings,
               state=state, activity=activity, broker=broker,
               executor=executor, provider=provider, econ=econ,
               collector=collector, rag=rag, trade_loop=trade_loop,
               reflection=reflection, scheduler=scheduler, commands=commands,
               registry=registry, core_lock=core_lock,
               mission_watch=mission_watch, notifier=notifier,
               runner=runner, owns_runner=owns_runner)


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


def _watchdog_tick(app: App) -> None:
    """1 回分の watchdog 監視 (上書き 3)。activity/notifier の失敗はスレッドを
    殺さない — 呼び出し元 (watchdog スレッド) 側も広い try で包む。

    missions 行には書かない (finalize の所有者は TradeLoop/ReflectionCycle の
    `_run_recorded` の finally のみ — 二重終端を作らない)。
    """
    entry = app.mission_watch.breached(grace_sec=60)
    if entry is None:
        return
    elapsed = time.monotonic() - entry.started
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
    app = build_app(root)

    warning = Policy(root / "policy" / "directives.md").size_warning()
    if warning:
        print(warning)
    print(build_splash(app))
    app.activity.write(Category.SYSTEM, "service_started",
                       f"daemon={daemon}")

    stop_event = _stop_event if _stop_event is not None else threading.Event()

    def scheduler_thread() -> None:
        last = 0.0
        while not stop_event.is_set():
            if time.monotonic() - last >= 60:
                last = time.monotonic()
                if stop_event.is_set():
                    break  # 停止フェーズ: 新しい tick を開始しない
                try:
                    with app.core_lock:
                        app.scheduler.tick(datetime.now(timezone.utc))
                except Exception:  # noqa: BLE001
                    _log.exception("tick failed")
            stop_event.wait(1)

    def watchdog_thread() -> None:
        while not stop_event.is_set():
            stop_event.wait(30)
            if stop_event.is_set():
                break
            try:
                _watchdog_tick(app)
            except Exception:  # noqa: BLE001 — スレッドを殺さない
                _log.exception("watchdog tick failed")

    # F2 (fix round 1): シグナルハンドラはスレッド起動より**前**に登録する。
    # 以前はスレッド起動後に登録しており、その間に SIGTERM が届くとデフォルト
    # 動作 (即時終了) で graceful shutdown 経路を経ずにプロセスが死ぬ窓が
    # あった。
    if daemon:
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    th = threading.Thread(target=scheduler_thread, daemon=True)
    th.start()
    wd = threading.Thread(target=watchdog_thread, daemon=True)
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
    finally:
        # F2: 待機部で想定外の例外 (KeyboardInterrupt 含む) が起きても、
        # shutdown 手順 (stop・join・close・記録) は必ず実行する。ここでは
        # 例外を握りつぶさない (return を置かない) — 記録後、元の例外があれば
        # そのまま再送出される。
        stop_event.set()
        # graceful shutdown: scheduler スレッドの終了を確認してから記録する
        # (tick は core_lock 下で走るため、join 完了 = 実行中 Mission も完了)
        th.join(timeout=30)
        # F3 (fix round 1): watchdog の join を service_stopped 記録より前に
        # 行う。notifier は最大 10 秒ブロックしうるため、記録を先にすると
        # 「graceful」記録の後に watchdog がまだ activity へ書き込める窓が
        # 生じる。判定権威は従来どおり th.join(30) のみ — wd はここで待つ
        # だけで graceful/timeout の判定には関与しない。
        wd.join(timeout=15)
        if th.is_alive():
            app.activity.write(Category.SYSTEM, "service_stopped",
                               "shutdown_timeout (Mission 継続中の可能性)")
        else:
            # 上書き 7: join 成功時のみ close する (使用中の client を
            # 別スレッドから閉じない)
            if app.owns_runner and isinstance(app.runner, LocalRunner):
                app.runner.close()
            app.activity.write(Category.SYSTEM, "service_stopped", "graceful")

    if th.is_alive():
        print("警告: 停止タイムアウト。実行中の処理が残っている可能性があります。")
        return 1
    print("停止しました。")
    return 0
