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
from agentic_fx.plugin.loader import PluginMeta, content_hash as _real_content_hash
from agentic_fx.plugin.sandbox import (
    SandboxError, check_source, run_plugin, _build_env, _SINGLE_THREAD_ENV,
    _reader_worker, PluginSession,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"
SAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"


@pytest.fixture(scope="module")
def plugin_settings():
    return load_settings(EXAMPLE).plugin


def _meta(base: Path, name: str, kind: str, plugin_py: str, *,
         max_bars: int = 200, timeframe: str | None = None,
         pairs: tuple[str, ...] = ()) -> PluginMeta:
    """`content_hash` は**実際の** plugin.py/config.yaml から算出する
    (レビュー fix round 1 F1: `PluginSession.__enter__` が実行時に
    ハッシュを再検証するようになったため、固定プレースホルダだと全テスト
    が起動直後の hash-mismatch で SandboxError になってしまう)。"""
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(f"kind: {kind}\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return PluginMeta(name=name, kind=kind, path=d, params={}, timeframe=timeframe,
                      pairs=pairs, max_bars=max_bars, content_hash=_real_content_hash(d))


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

SIGNAL_EMPTY_RATIONALE_PY = """
def detect(df, params):
    return [{"direction": "long", "strength": 0.8, "rationale": ""}]
"""

# 最終レビュー F6: rationale 長さ上限のテスト用 — params["rationale_len"]
# 文字ちょうどの rationale を返す (境界値テストのため長さを外から制御)。
SIGNAL_RATIONALE_LEN_PY = """
def detect(df, params):
    return [{"direction": "long", "strength": 0.8,
             "rationale": "x" * params["rationale_len"]}]
"""

STRATEGY_RATIONALE_LEN_PY = """
def evaluate(df, indicators, signals, params):
    return {"action": "hold", "rationale": "x" * params["rationale_len"]}
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


# --- レビュー fix round 1 F1: from-import / 裸 Name のバイパス封鎖 --------

def test_check_source_rejects_from_import_read_csv(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text(
        "from pandas import read_csv\n"
        "def compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError, match="read_csv"):
        check_source(src)


def test_check_source_rejects_from_import_read_csv_with_asname(tmp_path):
    """`asname` で別名を付けても import 元の名前 (alias.name) で判定する
    ため回避できない。"""
    src = tmp_path / "bad.py"
    src.write_text(
        "from pandas import read_csv as rc\n"
        "def compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError, match="read_csv"):
        check_source(src)


def test_check_source_rejects_bare_read_csv_reference(tmp_path):
    """import 文自体を素通りさせても (仮に), from-import で束縛された
    裸の名前への参照そのものが `ast.Name` 検査で拒否される。"""
    src = tmp_path / "bad.py"
    src.write_text(
        "def compute(df, params):\n"
        "    fn = read_csv\n"
        "    return {}\n")
    with pytest.raises(SandboxError, match="read_csv"):
        check_source(src)


def test_check_source_rejects_from_numpy_import_load(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text(
        "from numpy import load\n"
        "def compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError, match="load"):
        check_source(src)


def test_check_source_rejects_function_named_with_denied_prefix(tmp_path):
    """plugin 自身が `read_xxx`/`to_xxx` (許可 4 種を除く) という名前の
    関数を定義するのも fail closed で拒否する — `ast.Attribute` を経由
    しない直接呼び出しでの denylist 回避を防ぐ。"""
    src = tmp_path / "bad.py"
    src.write_text(
        "def read_secret_file():\n    pass\n"
        "def compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError, match="read_secret_file"):
        check_source(src)


def test_check_source_allows_from_import_of_to_numpy_allowed_name(tmp_path):
    """`to_numpy` は許可 4 種の 1 つ — from-import で持ち込んでも拒否
    されないことのピン (実在の pandas API では無い名前だが、
    `check_source` は AST のみを見て実行はしないため合成できる)。"""
    src = tmp_path / "ok.py"
    src.write_text(
        "from pandas import to_numpy\n"
        "def compute(df, params):\n    return {}\n")
    check_source(src)  # 例外を投げなければ OK


# --- レビュー fix round 1 F4: ExcelWriter/HDFStore/dump denylist 補完 ----

def test_check_source_rejects_pd_excel_writer_attribute(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text(
        "import pandas as pd\n"
        "def compute(df, params):\n    pd.ExcelWriter('x.xlsx')\n    return {}\n")
    with pytest.raises(SandboxError, match="ExcelWriter"):
        check_source(src)


def test_check_source_rejects_pd_hdf_store_attribute(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text(
        "import pandas as pd\n"
        "def compute(df, params):\n    pd.HDFStore('x.h5')\n    return {}\n")
    with pytest.raises(SandboxError, match="HDFStore"):
        check_source(src)


def test_check_source_rejects_ndarray_dump(tmp_path):
    src = tmp_path / "bad.py"
    src.write_text(
        "def compute(df, params):\n"
        "    arr = df['close'].to_numpy()\n"
        "    arr.dump('x')\n"
        "    return {}\n")
    with pytest.raises(SandboxError, match="dump"):
        check_source(src)


# --- 最終レビュー F5 (codex Critical): pytest.importorskip 迂回封鎖 -------


def test_check_source_rejects_pytest_importorskip(tmp_path):
    """`pytest.importorskip("os")` は許可済み `pytest` の属性呼び出しとし
    て import allowlist/denylist の双方を素通りし、submit 実行だけで任意
    モジュールが手に入る迂回経路だった。test_plugin.py の `extra_allowed`
    (pytest 許可) を付けても reject されること。"""
    src = tmp_path / "test_plugin.py"
    src.write_text(
        "import pytest\n"
        "def test_bypass():\n"
        "    os = pytest.importorskip('os')\n"
        "    os.system('true')\n")
    with pytest.raises(SandboxError, match="importorskip"):
        check_source(src, extra_allowed=frozenset({"pytest", "plugin"}))


@pytest.mark.parametrize("fixture_name", ["capsys", "capfd", "capsysbinary", "capfdbinary"])
def test_check_source_rejects_pytest_capture_fixtures(tmp_path, fixture_name):
    src = tmp_path / "test_plugin.py"
    src.write_text(f"def test_capture({fixture_name}): pass\n")
    with pytest.raises(SandboxError):
        check_source(src, extra_allowed=frozenset({"pytest", "plugin"}))


def test_check_source_rejects_from_pytest_import_importorskip(tmp_path):
    """`from pytest import importorskip` のような別名バイパスも、裸 Name
    への束縛検査 (`_is_denied_bare_name`) で拒否される (F1 レビュー fix
    round 1 と同じ機構への追加)。"""
    src = tmp_path / "test_plugin.py"
    src.write_text(
        "from pytest import importorskip\n"
        "def test_bypass():\n"
        "    importorskip('os')\n")
    with pytest.raises(SandboxError, match="importorskip"):
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


# --- レビュー fix round 1 F5: シリアライズ失敗の契約・timeout_sec 検証 ---

class _Unserializable:
    """json.dumps できない型のスタンドイン (numpy スカラー等の代役)。"""


def test_call_with_unserializable_params_raises_sandbox_error_session_survives(
        tmp_path, plugin_settings):
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    with PluginSession(meta, settings=plugin_settings) as session:
        with pytest.raises(SandboxError, match="serialize"):
            session.call({"df": _df(), "params": {"bad": _Unserializable()}})
        # F5: シリアライズ失敗は書き込み前に起きるためセッションは継続可能
        # (timeout/oversize/EOF とは異なり session を破棄しない)。
        out = session.call({"df": _df(), "params": {}})
        assert out["mean_close"] == pytest.approx(_df()["close"].mean())


def test_run_plugin_rejects_non_positive_timeout_sec(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    with pytest.raises(SandboxError, match="timeout_sec"):
        run_plugin(meta, {"df": _df(), "params": {}}, timeout_sec=0,
                  settings=plugin_settings)
    with pytest.raises(SandboxError, match="timeout_sec"):
        run_plugin(meta, {"df": _df(), "params": {}}, timeout_sec=-1.0,
                  settings=plugin_settings)


# --- ③④⑤ (check_source を通した実行経路: run_plugin も reject する) ------

def test_run_plugin_rejects_disallowed_import_before_spawning(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "bad", "indicator",
                "import os\ndef compute(df, params):\n    return {}\n")
    with pytest.raises(SandboxError):
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)


# --- レビュー fix round 1 F1 (Task 3 レビュー, codex Critical):
# 起動時ハッシュ照合と実行時 import の TOCTOU を worker 起動前の再検証で
# 封鎖する。`approved_plugins()` は起動時 (discover 時点) にディスクの
# ハッシュを検証するだけで、実際に worker が plugin.py を import するのは
# その後の呼び出し時 — この間隔で plugin.py が承認内容と異なる内容に
# 差し替えられても、再検証が無ければ古い承認のまま新しいコードが実行され
# てしまう ("承認は内容ハッシュに対して行う" 設計書 §6 の実行時破れ)。

def test_run_plugin_rejects_when_plugin_edited_after_meta_captured(
        tmp_path, plugin_settings):
    """① meta 取得後に plugin.py を書き換える → run_plugin (= PluginSession.
    __enter__ 経由) が worker を起動する前に SandboxError を送出する。"""
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    (meta.path / "plugin.py").write_text(
        INDICATOR_OK_PY + "\n# edited after meta was captured\n")
    with pytest.raises(SandboxError, match="content changed since discovery"):
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)


def test_run_plugin_unedited_meta_still_runs_normally(tmp_path, plugin_settings):
    """③ 回帰: plugin.py を編集しなければ従来どおり通る (①の対比 — 常に
    reject される変異 (`raise SandboxError` を常時実行等) を検出する)。"""
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    out = run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)
    assert out["mean_close"] == pytest.approx(_df()["close"].mean())


# --- ⑥ 無限ループ → timeout 1s で SandboxError (実測上限 1 秒。唯一の実 sleep) ---

def test_infinite_loop_times_out_within_one_second(tmp_path, plugin_settings):
    import os
    import time

    meta = _meta(tmp_path, "loop", "indicator", INFINITE_LOOP_PY)
    # `sandbox_session_cpu_sec` も併せて絞る: `_kill()` が壊れた場合の
    # 保険 (万一 killpg が効かなくても RLIMIT_CPU の SIGXCPU で worker が
    # 自滅し、`close()` の `proc.stdout.close()` がブロックし続ける最悪
    # ケースの上限を既定 60s から 10s に縮める — レビュー fix round 1 F3
    # の変異注入で実際にこの経路 (60s 張り付き) を踏んだ実測に基づく)。
    tight = plugin_settings.model_copy(
        update={"sandbox_timeout_sec": 1.0, "sandbox_session_cpu_sec": 10})
    session = PluginSession(meta, settings=tight)
    with session:
        pid = session.pid
        start = time.monotonic()
        with pytest.raises(SandboxError, match="timed out"):
            session.call({"df": _df(), "params": {}})
        elapsed = time.monotonic() - start
        assert elapsed < 2.0  # 実測上限 1 秒 + 若干のプロセス kill 猶予

        # レビュー fix round 1 F3: `_kill()` を no-op に劣化させる変異が
        # 全テスト生存していた (timeout の判定はタイミングと `_dead`
        # フラグしか見ていなかった) — worker の OS プロセスが実際に
        # 死んでいることを直接確認する。`_kill()` 内で既に
        # `proc.wait(timeout=5)` 済みのはずなので通常は即座に
        # ProcessLookupError になる (ポーリングは念のための上限)。
        assert pid is not None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"worker pid {pid} still alive {2.0}s after kill")

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


# --- レビュー fix round 1 F7: _reader_worker の境界ピン (実プロセス無し) --

class _FakeStream:
    """`.read1(n)` だけを実装する最小限のフェイク stdout。事前に用意した
    チャンク列を順に返す (実プロセス無しで境界の意味論を決定的に検証)。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read1(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def test_reader_worker_accepts_exact_boundary_size():
    import queue as queue_module

    content = b'{"ok": true, "x": 1}'
    line = content + b"\n"

    q = queue_module.Queue()
    _reader_worker(_FakeStream([line]), len(line), q)  # ちょうど境界: 受理
    kind, payload = q.get_nowait()
    assert kind == "line"
    assert payload == content


def test_reader_worker_rejects_one_byte_over_boundary():
    import queue as queue_module

    content = b'{"ok": true, "x": 1}'
    line = content + b"\n"

    q = queue_module.Queue()
    _reader_worker(_FakeStream([line]), len(line) - 1, q)  # 1 バイト超過
    kind, _payload = q.get_nowait()
    assert kind == "oversize"


def test_reader_worker_oversize_detected_incrementally_across_chunks():
    """1 行がまだ完成していなくても、蓄積済みバイト数が上限を超えた時点
    (次チャンク到着時) で打ち切る意味論のピン。"""
    import queue as queue_module

    q = queue_module.Queue()
    _reader_worker(_FakeStream([b"aaaa", b"bbbb\n"]), 6, q)
    kind, _payload = q.get_nowait()
    assert kind == "oversize"


# --- worker 起動失敗 (__enter__ の ready=false 診断パス) -------------------

def test_worker_import_time_crash_reports_startup_error(tmp_path, plugin_settings):
    """check_source は通る (denylist に無い名前のみ) が plugin.py の
    トップレベルで例外が起きるケース — __enter__ が起動応答の error
    メッセージ付きで SandboxError を送出することを確認する。"""
    meta = _meta(tmp_path, "crash", "indicator", IMPORT_TIME_CRASH_PY)
    with pytest.raises(SandboxError, match="ZeroDivisionError"):
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)


# --- レビュー fix round 1 F2: __enter__ 失敗時の fd リーク -----------------

def test_enter_failure_closes_pipes_and_marks_dead(tmp_path, plugin_settings,
                                                    monkeypatch):
    """`__enter__` が起動応答 (ready=false) で失敗するケースで、
    stdin/stdout パイプが確実に close されていることを直接検証する
    (`subprocess.Popen` を実物のまま素通しさせつつ生成された proc を
    横取りして、`close()` 後の `.closed` を見る)。以前は `close()` を
    呼ばない失敗パスがあり fd が GC 任せになっていた。"""
    import subprocess as subprocess_module

    from agentic_fx.plugin import sandbox as sandbox_module

    meta = _meta(tmp_path, "crash", "indicator", IMPORT_TIME_CRASH_PY)
    captured: list = []
    real_popen = subprocess_module.Popen

    def _capturing_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured.append(proc)
        return proc

    monkeypatch.setattr(sandbox_module.subprocess, "Popen", _capturing_popen)

    session = PluginSession(meta, settings=plugin_settings)
    with pytest.raises(SandboxError, match="ZeroDivisionError"):
        session.__enter__()

    assert session._dead is True
    assert session._proc is None  # close() が proc を確実に None にした
    assert len(captured) == 1
    proc = captured[0]
    assert proc.stdin.closed
    assert proc.stdout.closed


def test_enter_wraps_unexpected_popen_failure_as_sandbox_error(
        tmp_path, plugin_settings, monkeypatch):
    """`subprocess.Popen` 自体が想定外の例外を投げても `SandboxError` に
    写像される (呼び出し側は `SandboxError` 1 種だけ catch すればよい
    契約を `__enter__` でも保つ)。"""
    from agentic_fx.plugin import sandbox as sandbox_module

    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)

    def _raising_popen(*args, **kwargs):
        raise OSError("simulated exec failure")

    monkeypatch.setattr(sandbox_module.subprocess, "Popen", _raising_popen)

    session = PluginSession(meta, settings=plugin_settings)
    with pytest.raises(SandboxError, match="simulated exec failure"):
        session.__enter__()
    assert session._dead is True
    assert session._proc is None


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
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings,
                  resolved=ResolvedIndicatorSet.empty(tmp_path))


