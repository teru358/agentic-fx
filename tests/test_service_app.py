import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.runners.worker_runner import WorkerRunner
from agentic_fx.service import (
    _assert_tools_registered, _check_llama_swap, _validate_startup,
    build_app, build_splash, run_init, run_service,
)
from agentic_fx.tools import market_tools
from tests.store.test_rag import FakeEmbedding

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _init(tmp_path):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml.example").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(tmp_path)


def _seam_app(tmp_path, runner):
    """run_service のテスト用シームで注入する App を組み立てる (F4)。

    FakeEmbedding を注入することで chromadb のモデル DL を避け、
    テスト高速化を実現する (Task 0)。
    """
    _init(tmp_path)
    return build_app(tmp_path, runner=runner, clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())


@contextmanager
def _no_real_network():
    """run_service の scheduler スレッドは起動直後に実時刻で 1 tick 実行する
    (`last = 0.0` のため `time.monotonic() - last >= 60` が即座に真になる)。
    `on_news_cycle`/`on_econ_cycle` は本物の `NewsCollector.collect` /
    `EconCalendar.refresh` に配線されており、これらは実際に外部 HTTP
    (RSS フィード・ForexFactory カレンダー) を叩く。`_stop_event` を事前
    set したテストでは scheduler スレッドの while 条件が起動時点で偽になる
    ため実害はないが (実測: 高速)、`_KeyboardInterruptOnMainWait` を使う
    テストは stop_event が未 set の状態でスレッドが走り出すため、メイン
    スレッドの KeyboardInterrupt 処理と競合して実 HTTP が発火しうる (実測:
    2 秒超のブレ)。フェッチ層そのものを patch して、タイミング (スレッド
    レース) に関係なく構造的に実 HTTP を遮断する。`app.scheduler.tick(...)`
    を直接呼ぶテスト (`test_tick_propagates_trigger_to_missions_row`) にも
    同じ理由で使う。"""
    from agentic_fx.datafeed.econ_calendar import CalendarFetch
    with patch("agentic_fx.datafeed.news_collector.fetch_feed",
               return_value=[]), \
         patch("agentic_fx.datafeed.news_collector.fetch_web",
               return_value=[]), \
         patch("agentic_fx.datafeed.econ_calendar.fetch_ff_calendar",
               return_value=CalendarFetch(events=[], dropped=0)):
        yield


def test_build_app_wires_everything(tmp_path):
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    assert app.trade_loop is not None
    assert app.scheduler is not None
    assert isinstance(app.core_lock, type(threading.RLock()))
    assert app.conn_core is not app.conn_shell  # スレッド別接続
    # F5: Commands は conn_shell 束縛の broker を持つ (conn_core をシェルスレッド
    # から触らせない配線の回帰ピン — conn_core/app.broker に差し替える変異を
    # 検出する)
    assert app.commands.conn is app.conn_shell
    assert app.commands.broker is not app.broker
    # ツールが登録されている
    for name in ("get_ohlcv", "search_news", "get_positions",
                 "get_recent_reflections"):
        assert name in app.registry.names()


