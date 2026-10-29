"""call 入力上限 (`MAX_CALL_INPUT_BYTES`) を実 worker で確かめる。

計測対象は pipe に書く request の UTF-8 JSON (末尾改行込み)。超過は pipe に何も
書く前に `call_input_too_large` で call だけを落とし、同じ session は使い続けられる。
"""
from __future__ import annotations

import dataclasses
import json
import secrets

import pytest

from agentic_fx.plugin import sandbox
from agentic_fx.plugin.sandbox import (
    MAX_CALL_INPUT_BYTES, PluginSession, SandboxError, _df_to_wire,
)

from tests.plugin.test_sandbox import (
    EXAMPLE, INDICATOR_OK_PY, RSI_SERIES_PY, STRATEGY_READS_INDICATORS_PY,
    _df, _indicator_dir, _meta, _resolved,
)
from agentic_fx.config import load_settings

REF_FRAME = "JP225@1h"


@pytest.fixture(scope="module")
def plugin_settings():
    return load_settings(EXAMPLE).plugin


@pytest.fixture(autouse=True)
def _isolated_orphan_registry(monkeypatch):
    monkeypatch.setattr(sandbox, "_ORPHANS", [])
    monkeypatch.setattr(sandbox, "_ORPHAN_OVERFLOW_LOGGED", False)


@pytest.fixture
def write_calls(monkeypatch):
    """`_write_line` (pipe への書込み入口) の呼び出し回数。"""
    calls: list[int] = []
    original = PluginSession._write_line

    def spy(self, obj, *, deadline):
        calls.append(1)
        return original(self, obj, deadline=deadline)

    with monkeypatch.context() as m:
        m.setattr(PluginSession, "_write_line", spy)
        yield calls


def _wire_len(df, pad: int, frames: dict | None = None, *, kind_keys=("df", "params")):
    """session が組み立てるのと同じ順序の request の byte 数 (改行込み)。"""
    request: dict = {"df": _df_to_wire(df), "params": {"pad": "x" * pad}}
    if frames is not None:
        request["frames"] = {k: _df_to_wire(v) for k, v in frames.items()}
    request["id"] = secrets.token_hex(8)
    return len(json.dumps(request).encode("utf-8") + b"\n")


def _pad_for(total: int, df, frames: dict | None = None) -> int:
    base = _wire_len(df, 0, frames)
    return total - base


def test_indicator_call_exactly_at_the_limit_runs(tmp_path, plugin_settings, write_calls):
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    df = _df()
    pad = _pad_for(MAX_CALL_INPUT_BYTES, df)
    assert _wire_len(df, pad) == MAX_CALL_INPUT_BYTES
    with PluginSession(meta, settings=plugin_settings) as session:
        started = len(write_calls)
        out = session.call({"df": df, "params": {"pad": "x" * pad}})
        assert len(write_calls) == started + 1
    assert out["mean_close"] == pytest.approx(df["close"].mean())


def test_one_byte_over_the_limit_is_rejected_before_the_pipe_and_session_survives(
        tmp_path, plugin_settings, write_calls):
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    df = _df()
    pad = _pad_for(MAX_CALL_INPUT_BYTES, df) + 1
    assert _wire_len(df, pad) == MAX_CALL_INPUT_BYTES + 1
    with PluginSession(meta, settings=plugin_settings) as session:
        pid = session.pid
        started = len(write_calls)
        with pytest.raises(SandboxError) as err:
            session.call({"df": df, "params": {"pad": "x" * pad}})
        assert err.value.code == "call_input_too_large"
        assert len(write_calls) == started
        assert session.worker_unreaped is False
        out = session.call({"df": df, "params": {}})
        assert out["mean_close"] == pytest.approx(df["close"].mean())
        assert session.pid == pid
        assert len(write_calls) == started + 1


def test_non_ascii_params_are_measured_as_utf8_bytes_not_characters(
        tmp_path, plugin_settings, write_calls):
    meta = _meta(tmp_path, "ind", "indicator", INDICATOR_OK_PY)
    df = _df()
    # json.dumps は既定で非 ASCII を \uXXXX (6 byte) にする。文字数ではなく
    # 実際に書く byte 列で数える。
    chars = (MAX_CALL_INPUT_BYTES - _wire_len(df, 0)) // 6 + 1
    text = "あ" * chars
    request = {"df": _df_to_wire(df), "params": {"pad": text}, "id": "0" * 16}
    assert len(json.dumps(request).encode("utf-8")) + 1 > MAX_CALL_INPUT_BYTES
    assert len(text) < MAX_CALL_INPUT_BYTES
    with PluginSession(meta, settings=plugin_settings) as session:
        started = len(write_calls)
        with pytest.raises(SandboxError) as err:
            session.call({"df": df, "params": {"pad": text}})
        assert err.value.code == "call_input_too_large"
        assert len(write_calls) == started


def _strategy_session_parts(tmp_path, aliases):
    root = tmp_path / "plugins"
    rsi_dir = _indicator_dir(root, "rsi", RSI_SERIES_PY, outputs=("rsi",),
                             params={"period": 14})
    resolved = _resolved(root, *[
        (alias, rsi_dir, ("rsi",), {"period": 14}, 200) for alias in aliases])
    resolved = dataclasses.replace(resolved, items=tuple(
        dataclasses.replace(item, frame_id=REF_FRAME) for item in resolved.items))
    meta = _meta(tmp_path, "strat", "strategy", STRATEGY_READS_INDICATORS_PY,
                 timeframe="1h", pairs=("USDJPY",))
    return meta, resolved


def test_aliases_sharing_one_frame_are_counted_for_one_frame(
        tmp_path, plugin_settings, write_calls):
    meta, resolved = _strategy_session_parts(tmp_path, ["rsi", "rsi_b", "rsi_c"])
    df, ref = _df(40), _df(40)
    one = {REF_FRAME: ref}
    pad = _pad_for(MAX_CALL_INPUT_BYTES, df, one)
    assert _wire_len(df, pad, one) == MAX_CALL_INPUT_BYTES
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as session:
        started = len(write_calls)
        out = session.call({"df": df, "params": {"pad": "x" * pad}, "frames": one})
        assert len(write_calls) == started + 1
        assert "keys=['rsi', 'rsi_b', 'rsi_c']" in out["rationale"]
        # 同じ座標を別の frame id で二重に送れば、その分が余計に計上されて超過する
        two = {REF_FRAME: ref, "JP225@1h#dup": ref}
        before = len(write_calls)
        with pytest.raises(SandboxError) as err:
            session.call({"df": df, "params": {"pad": "x" * pad}, "frames": two})
        assert err.value.code == "call_input_too_large"
        assert len(write_calls) == before
        again = session.call({"df": df, "params": {}, "frames": one})
        assert again["action"] == "hold"


def test_shared_frame_one_byte_over_is_rejected(tmp_path, plugin_settings, write_calls):
    meta, resolved = _strategy_session_parts(tmp_path, ["rsi", "rsi_b"])
    df, ref = _df(40), _df(40)
    one = {REF_FRAME: ref}
    pad = _pad_for(MAX_CALL_INPUT_BYTES, df, one) + 1
    with PluginSession(meta, settings=plugin_settings, resolved=resolved) as session:
        started = len(write_calls)
        with pytest.raises(SandboxError) as err:
            session.call({"df": df, "params": {"pad": "x" * pad}, "frames": one})
        assert err.value.code == "call_input_too_large"
        assert len(write_calls) == started
        out = session.call({"df": df, "params": {}, "frames": one})
        assert out["action"] == "hold"