# --- ⑧ open で stop_loss 無し → SandboxError ------------------------------

def test_strategy_open_without_stop_loss_rejected(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_OPEN_NO_SL_PY,
                timeframe="1h", pairs=("USDJPY",))
    with pytest.raises(SandboxError):
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings,
                  resolved=ResolvedIndicatorSet.empty(tmp_path))


def test_strategy_open_happy_path(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_OPEN_OK_PY,
                timeframe="1h", pairs=("USDJPY",))
    out = run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings,
                     resolved=ResolvedIndicatorSet.empty(tmp_path))
    assert out["action"] == "open"
    assert out["direction"] == "long"
    assert out["entry_type"] == "market"
    assert out["stop_loss"] == pytest.approx(_df()["close"].iloc[-1] - 1.0)


def test_strategy_hold_happy_path(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_HOLD_PY,
                timeframe="1h", pairs=("USDJPY",))
    out = run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings,
                     resolved=ResolvedIndicatorSet.empty(tmp_path))
    assert out["action"] == "hold"


# --- 最終レビュー F6 (codex Important, 注入面縮小): rationale 最大長 -----


def test_strategy_rationale_at_2000_chars_accepted(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "strat_rat_ok", "strategy", STRATEGY_RATIONALE_LEN_PY,
                timeframe="1h", pairs=("USDJPY",))
    out = run_plugin(
        meta, {"df": _df(), "params": {"rationale_len": 2000}},
        settings=plugin_settings, resolved=ResolvedIndicatorSet.empty(tmp_path))
    assert len(out["rationale"]) == 2000