def test_build_app_registers_get_signals(tmp_path):
    """⑦プラン 7 Task 9: signal_tools.build が service に配線され、起動時
    _assert_tools_registered を通ること (build_app 相当の起動テスト)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    assert "get_signals" in app.registry.names()


def test_get_signals_via_registry_execute_returns_decoded_payload(tmp_path):
    """A (advisor 指摘): これまでのテストは `tool.func(...)` を直接呼ぶだけ
    で、本番経路である `registry.execute` (jsonschema 検証 + json.dumps)
    を一度も通していなかった (verify-integration-not-just-units と同型の
    穴)。実際に LLM 相当の呼び出し形 (dict 引数 → JSON 文字列) で疎通する
    ことを確認する。"""
    import json as _json

    from agentic_fx.store import signals as signals_store

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    signals_store.add(
        app.conn_core, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=(NOW - timedelta(hours=1)).isoformat(),
        kind="signal", payload={"direction": "long"}, now=NOW)

    raw = app.registry.execute(
        "get_signals", {"pair": "USDJPY"}, ["get_signals"])
    out = _json.loads(raw)
    assert isinstance(out, list) and len(out) == 1
    assert out[0]["payload"] == {"direction": "long"}


def test_get_signals_via_registry_execute_since_hours_over_max_is_schema_error(tmp_path):
    """スキーマ maximum が先に弾く経路 (関数側クランプとは別の防御層):
    LLM 経由 (registry.execute) では since_hours=100 はクランプされず
    invalid arguments エラーになる — brief ①の「クランプ」は関数を直接
    呼ぶ経路 (テスト・将来の呼び出し元) の防波堤であり、registry.execute
    経由では二重防御のうちスキーマ側が先に発火する (opus R2 M11 の
    「lookback 上限で遮断は保たれる」ことの確認)。"""
    import json as _json

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    raw = app.registry.execute(
        "get_signals", {"pair": "USDJPY", "since_hours": 100},
        ["get_signals"])
    out = _json.loads(raw)
    assert "error" in out


def test_bless_is_not_registered_as_a_tool(tmp_path):
    """改善ループ非露出ピンの一部: bless は人間 CLI 専用であり、そもそも
    ToolDef として registry に登録されない (プラン 9 の allowed リスト
    実装前でも、この経路自体が存在しないことを固定する)。"""
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    assert "bless" not in app.registry.names()


def test_build_app_wires_approved_plugins_into_market_tools(tmp_path):
    """プラン 7 Task 3: `approved_plugins(conn_core, root / "plugins")` の
    結果が `market_tools.build(..., indicator_plugins=...)` まで実際に届く
    ことのピン。`indicator_plugins=approved` の削除や `plugins_dir` の
    typo (例: `root / "plugin"`) をしても、plugins/ が存在しない通常の
    テスト環境では `approved_plugins` が `[]` を返すだけで単体テストは
    通ってしまう — 配線そのものを検証しないと検出できない回帰
    (メモリ: verify-integration-not-just-units)。
    """
    _init(tmp_path)
    sentinel_meta = object()  # market_tools.build に渡る値だけを見る (中身は不問)
    captured: dict = {}
    real_build = market_tools.build

    def spy_build(*args, **kwargs):
        captured.update(kwargs)
        return real_build(*args, **{**kwargs, "indicator_plugins": None})

    with patch("agentic_fx.service.plugin_loader.approved_plugins") as approved, \
         patch("agentic_fx.tools.mission_registry.market_tools.build",
               side_effect=spy_build) as build_spy:
        approved.return_value = [sentinel_meta]
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))

    assert approved.call_count == 1
    conn_arg, plugins_dir_arg = approved.call_args[0]
    assert conn_arg is app.conn_core
    assert plugins_dir_arg == tmp_path / "plugins"
    assert build_spy.call_count == 1
    assert captured["indicator_plugins"] == [sentinel_meta]
    # spy 経由でも実装 (real_build) を実際に呼んでおり、登録は正常に完了する
    assert "get_ohlcv" in app.registry.names()


def test_splash_contains_key_fields(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    splash = build_splash(app)
    assert "learning" in splash
    assert "USDJPY" in splash
    assert "qwen" in splash  # runner モデル名


def test_on_trade_mission_runs_loop_and_reflection(tmp_path):
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    from agentic_fx.core.accounting import record_snapshot
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    with patch.object(app.provider, "healthcheck", return_value="yfinance"):
        app.scheduler.on_trade_mission("cron")
    assert len(fake.missions) >= 1  # trade mission が実行された
    rows = app.conn_core.execute("SELECT * FROM missions").fetchall()
    assert any(r["loop"] == "trade" for r in rows)


def test_on_trade_mission_wrapper_also_runs_reflection(tmp_path):
    """変異テストで発見 (mutation round): `on_trade_mission` から
    `reflection.run_pending()` を消しても `test_on_trade_mission_runs_loop_and_
    reflection` は緑のまま生存する (trade mission 実行の確認しかしていない
    ため)。closed 注文を 1 件用意し、`missions` に reflection loop の行が
    実際に作られること (= reflection.run_pending が呼ばれたこと) を直接
    ピンする。"""
    _init(tmp_path)
    fake = FakeRunner([
        MissionResult("completed", {"action": "hold", "reasoning": "w"}, []),
        MissionResult("completed", {"content": "振り返り"}, []),
    ])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    from agentic_fx.core.accounting import record_snapshot
    from agentic_fx.store import orders as orders_store
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    orders_store.insert(
        app.conn_core, pair="USDJPY", direction="long", entry_type="market",
        horizon="day", status="closed", now=NOW, quantity=0.1,
        avg_fill_price=148.0, close_price=149.0, realized_pnl=100.0,
        close_reason="tp")
    with patch.object(app.provider, "healthcheck", return_value="yfinance"):
        app.scheduler.on_trade_mission("cron")
    rows = app.conn_core.execute("SELECT * FROM missions").fetchall()
    assert any(r["loop"] == "reflection" for r in rows)
    refl_rows = app.conn_core.execute("SELECT * FROM reflections").fetchall()
    assert len(refl_rows) == 1


def test_tick_propagates_trigger_to_missions_row(tmp_path):
    """tick → on_trade_mission(reason) → run_once(trigger) → missions.trigger。

    この配線は wrapper が引数を捨てても各層の単体テストでは緑のままに
    なるため、tick 起点で通しで検証する (codex レビュー 1-4)。
    """
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    from agentic_fx.core.accounting import record_snapshot
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    # tick は on_news_cycle/on_econ_cycle 経由で本物の NewsCollector.collect /
    # EconCalendar.refresh を呼ぶため、_no_real_network() を挟まないと実 HTTP
    # (RSS フィード・ForexFactory カレンダー) が発火する (実測: 数秒のブレ —
    # グローバル制約「実 HTTP を混入させない」への抵触)。assert 対象の
    # trigger 伝搬とは無関係な経路なので、遮断してもテストの意図は弱まらない。
    with _no_real_network(), \
         patch.object(app.provider, "healthcheck", return_value="yfinance"):
        app.scheduler.tick(NOW)
    row = app.conn_core.execute(
        "SELECT trigger FROM missions WHERE loop='trade'").fetchone()
    assert row["trigger"] == "cron"


# ---- プラン 7 Task 8 fix round 1 F1: service.py の signal 実配線 --------
#
# sonnet の実証: service.py から D2 条件を削除しても、起動時 reclaim を
# 削除しても、テストは全緑のままだった (scheduler/trade_loop の単体テスト
# は service の closure をミラーした local combinator を使っているため —
# 「単体が緑でも配線が誰からも呼ばれない」欠陥クラス)。ここでは
# `build_app` が返す実 App (実 Scheduler・実 on_signal_maintenance・実
# signal_due_fn・実 SignalProducer) を通しで検証する。実サブプロセス
# (PluginSession) だけは `test_signal_eval.py` と同じ流儀で fake に差し替え
# る (sandbox 起動の健全性自体は Task 2 の関心)。


class _FakeSignalSession:
    """`plugin_sandbox.PluginSession` の代わりに注入する fake (F1(a))。
    `signal_producer.py` の `sandbox_run=None` (本番既定) パスが実際に
    使うクラスをそのまま差し替える — sandbox_run 引数の注入シームは
    service.py の配線には存在しない (常に None) ため、これが唯一の seam。
    """

    def __init__(self, meta, *, settings) -> None:
        del meta, settings

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def call(self, payload: dict) -> dict:
        del payload
        return {"signals": [{"direction": "long", "strength": 0.8,
                             "rationale": "up"}]}

    def close(self) -> None:
        pass


def _seed_1m(conn, source: str, start, minutes: int, *, price: float = 100.0,
            symbol: str = "USDJPY") -> None:
    from agentic_fx.store import ohlcv as ohlcv_store
    rows = [(symbol, "1m", (start + timedelta(minutes=i)).isoformat(),
             price, price, price, price, 10.0, None) for i in range(minutes)]
    ohlcv_store.import_bars(conn, rows, source=source)


def test_f1a_signal_maintenance_wiring_inserts_rows_via_real_tick(tmp_path):
    """F1(a): 承認済み plugin + settings.pairs 内のデータを仕込み、実
    `scheduler.tick()` を 1 回呼ぶと producer が実際に評価され signals
    テーブルへ行が挿入されること (on_signal_maintenance の実配線)。"""
    from agentic_fx.core.accounting import record_snapshot
    from agentic_fx.plugin.loader import PluginMeta

    _init(tmp_path)
    meta = PluginMeta(name="sig1", kind="signal", path=Path("/nonexistent"),
                      params={}, timeframe="1h", pairs=("USDJPY",),
                      max_bars=50, content_hash="h" * 64)

    with patch("agentic_fx.service.plugin_loader.approved_plugins",
              return_value=[meta]), \
         patch("agentic_fx.plugin.signal_producer.plugin_sandbox.PluginSession",
              _FakeSignalSession):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
        record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                        equity=1_000_000)
        _seed_1m(app.conn_core, app.settings.plugin.producer_source,
                NOW - timedelta(hours=3), 3 * 60 + 1)
        with _no_real_network(), \
             patch.object(app.provider, "healthcheck", return_value="yfinance"):
            app.scheduler.tick(NOW)

    count = app.conn_core.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"]
    assert count >= 1


def test_f1b_signal_mission_does_not_fire_without_d2_position(tmp_path):
    """F1(b): open/pending_fill の注文が皆無 (D2 不成立) だと、pending
    signal があっても signal トリガーの trade mission が起動しないこと。"""
    from agentic_fx.store import signals as signals_store

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())
    from agentic_fx.core.accounting import record_snapshot
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    signals_store.add(app.conn_core, plugin="sig1", content_hash="h1",
                      pair="USDJPY", timeframe="1h",
                      bar_ts=(NOW - timedelta(hours=1)).isoformat(),
                      kind="signal", payload={"direction": "long"}, now=NOW)
    app.scheduler._last_cron_trade = NOW  # cron を抑制し signal 経路だけ見る
    app.trade_loop.run_once = MagicMock(wraps=app.trade_loop.run_once)

    with _no_real_network(), \
         patch.object(app.provider, "healthcheck", return_value="yfinance"):
        app.scheduler.tick(NOW + timedelta(minutes=5))

    app.trade_loop.run_once.assert_not_called()  # D2 不成立で起動しない


def test_f1c_startup_reclaim_recovers_claimed_signal(tmp_path):
    """F1(c): 停止時に claimed のまま残った signal 行が、次の build_app
    (= 次回起動) 直後、tick を待たずに pending へ回収されること。"""
    from agentic_fx.store import signals as signals_store

    _init(tmp_path)
    app1 = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())
    sid = signals_store.add(
        app1.conn_core, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=(NOW - timedelta(hours=1)).isoformat(),
        kind="signal", payload={"direction": "long"}, now=NOW)
    lease_min = app1.settings.plugin.signal_lease_min
    old = NOW - timedelta(minutes=lease_min + 5)
    claimed = signals_store.claim_oldest(app1.conn_core, mission_id=999,
                                         now=old, freshness_bars=None)
    assert claimed is not None and claimed["id"] == sid  # 前提

    # 「再起動」を模して同じ DB に対しもう一度 build_app する
    app2 = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())

    row = app2.conn_core.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending"  # 起動時 reclaim が回収した


# ---- 上書き 3: MissionWatch は 1 インスタンスを共有注入 --------------------

def test_mission_watch_is_shared_single_instance(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    assert app.trade_loop.watch is app.mission_watch
    assert app.reflection.watch is app.mission_watch


# ---- 上書き 4: 起動時 schema 検証 + pairs 非空 ------------------------------

def test_validate_startup_rejects_empty_pairs():
    stub = MagicMock()
    stub.pairs = []
    with pytest.raises(RuntimeError):
        _validate_startup(stub)


def test_validate_startup_accepts_real_settings(tmp_path):
    _init(tmp_path)
    from agentic_fx.config import load_settings
    settings = load_settings(tmp_path / "config" / "settings.yaml")
    _validate_startup(settings)  # 例外を出さない


# ---- 上書き 5: wiring assert ------------------------------------------------

def test_assert_tools_registered_raises_on_missing():
    from agentic_fx.tools.registry import ToolRegistry
    registry = ToolRegistry()
    with pytest.raises(RuntimeError, match="get_ohlcv"):
        _assert_tools_registered(registry, ["get_ohlcv", "search_news"])


def test_assert_tools_registered_passes_when_complete():
    from agentic_fx.tools.registry import ToolDef, ToolRegistry
    registry = ToolRegistry()
    registry.register(ToolDef("get_ohlcv", "d", {"type": "object"},
                              lambda: None))
    _assert_tools_registered(registry, ["get_ohlcv"])  # 例外なし


# ---- 上書き 6: reflection_tools の本番配線 (pairs enum) ---------------------

def test_registry_pair_enum_matches_settings_pairs(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    tools = app.registry.openai_tools(["get_ohlcv"])
    params = tools[0]["function"]["parameters"]
    assert params["properties"]["pair"]["enum"] == list(app.settings.pairs)


# ---- 上書き 7: runner close の所有権 ----------------------------------------

def test_owns_runner_false_when_injected(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    assert app.owns_runner is False


def test_owns_runner_true_when_built_locally(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, clock=FixedClock(NOW))
    assert app.owns_runner is True
    assert isinstance(app.runner, WorkerRunner)
    app.runner.close()


# ---- 上書き 2: _check_llama_swap (3 分岐 x 成功/失敗) -----------------------

class _LlamaSwapStub:
    base_url = "http://localhost:8080/v1"


class _RunnerChoiceStub:
    model = "qwen3.6-35b"


class _RunnerStub:
    trade = _RunnerChoiceStub()


class _StubSettings:
    llama_swap = _LlamaSwapStub()
    runner = _RunnerStub()


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# F1 (fix round 1): 6 分岐テスト全部で httpx.get / httpx.post の**両方**を必ず
# patch する。model_missing 分岐を掃引したところ、`if model not in ids:` を
# `if False:` に変異させると、post 未 patch のテストでは実際に httpx.post が
# 実行され (llama-swap 稼働環境なら 404 → smoke 警告、未稼働なら
# ConnectError → 同)、いずれも警告文にモデル名が含まれるため assert が
# 誤って通っていた (SURVIVED 実測)。「呼ばれてはならない分岐で post が
# 呼ばれたら即 AssertionError にする」ことで、ネットワーク到達を構造的に
# 遮断しつつ、誤って post まで到達する変異を検出できるようにする。
# 加えて各テストの assert には**分岐固有の文言**を必須にする。


def _forbidden_post(branch: str):
    return patch("httpx.post", side_effect=AssertionError(
        f"httpx.post は {branch} 分岐では呼ばれてはならない"))


def test_check_llama_swap_ok(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        return httpx.Response(200, json={})
    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "OK" in out and "qwen3.6-35b" in out
    assert "存在しません" not in out and "smoke" not in out and "一覧" not in out


def test_check_llama_swap_model_missing(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})
    client = _mock_client(handler)
    with patch("httpx.get", client.get), _forbidden_post("model_missing"):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "qwen3.6-35b" in out and "警告" in out
    assert "存在しません" in out  # 分岐固有の文言
    assert "OK" not in out


def test_check_llama_swap_list_connect_error(capsys):
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")), \
         _forbidden_post("一覧取得失敗 (ConnectError)"):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "警告" in out and "一覧" in out
    assert "OK" not in out and "smoke" not in out


def test_check_llama_swap_list_http_error(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    client = _mock_client(handler)
    with patch("httpx.get", client.get), \
         _forbidden_post("一覧取得失敗 (HTTPStatusError)"):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    # 一覧取得失敗の警告文であること (smoke 失敗の警告文と取り違えていないか
    # を区別する — "警告" だけの assert だと 3 分岐のどれでも通ってしまう)
    assert "警告" in out and "一覧" in out
    assert "OK" not in out and "smoke" not in out


def test_check_llama_swap_smoke_http_error(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        return httpx.Response(500)
    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "警告" in out and "smoke" in out
    assert "OK" not in out and "存在しません" not in out and "一覧" not in out


def test_check_llama_swap_smoke_timeout(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        raise httpx.TimeoutException("timeout")
    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "警告" in out and "smoke" in out
    assert "OK" not in out and "存在しません" not in out and "一覧" not in out


# ---- F4 (fix round 1): run_service の shutdown 経路 -------------------------
#
# 上書き 3 は「watchdog スレッド固有の配線は E2E で検証しない (スリープ依存
# テストを作らない)」としているが、shutdown シーケンス自体はこの免除の対象
# 外 (owns_runner ガード・runner.close() の呼び分けが壊れても既存テストは
# 全て緑のまま — 実測 SURVIVED)。`run_service(..., _stop_event=...)` の
# テスト用シームを使い、実スリープ・実シグナル・実 HTTP なしで検証する。
# (`_seam_app` / `_no_real_network` はファイル冒頭に定義 — 前者はここでのみ
# 使うが、後者は `test_tick_propagates_trigger_to_missions_row` からも使う)

def test_run_service_daemon_graceful_shutdown_with_injected_runner(tmp_path):
    """F4-①: 事前 set 済み stop_event + daemon=True で即座に graceful
    shutdown する。owns_runner=False (runner 注入) のため、close 未実装の
    FakeRunner でも close() が呼ばれてはならない — owns_runner ガードが
    `if True:` のように壊れると `FakeRunner` に `close` 属性が無く
    `AttributeError` でこのテスト自体が落ちる (仕様上のピン)。"""
    fake = FakeRunner([])
    app = _seam_app(tmp_path, fake)
    assert app.owns_runner is False
    stop_event = threading.Event()
    stop_event.set()  # 実スリープなしで即座に shutdown 経路へ入る
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        rc = run_service(tmp_path, daemon=True, _stop_event=stop_event)
    assert rc == 0
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "service_stopped" in act and "graceful" in act


def test_run_service_closes_owned_runner_on_graceful_shutdown(tmp_path):
    """F4-②: owns_runner=True 相当 (`WorkerRunner` の spec を持つ mock に
    差し替え)。graceful shutdown で close() が 1 回だけ呼ばれること。"""
    app = _seam_app(tmp_path, FakeRunner([]))
    mock_runner = MagicMock(spec=WorkerRunner)
    app.runner = mock_runner
    app.owns_runner = True
    stop_event = threading.Event()
    stop_event.set()
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        rc = run_service(tmp_path, daemon=True, _stop_event=stop_event)
    assert rc == 0
    mock_runner.close.assert_called_once()
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "service_stopped" in act and "graceful" in act


class _KeyboardInterruptOnMainWait(threading.Event):
    """メインスレッドの最初の `wait()` 呼び出しだけ `KeyboardInterrupt` を
    送出する (F2 のピン)。バックグラウンドスレッド (scheduler/watchdog) からの
    `wait()` は本物の `threading.Event.wait()` に委譲する — スレッド判定で
    分岐するため、どのスレッドが先に `wait()` を呼ぶかに依存しない
    (レース非依存)。"""

    def __init__(self) -> None:
        super().__init__()
        self._raised = False

    def wait(self, timeout=None):  # noqa: D102
        if (not self._raised
                and threading.current_thread() is threading.main_thread()):
            self._raised = True
            raise KeyboardInterrupt
        return super().wait(timeout)


def test_run_service_daemon_survives_keyboard_interrupt_during_wait(tmp_path):
    """F2-③: daemon の待機ループ中に `KeyboardInterrupt` が発生しても、素通り
    せず graceful shutdown (stop・join・close・記録) が最後まで実行される
    こと。以前は `stop_event.wait(1)` が try/finally の外にあり、例外が
    そのまま伝播して graceful 記録・runner close をすべて飛ばしていた。"""
    app = _seam_app(tmp_path, FakeRunner([]))
    stop_event = _KeyboardInterruptOnMainWait()
    with _no_real_network(), \
         patch("agentic_fx.service.build_app", return_value=app), \
         patch("agentic_fx.service.signal.signal"):
        rc = run_service(tmp_path, daemon=True, _stop_event=stop_event)
    assert rc == 0
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "service_stopped" in act and "graceful" in act


# ---- Task 0: build_app の embedding seam ---------------------------------

def test_build_app_accepts_embedding_fn(tmp_path):
    """embedding_fn 注入で chromadb 既定モデルの probe を回避できる。"""
    _init(tmp_path)
    calls = []

    class TrackingEmbedding(FakeEmbedding):
        def __call__(self, input):  # noqa: A002
            calls.append(list(input))
            return super().__call__(input)

    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=TrackingEmbedding())
    assert app.rag is not None
    # Rag 初期化時に probe が実行されるか、あるいは最初の操作で呼ばれるか
    # を確認する (calls に記録がある = fake が通った)。
    if not calls:
        # Rag.__init__ が probe しない場合、add_news を呼んで検証
        app.rag.add_news([{"url": "https://ex.com/a1", "title": "Test",
                           "body": "Test article",
                           "source_name": "ex", "published": None}], NOW)
    assert calls  # embedding function が呼ばれた


# ---- Task 3: B 束小口 6 項目 (maintenance 順序) ---------------------------------

def test_signal_maintenance_reclaims_before_expiring(monkeypatch):
    """reclaim_expired → expire_stale の順で呼ばれる (順序入替、codex M⑤)。
    reclaim で pending に戻った直後の stale 行が、同じ tick 内の
    expire_stale でまだ拾われずに 1 tick 分だけ実行機会を得ることを、
    呼び出し順の記録で確認する。(裁定書 F-16/IM-10) `service.py` の
    実クロージャが呼ぶ module レベル関数 `_run_signal_maintenance` を
    直接呼び、`agentic_fx.store.signals` の実モジュール関数を
    monkeypatch する — テスト内の再定義フェイクに対して assert する
    恒真テストを避ける。
    """
    import agentic_fx.service as service_mod

    calls: list[str] = []

    def fake_reclaim(*a, **k):
        calls.append("reclaim")
        return []

    def fake_expire(*a, **k):
        calls.append("expire")
        return 0

    monkeypatch.setattr(service_mod.signals, "reclaim_expired", fake_reclaim)
    monkeypatch.setattr(service_mod.signals, "expire_stale", fake_expire)

    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    fake_producer = object()  # evaluate_due_plugins は呼ばれない前提で
    # 属性アクセスされたら AttributeError で明示的に落ちるようにする
    # (順序検証の対象外だが、意図せず呼ばれた場合は検出したい)。

    class _NoOpProducer:
        def evaluate_due_plugins(self, **k):
            calls.append("producer")

    service_mod._run_signal_maintenance(
        conn=None, signal_producer=_NoOpProducer(), approved=[],
        settings=service_mod.load_settings(
            Path(__file__).resolve().parents[1]
            / "config" / "settings.yaml.example"),
        now=now)

    assert calls == ["reclaim", "expire", "producer"]


def test_signal_maintenance_state_transition_stale_claimed_becomes_abandoned(tmp_path):
    """stale かつ lease 切れの claimed 行が、reclaim → expire 後に
    abandoned 状態になることを検証する (裁定書 I-1)。

    期待値: stale な claimed 行は reclaim_expired で pending に戻された後、
    同じ tick 内の expire_stale で abandoned に落ちる。最終状態は:
    - status = 'abandoned'
    - requeue_count = 元の値 + 1 (reclaim が +1 する。expire は触らない)
    """
    import agentic_fx.service as service_mod
    from agentic_fx.store.db import connect, init_db

    # テスト用 DB とデータ構築
    _init(tmp_path)
    conn = connect(tmp_path / "data" / "agentic.db")
    settings = service_mod.load_settings(
        tmp_path / "config" / "settings.yaml.example")

    # テスト時点の time
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    # cutoff よりずっと古いバー (stale 化させる)
    stale_bar_ts = (now - timedelta(hours=100)).isoformat()
    # lease 切れ (lease_min=15分。20分以上前に claimed)
    expired_claimed_at = (now - timedelta(minutes=20)).isoformat()

    # stale かつ lease 切れの claimed 行を INSERT
    cursor = conn.execute(
        """
        INSERT INTO signals
        (plugin, content_hash, pair, timeframe, bar_ts, kind, status,
         requeue_count, claimed_at, created_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("test_plugin", "hash1", "USDJPY", "1h", stale_bar_ts, "signal",
         "claimed", 0, expired_claimed_at, now.isoformat(), '{"value": 1}'))
    signal_id = cursor.lastrowid

    # maintenance 実行前の状態確認
    row_before = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id = ?",
        (signal_id,)).fetchone()
    assert row_before["status"] == "claimed"
    assert row_before["requeue_count"] == 0

    # maintenance 実行
    class _NoOpProducer:
        def evaluate_due_plugins(self, **k):
            pass

    service_mod._run_signal_maintenance(
        conn=conn, signal_producer=_NoOpProducer(), approved=[],
        settings=settings, now=now)

    # maintenance 後の状態確認
    row_after = conn.execute(
        "SELECT status, requeue_count FROM signals WHERE id = ?",
        (signal_id,)).fetchone()

    # 期待値: stale だから claimed → pending (by reclaim) → abandoned (by expire)
    assert row_after["status"] == "abandoned", \
        f"Expected abandoned, got {row_after['status']}"
    # requeue_count は reclaim が +1 する (expire は touch しない)
    assert row_after["requeue_count"] == 1, \
        f"Expected requeue_count=1, got {row_after['requeue_count']}"

    conn.close()


