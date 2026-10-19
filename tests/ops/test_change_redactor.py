"""共通 redactor の検出対象、閾値の境界、fail-closed。"""
from __future__ import annotations

import pytest

from agentic_fx.ops import change_limits as L
from agentic_fx.ops import redactor as R

from ._change_fixtures import ADVERSARIAL_PLUGIN, ADVERSARIAL_SECRETS


def test_ac22a_adversarial_fixture_is_fully_redacted():
    for secret in ADVERSARIAL_SECRETS:
        assert secret in ADVERSARIAL_PLUGIN  # 入力側に存在する
    out = R.redact(ADVERSARIAL_PLUGIN)
    for secret in ADVERSARIAL_SECRETS:
        assert secret not in out, secret
    assert L.REDACTED in out


@pytest.mark.parametrize("text", [
    "see /home/u/x.py", "open ~/keys/id", r"C:\Users\bob\a.txt", "/tmp", "../../etc/passwd",
    "https://h.example.org/p", "ftp://10.0.0.1/x", "www.example-site.net/a",
    "mail a@corp.example.com", "db.prod.internal", "localhost", "10.1.2.3", "fe80::1",
    "api_key=abc", "password: 'p w'", "SECRET_TOKEN = \"x\"", "Authorization: Bearer abcdefgh1234",
    "sk-abcdefghijklmnop1234", "AKIAABCDEFGHIJKLMNOP", "12345678", "9999-8888-7777",
])
def test_ac22a_each_detection_class(text):
    assert R.redact(text) != text and L.REDACTED in R.redact(text)


@pytest.mark.parametrize("text", [
    "period: 14", "pip_size: 0.01", "x = a / b", "y[::2]", "1/2", "self.config.yaml",
    "np.log(os.path.join(a, b))", "key_period: 14", "monkey: 1", "def compute(df, params):",
    "calculate_exponential_moving_average_value_series", "v: 1234567", "ratio = 0.00012345678",
])
def test_ordinary_strategy_code_is_not_over_redacted(text):
    assert R.redact(text) == text


@pytest.mark.parametrize("length,expected", [(L.TOKEN_MIN_LENGTH - 1, False),
                                             (L.TOKEN_MIN_LENGTH, True),
                                             (L.TOKEN_MIN_LENGTH + 1, True)])
def test_token_length_boundary(length, expected):
    token = ("a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuV" * 3)[:length]
    assert (R.redact(token) == L.REDACTED) is expected


@pytest.mark.parametrize("digits,expected", [(L.ACCOUNT_MIN_DIGITS - 1, False),
                                             (L.ACCOUNT_MIN_DIGITS, True),
                                             (L.ACCOUNT_MIN_DIGITS + 1, True)])
def test_account_digits_boundary(digits, expected):
    number = "7" * digits
    assert (R.redact(number) == L.REDACTED) is expected


@pytest.mark.parametrize("delta,expected", [(-1, False), (0, False), (1, True)])
def test_ac5_value_target_limit_boundary_redacts_whole_value(delta, expected):
    value = "p" * (L.MAX_REDACT_VALUE_BYTES + delta)
    assert (R.redact_value(value) == L.REDACTED) is expected
    if not expected:
        assert R.redact_value(value) == value


def test_value_over_limit_is_not_cut_it_is_hidden_entirely():
    value = "safe " * 3000 + "ghp_Zx81QwErTyUiOpAsDfGhJkLzXcVbNm0123"
    assert R.redact_value(value) == L.REDACTED


def test_redact_value_fails_closed(monkeypatch):
    monkeypatch.setattr(R, "redact", lambda text: 1 / 0)
    assert R.redact_value("anything") == L.REDACTED
    assert R.redact_value(123) == L.REDACTED
    assert R.redact_value(None) == L.REDACTED


@pytest.mark.parametrize("data", [b"\xff\xfe\x00bad", b"has\x00nul", b"x\xc3\x28",
                                  b"-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"])
def test_ac22a_undeterminable_files_are_not_decoded(data):
    assert R.decode_or_none(data) is None


def test_decodable_file_is_text():
    assert R.decode_or_none("日本語 ok\n".encode()) == "日本語 ok\n"


@pytest.mark.parametrize("key,expected", [("api_key", True), ("token", True), ("db.password", True),
                                           ("SECRET", True), ("key_period", False),
                                           ("period", False), ("monkey", False)])
def test_credential_key_names(key, expected):
    assert R.is_credential_key(key) is expected
