"""PluginSession の thread 所有の検査と、別 thread からの close の後始末。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from agentic_fx.config import PluginSettings, load_settings
from agentic_fx.plugin import sandbox
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import PluginSession, SandboxError

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"


def _fake_meta(tmp_path) -> PluginMeta:
    (tmp_path / "plugin.py").write_text(
        "def compute(df, params):\n    return {}\n")
    (tmp_path / "config.yaml").write_text(
        "max_bars: 200\nparams: {}\n")
    from agentic_fx.plugin.loader import content_hash
    return PluginMeta(name="x", kind="indicator", path=tmp_path, params={},
                      timeframe=None, pairs=(), max_bars=200,
                      content_hash=content_hash(tmp_path))


def test_call_from_other_thread_raises_runtime_error(tmp_path):
    """`__enter__` を呼んだスレッド以外からの `call()` は RuntimeError
    (SandboxError ではない — プログラミングエラーと plugin 実行時エラーの
    区別)。実 subprocess は起動せず、owner thread チェックが __enter__
    完了前の早い段階 (spawn 前) で発火することを確認する — session が
    未起動 (`self._proc is None`) の状態でも境界チェックが機能すること
    のピン。
    """
    session = PluginSession(_fake_meta(tmp_path), settings=PluginSettings())
    session._owner_thread = threading.get_ident() + 999999  # 別スレッドを偽装

    with pytest.raises(RuntimeError, match="owner thread"):
        session.call({"df": None, "params": {}})


def test_enter_from_other_thread_raises_runtime_error(tmp_path):
    """`__enter__` を別スレッドから呼ぶと RuntimeError。owner-thread の検査は
    `__enter__` と `call` に掛ける (このテストは __enter__ の検査)。"""
    session = PluginSession(_fake_meta(tmp_path), settings=PluginSettings())
    session._owner_thread = threading.get_ident() + 999999  # 別スレッドを偽装

    with pytest.raises(RuntimeError, match="owner thread"):
        session.__enter__()


def test_close_of_an_unstarted_session_from_other_thread_does_not_raise(tmp_path):
    """close は後始末の契約 (例外を出さない) を優先し、呼んだ thread を問わない。"""
    session = PluginSession(_fake_meta(tmp_path), settings=PluginSettings())
    session._owner_thread = threading.get_ident() + 999999  # 別スレッドを偽装

    session.close()
    session.__exit__(None, None, None)


# --- 実 worker: 別 thread からの close も kill・reap・fd close まで行う --------

_needs_isolation = pytest.mark.skipif(sandbox.host_preflight()[0] is not None,
                                      reason="this host cannot isolate plugin workers")

_SPIN_PLUGIN = (
    "def compute(df, params):\n"
    "    n = 0\n"
    "    while True:\n"
    "        n += 1\n")


def _real_meta(base, name: str, plugin_py: str) -> PluginMeta:
    from agentic_fx.plugin.loader import content_hash
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text("kind: indicator\n")
    return PluginMeta(name=name, kind="indicator", path=d, params={}, timeframe=None,
                      pairs=(), max_bars=200, content_hash=content_hash(d))


def _df(n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05", periods=n, freq="1h", tz="UTC")
    close = [100.0 + i for i in range(n)]
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                         "volume": [1.0] * n}, index=idx)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _in_thread(fn, limit: float) -> dict:
    box: dict = {}

    def run():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(limit)
    box["finished"] = not thread.is_alive()
    return box


@pytest.fixture(scope="module")
def real_settings():
    return load_settings(EXAMPLE).plugin


@_needs_isolation
@pytest.mark.parametrize("closer", ["close", "__exit__"])
def test_close_from_other_thread_kills_reaps_and_closes_fds(tmp_path, real_settings,
                                                            closer):
    session = PluginSession(_real_meta(tmp_path, "ok", "def compute(df, params):\n"
                                       "    return {'x': 1.0}\n"), settings=real_settings)
    session.__enter__()
    pid = session.pid
    proc = session._proc
    stdin, stdout = proc.stdin, proc.stdout
    try:
        if closer == "close":
            box = _in_thread(session.close, 15.0)
        else:
            box = _in_thread(lambda: session.__exit__(None, None, None), 15.0)
        assert box["finished"]
        assert "error" not in box, box
        assert stdin.closed and stdout.closed
        assert session._proc is None
        assert session._pidfd is None
        assert _gone(pid)
    finally:
        session.close()


@_needs_isolation
def test_close_from_other_thread_during_a_call_ends_without_deadlock(tmp_path,
                                                                     real_settings):
    settings = real_settings.model_copy(update={"sandbox_timeout_sec": 20.0})
    session = PluginSession(_real_meta(tmp_path, "spin", _SPIN_PLUGIN), settings=settings)
    session.__enter__()
    pid = session.pid
    closer: dict = {}

    def close_later():
        time.sleep(0.5)
        closer.update(_in_thread(session.close, 10.0))

    timer = threading.Thread(target=close_later, daemon=True)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(SandboxError):
            session.call({"df": _df(), "params": {}})
        elapsed = time.monotonic() - started
        timer.join(15.0)
        assert closer.get("finished") is True
        assert "error" not in closer, closer
        # 呼び出しの timeout (20 秒) を待たずに終わる
        assert elapsed < 8.0
        assert session._proc is None
        assert _gone(pid)
    finally:
        session.close()