def test_signal_maintenance_callback_integration(tmp_path, monkeypatch):
    """build_app の on_signal_maintenance クロージャが scheduler に
    正しく配線されていることを検証する (codex M-1)。

    scheduler に渡された on_signal_maintenance callback を spy で監視し、
    callback が _run_signal_maintenance を正しい引数で呼んでいることを
    確認する。委譲を削除・旧本体に戻す変異は検出される。
    """
    import agentic_fx.service as service_mod

    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                    embedding_fn=FakeEmbedding())

    # scheduler の on_signal_maintenance callback が _run_signal_maintenance
    # を呼ぶ spy に切り替える
    calls: list[dict] = []

    def spy_run_signal_maintenance(*, conn, signal_producer, approved, settings, now):
        calls.append({
            "conn": conn is not None,
            "signal_producer": signal_producer is not None,
            "approved": approved is not None,
            "settings": settings is not None,
            "now": now is not None,
            "now_value": now
        })

    monkeypatch.setattr(
        service_mod, "_run_signal_maintenance", spy_run_signal_maintenance)

    # scheduler の callback を通じて on_signal_maintenance を呼ぶ
    test_now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    app.scheduler.on_signal_maintenance(test_now)

    # callback が呼ばれたことと、全引数が渡されたことを確認
    assert len(calls) == 1, f"Expected 1 call, got {len(calls)}"
    call = calls[0]
    assert call["conn"] is True, "conn should be passed"
    assert call["signal_producer"] is True, "signal_producer should be passed"
    assert call["approved"] is True, "approved should be passed"
    assert call["settings"] is True, "settings should be passed"
    assert call["now"] is True, "now should be passed"
    assert call["now_value"] == test_now, "now value should match"


