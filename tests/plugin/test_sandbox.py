"""sandbox.py / worker.py のテスト (プラン 7 Task 2)。

実 HTTP/git/乱数/実時計/実 sleep は使わない。**唯一の例外**は test⑥
(無限ループ plugin の timeout) — `sandbox_timeout_sec=1` で実測上限
1 秒に抑える。それ以外のテストは実 subprocess こそ起動するが (本 task
に本質的で許容) 待ち時間はゼロ〜数十 ms のプロセス起動コストのみ。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from agentic_fx.config import load_settings
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import (
    SandboxError, check_source, run_plugin, _build_env, _SINGLE_THREAD_ENV,
    PluginSession,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"


@pytest.fixture(scope="module")
def plugin_settings():
    return load_settings(EXAMPLE).plugin


def _meta(base: Path, name: str, kind: str, plugin_py: str, *,
         max_bars: int = 200, timeframe: str | None = None,
         pairs: tuple[str, ...] = ()) -> PluginMeta:
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(f"kind: {kind}\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return PluginMeta(name=name, kind=kind, path=d, params={}, timeframe=timeframe,
                      pairs=pairs, max_bars=max_bars, content_hash="0" * 64)


def _df(n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    closes = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * n}, index=idx)


INDICATOR_OK_PY = """
from __future__ import annotations

import pandas as pd


def compute(df, params):
    return {"mean_close": float(df["close"].mean())}
"""

STRATEGY_EXIT_PY = """
def evaluate(df, indicators, signals, params):
    return {"action": "exit", "rationale": "not a valid vocabulary word"}
"""

STRATEGY_OPEN_NO_SL_PY = """
def evaluate(df, indicators, signals, params):
    return {"action": "open", "direction": "long", "entry_type": "market",
            "rationale": "missing stop_loss"}
"""

STRATEGY_OPEN_OK_PY = """
def evaluate(df, indicators, signals, params):
    last = float(df["close"].iloc[-1])
    return {"action": "open", "direction": "long", "entry_type": "market",
            "stop_loss": last - 1.0, "rationale": "ok"}
"""

STRATEGY_HOLD_PY = """
def evaluate(df, indicators, signals, params):
    return {"action": "hold", "rationale": "nothing to do"}
"""

SIGNAL_OK_PY = """
def detect(df, params):
    return [{"direction": "long", "strength": 0.8, "rationale": "test signal"}]
"""

SIGNAL_BAR_TS_PY = """
def detect(df, params):
    return [{"direction": "long", "strength": 0.8, "rationale": "test",
             "bar_ts": "2026-01-01T00:00:00+00:00"}]
"""

INFINITE_LOOP_PY = """
def compute(df, params):
    while True:
        pass
"""

# check_source を素通りする (denylist に無い名前・import のみ) が、
# worker 起動時の plugin.py import 自体が失敗するケース。__enter__ の
# 診断用起動応答パス (ready=false) を検証する。
IMPORT_TIME_CRASH_PY = """
_ = 1 / 0


def compute(df, params):
    return {}
"""

# stdout 保護 (worker._protect_protocol_stdout) の検証用。**短い print
# 1 回だけでは検出力が無い** — `sys.stdout` はパイプ相手だとテキスト層が
# ブロックバッファ (数 KB) されるため、短い出力は JSON 応答行より先に
# flush されず、fd 付け替えの有無に関わらずテストが通ってしまう
# (advisor 指摘で実測確認済み)。バッファを確実に溢れさせる大きさの
# print にして、修正を外すと実際に fail する状態にする。
PRINT_THEN_COMPUTE_PY = """
def compute(df, params):
    print("x" * 100_000)
    return {"x": 1.0}
