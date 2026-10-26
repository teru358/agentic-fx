"""繰り返しの文法・停止判定・一時指示の節 (純粋な部分)。"""
from __future__ import annotations

import threading

import pytest

from agentic_fx.core import improve_repeat as ir


# ---- 期間 ----

@pytest.mark.parametrize("text,sec", [
    ("2h", 7200), ("45m", 2700), ("1h30m", 5400), ("90m", 5400),
    ("1h90m", 9000), ("24h", 86400), ("1m", 60), ("23h60m", 86400),
])
def test_parse_duration_accepts(text, sec):
    assert ir.parse_duration(text) == sec


@pytest.mark.parametrize("text", [
    "30", "1.5h", "1d", "1H", "0m", " 1h", "1h ", "25h", "24h1m", "", "h", "m",
    "0h0m", "30m1h", "1h1h", "-1h", "1h\n", "１h",
])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError) as e:
        ir.parse_duration(text)
    assert text.strip() == "" or text not in str(e.value)


def test_parse_duration_rejects_non_str():
    with pytest.raises(ValueError):
        ir.parse_duration(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("n", [1, 2, 24])
def test_validate_max_runs_accepts(n):
    assert ir.validate_max_runs(n) == n


@pytest.mark.parametrize("n", [0, 25, -1, True, False, 1.0, "3", None])
def test_validate_max_runs_rejects(n):
    with pytest.raises(ValueError):
        ir.validate_max_runs(n)


def test_constants_are_fixed():
    assert (ir.MAX_DURATION_SEC, ir.MAX_RUNS, ir.COMMENT_MAX_CHARS,
            ir.CONSECUTIVE_FAILURE_LIMIT, ir.BACKOFF_SEC,
            ir.WAIT_RECHECK_SEC) == (86400, 24, 1000, 2, 300, 30)
    assert set(ir.STOP_REASONS) == {
        "deadline", "max_runs", "stopped", "consecutive_failures",
        "resource_exhausted", "autopilot", "shutdown"}


# ---- comment ----

def test_comment_folds_allowed_whitespace_and_trims():
    assert ir.normalize_comment("  a \t b\r\n\r\n c  ") == "a b c"
    assert ir.normalize_comment("x") == "x"


def test_comment_length_is_counted_after_folding():
    assert ir.normalize_comment("a" * 1000) == "a" * 1000
    with pytest.raises(ValueError):
        ir.normalize_comment("a" * 1001)
    assert ir.normalize_comment("a" + " " * 5000 + "b") == "a b"
    assert len(ir.normalize_comment("あ" * 1000)) == 1000


@pytest.mark.parametrize("text", [
    "", "   ", "\t\r\n", "a\x00b", "a\x01b", "a\x1bb", "a\x7fb",
    "a​b",          # Cf (zero width space)
    "a‮b",          # Cf (bidi override)
    "a\ud800b",          # Cs
    "a b", " ", "a　b", "a b", "a b",
    "a b",          # Zs
])
def test_comment_rejects(text):
    with pytest.raises(ValueError) as e:
        ir.normalize_comment(text)
    assert "a" * 3 not in str(e.value)


@pytest.mark.parametrize("ch", ["\x0b", "\x0c", "\x85", "\x1c", "\x1d", "\x1e",
                                "\x1f", " "])
def test_comment_rejects_characters_that_str_split_would_eat(ch):
    assert ("a" + ch + "b").split() == ["a", "b"]
    with pytest.raises(ValueError):
        ir.normalize_comment("a" + ch + "b")


def test_comment_error_does_not_echo_input():
    secret = "SECRET-CANARY\x00"
    with pytest.raises(ValueError) as e:
        ir.normalize_comment(secret)
    assert "SECRET" not in str(e.value)


def test_comment_holder_get_and_clear_are_thread_safe():
    holder = ir.CommentHolder("hello")
    assert holder.get() == "hello"
    seen = []
    barrier = threading.Barrier(2)

    def reader():
        barrier.wait()
        seen.append(holder.get())

    t = threading.Thread(target=reader)
    t.start()
    barrier.wait()
    t.join()
    holder.clear()
    assert holder.get() is None
    assert seen[0] in ("hello", None)


# ---- 停止判定 ----

def _decide(**over):
    base = dict(runs=1, max_runs=3, mono_now=100.0, deadline_mono=200.0,
                consecutive_failures=0, last_resource_exhausted=False,
                stop_requested=False, daemon_stopping=False, autopilot=False)
    base.update(over)
    return ir.decide_next(**base)


def test_decide_continues_when_nothing_applies():
    assert _decide() is None


def test_decide_deadline_is_inclusive_of_equality():
    assert _decide(mono_now=199.0) is None
    assert _decide(mono_now=200.0) == "deadline"
    assert _decide(mono_now=201.0) == "deadline"


def test_decide_max_runs_counts_started():
    assert _decide(runs=2, max_runs=3) is None
    assert _decide(runs=3, max_runs=3) == "max_runs"


def test_decide_first_run_ignores_deadline_runs_and_failures():
    assert _decide(runs=0, mono_now=999.0, max_runs=0, consecutive_failures=5,
                   last_resource_exhausted=True) is None


@pytest.mark.parametrize("over,reason", [
    (dict(stop_requested=True), "stopped"),
    (dict(daemon_stopping=True), "shutdown"),
    (dict(autopilot=True), "autopilot"),
])
def test_decide_stop_signals_apply_to_the_first_run_too(over, reason):
    assert _decide(runs=0, **over) == reason
    assert _decide(runs=2, **over) == reason


def test_decide_failure_stops():
    assert _decide(consecutive_failures=1) is None
    assert _decide(consecutive_failures=2) == "consecutive_failures"
    assert _decide(last_resource_exhausted=True) == "resource_exhausted"


def test_decide_priority_is_pinned():
    everything = dict(stop_requested=True, daemon_stopping=True, autopilot=True,
                      last_resource_exhausted=True, consecutive_failures=2,
                      runs=3, max_runs=3, mono_now=999.0)
    assert _decide(**everything) == "shutdown"
    everything["daemon_stopping"] = False
    assert _decide(**everything) == "stopped"
    everything["stop_requested"] = False
    assert _decide(**everything) == "autopilot"
    everything["autopilot"] = False
    assert _decide(**everything) == "resource_exhausted"
    everything["last_resource_exhausted"] = False
    assert _decide(**everything) == "consecutive_failures"
    everything["consecutive_failures"] = 0
    assert _decide(**everything) == "max_runs"
    everything["runs"] = 1
    assert _decide(**everything) == "deadline"


# ---- 節 ----

def test_oneoff_section_is_empty_without_a_comment():
    assert ir.render_oneoff_section(None) == ""


def test_oneoff_section_text():
    text = ir.render_oneoff_section("EURUSD を重点的に")
    assert text.startswith("\n\n## 今回だけの指示\n\n")
    assert text.endswith("\n\n- EURUSD を重点的に")
    assert "規律" in text and "転記しないでください" in text
    assert text.count("## 今回だけの指示") == 1
