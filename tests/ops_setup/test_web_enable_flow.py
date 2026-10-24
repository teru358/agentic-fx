"""`afx ops web enable` のウィザード: 入口表 (stage × TTY × flag)、質問、前処理、probe の再質問。"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from agentic_fx.ops_setup.feature_config import (InterfaceLookupFailed, collect, web_access_reason,
                                                 web_secret_state, write_inputs)
from agentic_fx.ops_setup.interactive import interactive_enable
from agentic_fx.ops_setup.setup_lease import acquire_setup_lease
from agentic_fx.ops_setup.stages import InvalidState
from agentic_fx.ops_setup.web_setup import SecretPlan, WebOptions, apply_target
from web_support import (ACCESS_Q, BIND_EXPLANATION, CANDIDATES, CONFIRM_Q, IFNAME, KEEP_BASIC_Q, KEEP_Q, LAN, LAN2,
                         ONE_CANDIDATE, PASSWORD_Q, SCOPE_Q, USER_Q, WARNING, ScriptIO, StubSetup, stage_file, tree)

PW = "AAECAwQFBgcICQoLDA0ODw"  # bytes(range(16)) の base64url (padding なし)
FIXED_RNG = lambda n: bytes(range(n))  # noqa: E731
BIND_Q = f"待受アドレス [1] {LAN} ({IFNAME})"
BIND_Q3 = "待受アドレス [1] 192.168.1.10 (enp6s0) [2] 192.168.1.11 (enp6s0) [3] 169.254.3.4 (wlp3s0)"
ALLOW_Q = "許可する IPv4 / CIDR (カンマ区切り)"
UNRESTRICTED_Q = "この設定で公開してよい場合は yes と入力してください"
REMOVE_Q = "既存の password が残っています。認証なしにして削除します。 [1] 削除して認証なしにする [2] 認証方式を選び直す"


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    return tmp_path / "c" / "agentic-fx" / "ops" / "web"


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "instance"
    path.mkdir()
    return path


class Run:
    def __init__(self, result, setup, probes, io):
        self.result, self.setup, self.probes, self.io = result, setup, probes, io

    @property
    def rc(self) -> int:
        return self.result.rc

    @property
    def reason(self) -> str:
        return self.result.reason_code


def run_enable(root, io, options=None, *, setup=None, probe=None, candidates=CANDIDATES, hook=None, lease=None):
    setup = setup or StubSetup()
    probes: list[str] = []

    def probe_runner(feature):
        probes.append(feature)
        return probe(len(probes)) if probe is not None else (0, "")

    def lister():
        if isinstance(candidates, Exception):
            raise candidates
        return candidates

    result = interactive_enable(root, "web", io=io, probe_runner=probe_runner, setup_factory=lambda _r: setup,
                                options=options or WebOptions(), candidates=lister, hook=hook,
                                token_bytes=FIXED_RNG, lease=lease)
    return Run(result, setup, probes, io)


def live(root):
    return collect(root, "web").values


def password_file(xdg: Path) -> Path:
    return xdg / "secret" / "web.password"


def config_text(xdg: Path) -> str:
    return (xdg / "config" / "ops.toml").read_text()


# --- 入口表: stage × TTY × flag -------------------------------------------------------------

STAGES = {"none": None, "disabled": ("disable", "disabled"), "enable-unfinished": ("enable", "staged"),
          "disable-unfinished": ("disable", "stopping-ops"), "enabled": ("enable", "enabled")}
FRESH = ("none", "disabled")
SUMMARY = "web: 待受 127.0.0.1:8788 / 公開 自機のみ / 認証 none"


def _arrange(root, xdg, stage):
    write_inputs(root, "web", {"port": 8788})
    if STAGES[stage] is not None:
        stage_file(root, *STAGES[stage])
    return tree(xdg), (root / "data" / "ops" / "web.stage.json").read_bytes() if STAGES[stage] else None


@pytest.mark.parametrize("stage", FRESH)
def test_fresh_tty_without_flag_shows_the_summary_and_one_question_then_goes_on(root, xdg, stage):
    before, _ = _arrange(root, xdg, stage)
    io = ScriptIO(answers=[""])
    run = run_enable(root, io)
    assert run.rc == 0 and run.setup.enabled == ["web"] and run.probes == ["web"]
    assert io.asked == [KEEP_Q]
    assert io.err == [SUMMARY]
    assert io.out == ["web: enabled"]
    assert tree(xdg) == before


@pytest.mark.parametrize("answer", ["", "1"])
def test_the_one_question_default_and_one_both_mean_keep(root, xdg, answer):
    _arrange(root, xdg, "none")
    io = ScriptIO(answers=[answer])
    assert run_enable(root, io).rc == 0
    assert io.asked == [KEEP_Q]


def test_the_one_question_changes_into_the_full_question_set_with_existing_values_as_defaults(root, xdg):
    write_inputs(root, "web", {"port": 9000})
    io = ScriptIO(answers=["2", "", ""])
    run = run_enable(root, io)
    assert run.rc == 0
    assert io.asked == [KEEP_Q, "port", SCOPE_Q]
    assert io.defaults[1] == ("port", "9000")
    assert io.err[0] == "web: 待受 127.0.0.1:9000 / 公開 自機のみ / 認証 none"


def test_an_unrecognised_answer_to_the_one_question_asks_again(root, xdg):
    _arrange(root, xdg, "none")
    io = ScriptIO(answers=["x", ""])
    assert run_enable(root, io).rc == 0
    assert io.asked == [KEEP_Q, KEEP_Q] and io.err.count("afx ops: setup_input_invalid") == 1


@pytest.mark.parametrize("stage", FRESH)
def test_eof_at_the_one_question_is_aborted_and_changes_nothing(root, xdg, stage):
    before, _ = _arrange(root, xdg, stage)
    io = ScriptIO(eof_after=0)
    run = run_enable(root, io)
    assert (run.rc, run.reason) == (1, "setup_aborted")
    assert run.setup.enabled == [] and run.probes == []
    assert tree(xdg) == before


@pytest.mark.parametrize("stage", FRESH)
def test_fresh_without_tty_and_without_flag_runs_on_a_complete_config_with_only_the_summary(root, xdg, stage):
    before, _ = _arrange(root, xdg, stage)
    io = ScriptIO(tty=False)
    run = run_enable(root, io)
    assert run.rc == 0 and run.setup.enabled == ["web"] and run.probes == ["web"]
    assert io.asked == [] and io.err == [SUMMARY] and io.out == ["web: enabled"]
    assert tree(xdg) == before


@pytest.mark.parametrize("stage", FRESH)
def test_fresh_without_tty_and_without_flag_needs_input_when_the_config_is_incomplete(root, xdg, stage):
    if STAGES[stage]:
        stage_file(root, *STAGES[stage])
    io = ScriptIO(tty=False)
    run = run_enable(root, io)
    assert (run.rc, run.reason) == (2, "setup_input_required")
    assert io.err == ["afx ops: setup_input_required"]
    assert run.setup.enabled == [] and run.probes == [] and not xdg.exists()


def test_the_summary_is_stderr_only_and_never_starts_with_the_stdout_contract_word_on_stdout(root, xdg):
    _arrange(root, xdg, "none")
    io = ScriptIO(answers=[""])
    run_enable(root, io)
    assert all(not line.startswith("web:") or line == "web: enabled" for line in io.out)
    assert not KEEP_Q.startswith("web:")


@pytest.mark.parametrize("stage", FRESH)
def test_ask_in_a_tty_asks_everything_with_the_existing_values_as_defaults(root, xdg, stage):
    _arrange(root, xdg, stage)
    io = ScriptIO(answers=["", ""])
    run = run_enable(root, io, WebOptions(ask=True))
    assert run.rc == 0 and run.setup.enabled == ["web"]
    assert io.asked == ["port", SCOPE_Q]
    assert io.defaults[0] == ("port", "8788")
    assert io.err == []


@pytest.mark.parametrize("stage", FRESH)
def test_ask_without_a_tty_is_input_required_and_changes_nothing(root, xdg, stage):
    before, _ = _arrange(root, xdg, stage)
    io = ScriptIO(tty=False)
    run = run_enable(root, io, WebOptions(ask=True))
    assert (run.rc, run.reason) == (2, "setup_input_required")
    assert run.setup.enabled == [] and run.probes == [] and tree(xdg) == before


@pytest.mark.parametrize("tty", [True, False])
@pytest.mark.parametrize("stage", FRESH)
def test_non_interactive_ignores_the_tty_and_asks_nothing(root, xdg, tmp_path, stage, tty):
    _arrange(root, xdg, stage)
    target = tmp_path / "t.toml"
    target.write_text("port = 9000\n")
    target.chmod(0o600)
    io = ScriptIO(tty=tty)
    run = run_enable(root, io, WebOptions(non_interactive=True, config_file=str(target), secret_action="absent"))
    assert run.rc == 0 and run.setup.enabled == ["web"] and io.asked == []
    assert io.err == ["web: 待受 127.0.0.1:9000 / 公開 自機のみ / 認証 none"]
    assert live(root) == {"port": 9000}


@pytest.mark.parametrize("tty", [True, False])
@pytest.mark.parametrize("flags", [None, WebOptions(ask=True)])
def test_an_unfinished_enable_resumes_only_without_flags_and_writes_nothing(root, xdg, tty, flags):
    before, stage = _arrange(root, xdg, "enable-unfinished")
    io = ScriptIO(tty=tty)
    run = run_enable(root, io, flags)
    stage_path = root / "data" / "ops" / "web.stage.json"
    if flags is None:
        assert run.rc == 0 and run.setup.enabled == ["web"] and run.probes == [] and io.asked == []
    else:
        assert (run.rc, run.reason) == (1, "invalid_state") and run.setup.enabled == []
    assert tree(xdg) == before and stage_path.read_bytes() == stage


def test_an_unfinished_enable_with_non_interactive_is_invalid_state(root, xdg, tmp_path):
    before, stage = _arrange(root, xdg, "enable-unfinished")
    target = tmp_path / "t.toml"
    target.write_text("port = 9000\n")
    target.chmod(0o600)
    run = run_enable(root, ScriptIO(), WebOptions(non_interactive=True, config_file=str(target),
                                                  secret_action="absent"))
    assert (run.rc, run.reason) == (1, "invalid_state") and run.setup.enabled == []
    assert tree(xdg) == before


@pytest.mark.parametrize("tty", [True, False])
@pytest.mark.parametrize("flags", [None, WebOptions(ask=True)])
def test_an_unfinished_disable_is_invalid_state_for_every_combination(root, xdg, tty, flags):
    before, stage = _arrange(root, xdg, "disable-unfinished")
    run = run_enable(root, ScriptIO(tty=tty), flags)
    assert (run.rc, run.reason) == (1, "invalid_state")
    assert run.setup.enabled == [] and run.probes == [] and tree(xdg) == before


@pytest.mark.parametrize("tty", [True, False])
def test_enabled_without_flags_goes_straight_to_the_existing_verification(root, xdg, tty):
    before, stage = _arrange(root, xdg, "enabled")
    io = ScriptIO(tty=tty)
    run = run_enable(root, io)
    assert run.rc == 0 and run.setup.enabled == ["web"]
    assert run.probes == [] and io.asked == [] and io.err == []
    assert tree(xdg) == before


@pytest.mark.parametrize("tty", [True, False])
@pytest.mark.parametrize("flags", ["ask", "non-interactive"])
def test_enabled_with_ask_or_non_interactive_is_already_enabled_with_the_reconfigure_guidance(
        root, xdg, tmp_path, tty, flags):
    before, stage = _arrange(root, xdg, "enabled")
    target = tmp_path / "t.toml"
    target.write_text("port = 9000\n")
    target.chmod(0o600)
    options = (WebOptions(ask=True) if flags == "ask" else
               WebOptions(non_interactive=True, config_file=str(target), secret_action="absent"))
    io = ScriptIO(tty=tty)
    run = run_enable(root, io, options)
    assert (run.rc, run.reason) == (1, "already_enabled")
    assert io.err == ["afx ops: already_enabled (設定の変更は afx ops web reconfigure)"]
    assert run.setup.enabled == [] and run.probes == [] and io.asked == []
    assert tree(xdg) == before
    assert (root / "data" / "ops" / "web.stage.json").read_bytes() == stage


@pytest.mark.parametrize("options", [
    WebOptions(ask=True, non_interactive=True, config_file="x", secret_action="keep"),
    WebOptions(config_file="x"), WebOptions(non_interactive=True), WebOptions(duplicated=True),
])
@pytest.mark.parametrize("stage", list(STAGES))
def test_an_option_outside_the_table_is_input_required_before_any_stage_decision(root, xdg, stage, options):
    before, _ = _arrange(root, xdg, stage)
    io = ScriptIO()
    run = run_enable(root, io, options)
    assert (run.rc, run.reason) == (2, "setup_input_required")
    assert io.err == ["afx ops: setup_input_required"] and tree(xdg) == before
    assert run.setup.enabled == [] and run.probes == []


# --- 要約: 公開 3 種 × 認証 2 種 -------------------------------------------------------------------

@pytest.mark.parametrize("values, line", [
    ({"port": 8788, "auth": "none"}, "web: 待受 127.0.0.1:8788 / 公開 自機のみ / 認証 none"),
    ({"port": 8788, "bind": LAN, "allow": ["10.0.0.1", "192.168.1.0/24"], "auth": "none"},
     f"web: 待受 {LAN}:8788 / 公開 許可範囲 2 件 / 認証 none"),
    ({"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"], "auth": "none"},
     f"web: 待受 {LAN}:8788 / 公開 到達できる全 IPv4 / 認証 none"),
    ({"port": 8788, "auth": "basic"}, "web: 待受 127.0.0.1:8788 / 公開 自機のみ / 認証 basic (afx)"),
    ({"port": 8788, "bind": "127.0.0.1", "allow": ["0.0.0.0/0"], "auth": "basic"},
     "web: 待受 127.0.0.1:8788 / 公開 自機のみ / 認証 basic (afx)"),
    ({"port": 8788, "bind": LAN, "allow": ["10.0.0.1", "192.168.1.0/24", "172.16.0.0/12"], "auth": "basic"},
     f"web: 待受 {LAN}:8788 / 公開 許可範囲 3 件 / 認証 basic (afx)"),
    ({"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"], "auth": "basic"},
     f"web: 待受 {LAN}:8788 / 公開 到達できる全 IPv4 / 認証 basic (afx)"),
])
def test_the_summary_line_for_every_existing_shape_goes_to_stderr_without_secrets(root, xdg, values, line):
    apply_target(root, values, SecretPlan("install", "Z" * 22) if values.get("auth") == "basic"
                 else SecretPlan("absent"))
    io = ScriptIO(tty=False)
    run = run_enable(root, io)
    assert run.rc == 0
    assert io.err == [line] and io.out == ["web: enabled"]
    assert "Z" * 22 not in io.text() and "10.0.0.1" not in io.text()


# --- 質問の順と書かれる設定 --------------------------------------------------------------------------

def test_local_only_writes_just_the_port(root, xdg):
    io = ScriptIO(answers=["9000", ""])
    run = run_enable(root, io)
    assert run.rc == 0
    assert io.asked == ["port", SCOPE_Q]
    assert config_text(xdg).splitlines()[2:] == ["port = 9000"]
    assert web_secret_state(root) == "absent"


def test_lan_with_an_allow_list_writes_all_four_keys_and_asks_no_secret_question(root, xdg):
    io = ScriptIO(answers=["9000", "2", "", "", "192.168.1.0/24, 10.0.0.5"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0
    assert io.asked == ["port", SCOPE_Q, BIND_Q, ACCESS_Q, ALLOW_Q]
    assert io.out[0] == BIND_EXPLANATION
    assert io.defaults[:4] == [("port", "8788"), (SCOPE_Q, "1"), (BIND_Q, "1"), (ACCESS_Q, "1")]
    assert live(root) == {"port": 9000, "bind": LAN, "allow": ["192.168.1.0/24", "10.0.0.5"], "auth": "none"}
    assert web_secret_state(root) == "absent"


def test_lan_with_password_only_generates_and_shows_the_password_exactly_once(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "2", ""], secret_answers=[""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0
    assert io.asked == ["port", SCOPE_Q, BIND_Q, ACCESS_Q, USER_Q]
    assert io.of("secret") == [PASSWORD_Q]
    assert io.defaults[-1] == (USER_Q, "afx")
    assert live(root) == {"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"], "auth": "basic", "user": "afx"}
    assert password_file(xdg).read_text() == PW and stat.S_IMODE(password_file(xdg).stat().st_mode) == 0o600
    shown = [text for text in io.out if PW in text]
    assert len(shown) == 1 and shown[0] == f"ユーザー名: afx / 生成した password (この表示は 1 回だけです): {PW}"
    assert PW not in "\n".join(io.err) and PW not in config_text(xdg)


def test_lan_with_both_asks_for_an_allow_list_and_a_password(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "3", "10.0.0.0/8", ""], secret_answers=[""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0
    assert live(root) == {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic", "user": "afx"}
    assert password_file(xdg).read_text() == PW


def test_a_typed_password_is_stored_and_never_shown(root, xdg):
    typed = "correct-horse-battery"
    io = ScriptIO(answers=["8788", "2", "", "2", ""], secret_answers=[typed, typed])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and password_file(xdg).read_text() == typed
    assert typed not in io.text()
    assert io.of("secret") == [PASSWORD_Q, CONFIRM_Q]


def test_an_invalid_typed_password_is_asked_again_without_echoing_it(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "2", ""],
                  secret_answers=["short", "x" * 33, "ok-password-123456", "ok-password-123456"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and password_file(xdg).read_text() == "ok-password-123456"
    assert io.err.count("afx ops: setup_input_invalid") == 2
    assert "short" not in io.text() and "x" * 33 not in io.text()


def test_unrestricted_prints_the_fixed_warning_then_needs_an_explicit_yes(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "4", "yes"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0
    assert io.asked == ["port", SCOPE_Q, BIND_Q, ACCESS_Q, UNRESTRICTED_Q]
    assert io.out[:2] == [BIND_EXPLANATION, WARNING]
    assert live(root) == {"port": 8788, "bind": LAN, "allow": ["0.0.0.0/0"], "auth": "none"}


@pytest.mark.parametrize("answer", ["no", "y", "YES", " ", "はい"])
def test_anything_but_yes_does_not_write_unrestricted_and_returns_to_the_question(root, xdg, answer):
    io = ScriptIO(answers=["8788", "2", "", "4", answer, "1", "10.0.0.0/8"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0
    assert live(root)["allow"] == ["10.0.0.0/8"] and live(root)["auth"] == "none"
    assert io.asked.count(UNRESTRICTED_Q) == 1 and io.asked.count(ACCESS_Q) == 2


@pytest.mark.parametrize("eof_after", [3, 4])
def test_eof_at_the_unrestricted_confirmation_writes_nothing_and_is_aborted(root, xdg, eof_after):
    io = ScriptIO(answers=["8788", "2", "", "4"], eof_after=eof_after)
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert (run.rc, run.reason) == (1, "setup_aborted")
    assert not (xdg / "config" / "ops.toml").exists() and run.probes == []


def test_the_unrestricted_warning_is_not_shown_for_the_other_choices(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "1", "10.0.0.0/8"])
    run_enable(root, io, candidates=ONE_CANDIDATE)
    assert WARNING not in io.text()


def test_an_empty_allow_answer_is_asked_again_and_never_means_unrestricted(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "1", "", "  ,  ", "10.0.0.0/8"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and live(root)["allow"] == ["10.0.0.0/8"]
    assert io.asked.count(ALLOW_Q) == 3 and io.err.count("afx ops: setup_input_invalid") == 2


@pytest.mark.parametrize("bad", ["0.0.0.0/0", "10.0.0.1/8", "fe80::1", "10.0.0.0/8, 10.0.0.0/8", "host.example",
                                 "10.0.0.0/8, 0.0.0.0/0", "010.0.0.1"])
def test_a_malformed_allow_answer_is_asked_again(root, xdg, bad):
    io = ScriptIO(answers=["8788", "2", "", "3", bad, "10.0.0.0/8", ""], secret_answers=[""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and live(root)["allow"] == ["10.0.0.0/8"]
    assert io.err.count("afx ops: setup_input_invalid") == 1


def test_several_candidates_force_an_explicit_choice_in_the_enumerated_order(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "7", "2", "1", "10.0.0.0/8"])
    run = run_enable(root, io, candidates=CANDIDATES)
    assert run.rc == 0 and live(root)["bind"] == LAN2
    assert io.asked.count(BIND_Q3) == 3
    assert io.err.count("afx ops: setup_input_invalid") == 2  # 空回答と範囲外


def test_a_link_scope_candidate_can_be_chosen(root, xdg):
    io = ScriptIO(answers=["8788", "2", "3", "1", "10.0.0.0/8"])
    run = run_enable(root, io, candidates=CANDIDATES)
    assert run.rc == 0 and live(root)["bind"] == "169.254.3.4"


def test_no_candidate_stops_with_bind_not_local_and_never_asks_for_an_address(root, xdg):
    io = ScriptIO(answers=["8788", "2"])
    run = run_enable(root, io, candidates=())
    assert (run.rc, run.reason) == (1, "bind_not_local")
    assert io.asked == ["port", SCOPE_Q] and run.probes == [] and not (xdg / "config" / "ops.toml").exists()


def test_an_enumeration_failure_stops_with_the_fixed_reason_and_no_fallback(root, xdg):
    io = ScriptIO(answers=["8788", "2"])
    run = run_enable(root, io, candidates=InterfaceLookupFailed())
    assert (run.rc, run.reason) == (1, "interface_lookup_failed")
    assert io.asked == ["port", SCOPE_Q] and run.probes == [] and not (xdg / "config" / "ops.toml").exists()


# --- 既存の password の扱い -----------------------------------------------------------------------

def _basic_live(root, allow=("10.0.0.0/8",)):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": list(allow), "auth": "basic"},
                 SecretPlan("install", "E" * 22))


def test_choosing_basic_with_a_safe_existing_password_asks_keep_or_reset_right_after_the_access_choice(root, xdg):
    _basic_live(root)
    io = ScriptIO(answers=["2", "8788", "2", "", "3", "1", "10.0.0.0/8"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0
    assert io.asked == [KEEP_Q, "port", SCOPE_Q, BIND_Q, ACCESS_Q, KEEP_BASIC_Q, ALLOW_Q]
    assert io.of("secret") == []
    assert password_file(xdg).read_text() == "E" * 22 and live(root)["user"] == "afx"
    assert not [text for text in io.out if "password" in text]  # 維持は表示しない


def test_keeping_the_basic_authentication_keeps_the_existing_user_and_never_asks_for_one(root, xdg):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic", "user": "ops-web"},
                 SecretPlan("install", "E" * 22))
    io = ScriptIO(answers=["2", "8788", "2", "", "2", "1"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and USER_Q not in io.asked and io.of("secret") == []
    assert live(root)["user"] == "ops-web" and password_file(xdg).read_text() == "E" * 22


def test_the_keep_or_reset_question_has_no_default_and_an_empty_answer_asks_again(root, xdg):
    _basic_live(root)
    io = ScriptIO(answers=["2", "8788", "2", "", "2", "", "1"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and io.asked.count(KEEP_BASIC_Q) == 2
    assert (KEEP_BASIC_Q, None) in io.defaults and io.err.count("afx ops: setup_input_invalid") == 1


def test_resetting_the_basic_authentication_asks_the_user_with_the_existing_one_as_the_default(root, xdg):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic", "user": "ops-web"},
                 SecretPlan("install", "E" * 22))
    io = ScriptIO(answers=["2", "8788", "2", "", "2", "2", "newuser"],
                  secret_answers=["fresh-password-1", "fresh-password-1"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and (USER_Q, "ops-web") in io.defaults
    assert live(root)["user"] == "newuser" and password_file(xdg).read_text() == "fresh-password-1"


def test_regenerating_over_an_existing_password_shows_the_new_one_once(root, xdg):
    _basic_live(root)
    io = ScriptIO(answers=["2", "8788", "2", "", "3", "2", "10.0.0.0/8", ""], secret_answers=[""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and password_file(xdg).read_text() == PW
    assert len([text for text in io.out if PW in text]) == 1


def test_choosing_no_auth_over_a_leftover_password_requires_an_explicit_remove(root, xdg):
    _basic_live(root)
    io = ScriptIO(answers=["2", "8788", "2", "", "1", "10.0.0.0/8", "1"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and io.asked[-1] == REMOVE_Q
    assert not password_file(xdg).exists() and live(root)["auth"] == "none"


def test_an_empty_answer_to_the_remove_question_is_not_a_removal(root, xdg):
    _basic_live(root)
    io = ScriptIO(answers=["2", "8788", "2", "", "1", "10.0.0.0/8", "", "1"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0
    assert io.asked.count(REMOVE_Q) == 2 and io.err.count("afx ops: setup_input_invalid") == 1


def test_reselecting_the_auth_instead_of_removing_keeps_the_password(root, xdg):
    _basic_live(root)
    io = ScriptIO(answers=["2", "8788", "2", "", "1", "10.0.0.0/8", "2", "3", "1", "10.0.0.0/8"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and password_file(xdg).read_text() == "E" * 22 and live(root)["auth"] == "basic"


@pytest.mark.parametrize("eof_after", range(0, 7))
def test_eof_anywhere_in_the_leftover_password_dialogue_removes_nothing(root, xdg, eof_after):
    _basic_live(root)
    before = tree(xdg)
    io = ScriptIO(answers=["2", "8788", "2", "", "1", "10.0.0.0/8", "1"], eof_after=eof_after)
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert (run.rc, run.reason) == (1, "setup_aborted")
    assert tree(xdg) == before and run.probes == []


def test_local_only_over_a_leftover_password_also_needs_the_explicit_remove(root, xdg):
    apply_target(root, {"port": 8788}, SecretPlan("install", "E" * 22))
    io = ScriptIO(answers=["", "1"])
    run = run_enable(root, io)
    assert run.rc == 0 and io.asked == [SCOPE_Q, REMOVE_Q] and not password_file(xdg).exists()


# --- 不足だけを聞く (TTY・flag 無し) --------------------------------------------------------------

def test_a_missing_config_asks_the_full_set_and_a_complete_one_asks_nothing_about_it(root, xdg):
    io = ScriptIO(answers=["8788", ""])
    assert run_enable(root, io).rc == 0
    assert io.asked == ["port", SCOPE_Q] and io.err == []


def test_a_lan_config_without_allow_asks_only_who_and_allow(root, xdg):
    write_inputs(root, "web", {"port": 8788, "bind": LAN})
    io = ScriptIO(answers=["1", "10.0.0.0/8"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and io.asked == [ACCESS_Q, ALLOW_Q] and io.err == []
    assert live(root) == {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "none"}


def test_a_bind_that_is_no_longer_local_asks_only_for_a_new_address(root, xdg):
    write_inputs(root, "web", {"port": 8788, "bind": "192.168.77.5", "allow": ["10.0.0.0/8"], "auth": "none"})
    io = ScriptIO(answers=["1"])
    run = run_enable(root, io, candidates=CANDIDATES)
    assert run.rc == 0 and io.asked == [BIND_Q3] and live(root)["bind"] == LAN


def test_basic_without_a_password_on_a_local_web_asks_the_scope_again(root, xdg):
    write_inputs(root, "web", {"port": 8788, "auth": "basic"})
    io = ScriptIO(answers=[""])
    run = run_enable(root, io)
    assert run.rc == 0 and io.asked == [SCOPE_Q] and live(root) == {"port": 8788}


def test_none_with_a_leftover_password_in_a_tty_asks_the_auth_choice(root, xdg):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "none"}, SecretPlan("absent"))
    (xdg / "secret").mkdir(exist_ok=True)
    password_file(xdg).write_text("L" * 22)
    password_file(xdg).chmod(0o600)
    assert web_access_reason(root, interfaces=lambda: [LAN]) == "secret_unexpected"
    io = ScriptIO(answers=["1", "10.0.0.0/8", "1"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and not password_file(xdg).exists()
    assert io.asked == [ACCESS_Q, ALLOW_Q, REMOVE_Q]


def test_a_leftover_password_without_a_tty_is_never_auto_removed_or_auto_basic(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    password_file(xdg).write_text("L" * 22)
    password_file(xdg).chmod(0o600)
    io = ScriptIO(tty=False)
    run = run_enable(root, io)
    assert (run.rc, run.reason) == (2, "setup_input_required")
    assert password_file(xdg).exists() and live(root) == {"port": 8788}


def test_an_unsafe_secret_stops_at_once_with_the_fixed_guidance_and_changes_nothing(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    (xdg / "secret" / "stray").write_text("x")
    before = tree(xdg)
    io = ScriptIO(answers=[""])
    run = run_enable(root, io)
    assert (run.rc, run.reason) == (1, "secret_invalid")
    assert io.asked == [] and run.probes == [] and tree(xdg) == before
    assert len(io.err) == 1 and io.err[0].startswith("afx ops: secret_invalid (")
    assert str(root) not in io.err[0] and "stray" not in io.err[0]


def test_an_unsafe_existing_ops_toml_fails_closed_for_ask_as_config_dir_invalid(root, xdg):
    write_inputs(root, "web", {"port": 8788})
    (xdg / "config" / "ops.toml").chmod(0o666)
    before = tree(xdg)
    run = run_enable(root, ScriptIO(), WebOptions(ask=True))
    assert (run.rc, run.reason) == (1, "config_dir_invalid") and tree(xdg) == before


# --- probe の再質問表 (§5.2) ------------------------------------------------------------------------

def _fail_then_ok(*failures):
    items = list(failures)
    return lambda n: items[n - 1] if n <= len(items) else (0, "")


def test_probe_bind_failed_reasks_the_port_then_the_address_for_a_lan(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "1", "10.0.0.0/8", "9001", ""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE, probe=_fail_then_ok((73, "probe_bind_failed")))
    assert run.rc == 0 and run.probes == ["web", "web"]
    assert io.asked[-2:] == ["port", BIND_Q]
    assert live(root)["port"] == 9001
    assert io.err == ["afx-ops: probe_bind_failed"]


def test_probe_bind_failed_reasks_only_the_port_for_a_local_only_web(root, xdg):
    io = ScriptIO(answers=["8788", "", "9001"])
    run = run_enable(root, io, probe=_fail_then_ok((73, "probe_bind_failed")))
    assert run.rc == 0 and io.asked == ["port", SCOPE_Q, "port"]


def test_bind_not_local_reasks_only_the_address(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "1", "10.0.0.0/8", ""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE, probe=_fail_then_ok((71, "bind_not_local")))
    assert run.rc == 0 and io.asked[-1] == BIND_Q and run.probes == ["web", "web"]


def test_allow_required_reasks_only_who_and_allow(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "1", "10.0.0.0/8", "1", "172.16.0.0/12"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE, probe=_fail_then_ok((71, "allow_required")))
    assert run.rc == 0 and io.asked[-2:] == [ACCESS_Q, ALLOW_Q] and live(root)["allow"] == ["172.16.0.0/12"]


def test_config_invalid_reasks_the_port_and_the_scope(root, xdg):
    io = ScriptIO(answers=["8788", "", "8788", ""])
    run = run_enable(root, io, probe=_fail_then_ok((71, "config_invalid")))
    assert run.rc == 0 and io.asked == ["port", SCOPE_Q, "port", SCOPE_Q]


def test_secret_missing_reasks_the_auth_and_secret(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "1", "10.0.0.0/8", "1", "10.0.0.0/8"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE, probe=_fail_then_ok((71, "secret_missing")))
    assert run.rc == 0 and io.asked[-2:] == [ACCESS_Q, ALLOW_Q]


def test_secret_unexpected_reasks_the_auth_with_the_explicit_remove_or_basic_choice(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "1", "10.0.0.0/8", "2", "1"])
    probe_seen = []

    def probe(n):
        probe_seen.append(n)
        return (71, "secret_unexpected") if n == 1 else (0, "")

    # 1 回目の probe の前に、残骸の password を置いて secret_unexpected を再現する
    password_dir = xdg / "secret"
    run_enable_called = {}

    def hook(name):
        if name == "config_applied" and not run_enable_called:
            run_enable_called["x"] = True
            password_dir.mkdir(exist_ok=True)
            (password_dir / "web.password").write_text("L" * 22)
            (password_dir / "web.password").chmod(0o600)

    run = run_enable(root, io, candidates=ONE_CANDIDATE, probe=probe, hook=hook)
    assert run.rc == 0 and live(root)["auth"] == "basic"
    assert io.asked[-2:] == [ACCESS_Q, KEEP_BASIC_Q]
    assert password_file(xdg).read_text() == "L" * 22


@pytest.mark.parametrize("reason", ["secret_invalid", "interface_lookup_failed"])
def test_secret_invalid_and_lookup_failure_end_the_attempt_at_once(root, xdg, reason):
    io = ScriptIO(answers=["8788", ""])
    run = run_enable(root, io, probe=_fail_then_ok((71, reason)))
    assert (run.rc, run.reason) == (1, reason)
    assert run.probes == ["web"] and io.asked == ["port", SCOPE_Q] and run.setup.enabled == []


def test_three_failures_end_with_the_last_fixed_reason_and_no_enable(root, xdg):
    io = ScriptIO(answers=["8788", "", "8788", "", "8788", ""])
    run = run_enable(root, io, probe=lambda n: (71, f"config_invalid"))
    assert (run.rc, run.reason) == (1, "config_invalid") and len(run.probes) == 3
    assert run.setup.enabled == []
    assert io.err == ["afx-ops: config_invalid"] * 3


def test_other_probe_failures_are_not_reasked(root, xdg):
    io = ScriptIO(answers=["8788", ""])
    run = run_enable(root, io, probe=_fail_then_ok((70, "runtime_unavailable")))
    assert (run.rc, run.reason) == (1, "runtime_unavailable") and run.probes == ["web"]


def test_eof_while_reasking_is_aborted_and_keeps_what_was_already_applied(root, xdg):
    io = ScriptIO(answers=["8788", ""], eof_after=2)
    run = run_enable(root, io, probe=_fail_then_ok((73, "probe_bind_failed")))
    assert (run.rc, run.reason) == (1, "setup_aborted")
    assert live(root) == {"port": 8788} and run.setup.enabled == []


def test_a_retry_does_not_regenerate_a_password_that_was_already_shown(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "2", "", "9001", ""], secret_answers=[""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE, probe=_fail_then_ok((73, "probe_bind_failed")))
    assert run.rc == 0
    assert len([text for text in io.out if PW in text]) == 1
    assert password_file(xdg).read_text() == PW


# --- 非対話 (file と action) ---------------------------------------------------------------------

def _target(tmp_path, text, mode=0o600, name="t.toml"):
    path = tmp_path / name
    path.write_text(text)
    path.chmod(mode)
    return str(path)


def _pw(tmp_path, value=PW, mode=0o600, name="p"):
    path = tmp_path / name
    path.write_text(value)
    path.chmod(mode)
    return str(path)


def test_non_interactive_install_writes_config_and_password_without_showing_it(root, xdg, tmp_path):
    options = WebOptions(non_interactive=True, secret_action="install",
                         config_file=_target(tmp_path, f'port = 8788\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n'),
                         password_file=_pw(tmp_path))
    io = ScriptIO()
    run = run_enable(root, io, options)
    assert run.rc == 0 and live(root)["auth"] == "basic" and password_file(xdg).read_text() == PW
    assert PW not in io.text() and str(tmp_path) not in io.text()
    assert io.err == [f"web: 待受 {LAN}:8788 / 公開 許可範囲 1 件 / 認証 basic (afx)"]


def test_the_password_file_path_is_recorded_nowhere(root, xdg, tmp_path):
    pw_path = _pw(tmp_path, name="very-distinctive-password-path")
    options = WebOptions(non_interactive=True, secret_action="install",
                         config_file=_target(tmp_path, f'port = 8788\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n'),
                         password_file=pw_path)
    run_enable(root, ScriptIO(), options)
    for path in list(xdg.rglob("*")) + list((root / "data").rglob("*")):
        if path.is_file():
            assert b"very-distinctive-password-path" not in path.read_bytes()


def test_non_interactive_remove_unlinks_the_password(root, xdg, tmp_path):
    _basic_live(root)
    options = WebOptions(non_interactive=True, secret_action="remove",
                         config_file=_target(tmp_path, "port = 8788\n"))
    run = run_enable(root, ScriptIO(), options)
    assert run.rc == 0 and not password_file(xdg).exists() and live(root) == {"port": 8788}


def test_non_interactive_keep_leaves_the_password_alone(root, xdg, tmp_path):
    _basic_live(root)
    before = password_file(xdg).stat().st_mtime_ns
    options = WebOptions(non_interactive=True, secret_action="keep",
                         config_file=_target(tmp_path, f'port = 9000\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n'))
    run = run_enable(root, ScriptIO(), options)
    assert run.rc == 0 and password_file(xdg).stat().st_mtime_ns == before and live(root)["port"] == 9000


def test_non_interactive_table_violation_changes_nothing(root, xdg, tmp_path):
    _basic_live(root)
    before = tree(xdg)
    options = WebOptions(non_interactive=True, secret_action="absent",
                         config_file=_target(tmp_path, "port = 8788\n"))
    run = run_enable(root, ScriptIO(), options)
    assert (run.rc, run.reason) == (2, "setup_input_required") and tree(xdg) == before and run.probes == []


def test_non_interactive_with_an_unsafe_live_secret_is_secret_invalid_and_changes_nothing(root, xdg, tmp_path):
    write_inputs(root, "web", {"port": 8788})
    (xdg / "secret" / "stray").write_text("x")
    before = tree(xdg)
    options = WebOptions(non_interactive=True, secret_action="absent",
                         config_file=_target(tmp_path, "port = 9000\n"))
    run = run_enable(root, ScriptIO(), options)
    assert (run.rc, run.reason) == (1, "secret_invalid") and tree(xdg) == before


@pytest.mark.parametrize("text", ['port = 8788\ninstance_root = "/x"\n', 'port = 8788\nkey_dir = "/k"\n',
                                  "port = 8788\nextra = 1\n", 'port = 8788\nbind = "10.9.9.9"\n',
                                  'port = 8788\nauth = "Basic"\n'])
def test_non_interactive_config_that_is_not_a_valid_target_is_input_invalid(root, xdg, tmp_path, text):
    write_inputs(root, "web", {"port": 8788})
    before = tree(xdg)
    options = WebOptions(non_interactive=True, secret_action="absent", config_file=_target(tmp_path, text))
    run = run_enable(root, ScriptIO(), options)
    assert (run.rc, run.reason) == (2, "setup_input_invalid") and tree(xdg) == before and run.probes == []


def test_non_interactive_probe_failure_is_one_attempt_with_the_fixed_reason(root, xdg, tmp_path):
    options = WebOptions(non_interactive=True, secret_action="absent",
                         config_file=_target(tmp_path, f'port = 8788\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\n'))
    io = ScriptIO()
    run = run_enable(root, io, options, probe=lambda n: (71, "bind_not_local"))
    assert (run.rc, run.reason) == (1, "bind_not_local") and run.probes == ["web"] and io.asked == []
    assert run.setup.enabled == []


def test_non_interactive_never_enumerates_the_interfaces(root, xdg, tmp_path):
    options = WebOptions(non_interactive=True, secret_action="absent",
                         config_file=_target(tmp_path, f'port = 8788\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\n'))
    run = run_enable(root, ScriptIO(), options, candidates=InterfaceLookupFailed())
    assert run.rc == 0


# --- 生成 password の表示順 --------------------------------------------------------------

def test_the_password_is_displayed_after_the_directory_sync_and_before_the_config_write(root, xdg):
    events: list[str] = []
    io = ScriptIO(answers=["8788", "2", "", "2", ""], secret_answers=[""])
    original = io.stdout

    def stdout(text):
        if PW in text:
            events.append("displayed")
        original(text)

    io.stdout = stdout
    run_enable(root, io, candidates=ONE_CANDIDATE, hook=events.append)
    assert events.index("secret_dir_synced") < events.index("displayed") < events.index("config_tmp_created")
    assert events.count("displayed") == 1


def test_a_crash_before_the_display_shows_nothing_and_a_rerun_makes_the_user_choose(root, xdg):
    class Crash(BaseException):
        pass

    def crash(name):
        if name == "secret_dir_synced":
            raise Crash

    io = ScriptIO(answers=["8788", "2", "", "2", ""], secret_answers=[""])
    with pytest.raises(Crash):
        run_enable(root, io, candidates=ONE_CANDIDATE, hook=crash)
    assert PW not in io.text()
    assert password_file(xdg).read_text() == PW           # 保存は済んでいるが利用者は見ていない
    assert web_access_reason(root, interfaces=lambda: [LAN]) == "config_dir_missing"
    io2 = ScriptIO(answers=["8788", "2", "", "2", "2", ""], secret_answers=[""])    # 再実行: 維持 / 再設定を明示選択
    run = run_enable(root, io2, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and io2.asked[-2:] == [KEEP_BASIC_Q, USER_Q]
    assert len([text for text in io2.out if PW in text]) == 1


def test_a_rerun_never_shows_an_old_password_again(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "2", ""], secret_answers=[""])
    run_enable(root, io, candidates=ONE_CANDIDATE)
    again = ScriptIO(answers=[""], tty=True)
    run = run_enable(root, again, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and PW not in again.text()


# --- 待受候補 --------------------------------------------------------

def test_the_wizard_lists_exactly_what_afx_ops_interfaces_returns_including_a_secondary_address(
        root, xdg, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from web_support import write_fake_interfaces
    fake = write_fake_interfaces(tmp_path / "afx-ops", {"interfaces": [
        {"address": "192.168.1.10", "ifindex": 2, "ifname": "enp6s0", "scope": "global"},
        {"address": "192.168.1.11", "ifindex": 2, "ifname": "enp6s0", "scope": "global"}], "version": 2})
    monkeypatch.setattr("agentic_fx.ops_setup.interactive.resolve_ops_exec",
                        lambda paths: SimpleNamespace(ops_exec=fake))
    io = ScriptIO(answers=["8788", "2", "2", "1", "10.0.0.0/8"])
    result = interactive_enable(root, "web", io=io, probe_runner=lambda _f: (0, ""),
                                setup_factory=lambda _r: StubSetup(), token_bytes=FIXED_RNG)
    assert result.rc == 0
    assert "待受アドレス [1] 192.168.1.10 (enp6s0) [2] 192.168.1.11 (enp6s0)" in io.asked
    assert live(root)["bind"] == LAN2


@pytest.mark.parametrize("document, rc", [(None, 71), ({"interfaces": [], "version": 1}, 0)])
def test_a_failing_or_malformed_listing_command_is_interface_lookup_failed(root, xdg, tmp_path, monkeypatch,
                                                                            document, rc):
    from types import SimpleNamespace

    from web_support import write_fake_interfaces
    fake = write_fake_interfaces(tmp_path / "afx-ops", document, rc=rc)
    monkeypatch.setattr("agentic_fx.ops_setup.interactive.resolve_ops_exec",
                        lambda paths: SimpleNamespace(ops_exec=fake))
    io = ScriptIO(answers=["8788", "2"])
    result = interactive_enable(root, "web", io=io, probe_runner=lambda _f: (0, ""),
                                setup_factory=lambda _r: StubSetup(), token_bytes=FIXED_RNG)
    assert (result.rc, result.reason_code) == (1, "interface_lookup_failed")
    assert io.asked == ["port", SCOPE_Q]


def test_an_unresolvable_ops_command_is_also_interface_lookup_failed(root, xdg, monkeypatch):
    from agentic_fx.ops_setup.units import OpsExecNotFound

    def missing(paths):
        raise OpsExecNotFound("none")

    monkeypatch.setattr("agentic_fx.ops_setup.interactive.resolve_ops_exec", missing)
    io = ScriptIO(answers=["8788", "2"])
    result = interactive_enable(root, "web", io=io, probe_runner=lambda _f: (0, ""),
                                setup_factory=lambda _r: StubSetup(), token_bytes=FIXED_RNG)
    assert (result.rc, result.reason_code) == (1, "interface_lookup_failed")


# --- setup lock / lease ---------------------------------------------------------------------------

def test_the_flow_holds_a_given_lease_and_never_reacquires_the_lock(root, xdg, monkeypatch):
    monkeypatch.setattr("agentic_fx.ops_setup.interactive.acquire_setup_lock",
                        lambda _root: (_ for _ in ()).throw(AssertionError("lock reacquired")))
    lease = acquire_setup_lease(root)
    try:
        run = run_enable(root, ScriptIO(answers=["8788", ""]), lease=lease)
    finally:
        lease.close()
    assert run.rc == 0


# --- 出力の秘密 ---------------------------------------------------------------------------------------

def test_no_output_or_file_outside_the_secret_file_carries_the_password(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "2", ""], secret_answers=[""])
    run_enable(root, io, candidates=ONE_CANDIDATE)
    haystack = "\n".join(text for kind, text in io.log if kind != "out" or PW not in text)
    assert PW not in haystack
    for path in list(root.rglob("*")) + list(xdg.rglob("*")):
        if path.is_file() and path != password_file(xdg):
            assert PW.encode() not in path.read_bytes()


# --- 待受アドレスの説明と interface 名 -----------------------------------------------------------

def test_the_bind_question_is_preceded_by_the_explanation_line_and_names_the_interface(root, xdg):
    io = ScriptIO(answers=["8788", "2", "", "1", "10.0.0.0/8"])
    assert run_enable(root, io, candidates=ONE_CANDIDATE).rc == 0
    assert BIND_EXPLANATION in io.out and io.asked.index(BIND_Q) == 2
    assert BIND_Q == f"待受アドレス [1] {LAN} (enp6s0)"
    assert (BIND_Q, "1") in io.defaults


def test_the_explanation_is_shown_once_per_address_question_before_it(root, xdg):
    io = ScriptIO(answers=["8788", "2", "x", "1", "10.0.0.0/8"])
    run_enable(root, io, candidates=CANDIDATES)
    positions = [i for i, (kind, text) in enumerate(io.log) if text == BIND_EXPLANATION]
    asks = [i for i, (kind, text) in enumerate(io.log) if text == BIND_Q3]
    assert len(positions) == 1 and positions[0] < asks[0]


def test_an_interface_name_with_unsafe_characters_is_shown_with_question_marks(root, xdg):
    from agentic_fx.ops_setup.interfaces import Candidate
    odd = (Candidate(LAN, 2, "global", "en p6\x1b[0mé"),)
    io = ScriptIO(answers=["8788", "2", "", "1", "10.0.0.0/8"])
    assert run_enable(root, io, candidates=odd).rc == 0
    assert f"待受アドレス [1] {LAN} (en?p6??0m?)" in io.asked
    assert "\x1b" not in io.text()


# --- Basic のユーザー名とパスワード (確認・文法・3 回) -----------------------------------------

BASIC_HEAD = ["8788", "2", "", "2"]


def _nothing_written(root, xdg, run):
    assert (run.rc, run.reason) == (1, "setup_aborted")
    assert not (xdg / "config" / "ops.toml").exists() and not password_file(xdg).exists()
    assert run.probes == [] and run.setup.enabled == []


def test_a_typed_password_mismatch_asks_the_password_again_and_never_stores_the_unconfirmed_value(root, xdg):
    io = ScriptIO(answers=[*BASIC_HEAD, ""],
                  secret_answers=["first-attempt-1", "other-attempt-1", "second-attempt-1", "second-attempt-1"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and password_file(xdg).read_text() == "second-attempt-1"
    assert io.of("secret") == [PASSWORD_Q, CONFIRM_Q, PASSWORD_Q, CONFIRM_Q]
    assert io.err.count("afx ops: setup_input_invalid") == 1
    assert "first-attempt-1" not in password_file(xdg).read_text()
    assert asked_once(io, USER_Q)


def asked_once(io, label):
    return io.asked.count(label) == 1


def test_an_empty_confirmation_of_a_typed_password_is_a_mismatch(root, xdg):
    io = ScriptIO(answers=[*BASIC_HEAD, ""], secret_answers=["first-attempt-1", "", "", ])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    # 2 回目の入力が空 Enter なので自動生成になり、保存されるのは確認を経ていない値ではなく生成値
    assert run.rc == 0 and password_file(xdg).read_text() == PW


def test_three_failures_in_total_abort_without_writing_anything(root, xdg):
    io = ScriptIO(answers=[*BASIC_HEAD, "Bad", "afx"],
                  secret_answers=["short", "validpass-123", "mismatch-pass-1"])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    _nothing_written(root, xdg, run)
    assert io.err.count("afx ops: setup_input_invalid") == 3


@pytest.mark.parametrize("answers, secrets", [
    (["Bad", "afx:x", "UPPER"], []),
    (["afx"], ["short", "a" * 33, "bad pass word"]),
    (["afx"], ["validpass-123", "mismatch-pass-1", "validpass-456", "mismatch-pass-2",
               "validpass-789", "mismatch-pass-3"]),
])
def test_each_kind_of_failure_counts_towards_the_same_limit_of_three(root, xdg, answers, secrets):
    io = ScriptIO(answers=[*BASIC_HEAD, *answers], secret_answers=list(secrets))
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    _nothing_written(root, xdg, run)


def test_two_failures_then_a_good_input_still_succeeds(root, xdg):
    io = ScriptIO(answers=[*BASIC_HEAD, "Bad", "afx:x", "ops-web"], secret_answers=[""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and live(root)["user"] == "ops-web"
    assert io.err.count("afx ops: setup_input_invalid") == 2


@pytest.mark.parametrize("user, ok", [("a", True), ("a" * 32, True), ("a" * 33, False), ("ops-web_1", True),
                                      ("Afx", False), ("afx:x", False), ("1afx", False), ("-a", False),
                                      ("afx\n", False), ("a b", False), ("afxé", False)])
def test_the_typed_user_follows_the_documented_grammar(root, xdg, user, ok):
    io = ScriptIO(answers=[*BASIC_HEAD, user, "afx"], secret_answers=[""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0
    assert live(root)["user"] == (user if ok else "afx")
    assert io.err.count("afx ops: setup_input_invalid") == (0 if ok else 1)


def test_an_empty_user_answer_takes_the_existing_user_as_the_initial_value(root, xdg):
    write_inputs(root, "web", {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic", "user": "ops-web"})
    password_file(xdg).parent.mkdir(exist_ok=True)
    password_file(xdg).write_text("E" * 22)
    password_file(xdg).chmod(0o600)
    io = ScriptIO(answers=["2", "8788", "2", "", "2", "2", ""], secret_answers=[""])
    run = run_enable(root, io, candidates=ONE_CANDIDATE)
    assert run.rc == 0 and (USER_Q, "ops-web") in io.defaults and live(root)["user"] == "ops-web"


def test_the_generated_password_line_names_the_typed_user(root, xdg):
    io = ScriptIO(answers=[*BASIC_HEAD, "ops-web"], secret_answers=[""])
    run_enable(root, io, candidates=ONE_CANDIDATE)
    assert [text for text in io.out if PW in text] == [
        f"ユーザー名: ops-web / 生成した password (この表示は 1 回だけです): {PW}"]


def test_the_summary_names_the_user_for_a_complete_basic_configuration(root, xdg):
    apply_target(root, {"port": 8788, "bind": LAN, "allow": ["10.0.0.0/8"], "auth": "basic", "user": "ops-web"},
                 SecretPlan("install", "Z" * 22))
    io = ScriptIO(answers=[""])
    run_enable(root, io, candidates=ONE_CANDIDATE)
    assert io.err == [f"web: 待受 {LAN}:8788 / 公開 許可範囲 1 件 / 認証 basic (ops-web)"]


def test_no_prompt_or_choice_text_uses_the_word_default(root, xdg):
    io = ScriptIO(answers=["2", "8788", "2", "", "3", "2", "10.0.0.0/8", ""], secret_answers=[""])
    _basic_live(root)
    run_enable(root, io, candidates=ONE_CANDIDATE)
    assert all("既定" not in label for label in io.asked + io.of("secret"))
    assert "既定" not in KEEP_Q


# --- 非対話の password の文字数 ----------------------------------------------------------------

@pytest.mark.parametrize("size, ok", [(7, False), (8, True), (32, True), (33, False)])
def test_non_interactive_install_accepts_only_8_to_32_byte_passwords(root, xdg, tmp_path, size, ok):
    options = WebOptions(non_interactive=True, secret_action="install",
                         config_file=_target(tmp_path, f'port = 8788\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\nauth = "basic"\n'),
                         password_file=_pw(tmp_path, "p" * size))
    run = run_enable(root, ScriptIO(), options)
    if ok:
        assert run.rc == 0 and password_file(xdg).read_text() == "p" * size
    else:
        assert (run.rc, run.reason) == (1, "secret_invalid") and not password_file(xdg).exists()


def test_non_interactive_writes_the_given_user(root, xdg, tmp_path):
    options = WebOptions(non_interactive=True, secret_action="install",
                         config_file=_target(tmp_path, f'port = 8788\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\n'
                                                       'auth = "basic"\nuser = "ops-web"\n'),
                         password_file=_pw(tmp_path))
    io = ScriptIO()
    assert run_enable(root, io, options).rc == 0
    assert live(root)["user"] == "ops-web"
    assert io.err == [f"web: 待受 {LAN}:8788 / 公開 許可範囲 1 件 / 認証 basic (ops-web)"]


@pytest.mark.parametrize("user", ["Afx", "afx:x", "afx\\n", "", "a" * 33])
def test_non_interactive_refuses_a_user_outside_the_grammar(root, xdg, tmp_path, user):
    options = WebOptions(non_interactive=True, secret_action="install",
                         config_file=_target(tmp_path, f'port = 8788\nbind = "{LAN}"\nallow = ["10.0.0.0/8"]\n'
                                                       f'auth = "basic"\nuser = "{user}"\n'),
                         password_file=_pw(tmp_path))
    write_inputs(root, "web", {"port": 8788})
    before = tree(xdg)
    run = run_enable(root, ScriptIO(), options)
    assert (run.rc, run.reason) == (2, "setup_input_invalid") and tree(xdg) == before