def test_strategy_rationale_over_2000_chars_rejected(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "strat_rat_ng", "strategy", STRATEGY_RATIONALE_LEN_PY,
                timeframe="1h", pairs=("USDJPY",))
    with pytest.raises(SandboxError, match="rationale exceeds max length"):
        run_plugin(
            meta, {"df": _df(), "params": {"rationale_len": 2001}},
            settings=plugin_settings, resolved=ResolvedIndicatorSet.empty(tmp_path))


# --- signal: happy path + bar_ts 拒否 (denylist mutation ガード) ----------

def test_signal_happy_path(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "sig", "signal", SIGNAL_OK_PY,
                timeframe="1h", pairs=("USDJPY",))
    out = run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)
    assert out["signals"] == [
        {"direction": "long", "strength": 0.8, "rationale": "test signal"}]


# --- fix round 2 F8: signal 経路の空 rationale 拒否 ------------------------

def test_signal_empty_rationale_rejected(tmp_path, plugin_settings):
    """strategy 経路は `StrategyDecision` の実構築で非空 str 検証が自動
    的にかかるが、signal 経路は Signal を構築せず手組み検証のため、
    以前は `rationale=""` が素通りしていた (fix round 1 レビューの
    残ギャップ)。"""
    meta = _meta(tmp_path, "sig", "signal", SIGNAL_EMPTY_RATIONALE_PY,
                timeframe="1h", pairs=("USDJPY",))
    with pytest.raises(SandboxError, match="non-empty"):
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)


