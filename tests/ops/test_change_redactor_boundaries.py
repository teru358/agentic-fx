"""redactor の閾値・上限・検出範囲の境界を、1 本ずつの緩めで赤になるように pin する。"""
from __future__ import annotations

import pytest

from agentic_fx.ops import change_limits as L
from agentic_fx.ops import redactor as R

_ALNUM_LOW_ENTROPY = "a1" * 16  # 英数混在、エントロピー 1.0
_MIXED_CASE_MID_ENTROPY = "abcdefghijkL" * 3  # 数字なし・大小混在、12 種で約 3.58 (3.3 以上 4.0 未満)
_MIXED_CASE_HIGH_ENTROPY = "AbCdEfGhIjKlMnOpQrStUvWxYz"[:20] * 2  # 数字なし・大小混在、20 種で約 4.32


@pytest.mark.parametrize("text", [_ALNUM_LOW_ENTROPY, _MIXED_CASE_MID_ENTROPY])
def test_low_entropy_runs_of_token_length_are_left_alone(text):
    assert len(text) >= L.TOKEN_MIN_LENGTH
    assert R.redact(text) == text


def test_the_digit_free_entropy_threshold_is_stricter_than_the_digit_one():
    with_digit = "abcdefghijk1" * 3  # 12 種、数字あり、約 3.58
    assert R.redact(with_digit) == L.REDACTED
    assert R.redact(_MIXED_CASE_MID_ENTROPY) == _MIXED_CASE_MID_ENTROPY
    assert R.redact(_MIXED_CASE_HIGH_ENTROPY) == L.REDACTED


def test_the_value_limit_counts_utf8_bytes_not_characters():
    over = "あ" * (L.MAX_REDACT_VALUE_BYTES // 3 + 1)  # 8193 byte、文字数は上限より少ない
    assert len(over) < L.MAX_REDACT_VALUE_BYTES
    assert R.redact_value(over) == L.REDACTED
    at_limit = "あ" * (L.MAX_REDACT_VALUE_BYTES // 3) + "ab"
    assert len(at_limit.encode()) == L.MAX_REDACT_VALUE_BYTES
    assert R.redact_value(at_limit) == at_limit


@pytest.mark.parametrize("key", ["api_keys", "tokens", "db_passwords", "secrets"])
def test_plural_credential_key_names_are_credentials(key):
    assert R.is_credential_key(key) is True
    assert R.redact(f"{key}=abc") == f"{key}=[redacted]"


@pytest.mark.parametrize("text, expected", [
    ("123456782026-10-01", "[redacted]"),  # 日付の直前が語の字
    ("a:2026-10-01", "a:[redacted]"),  # 日付の直前が ``:``
    ("a=12345678-2026-10-01", "a=[redacted]"),  # 日付の直前が ``-`` (口座番号に続く)
])
def test_a_date_must_stand_alone_to_be_exempt_from_account_detection(text, expected):
    assert R.redact(text) == expected


def test_a_standalone_date_is_still_left_alone():
    assert R.redact("2026-10-01") == "2026-10-01"


@pytest.mark.parametrize("text, expected", [
    (".evil.xyz", ".[redacted]"),  # hostname の前の ``.`` は語の一部ではない
    ("my-localhost", "my-localhost"),  # ``-`` でつながる語は localhost そのものではない
])
def test_host_word_boundaries(text, expected):
    assert R.redact(text) == expected
