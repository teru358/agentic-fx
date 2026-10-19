"""検出した範囲の全体が伏せられること (一部だけ残らないこと) を、出力の完全一致で確かめる。"""
from __future__ import annotations

import pytest

from agentic_fx.ops import redactor as R


@pytest.mark.parametrize("text, expected", [
    ("see https://example.com/a?b=1#c ok", "see [redacted] ok"),
    ("http://10.0.0.5:8080/x y", "[redacted] y"),
    ("git+ssh://git@host/r.git z", "[redacted] z"),
    ("ws://u:p@h:1/", "[redacted]"),
    ("go to HTTP://x", "go to [redacted]"),
    ("www.mysite.org/x", "[redacted]"),
    ("cd ~/ now", "cd [redacted] now"),
    ("ls ~/.config/x now", "ls [redacted] now"),
    ("12-34-56-78", "[redacted]"),
    ("token aBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgHiJkL end", "token [redacted] end"),
])
def test_whole_detected_span_is_replaced(text, expected):
    assert R.redact(text) == expected


@pytest.mark.parametrize("text", [
    "12-34-56-7",
    "abcdefghijklmnopqrstuvwxyzabcdefghijklmnop",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMNOP",
])
def test_just_below_the_thresholds_is_left_alone(text):
    assert R.redact(text) == text


@pytest.mark.parametrize("text, expected", [
    ("sk-ab\x00cdefghijklmnopq123", "[redacted]"),
    ("10.0.0.\x001", "[redacted]"),
    ("/h​ome/bob", "[redacted]"),
    ("sk-abcdefgh‍ijklmnopq123", "[redacted]"),
    ("sk-abcdefgh\ud800ijklmnopq123", "[redacted]"),
    ("host 10.0.0.\r1 end", "host [redacted] end"),
    ("a\x00b​c\n\td", "abc\n\td"),
])
def test_control_characters_cannot_split_a_secret_and_are_removed_from_output(text, expected):
    assert R.redact(text) == expected
    assert R.redact_value(text) == expected