def test_validate_startup_rejects_unknown_producer_source():
    """producer_source が KNOWN_OHLCV_SOURCES に含まれない場合、
    RuntimeError で reject する。"""
    from agentic_fx.service import _validate_startup
    from agentic_fx.config import load_settings

    settings = load_settings(
        Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")
    settings = settings.model_copy(
        update={"plugin": settings.plugin.model_copy(
            update={"producer_source": "typo-source"})})
    with pytest.raises(RuntimeError, match="producer_source"):
        _validate_startup(settings)


def test_app_has_clock_field(tmp_path):
    """App インスタンスが clock フィールドを持つことを確認。"""
    from agentic_fx.core.contracts import FixedClock

    fixed = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    _init(tmp_path)
    app = build_app(tmp_path, clock=fixed)
    assert app.clock is fixed


class _FakeProvider:
    """PriceProvider 互換の最小限 fake (provider seam test 用)。"""
    def __init__(self):
        self.get_quote = lambda pair: None
        self.spec = lambda pair: None
        self.latest_1m_bar = lambda pair: None


def test_build_app_provider_seam_bypasses_quote_fn_patch(tmp_path):
    """provider を直接注入した場合、quote_fn/spec_fn/bars_fn の
    bound-method 差し替えは行われない (provider が全挙動を持つ)。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    app = build_app(tmp_path, provider=fake_provider)
    assert app.provider is fake_provider


def test_watchdog_tick_uses_mission_watch_time_fn(monkeypatch):
    """_watchdog_tick の elapsed 算出が MissionWatch の time_fn 経由で
    行われる (fable M4) — 生の time.monotonic() を直接呼ばない。"""
    from agentic_fx.service import _watchdog_tick, App
    from agentic_fx.loops.mission_watch import MissionWatch

    fake_time = [1000.0]
    watch = MissionWatch(time_fn=lambda: fake_time[0])
    watch.begin(mission_id=1, loop="trade", timeout_sec=10.0)
    fake_time[0] = 1000.0 + 10.0 + 61.0  # timeout + grace(60) を超過

    calls: list[str] = []

    class FakeActivity:
        def write(self, *a, **k):
            calls.append("write")

    class FakeNotifier:
        def send(self, *a, **k):
            calls.append("send")

    app = App(conn_core=None, conn_shell=None, settings=None, state=None,
              activity=FakeActivity(), broker=None, executor=None,
              provider=None, econ=None, collector=None, rag=None,
              trade_loop=None, reflection=None, scheduler=None,
              commands=None, registry=None, core_lock=None,
              mission_watch=watch, notifier=FakeNotifier(), runner=None,
              owns_runner=False, clock=None)
    _watchdog_tick(app)
    assert calls == ["write", "send"]


def test_build_app_provider_seam_rejects_concurrent_quote_fn(tmp_path):
    """provider と quote_fn を併用する場合は ValueError を送出して排他を
    強制する (fail closed: provider が全挙動を握る seam の混合は設定ミス)。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    with pytest.raises(ValueError, match="provider と quote_fn/spec_fn/bars_fn は併用不可"):
        build_app(tmp_path, provider=fake_provider,
                  quote_fn=lambda pair: None)


def test_build_app_provider_seam_rejects_concurrent_spec_fn(tmp_path):
    """provider と spec_fn を併用する場合は ValueError を送出して排他を
    強制する。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    with pytest.raises(ValueError, match="provider と quote_fn/spec_fn/bars_fn は併用不可"):
        build_app(tmp_path, provider=fake_provider,
                  spec_fn=lambda pair: None)


def test_build_app_provider_seam_rejects_concurrent_bars_fn(tmp_path):
    """provider と bars_fn を併用する場合は ValueError を送出して排他を
    強制する。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    with pytest.raises(ValueError, match="provider と quote_fn/spec_fn/bars_fn は併用不可"):
        build_app(tmp_path, provider=fake_provider,
                  bars_fn=lambda pair: None)


def test_build_app_provider_seam_falls_back_to_provider_bound_methods(tmp_path):
    """provider のみ注入 (quote_fn/spec_fn/bars_fn 未指定) の場合でも、
    Executor/Scheduler に渡る quote_fn/spec_fn/bars_fn は None ではなく
    provider の束縛メソッドにフォールバックすること (fallback binding が
    削除されると Mission 実行時に TypeError で遅延失敗する)。"""
    _init(tmp_path)
    fake_provider = _FakeProvider()
    app = build_app(tmp_path, provider=fake_provider)
    # provider のみを渡し、quote_fn/spec_fn/bars_fn は未指定の場合、
    # Executor/Scheduler が保持する関数が provider の束縛メソッドになるはず
    assert app.executor.quote_fn is fake_provider.get_quote
    assert app.executor.spec_fn is fake_provider.spec
    assert app.scheduler.bars_fn is fake_provider.latest_1m_bar


def test_scheduler_tick_once_uses_app_clock(tmp_path):
    """scheduler_thread の 1 tick 分が app.clock.now() を読むことを直接確認
    (app.clock を壁時計に戻す変異で red になるべき)。"""
    from agentic_fx.service import _scheduler_tick_once
    from agentic_fx.core.contracts import FixedClock

    fixed = FixedClock(datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc))
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=fixed)
    seen: list = []
    app.scheduler.tick = lambda now: seen.append(now)
    _scheduler_tick_once(app)
    assert seen == [fixed.now()]


