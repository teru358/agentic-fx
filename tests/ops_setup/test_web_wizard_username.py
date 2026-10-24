"""Basic 認証のユーザー名の入力: 空白を取り除かない入力経路と、選び直しでの既定値の維持。"""
from __future__ import annotations

import getpass

import pytest

from agentic_fx.ops_setup.interactive import ConsoleIO
from agentic_fx.ops_setup.web_setup import WebSetupError
from agentic_fx.ops_setup.web_wizard import USER_LABEL, Session
from web_support import ScriptIO


def _session(tmp_path, io) -> Session:
    return Session(tmp_path, io, probe=lambda: (0, ""), candidates=lambda: ())


def _typed(monkeypatch, lines, secrets=()):
    """実 ConsoleIO の `input` / `getpass` に行を順に渡す。"""
    queue, passwords, shown = list(lines), list(secrets), []

    def fake_input(prompt=""):
        shown.append(prompt)
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": passwords.pop(0))
    return shown


@pytest.mark.parametrize("typed", [" afx ", "   ", "afx ", " afx", "\tafx", "a fx"])
def test_a_username_with_whitespace_through_the_real_console_is_a_grammar_violation(tmp_path, monkeypatch, typed):
    _typed(monkeypatch, [typed, typed, typed])
    with pytest.raises(WebSetupError) as raised:
        _session(tmp_path, ConsoleIO())._ask_credentials("afx")
    assert raised.value.reason == "setup_aborted"


def test_whitespace_answers_count_toward_the_same_three_failures_as_the_other_violations(tmp_path, monkeypatch):
    shown = _typed(monkeypatch, [" afx ", "   ", "ops"], secrets=["", ])
    user, plan = _session(tmp_path, ConsoleIO())._ask_credentials("afx")
    assert user == "ops" and plan.generated
    assert len(shown) == 3

    _typed(monkeypatch, [" afx ", "afx"], secrets=["short", "short", ""])
    # 2 回の違反 (username 1 回 + password 1 回) のあと、確認の不一致でちょうど 3 回目に打ち切る
    with pytest.raises(WebSetupError) as raised:
        _session(tmp_path, ConsoleIO())._ask_credentials("afx")
    assert raised.value.reason == "setup_aborted"


def test_an_empty_line_takes_the_default_user_and_a_plain_name_is_kept_as_typed(tmp_path, monkeypatch):
    shown = _typed(monkeypatch, ["", "alice"], secrets=["", ""])
    session = _session(tmp_path, ConsoleIO())
    assert session._ask_credentials("bob")[0] == "bob"
    assert session._ask_credentials("bob")[0] == "alice"
    assert shown[0] == f"{USER_LABEL} [bob]: "


def test_choosing_none_and_going_back_keeps_the_existing_username_as_the_basic_default(tmp_path):
    io = ScriptIO(answers=["4", "yes", "2", "2", "2", ""], secret_answers=[""])
    cur = {"port": 8788, "bind": "192.168.1.10", "allow": ["0.0.0.0/0"], "auth": "none", "user": "alice"}
    plan = _session(tmp_path, io)._ask_who(cur, "safe")
    assert plan.generated and cur["auth"] == "basic" and cur["user"] == "alice"
    assert (USER_LABEL, "alice") in io.defaults


def test_choosing_none_for_good_drops_the_username_from_the_target(tmp_path):
    io = ScriptIO(answers=["4", "yes", "1"])
    cur = {"port": 8788, "bind": "192.168.1.10", "allow": ["10.0.0.0/8"], "auth": "basic", "user": "alice"}
    plan = _session(tmp_path, io)._ask_who(cur, "safe")
    assert plan.action == "remove" and cur["auth"] == "none" and "user" not in cur