def test_signal_bar_ts_key_rejected(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "sig", "signal", SIGNAL_BAR_TS_PY,
                timeframe="1h", pairs=("USDJPY",))
    # "harness-owned audit key" は bar_ts 専用の分岐だけが出すメッセージ
    # (汎用の unknown-keys 分岐と区別する — bar_ts が _SIGNAL_ALLOWED_KEYS
    # から静かに外れているだけで拾われている偽陽性ではないことを保証する)。
    with pytest.raises(SandboxError, match="harness-owned audit key"):
        run_plugin(meta, {"df": _df(), "params": {}}, settings=plugin_settings)


# --- 最終レビュー F6 (codex Important, 注入面縮小): rationale 最大長 -----


def test_signal_rationale_at_2000_chars_accepted(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "sig_rat_ok", "signal", SIGNAL_RATIONALE_LEN_PY,
                timeframe="1h", pairs=("USDJPY",))
    out = run_plugin(meta, {"df": _df(), "params": {"rationale_len": 2000}},
                     settings=plugin_settings)
    assert len(out["signals"][0]["rationale"]) == 2000


def test_signal_rationale_over_2000_chars_rejected(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "sig_rat_ng", "signal", SIGNAL_RATIONALE_LEN_PY,
                timeframe="1h", pairs=("USDJPY",))
    with pytest.raises(SandboxError, match="rationale exceeds max length"):
        run_plugin(meta, {"df": _df(), "params": {"rationale_len": 2001}},
                  settings=plugin_settings)