def test_watchdog_tick_uses_mission_watch_time_fn_directly(tmp_path):
    """_watchdog_tick の elapsed 算出が MissionWatch.time_fn 経由で行われ、
    結果を FakeActivity/FakeNotifier に観測する (time_fn を壁時計に戻す
    変異で red になるべき)。"""
    from agentic_fx.service import _watchdog_tick, App
    from agentic_fx.loops.mission_watch import MissionWatch

    fake_time = [1000.0]
    watch = MissionWatch(time_fn=lambda: fake_time[0])
    watch.begin(mission_id=1, loop="trade", timeout_sec=10.0)
    fake_time[0] = 1000.0 + 10.0 + 71.0  # timeout(10) + grace(60) + margin(1) を超過 → elapsed 81s

    captured_messages: list[str] = []

    class FakeActivity:
        def write(self, category, key, message):
            captured_messages.append(message)

    class FakeNotifier:
        def send(self, message):
            captured_messages.append(message)

    app = App(conn_core=None, conn_shell=None, settings=None, state=None,
              activity=FakeActivity(), broker=None, executor=None,
              provider=None, econ=None, collector=None, rag=None,
              trade_loop=None, reflection=None, scheduler=None,
              commands=None, registry=None, core_lock=None,
              mission_watch=watch, notifier=FakeNotifier(), runner=None,
              owns_runner=False, clock=None)
    _watchdog_tick(app)
    # elapsed は fake_time に基づいた値 (81s) になるはず。
    # 生の time.monotonic() (システム起動からの経過、通常大きい数値)
    # に戻す変異はこのテストの assertion で red になる。
    assert any("81" in str(msg) for msg in captured_messages), \
        f"elapsed 81s を期待するが captured_messages={captured_messages}"


