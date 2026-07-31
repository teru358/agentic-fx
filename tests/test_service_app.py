import threading
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
    build_app, build_splash, run_init,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _init(tmp_path):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml.example").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(tmp_path)


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
    with patch.object(app.provider, "healthcheck", return_value="yfinance"):
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


def test_check_llama_swap_model_missing(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "other-model"}]})
    client = _mock_client(handler)
    with patch("httpx.get", client.get):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "qwen3.6-35b" in out and "警告" in out


def test_check_llama_swap_list_connect_error(capsys):
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    assert "警告" in out and "一覧" in out


def test_check_llama_swap_list_http_error(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    client = _mock_client(handler)
    with patch("httpx.get", client.get):
        _check_llama_swap(_StubSettings())
    out = capsys.readouterr().out
    # 一覧取得失敗の警告文であること (smoke 失敗の警告文と取り違えていないか
    # を区別する — "警告" だけの assert だと 3 分岐のどれでも通ってしまう)
    assert "警告" in out and "一覧" in out


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
    assert "OK" not in out


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
    assert "OK" not in out