# --- integration: サンプル plugin (rsi_indicator / sma_cross) -------------

def test_sample_rsi_indicator_passes_check_source_and_runs(plugin_settings):
    # [indicator-consumption-wiring] 2026-09-14: サンプルは系列返却
    # (`outputs: [rsi]`) に更新済み。
    plugin_dir = SAMPLES / "rsi_indicator"
    check_source(plugin_dir / "plugin.py")
    meta = PluginMeta(name="rsi_indicator", kind="indicator", path=plugin_dir,
                      params={"period": 14}, timeframe=None, pairs=(),
                      max_bars=200, content_hash=_real_content_hash(plugin_dir),
                      outputs=("rsi",))
    closes = [100 + i * 0.1 for i in range(30)]
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)}, index=idx)
    out = run_plugin(meta, {"df": df, "params": {"period": 14}}, settings=plugin_settings)
    assert "rsi" in out
    assert 0.0 <= out["rsi"][-1] <= 100.0


def test_sample_sma_cross_passes_check_source_and_runs(plugin_settings):
    plugin_dir = SAMPLES / "sma_cross"
    check_source(plugin_dir / "plugin.py")
    meta = PluginMeta(name="sma_cross", kind="strategy", path=plugin_dir,
                      params={"fast_period": 5, "slow_period": 20,
                             "stop_loss_pips": 20, "pip_size": 0.01},
                      timeframe="1h", pairs=("USDJPY",), max_bars=200,
                      content_hash=_real_content_hash(plugin_dir))
    closes = [120 - i for i in range(20)] + [200]
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [1.0] * len(closes)}, index=idx)
    out = run_plugin(
        meta, {"df": df, "params": meta.params},
        settings=plugin_settings, resolved=ResolvedIndicatorSet.empty(plugin_dir.parent))
    assert out["action"] == "open"
    assert out["direction"] == "long"


# --- [indicator-consumption-wiring] T2: 同居実行 --------------------------

from agentic_fx.plugin.resolve import (
    ResolvedIndicator, ResolvedIndicatorSet, freeze_params,
)

RSI_SERIES_PY = """
from __future__ import annotations

import pandas as pd


def compute(df, params):
    period = int(params.get("period", 14))
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False,
                                     min_periods=period).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False,
                                        min_periods=period).mean()
    rs = gain / loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(loss != 0.0, 100.0)
    out = out.where(~((gain == 0.0) & (loss == 0.0)), 50.0)
    return {"rsi": out}
"""

STRATEGY_READS_INDICATORS_PY = """
def evaluate(df, indicators, signals, params):
    r = indicators["rsi"]["rsi"]
    return {"action": "hold",
            "rationale": f"len={len(r)} last={float(r.iloc[-1]):.4f} "
                         f"signals={signals!r} keys={sorted(indicators)}"}
"""

MUTATOR_INDICATOR_PY = """
import pandas as pd


def compute(df, params):
    df["close"] = 0.0
    params["nested"]["x"].append(999)
    return {"v": pd.Series([float(len(params["nested"]["x"]))] * len(df),
                           index=df.index)}
"""


def _indicator_dir(base, name, plugin_py, *, outputs=("v",), params=None,
                   max_bars=200):
    import yaml
    d = base / name
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "indicator", "outputs": list(outputs),
         "params": params or {}, "max_bars": max_bars}, sort_keys=False))
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return d


def _resolved(root, *entries):
    """entries = [(alias, dir, outputs, params, max_bars)]

    opus r1 C5 是正: `ResolvedIndicator` は 8 フィールド全てが既定値なしの
    必須引数 (`@dataclass(frozen=True, slots=True)`、Step 1-3 の Produces)
    なので `pinned=` を落とすと `TypeError: __init__() missing 1 required
    positional argument: 'pinned'` で本ヘルパを使う 6 テストが全滅する。
    同 task の `tests/plugin/test_indicator_containment.py` 側は
    `pinned=True` を渡しており、こちらが転写ミスだった。
    """
    from agentic_fx.plugin.loader import content_hash as _ch
    items = tuple(sorted(
        (ResolvedIndicator(alias=a, plugin_name=d.name, plugin_py=d / "plugin.py",
                           content_hash=_ch(d), params=freeze_params(p),
                           max_bars=mb, outputs=tuple(o), pinned=True)
         for a, d, o, p, mb in entries), key=lambda i: i.alias))
    return ResolvedIndicatorSet(inventory_root=root.resolve(), items=items,
                                all_pinned=True)


def test_strategy_receives_indicator_series_from_same_worker(tmp_path,
                                                             plugin_settings):
    root = tmp_path / "plugins"
    rsi_dir = _indicator_dir(root, "rsi", RSI_SERIES_PY, outputs=("rsi",),
                             params={"period": 14})
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_READS_INDICATORS_PY,
                 timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("rsi", rsi_dir, ("rsi",), {"period": 14}, 200))
    df = _df(40)
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        out = s.call({"df": df, "params": {}})
    assert out["action"] == "hold"
    assert f"len={len(df)}" in out["rationale"]
    assert "keys=['rsi']" in out["rationale"]
    # N1: signals は依然 None
    assert "signals=None" in out["rationale"]