def test_build_app_calls_assert_tools_registered(tmp_path):
    """build_app が起動時に _assert_tools_registered を呼んで、必須ツール
    が登録されていることを確認する (tool 検証呼び出し削除を検出する)。

    spy で _assert_tools_registered が呼ばれることを直接確認し、引数の
    tool_list が実際に _TRADE_TOOLS であることを検証する。呼び出し削除および
    tool_list を空にする変異に対して red になることを保証する。"""
    from agentic_fx.loops.trade_loop import _TRADE_TOOLS

    _init(tmp_path)

    call_args = []
    original_assert = _assert_tools_registered

    def spy_assert(registry, tool_list):
        call_args.append({
            "registry": registry,
            "tool_list": tool_list,
            "tool_list_is_trade_tools": tool_list is _TRADE_TOOLS,
            "tool_list_not_empty": len(tool_list) > 0,
            "tool_list_value": list(tool_list) if hasattr(tool_list, '__iter__') else tool_list,
        })
        # 本来の検証も実行
        return original_assert(registry, tool_list)

    with patch("agentic_fx.service._assert_tools_registered",
               side_effect=spy_assert):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))

    # _assert_tools_registered が呼ばれたこと
    assert len(call_args) >= 1, \
        "_assert_tools_registered is not called from build_app"
    # 渡された tool_list が _TRADE_TOOLS であること (参照の同一性で確認)
    assert call_args[0]["tool_list_is_trade_tools"], \
        f"tool_list is not _TRADE_TOOLS (got {call_args[0]['tool_list_value']})"
    # tool_list が空でないこと (空集合置換変異を検出する)
    assert call_args[0]["tool_list_not_empty"], \
        "tool_list is empty (should contain required tools like 'get_ohlcv')"
    # ツールが登録されている
    assert "get_ohlcv" in app.registry.names()


