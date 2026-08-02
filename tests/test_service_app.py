import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.service import (
    _assert_tools_registered, _check_llama_swap, _validate_startup,
    build_app, build_splash, run_init, run_service,
)
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
    assert isinstance(app.runner, LocalRunner)
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
    """F4-②: owns_runner=True 相当 (`LocalRunner` の spec を持つ mock に
    差し替え)。graceful shutdown で close() が 1 回だけ呼ばれること。"""
    app = _seam_app(tmp_path, FakeRunner([]))
    mock_runner = MagicMock(spec=LocalRunner)
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