def test_indicator_mutation_does_not_leak_across_calls_or_to_strategy(
        tmp_path, plugin_settings):
    """V2: df / params (nested) の書き換えが strategy にも次回 call にも届かない。"""
    root = tmp_path / "plugins"
    mut_dir = _indicator_dir(root, "mut", MUTATOR_INDICATOR_PY,
                             params={"nested": {"x": [1]}})
    meta = _meta(tmp_path, "strat2", "strategy", """
def evaluate(df, indicators, signals, params):
    return {"action": "hold",
            "rationale": f"close0={float(df['close'].iloc[0]):.2f} "
                         f"v={float(indicators['mut']['v'].iloc[-1]):.0f} "
                         f"p={params['keep']}"}
""", timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("mut", mut_dir, ("v",), {"nested": {"x": [1]}}, 200))
    df = _df(10)
    first_close = float(df["close"].iloc[0])
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        out1 = s.call({"df": df, "params": {"keep": [1]}})
        out2 = s.call({"df": df, "params": {"keep": [1]}})
    # indicator が書き換えた close は strategy に届かない
    assert f"close0={first_close:.2f}" in out1["rationale"]
    # 次回 call でも params は元の 1 要素から始まる (v=2 が 2 回)
    assert "v=2" in out1["rationale"] and "v=2" in out2["rationale"]
    assert "p=[1]" in out1["rationale"] and "p=[1]" in out2["rationale"]
    # 親側の df も無傷
    assert float(df["close"].iloc[0]) == first_close


def test_indicator_failure_becomes_sandbox_error(tmp_path, plugin_settings):
    root = tmp_path / "plugins"
    bad = _indicator_dir(root, "bad", "def compute(df, params):\n    raise ValueError('boom')\n")
    meta = _meta(tmp_path, "strat3", "strategy", STRATEGY_READS_INDICATORS_PY,
                 timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("bad", bad, ("v",), {}, 200))
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        with pytest.raises(SandboxError, match="boom"):
            s.call({"df": _df(10), "params": {}})


def test_indicator_output_set_mismatch_becomes_sandbox_error(tmp_path,
                                                             plugin_settings):
    """V1: outputs 集合不一致は親に SandboxError として届く。"""
    root = tmp_path / "plugins"
    wrong = _indicator_dir(root, "wrong",
                           "def compute(df, params):\n    return {'other': 1.0}\n",
                           outputs=("v",))
    meta = _meta(tmp_path, "strat4", "strategy", STRATEGY_READS_INDICATORS_PY,
                 timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("wrong", wrong, ("v",), {}, 200))
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        with pytest.raises(SandboxError, match="outputs"):
            s.call({"df": _df(10), "params": {}})


def test_indicator_tail_is_clamped_to_its_own_max_bars(tmp_path, plugin_settings):
    root = tmp_path / "plugins"
    counter = _indicator_dir(
        root, "cnt",
        "import pandas as pd\n"
        "def compute(df, params):\n"
        "    return {'v': pd.Series([float(len(df))] * len(df), index=df.index)}\n",
        max_bars=5)
    meta = _meta(tmp_path, "strat5", "strategy", """
def evaluate(df, indicators, signals, params):
    v = indicators["cnt"]["v"]
    return {"action": "hold",
            "rationale": f"n={len(v)} head={'nan' if v.isna().iloc[0] else 'val'} "
                         f"last={float(v.iloc[-1]):.0f}"}
""", timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("cnt", counter, ("v",), {}, 5))
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        out = s.call({"df": _df(20), "params": {}})
    # 系列は strategy の df.index (20 本) に左 NaN 埋めで整列される
    assert "n=20" in out["rationale"]
    assert "head=nan" in out["rationale"]
    assert "last=5" in out["rationale"]      # indicator は 5 本だけ見た


def test_handshake_omits_outputs_key_for_non_indicator_kind(tmp_path,
                                                             plugin_settings,
                                                             monkeypatch):
    """[indicator-consumption-wiring] T2: `outputs` は kind == "indicator"
    のときだけ handshake に載る契約 (codex plan r1 M5)。strategy の
    handshake には `"outputs"` キー自体が存在しないことを pin する。"""
    meta = _meta(tmp_path, "strat_hs", "strategy", STRATEGY_HOLD_PY,
                timeframe="1h", pairs=("USDJPY",))
    resolved = ResolvedIndicatorSet.empty(tmp_path)
    sent: list[dict] = []
    session = PluginSession(meta, settings=plugin_settings, resolved=resolved)
    real_write_line = session._write_line

    def _spy(obj):
        sent.append(obj)
        real_write_line(obj)

    monkeypatch.setattr(session, "_write_line", _spy)
    with session:
        pass
    assert sent, "handshake was never sent"
    assert "outputs" not in sent[0]