def test_build_app_provider_seam_passed_to_mission_registry(tmp_path):
    """build_app が構築した provider が build_mission_registry に実際に
    渡されることを確認する (provider seam が registry に透通する)。

    registry への provider 渡しが削除されると、テスト注入の provider が
    tool registry では無視されるため、後続の E2E 決定性テストが予測不可能に
    なる。provider=provider が削除される変異 (provider=None など) を検出する
    ために、渡された provider インスタンスが非 None であることを確認する。"""
    _init(tmp_path)

    # build_mission_registry の呼び出しを spy して、provider が実際に渡されているか確認
    from agentic_fx.tools import mission_registry as mr_module
    real_build_registry = mr_module.build_mission_registry
    calls: list[dict] = []

    def spy_build_registry(*args, **kwargs):
        provider_arg = kwargs.get("provider")
        calls.append({
            "has_provider": "provider" in kwargs,
            "provider_is_not_none": provider_arg is not None,
            "provider_value": provider_arg,
        })
        return real_build_registry(*args, **kwargs)

    with patch("agentic_fx.service.build_mission_registry",
               side_effect=spy_build_registry):
        app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))

    # build_mission_registry が provider kwarg を受け取ったことを確認
    assert len(calls) >= 1, "build_mission_registry not called"
    assert calls[0]["has_provider"], \
        "provider kwarg not passed to build_mission_registry"
    # provider の値が None でないことを確認 (provider=None 変異を検出する)
    assert calls[0]["provider_is_not_none"], \
        "provider kwarg passed but value is None (should be a PriceProvider instance)"
    # 渡された provider が registry に反映されていることを確認する
    # (registry の tool が provider インスタンスを束縛しているため)
    assert "get_ohlcv" in app.registry.names(), "get_ohlcv not in registry"
