"""初回設定の表示 tz は地域名だけを受ける。"""
from __future__ import annotations

import pytest

from agentic_fx.setup.wizard import run_wizard

from tests.setup.test_wizard_rerun import installed_root
from tests.setup.wizard_support import ScriptedWizardIO, fresh_responder, make_deps, make_root

_ASK = "表示タイムゾーン (例"


@pytest.mark.parametrize("rejected", ["EST", "EST5EDT", "Japan", "Etc/GMT-9", "JST", "Etc/UTC", ""])
def test_a_non_region_name_is_asked_again_and_a_region_name_is_accepted(tmp_path, rejected):
    root, _ = installed_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    answers = (rejected, "America/New_York") if rejected else ("Japan", "America/New_York")
    io = ScriptedWizardIO(responder=fresh_responder((_ASK, answers)))
    assert run_wizard(root, io, deps) == 0, io.text
    assert len(io.asked_with(_ASK)) == 2
    assert "'America/New_York' のような地域名で入力してください" in io.text
    assert "display_timezone: America/New_York" in (root / "config" / "settings.yaml").read_text()


@pytest.mark.parametrize("accepted", ["Asia/Tokyo", "America/New_York", "UTC", "Europe/London"])
def test_a_region_name_or_utc_is_accepted_at_once(tmp_path, accepted):
    root, _ = installed_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder((_ASK, accepted)))
    assert run_wizard(root, io, deps) == 0, io.text
    assert len(io.asked_with(_ASK)) == 1
    assert f"display_timezone: {accepted}" in (root / "config" / "settings.yaml").read_text()


def test_the_question_explains_what_the_zone_affects(tmp_path):
    root, _ = installed_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder((_ASK, ("?", "UTC"))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "変えると次回の改善が数時間前後します" in io.text
    assert "地域名" in io.text


@pytest.mark.parametrize(("zone", "shown"), [
    ("Asia/Tokyo", "2026-10-02 15:00:00 +09:00"),
    ("America/New_York", "2026-10-02 02:00:00 -04:00"),
    ("UTC", "2026-10-02 06:00:00 +00:00"),
])
def test_the_bridge_clock_is_shown_in_the_zone_just_answered(tmp_path, zone, shown):
    root, _ = installed_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder((_ASK, zone)))
    assert run_wizard(root, io, deps) == 0, io.text
    assert f"(bridge の時刻 {shown})" in io.text


@pytest.mark.parametrize(("guess", "shown"), [
    ("Etc/UTC", "`Etc/UTC`"),
    ("Japan", "`Japan`"),
    ("Nowhere/City", "`Nowhere/City`"),
])
def test_a_discarded_machine_zone_guess_is_explained_before_the_question(tmp_path, guess, shown):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, local_timezone=lambda: guess)
    inner = fresh_responder((_ASK, "UTC"))
    said_before_ask: list[str] = []

    def responder(prompt):
        if _ASK in prompt and not said_before_ask:
            said_before_ask.append("\n".join(io.out))
        return inner(prompt)

    io = ScriptedWizardIO(responder=responder)
    assert run_wizard(root, io, deps) == 0, io.text
    reason = f"この機械の tz {shown} は地域名として読めないため、初期値を UTC にしました"
    assert reason in said_before_ask[0]


def test_an_unsafe_machine_zone_name_is_not_echoed(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, local_timezone=lambda: "Bad\x1b[31m/Name")
    io = ScriptedWizardIO(responder=fresh_responder((_ASK, "UTC")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "\x1b" not in io.text
    assert "この機械の tz は地域名として読めないため、初期値を UTC にしました" in io.text


@pytest.mark.parametrize("guess", ["Asia/Tokyo", "UTC"])
def test_a_usable_machine_zone_guess_needs_no_explanation(tmp_path, guess):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, local_timezone=lambda: guess)
    io = ScriptedWizardIO(responder=fresh_responder((_ASK, guess)))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "初期値を UTC にしました" not in io.text