def test_handshake_includes_outputs_key_for_indicator_kind(tmp_path,
                                                            plugin_settings,
                                                            monkeypatch):
    meta = _meta(tmp_path, "ind_hs", "indicator", INDICATOR_OK_PY)
    sent: list[dict] = []
    session = PluginSession(meta, settings=plugin_settings)
    real_write_line = session._write_line

    def _spy(obj):
        sent.append(obj)
        real_write_line(obj)

    monkeypatch.setattr(session, "_write_line", _spy)
    with session:
        pass
    assert sent, "handshake was never sent"
    assert "outputs" in sent[0]


def test_cpu_sec_is_float_after_graceful_close(tmp_path, plugin_settings):
    """C1: 正常終了で float。close 前は None。"""
    meta = _meta(tmp_path, "ind_cpu", "indicator", INDICATOR_OK_PY)
    session = PluginSession(meta, settings=plugin_settings)
    session.__enter__()
    assert session.cpu_sec is None
    session.call({"df": _df(), "params": {}})
    assert session.cpu_sec is None
    session.close()
    assert isinstance(session.cpu_sec, float)
    assert session.cpu_sec >= 0.0


def test_cpu_sec_is_none_when_enter_failed(tmp_path, plugin_settings):
    """C1: __enter__ 失敗 (worker 未起動) は None のまま。"""
    meta = _meta(tmp_path, "ind_bad_hash", "indicator", INDICATOR_OK_PY)
    (meta.path / "plugin.py").write_text(INDICATOR_OK_PY + "\n# tamper\n")
    session = PluginSession(meta, settings=plugin_settings)
    with pytest.raises(SandboxError):
        session.__enter__()
    assert session.cpu_sec is None


def test_cpu_sec_is_none_after_timeout_kill(tmp_path):
    """C1: SIGKILL fallback (timeout でセッションが死んだ後) は None。"""
    settings = load_settings(EXAMPLE).plugin.model_copy(
        update={"sandbox_timeout_sec": 1.0})
    meta = _meta(tmp_path, "ind_spin", "indicator",
                 "def compute(df, params):\n"
                 "    while True:\n        pass\n")
    session = PluginSession(meta, settings=settings)
    session.__enter__()
    with pytest.raises(SandboxError, match="timed out"):
        session.call({"df": _df(), "params": {}})
    session.close()
    assert session.cpu_sec is None


def test_cpu_sec_is_float_after_plugin_error(tmp_path, plugin_settings):
    """C1: plugin error 後は worker が生きているので graceful close が成立し
    `cpu_sec` は **float**。plugin コード自身の例外はセッションを `_dead` に
    しない既存契約 (`sandbox.py`) のため。`None` になるのは SIGKILL fallback
    (timeout 後) と worker 未起動 (`__enter__` 失敗) の 2 経路だけ。"""
    meta = _meta(tmp_path, "ind_err", "indicator",
                 "def compute(df, params):\n    raise ValueError('x')\n")
    session = PluginSession(meta, settings=plugin_settings)
    session.__enter__()
    with pytest.raises(SandboxError):
        session.call({"df": _df(), "params": {}})
    session.close()
    assert isinstance(session.cpu_sec, float)


@pytest.mark.parametrize("snippet,needle", [
    ("pd.set_option('mode.chained_assignment', None)", "set_option"),
    ("pd.reset_option('mode.chained_assignment')", "reset_option"),
    ("pd.set_eng_float_format(accuracy=3)", "set_eng_float_format"),
    ("np.seterr(all='ignore')", "seterr"),
    ("np.seterrcall(None)", "seterrcall"),
    ("np.setbufsize(8192)", "setbufsize"),
    ("np.set_printoptions(threshold=5)", "set_printoptions"),
])
def test_check_source_denies_global_state_mutators(tmp_path, snippet, needle):
    path = tmp_path / f"p_{needle}.py"
    path.write_text("import numpy as np\nimport pandas as pd\n"
                    f"def compute(df, params):\n    {snippet}\n    return {{}}\n")
    with pytest.raises(SandboxError, match=needle):
        check_source(path)


@pytest.mark.parametrize("snippet", [
    "pd.options.mode.chained_assignment = None",
    "pd.options.display.max_rows: int = 5",
    "pd.options.display.max_rows += 1",
    "(a, np.x.y) = (1, 2)",
    "for pd.options.x.y in [1]:\n        pass",
    "del pd.options.x.y",
    "with open_ctx() as pd.options.x.y:\n        pass",
])
def test_check_source_rejects_attribute_store(tmp_path, snippet):
    path = tmp_path / "attr_store.py"
    path.write_text("import numpy as np\nimport pandas as pd\n"
                    f"def compute(df, params):\n    {snippet}\n    return {{}}\n")
    with pytest.raises(SandboxError, match="attribute assignment"):
        check_source(path)


def test_check_source_allows_local_attribute_free_assignment(tmp_path):
    """自ローカル変数への代入・添字代入は従来どおり許す (誤検出しない)。"""
    path = tmp_path / "ok.py"
    path.write_text(
        "import pandas as pd\n"
        "def compute(df, params):\n"
        "    out = {}\n"
        "    out['v'] = 1.0\n"
        "    x, y = 1, 2\n"
        "    for i in range(3):\n        x += i\n"
        "    return out\n")
    check_source(path)


