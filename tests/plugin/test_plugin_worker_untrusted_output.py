"""load の後に worker から届く行を、候補が制御し得る入力として親が扱うこと。

plugin は worker と同じ process で動くので、protocol 用の fd へ直接書ける。
ここでは実 worker の上で、plugin が protocol fd へ偽の行・壊れた行・余分な行を
書く悪性 plugin を動かし、親だけで安全が成り立つことを確かめる。worker が
残らないことは pid で確かめる。
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pandas as pd
import pytest

from agentic_fx.config import load_settings
from agentic_fx.plugin import sandbox
from agentic_fx.plugin.loader import PluginMeta, content_hash
from agentic_fx.plugin.sandbox import PluginSession, SandboxError

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"

pytestmark = pytest.mark.skipif(sandbox.host_preflight()[0] is not None,
                                reason="this host cannot isolate plugin workers")


@pytest.fixture(scope="module")
def plugin_settings():
    return load_settings(EXAMPLE).plugin


@pytest.fixture(autouse=True)
def _fresh_orphans(monkeypatch):
    monkeypatch.setattr(sandbox, "_ORPHANS", [])
    monkeypatch.setattr(sandbox, "_ORPHAN_OVERFLOW_LOGGED", False)


# protocol fd (worker が退避した元の stdout = pipe) を探して書く。fd 0 は stdin の
# pipe、1 と 2 は stderr の一時ファイルなので、3 以上で最初の FIFO が protocol fd。
_HEAD = textwrap.dedent("""
    import pandas as pd

    _os = pd.io.common.os


    def _pfd():
        for fd in range(3, 64):
            try:
                st = _os.fstat(fd)
            except OSError:
                continue
            if (st.st_mode & 0o170000) == 0o010000:
                return fd
        return -1


    def _say(data):
        _os.write(_pfd(), data)


    def _request_id():
        # worker の main の frame から、いま処理中の要求の id を盗む
        return _os.sys._getframe(2).f_locals["request"]["id"]


    def _forged(result_json, rid):
        return ('{"ok": true, "result": ' + result_json + ', "pid": '
                + str(_os.getpid()) + ', "id": "' + rid + '"}\\n').encode()


    def _pause():
        n = 0
        for _ in range(3000000):
            n += 1
    """)


def _plugin(body: str) -> str:
    return _HEAD + textwrap.dedent(body)


def _meta(base: Path, name: str, plugin_py: str, outputs=("x",)) -> PluginMeta:
    d = base / name
    d.mkdir()
    (d / "plugin.py").write_text(plugin_py)
    (d / "config.yaml").write_text("kind: indicator\n")
    (d / "test_plugin.py").write_text("def test_placeholder():\n    pass\n")
    return PluginMeta(name=name, kind="indicator", path=d, params={}, timeframe=None,
                      pairs=(), max_bars=200, content_hash=content_hash(d),
                      outputs=outputs)


def _df(n: int = 20) -> pd.DataFrame:
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


def _run(meta, settings, calls: int = 1):
    """session を開いて `calls` 回 call し、(結果か例外, pid) を返す。"""
    session = PluginSession(meta, settings=settings)
    outcomes: list = []
    try:
        with session:
            for _ in range(calls):
                try:
                    outcomes.append(session.call({"df": _df(), "params": {}}))
                except SandboxError as exc:
                    outcomes.append(exc)
                    if session._dead:
                        break
    except SandboxError as exc:
        outcomes.append(exc)
    return outcomes, session


# --- (a) 先回りの応答 -------------------------------------------------------

def test_plugin_ready_forged_at_top_level_leaves_a_stray_line_that_fails_the_first_call(
        tmp_path, plugin_settings):
    meta = _meta(tmp_path, "early", _plugin("""
        _say(b'{"phase": "plugin_ready", "ok": true}\\n')

        def compute(df, params):
            return {"x": 1.0}
        """))
    outcomes, session = _run(meta, plugin_settings)
    (error,) = outcomes
    assert isinstance(error, SandboxError) and error.code == "protocol_error"
    assert _gone(session.pid)


def test_valid_looking_response_with_a_wrong_id_is_a_protocol_violation(
        tmp_path, plugin_settings):
    meta = _meta(tmp_path, "wrong_id", _plugin("""
        def compute(df, params):
            _say(_forged('{"x": 7.0}', "0" * 16))
            _pause()
            return {"x": 1.0}
        """))
    outcomes, session = _run(meta, plugin_settings)
    (error,) = outcomes
    assert isinstance(error, SandboxError) and error.code == "protocol_error"
    assert session.parent_kill_sent is True
    assert _gone(session.pid)


def test_forged_response_with_the_right_id_is_only_ever_a_value_the_parent_validated(
        tmp_path, plugin_settings):
    """契約上の残余: 正しい id で親の validator を通る値を書かれると、plugin の通常の
    戻り値と区別できない。採用されるのは validator を通った値だけで、その後に届く
    本物の応答行は次の要求の前に protocol 違反として捕まる。"""
    meta = _meta(tmp_path, "right_id", _plugin("""
        def compute(df, params):
            _say(_forged('{"x": 42.0}', _request_id()))
            _pause()
            return {"x": 1.0}
        """))
    outcomes, session = _run(meta, plugin_settings, calls=2)
    assert outcomes[0] == {"x": 42.0}
    assert isinstance(outcomes[1], SandboxError) and outcomes[1].code == "protocol_error"
    assert _gone(session.pid)


@pytest.mark.parametrize("forged", [
    '{"x": "evil"}', '{"x": 1.0, "y": 2.0}', '{"x": 1e999}', '{"x": {"series": [1.0]}}',
    '{"x": 1' + "0" * 400 + '}'])
def test_forged_response_with_the_right_id_never_bypasses_the_parent_validator(
        tmp_path, plugin_settings, forged):
    meta = _meta(tmp_path, "right_id_bad", _plugin(f"""
        def compute(df, params):
            _say(_forged({forged!r}, _request_id()))
            _pause()
            return {{"x": 1.0}}
        """))
    outcomes, session = _run(meta, plugin_settings)
    (error,) = outcomes
    assert isinstance(error, SandboxError)
    assert error.code in {"backtest_failed", "protocol_error"}
    assert _gone(session.pid)


# --- (b) 壊れた行・パーサを壊す行 ------------------------------------------

@pytest.mark.parametrize("line", [
    pytest.param("b'{\\n'", id="broken"),
    pytest.param("b'{\"ok\": true, \"result\": {\"x\": ' + b'1' * 5000 + b'}}\\n'",
                 id="huge-int"),
    pytest.param("b'[' * 100000 + b'\\n'", id="deep-nesting"),
    pytest.param("b'{\"ok\": true, \"result\": {\"x\": NaN}}\\n'", id="nan"),
    pytest.param("b'{\"ok\": true, \"ok\": false}\\n'", id="duplicate-key"),
    pytest.param("b'x' * (2 * 1024 * 1024)", id="over-1mib"),
])
def test_unparsable_output_is_a_protocol_violation_and_kills_the_worker(
        tmp_path, plugin_settings, line):
    meta = _meta(tmp_path, "broken", _plugin(f"""
        def compute(df, params):
            _say({line})
            _pause()
            return {{"x": 1.0}}
        """))
    outcomes, session = _run(meta, plugin_settings)
    (error,) = outcomes
    assert isinstance(error, SandboxError) and error.code == "protocol_error"
    assert _gone(session.pid)


# --- (c) 余分な行 / (d) 起動応答の偽造 ---------------------------------------

def test_two_lines_for_one_request_is_a_protocol_violation(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "two_lines", _plugin("""
        def compute(df, params):
            rid = _request_id()
            _say(_forged('{"x": 1.0}', rid) + _forged('{"x": 2.0}', rid))
            _pause()
            return {"x": 1.0}
        """))
    outcomes, session = _run(meta, plugin_settings)
    (error,) = outcomes
    assert isinstance(error, SandboxError) and error.code == "protocol_error"
    assert _gone(session.pid)


@pytest.mark.parametrize("forged", [
    '{"phase": "sandbox_ready", "ok": true, "pid": 1}',
    '{"phase": "plugin_ready", "ok": true}'])
def test_startup_messages_forged_after_load_are_protocol_violations(
        tmp_path, plugin_settings, forged):
    meta = _meta(tmp_path, "forge_ready", _plugin(f"""
        def compute(df, params):
            _say({forged!r}.encode() + b"\\n")
            _pause()
            return {{"x": 1.0}}
        """))
    outcomes, session = _run(meta, plugin_settings)
    (error,) = outcomes
    assert isinstance(error, SandboxError) and error.code == "protocol_error"
    assert _gone(session.pid)


# --- (e) 書いた直後に死ぬ・回り続ける -----------------------------------------

def test_response_written_right_before_a_sigsys_death_is_not_used(tmp_path, plugin_settings):
    meta = _meta(tmp_path, "die", _plugin("""
        def compute(df, params):
            _say(_forged('{"x": 9.0}', _request_id()))
            _os.getcwd()
        """))
    outcomes, session = _run(meta, plugin_settings, calls=1)
    (outcome,) = outcomes
    # 死を観測する前に届いた行は、親の validator を通った値としてだけ採用され得る
    # (上の残余と同じ)。死を観測した後なら捨てて crashed にする
    assert outcome == {"x": 9.0} or (
        isinstance(outcome, SandboxError) and outcome.code in {"crashed", "protocol_error"})
    assert _gone(session.pid)


def test_forged_line_then_spinning_is_killed_as_a_protocol_violation(tmp_path,
                                                                     plugin_settings):
    meta = _meta(tmp_path, "spin", _plugin("""
        def compute(df, params):
            _say(_forged('{"x": 9.0}', "f" * 16))
            n = 0
            while True:
                n += 1
        """))
    outcomes, session = _run(meta, plugin_settings)
    (error,) = outcomes
    assert isinstance(error, SandboxError) and error.code == "protocol_error"
    assert session.parent_kill_sent is True
    assert _gone(session.pid)


# --- 親だけで成り立つ規則 (純関数) ------------------------------------------

@pytest.mark.parametrize("payload", [
    b"{", b"1" * 5000, b"[" * 100000, b'{"x": NaN}', b'{"x": Infinity}',
    b'{"x": -Infinity}', b'{"a": 1, "a": 2}', b"\xff\xfe"])
def test_parse_worker_line_turns_every_parser_failure_into_a_protocol_violation(payload):
    with pytest.raises(sandbox._ProtocolViolation):
        sandbox.parse_worker_line(payload)


def test_untrusted_text_strips_control_characters_and_is_bounded():
    text = sandbox.untrusted_text("a\nb\x1b[31mc‮d" + "z" * 1000)
    assert "\n" not in text and "\x1b" not in text and "‮" not in text
    assert text.startswith("ab[31mcd")
    assert len(text) <= sandbox.UNTRUSTED_TEXT_MAX_CHARS + len("...(truncated)")
    assert sandbox.untrusted_text(None) == "unknown error"
    assert sandbox.untrusted_text({"x": 1}, fallback="fb") == "fb"


def test_worker_error_text_reaches_the_exception_only_through_untrusted_text(
        tmp_path, plugin_settings):
    meta = _meta(tmp_path, "noisy", textwrap.dedent("""
        def compute(df, params):
            raise ValueError("line1\\nline2\\x1b[2J" + "y" * 5000)
        """))
    outcomes, session = _run(meta, plugin_settings)
    (error,) = outcomes
    assert error.code == "plugin_error"
    assert "\n" not in str(error) and "\x1b" not in str(error)
    assert len(str(error)) <= sandbox.UNTRUSTED_TEXT_MAX_CHARS + len("...(truncated)")


@pytest.mark.parametrize("result,expected_len", [({"x": {"series": [1.0, 2.0]}}, 3)])
def test_indicator_series_length_is_checked_against_the_parent_df(result, expected_len):
    index = pd.date_range("2026-01-05", periods=expected_len, freq="1h", tz="UTC")
    with pytest.raises(SandboxError):
        sandbox._validate_indicator_result(result, outputs=("x",), df_index=index)


@pytest.mark.parametrize("result", [
    {"x": 10 ** 400}, {"x": {"series": [10 ** 400]}}])
def test_indicator_values_that_overflow_float_are_sandbox_errors(result):
    with pytest.raises(SandboxError):
        sandbox._validate_indicator_result(result, outputs=("x",))


@pytest.mark.parametrize("item", [
    {"direction": "long", "strength": 10 ** 400, "rationale": "r"},
    {"direction": "long", "strength": 0.5, "rationale": "r", "stop_loss": 10 ** 400}])
def test_signal_values_that_overflow_float_are_sandbox_errors(item):
    with pytest.raises(SandboxError):
        sandbox._validate_signal_result([item])


def test_strategy_values_that_overflow_float_are_sandbox_errors():
    with pytest.raises(SandboxError):
        sandbox._validate_strategy_result({
            "action": "open", "rationale": "r", "direction": "long",
            "entry_type": "market", "stop_loss": 10 ** 400})


def test_close_never_raises_and_still_reaps_when_the_graceful_path_breaks(
        tmp_path, plugin_settings, monkeypatch):
    meta = _meta(tmp_path, "close", "def compute(df, params):\n    return {'x': 1.0}\n")
    session = PluginSession(meta, settings=plugin_settings)
    session.__enter__()
    pid = session.pid

    def broken(*_a, **_k):
        raise RecursionError("parser blew up")

    monkeypatch.setattr(session, "_read_response", broken)
    session.close()
    assert session._proc is None
    assert _gone(pid)


# --- 申告 pid の照合と、応答の後の死 ------------------------------------------------

@pytest.mark.parametrize("pid_text", [
    pytest.param("str(_os.getpid() + 1)", id="other-pid"),
    pytest.param("str(_os.getpid()) + '.0'", id="float-pid"),
    pytest.param("'\"' + str(_os.getpid()) + '\"'", id="string-pid"),
])
def test_forged_response_with_the_right_id_but_a_wrong_pid_is_a_protocol_violation(
        tmp_path, plugin_settings, pid_text):
    meta = _meta(tmp_path, "wrong_pid", _plugin(f"""
        def compute(df, params):
            _say(('{{"ok": true, "result": {{"x": 42.0}}, "pid": ' + {pid_text}
                  + ', "id": "' + _request_id() + '"}}\\n').encode())
            _pause()
            return {{"x": 1.0}}
        """))
    outcomes, session = _run(meta, plugin_settings)
    (error,) = outcomes
    assert isinstance(error, SandboxError) and error.code == "protocol_error"
    assert _gone(session.pid)


def test_response_of_a_worker_that_died_right_after_it_is_not_used(
        tmp_path, plugin_settings, monkeypatch):
    meta = _meta(tmp_path, "die_after", _plugin("""
        def compute(df, params):
            _say(_forged('{"x": 9.0}', _request_id()))
            _os.getcwd()
        """))
    session = PluginSession(meta, settings=plugin_settings)
    with pytest.raises(SandboxError) as error:
        with session:
            real_read = session._read_response

            # 応答を読み終えた時点で死が見える順序に固定する
            def read_then_wait_for_the_death(timeout_sec, max_bytes):
                response = real_read(timeout_sec, max_bytes)
                os.waitid(os.P_PID, session.pid, os.WEXITED | os.WNOWAIT)
                return response

            monkeypatch.setattr(session, "_read_response", read_then_wait_for_the_death)
            session.call({"df": _df(), "params": {}})
    assert (error.value.code, error.value.sandbox_reason) == ("crashed", "sigsys_unattributed")
    assert _gone(session.pid)


def test_untrusted_text_strips_unicode_line_and_paragraph_separators():
    assert sandbox.untrusted_text("a b c") == "abc"
