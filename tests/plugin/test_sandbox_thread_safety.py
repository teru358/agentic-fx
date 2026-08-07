"""PluginSession の単一スレッド所有 assert (プラン 8 B 束)。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.config import PluginSettings
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import PluginSession, SandboxError


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
    """`__enter__` を別スレッドから呼ぶと RuntimeError (IMPORTANT)。
    owner-thread 防御は __enter__/__exit__/call/close の 4 箇所全て
    に必要 — このテストは __enter__ の検査 (変異テスト対象)。"""
    session = PluginSession(_fake_meta(tmp_path), settings=PluginSettings())
    session._owner_thread = threading.get_ident() + 999999  # 別スレッドを偽装

    with pytest.raises(RuntimeError, match="owner thread"):
        session.__enter__()


def test_close_from_other_thread_raises_runtime_error(tmp_path):
    """`close()` を別スレッドから呼ぶと RuntimeError (IMPORTANT)。
    owner-thread 防御は __enter__/__exit__/call/close の 4 箇所全て
    に必要 — このテストは close() の検査 (変異テスト対象)。"""
    session = PluginSession(_fake_meta(tmp_path), settings=PluginSettings())
    session._owner_thread = threading.get_ident() + 999999  # 別スレッドを偽装

    with pytest.raises(RuntimeError, match="owner thread"):
        session.close()