def test_worker_asserts_global_state_unchanged(tmp_path, plugin_settings):
    """V2: check_source を通り抜けた動的変更があっても worker の
    不変 assert が SandboxError に倒す (多層防御)。"""
    root = tmp_path / "plugins"
    sneaky = _indicator_dir(
        root, "sneaky",
        "import numpy as np\n"
        "import pandas as pd\n"
        "def compute(df, params):\n"
        "    fn = np.__dict__['seterr']\n"
        "    fn(all='ignore')\n"
        "    return {'v': 1.0}\n")
    meta = _meta(tmp_path, "strat_g", "strategy",
                 STRATEGY_READS_INDICATORS_PY.replace('indicators["rsi"]["rsi"]',
                                                      'indicators["sneaky"]["v"]')
                 .replace("len(r)", "1").replace("r.iloc[-1]", "r"),
                 timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("sneaky", sneaky, ("v",), {}, 200))
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        with pytest.raises(SandboxError, match="global state"):
            s.call({"df": _df(10), "params": {}})


def test_worker_asserts_pandas_global_state_unchanged(tmp_path, plugin_settings):
    """1 周目 ローカル LLM (c07 qwen): グローバル状態不変 assert の
    **pandas 側**。

    worker は `pd.get_option("mode.chained_assignment") != baseline_chained
    or dict(np.geterr()) != baseline_errstate` の 2 項を見るが、既存の
    `test_worker_asserts_global_state_unchanged` の sneaky indicator は
    **numpy しか触らない**。そのため or の pandas 項を丸ごと落とす変異が
    134 passed で生存した。多層防御の片側が黙って外れるので、pandas の
    オプションを動的に書き換える indicator を別に立てて or の両辺を独立に
    踏ませる。
    """
    root = tmp_path / "plugins"
    sneaky = _indicator_dir(
        root, "sneaky_pd",
        "import pandas as pd\n"
        "def compute(df, params):\n"
        "    fn = pd.__dict__['set_option']\n"
        "    fn('mode.chained_assignment', 'raise')\n"
        "    return {'v': 1.0}\n")
    meta = _meta(tmp_path, "strat_pd", "strategy",
                 STRATEGY_READS_INDICATORS_PY.replace('indicators["rsi"]["rsi"]',
                                                      'indicators["sneaky_pd"]["v"]')
                 .replace("len(r)", "1").replace("r.iloc[-1]", "r"),
                 timeframe="1h", pairs=("USDJPY",))
    resolved = _resolved(root, ("sneaky_pd", sneaky, ("v",), {}, 200))
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as s:
        with pytest.raises(SandboxError, match="global state"):
            s.call({"df": _df(10), "params": {}})


def test_standalone_indicator_response_rejects_bool_and_non_finite_scalars():
    """1 周目 ローカル LLM (c06 qwen): 親側再検証のスカラー型ガード。

    既存の `test_standalone_indicator_response_rejects_extra_or_missing_
    outputs_keys` は**キー集合**しか見ないため、
    `isinstance(value, bool) or not isinstance(value, (int, float))` から
    bool 判定を外す変異が 151 passed で生存した。この変異下では wire の
    `true` が `float(True) == 1.0` になり、後段の共通 validator には
    **もう bool ではなく float として**渡るので誰も気づけない
    (契約 7「bool は拒否」が親側で丸ごと外れる)。±Inf と数値文字列も
    同じ 1 行が守っているので併せて pin する。
    """
    from agentic_fx.plugin.sandbox import SandboxError, _validate_indicator_result

    for bad in (True, False, float("inf"), float("-inf"), "1.5"):
        with pytest.raises(SandboxError):
            _validate_indicator_result({"a": bad}, outputs=("a",))
    # 系列側の同じガード (要素の bool / 非有限)
    for bad in (True, float("inf")):
        with pytest.raises(SandboxError):
            _validate_indicator_result({"a": {"series": [1.0, bad]}},
                                       outputs=("a",))
    # 正常系は通る (恒真 assert になっていないことの裏取り)
    assert _validate_indicator_result({"a": 1.5}, outputs=("a",)) == {"a": 1.5}


def test_standalone_indicator_response_rejects_extra_or_missing_outputs_keys():
    """codex plan r2 束1 Important: worker を迂回した/破損した応答 (wire を
    直接偽装した呼び出し) が `meta.outputs` と食い違うキー集合を返したとき、
    **親側の `_validate_indicator_result` が拒否する**こと (worker 側の検査
    だけに頼らない — 親子の信頼境界を跨いだ値の再検証、S1)。"""
    from agentic_fx.plugin.sandbox import SandboxError, _validate_indicator_result

    outputs = ("a", "b")
    # 欠落: outputs=("a","b") のうち "b" が無い
    with pytest.raises(SandboxError, match="outputs"):
        _validate_indicator_result({"a": 1.0}, outputs=outputs)
    # 余分: 宣言に無い "c" が混ざる
    with pytest.raises(SandboxError, match="outputs"):
        _validate_indicator_result({"a": 1.0, "b": 2.0, "c": 3.0},
                                   outputs=outputs)
    # outputs=None (standalone 宣言なし、S1) は任意のキー集合を許す
    assert _validate_indicator_result({"whatever": 1.0}, outputs=None) == \
        {"whatever": 1.0}