"""


# --- check_source: allowlist/denylist -----------------------------------

def test_check_source_accepts_allowed_imports_and_future(tmp_path):
    src = tmp_path / "ok.py"
    src.write_text(
        "from __future__ import annotations\n"
        "import math, statistics, numpy, pandas\n"
        "def compute(df, params):\n    return {}\n")
    check_source(src)  # 例外を投げなければ OK


def test_check_source_rejects_import_os(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text("import os\ndef compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError, match="os"):
        check_source(src)


@pytest.mark.parametrize("name", [
    "open", "eval", "exec", "compile", "__import__", "input", "breakpoint",
    "globals", "getattr", "setattr", "delattr", "vars",
])
def test_check_source_rejects_denylisted_names(tmp_path, name):
    src = tmp_path / "bad.py"
    src.write_text(f"def compute(df, params):\n    {name}\n    return {{}}\n")
    with pytest.raises(SandboxError):
        check_source(src)


def test_check_source_rejects_read_csv_but_allows_to_numpy(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import pandas as pd\n"
        "def compute(df, params):\n    pd.read_csv('x.csv')\n    return {}\n")
    with pytest.raises(SandboxError, match="read_"):
        check_source(bad)

    ok = tmp_path / "ok.py"
    ok.write_text(
        "def compute(df, params):\n"
        "    arr = df['close'].to_numpy()\n    return {'x': float(arr[0])}\n")
    check_source(ok)


def test_check_source_rejects_to_csv(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text(
        "def compute(df, params):\n    df.to_csv('x.csv')\n    return {}\n")
    with pytest.raises(SandboxError, match="to_csv"):
        check_source(src)


def test_check_source_rejects_pickle_import(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text("import pickle\ndef compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError):
        check_source(src)


def test_check_source_rejects_pandas_io_import(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text("import pandas.io\ndef compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError, match="pandas.io"):
        check_source(src)


def test_check_source_rejects_numpy_lib_npyio_import(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text(
        "import numpy.lib.npyio\ndef compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError, match="npyio"):
        check_source(src)


def test_check_source_rejects_global_statement(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text(
        "x = 1\n"
        "def compute(df, params):\n    global x\n    x = 2\n    return {}\n")
    with pytest.raises(SandboxError, match="global"):
        check_source(src)


def test_check_source_extra_allowed_permits_additional_root(tmp_path):
    src = tmp_path / "test_plugin.py"
    src.write_text("import pytest\nfrom plugin import compute\n")
    with pytest.raises(SandboxError):
        check_source(src)
    check_source(src, extra_allowed=frozenset({"pytest", "plugin"}))


# --- env builder (⑨: AFX_* 非伝播) --------------------------------------

def test_build_env_minimal_and_no_afx_propagation():
    fake_parent_env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": "/some/evil/path",
        "AFX_SECRET_API_KEY": "super-secret",
        "AFX_ANYTHING": "leaked?",
        "HOME": "/home/someone",
        "LD_PRELOAD": "/tmp/evil.so",
    }
    env = _build_env(fake_parent_env)

    assert env["PATH"] == "/usr/bin:/bin"
    assert not env["PYTHONPATH"].__contains__("evil")
    assert env["PYTHONSAFEPATH"] == "1"
    # 完全一致 (部分チェックの積み重ねだと将来の伝播漏れを見落とし得る —
    # 「このキー集合しか渡さない」を構造的に固定する)。
    assert set(env) == {"PATH", "PYTHONPATH", "PYTHONSAFEPATH", *_SINGLE_THREAD_ENV}


def test_build_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("AFX_SHOULD_NOT_LEAK", "x")
    env = _build_env()
    assert env["PATH"] == "/usr/bin"
    assert "AFX_SHOULD_NOT_LEAK" not in env


# --- ① indicator happy path (run_plugin) --------------------------------

def test_run_plugin_indicator_happy_path(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    out = run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)
    assert out["mean_close"] == pytest.approx(_df()["close"].mean())


# --- ② セッションの同一プロセス性 (pid 変化なし) --------------------------

def test_session_reuses_same_worker_process_across_calls(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    with PluginSession(meta, settings=plugin_settings) as session:
        session.call({"df": _df(), "params": {}})
        pid_1 = session.pid
        session.call({"df": _df(), "params": {}})
        pid_2 = session.pid
    assert pid_1 is not None
    assert pid_1 == pid_2


# --- ③④⑤ (check_source を通した実行経路: run_plugin も reject する) ------

def test_run_plugin_rejects_disallowed_import_before_spawning(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "bad", "indicator",
                "import os\ndef compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError):
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)


# --- ⑥ 無限ループ → timeout 1s で SandboxError (実測上限 1 秒。唯一の実 sleep) ---

def test_infinite_loop_times_out_within_one_second(tmp_path, plugin_settings):
    import time

    meta = _meta(tmp_path, "loop", "indicator", INFINITE_LOOP_PY)
    tight = plugin_settings.model_copy(update={"sandbox_timeout_sec": 1.0})
    session = PluginSession(meta, settings=tight)
    with session:
        start = time.monotonic()
        with pytest.raises(SandboxError, match="timed out"):
            session.call({"df": _df(), "params": {}})
        elapsed = time.monotonic() - start
        assert elapsed < 2.0  # 実測上限 1 秒 + 若干のプロセス kill 猶予

        # session-dead-after-timeout: セッションは以後使用不能
        with pytest.raises(SandboxError, match="not usable"):
            session.call({"df": _df(), "params": {}})


# --- oversize 出力 → SandboxError + セッション使用不能 --------------------

def test_oversize_output_marks_session_dead(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    tiny_cap = plugin_settings.model_copy(update={"sandbox_output_max_bytes": 10})
    session = PluginSession(meta, settings=tiny_cap)
    with session:
        with pytest.raises(SandboxError, match="exceeded"):
            session.call({"df": _df(), "params": {}})
        with pytest.raises(SandboxError, match="not usable"):
            session.call({"df": _df(), "params": {}})


# --- worker 起動失敗 (__enter__ の ready=false 診断パス) -------------------

def test_worker_import_time_crash_reports_startup_error(tmp_path, plugin_settings):
    """check_source は通る (denylist に無い名前のみ) が plugin.py の
    トップレベルで例外が起きるケース — __enter__ が起動応答の error
    メッセージ付きで SandboxError を送出することを確認する。"""
    meta = _meta(tmp_path, "crash", "indicator", IMPORT_TIME_CRASH_PY)
    with pytest.raises(SandboxError, match="ZeroDivisionError"):
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)


# --- print() がプロトコルに混入しない (stdout 保護) -------------------------

def test_plugin_stray_print_does_not_corrupt_protocol(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "ind", "indicator", PRINT_THEN_COMPUTE_PY)
    out = run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)
    assert out == {"x": 1.0}


# --- worker._poison_network_modules を直接検証 (check_source と独立) -------

def test_poison_network_modules_blocks_network_capable_submodules():
    import sys

    from agentic_fx.plugin import worker

    saved = {name: sys.modules.get(name) for name in
             ("socket", "urllib.request", "http.client", "http.server")}
    try:
        for name in saved:
            sys.modules.pop(name, None)
        worker._poison_network_modules()
        # `import socket` 自体が (import 機構が `sys.modules` 上の既登録
        # モジュールの `__spec__` を参照するため) この時点で ImportError
        # になる — 「使ってから初めて落ちる」より強い、import 文そのもの
        # を塞ぐ結果になっている (実測で確認)。
        with pytest.raises(ImportError):
            import socket  # noqa: F401
        with pytest.raises(ImportError):
            import urllib.request  # noqa: F401
        with pytest.raises(ImportError):
            import http.client  # noqa: F401
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


# --- ⑦ action="exit" → SandboxError --------------------------------------

def test_strategy_exit_action_rejected(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_EXIT_PY,
                timeframe="1h", pairs=("USDJPY",))
    with pytest.raises(SandboxError):
        run_plugin(meta, {"df": _df(), "indicators": {}, "signals": [], "params": {}},
                  settings=plugin_settings)


# --- ⑧ open で stop_loss 無し → SandboxError ------------------------------

def test_strategy_open_without_stop_loss_rejected(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_OPEN_NO_SL_PY,
                timeframe="1h", pairs=("USDJPY",))
    with pytest.raises(SandboxError):
        run_plugin(meta, {"df": _df(), "indicators": {}, "signals": [], "params": {}},
                  settings=plugin_settings)


def test_strategy_open_happy_path(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_OPEN_OK_PY,
                timeframe="1h", pairs=("USDJPY",))
    out = run_plugin(meta, {"df": _df(), "indicators": {}, "signals": [], "params": {}},
                     settings=plugin_settings)
    assert out["action"] == "open"
    assert out["direction"] == "long"
    assert out["entry_type"] == "market"
    assert out["stop_loss"] == pytest.approx(_df()["close"].iloc[-1] - 1.0)


def test_strategy_hold_happy_path(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_HOLD_PY,
                timeframe="1h", pairs=("USDJPY",))
    out = run_plugin(meta, {"df": _df(), "indicators": {}, "signals": [], "params": {}},
                     settings=plugin_settings)
    assert out["action"] == "hold"


# --- signal: happy path + bar_ts 拒否 (denylist mutation ガード) ----------

def test_signal_happy_path(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "sig", "signal", SIGNAL_OK_PY,
                timeframe="1h", pairs=("USDJPY",))
    out = run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)
    assert out["signals"] == [
        {"direction": "long", "strength": 0.8, "rationale": "test signal"}]


def test_signal_bar_ts_key_rejected(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "sig", "signal", SIGNAL_BAR_TS_PY,
                timeframe="1h", pairs=("USDJPY",))
    # "harness-owned audit key" は bar_ts 専用の分岐だけが出すメッセージ
    # (汎用の unknown-keys 分岐と区別する — bar_ts が _SIGNAL_ALLOWED_KEYS
    # から静かに外れているだけで拾われている偽陽性ではないことを保証する)。
    with pytest.raises(SandboxError, match="harness-owned audit key"):
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)


# --- integration: サンプル plugin (rsi_indicator / sma_cross) -------------

def test_sample_rsi_indicator_passes_check_source_and_runs(plugin_settings):
    plugin_dir = SAMPLES / "rsi_indicator"
    check_source(plugin_dir / "plugin.py")
    meta = PluginMeta(name="rsi_indicator", kind="indicator", path=plugin_dir,
                      params={"period": 14}, timeframe=None, pairs=(),
                      max_bars=200, content_hash="0" * 64)
    closes = [100 + i * 0.1 for i in range(30)]
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)}, index=idx)
    out = run_plugin(meta, {"df": df, "params": {"period": 14}}, settings=plugin_settings)
    assert "rsi_14" in out
    assert 0.0 <= out["rsi_14"] <= 100.0


def test_sample_sma_cross_passes_check_source_and_runs(plugin_settings):
    plugin_dir = SAMPLES / "sma_cross"
    check_source(plugin_dir / "plugin.py")
    meta = PluginMeta(name="sma_cross", kind="strategy", path=plugin_dir,
                      params={"fast_period": 5, "slow_period": 20,
                             "stop_loss_pips": 20, "pip_size": 0.01},
                      timeframe="1h", pairs=("USDJPY",), max_bars=200,
                      content_hash="0" * 64)
    closes = [120 - i for i in range(20)] + [200]
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)}, index=idx)
    out = run_plugin(
        meta, {"df": df, "indicators": {}, "signals": [], "params": meta.params},
        settings=plugin_settings)
    assert out["action"] == "open"
    assert out["direction"] == "long"
