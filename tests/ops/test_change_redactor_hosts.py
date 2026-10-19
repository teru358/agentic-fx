"""裸の hostname の検出範囲 (入力の種類で分ける) と、口座らしい文脈の日付、変更要約の差分計算回数。"""
from __future__ import annotations

import json
import urllib.parse

import pytest

from agentic_fx.ops import change as C
from agentic_fx.ops import change_limits as L
from agentic_fx.ops import client
from agentic_fx.ops import redactor as R

from .conftest import OPERATOR
from .test_change_web_e2e import _page, _scenario

CFG = "kind: strategy\nparams:\n  period: 10\n"


def _twice(text: str) -> str:
    return urllib.parse.quote(urllib.parse.quote(text, safe=""), safe="")


def _thrice(text: str) -> str:
    return urllib.parse.quote(_twice(text), safe="")


# 自由文として伏せる入力 (入力, 表示面に現れてはならない断片)
FREE = {
    "generic_tld": ("evil.xyz", ["evil.xyz"]),
    "generic_tld_japanese_neighbour": ("設定はevil.xyzを使う", ["evil.xyz"]),
    "punycode_com": ("xn--80ak6aa92e.com", ["80ak6aa92e"]),
    "punycode_any_tld": ("xn--80ak6aa92e.zzzzqq", ["80ak6aa92e"]),
    "double_encoded_url": (_twice("http://evil.xyz/a"), ["evil", "%2568"]),
    "double_encoded_host": ("evil%252Exyz", ["evil", "%252E"]),
    "triple_encoded_host": (_thrice("evil.xyz"), ["evil", "%25"]),
    "ideographic_full_stop": ("host。example。xyz", ["example"]),
    "fullwidth_full_stop": ("host．example．xyz", ["example"]),
    "halfwidth_ideographic_stop": ("host｡example｡xyz", ["example"]),
    "idn_label": ("例え.com", ["例え"]),
    "fullwidth_slash_url": ("https:／／db.internal／x", ["db.internal"]),
    "fullwidth_at": ("user＠mail.info", ["mail.info"]),
}
FREE_IDS = list(FREE)


@pytest.mark.parametrize("case", FREE_IDS)
def test_unit_free_text_hides_any_tld_host(case):
    text, fragments = FREE[case]
    out = R.redact_value("see " + text + " end")
    for fragment in fragments:
        assert fragment not in out, (case, out)
    assert L.REDACTED in out


@pytest.mark.parametrize("case", FREE_IDS)
def test_api_free_text_surfaces_hide_any_tld_host(ops_env, case):
    text, fragments = FREE[case]
    approval_id, _, _ = _scenario(
        ops_env, "x = 2\n", CFG + "  where: '" + text + "'\n",
        agent_claims={"summary": "see " + text, "selection_rationale": "r " + text})
    service = ops_env.service()
    dumped = json.dumps(service.approval_change(OPERATOR, approval_id), ensure_ascii=False) \
        + json.dumps(service.approval_detail(OPERATOR, approval_id)["change"], ensure_ascii=False)
    for fragment in fragments:
        assert fragment not in dumped, (case, fragment)
    assert L.REDACTED in dumped


@pytest.mark.parametrize("case", FREE_IDS)
def test_cli_free_text_hides_any_tld_host(ops_env, case):
    text, fragments = FREE[case]
    approval_id, _, _ = _scenario(
        ops_env, "x = 2\n", CFG + "  where: '" + text + "'\n",
        agent_claims={"summary": "see " + text})
    detail = ops_env.service().approval_detail(OPERATOR, approval_id)
    detail["change"]["params"]["items"].append({"key": "raw", "old": text, "new": text})
    printed = client.format_approval_detail(detail) + "\n" \
        + "\n".join(client.format_change_lines(detail["change"]))
    for fragment in fragments:
        assert fragment not in printed, (case, fragment)


@pytest.mark.parametrize("case", FREE_IDS)
def test_web_free_text_hides_any_tld_host(ops_env, case):
    text, fragments = FREE[case]
    approval_id, _, _ = _scenario(
        ops_env, "x = 2\n", CFG + "  where: '" + text + "'\n",
        agent_claims={"summary": "see " + text, "selection_rationale": "r " + text})
    page = _page(ops_env, approval_id)
    unquoted = urllib.parse.unquote(page)
    for fragment in fragments:
        assert fragment not in page and fragment not in unquoted, (case, fragment)
    assert L.REDACTED in page


def test_plugin_file_names_are_not_taken_for_hosts_in_free_text():
    for name in L.PLUGIN_FILES:
        assert R.redact_value("edit " + name + " only") == "edit " + name + " only"


# ---- Python ソース行は文字列とコメントだけを対象にする

def _redact_source(source: str) -> list[str]:
    spans = R.python_text_spans(source)
    lines = source.split("\n")
    return [R.redact(line, None if spans is None else spans[i]) for i, line in enumerate(lines)]


@pytest.mark.parametrize("line", [
    "logger.info('x')", "self.local = 1", "a = b[1::2]", "y = evil.xyz", "import os.path",
    "value = self.config.dev + other.app", "z = df.in_range",
])
def test_unit_identifier_chains_in_python_code_stay(line):
    assert _redact_source("x = 1\n" + line + "\n")[1] == line


@pytest.mark.parametrize("line,gone", [
    ('name = "evil.xyz"', "evil.xyz"),
    ("name = 'xn--80ak6aa92e.com'", "80ak6aa92e"),
    ('u = "https://db.internal"', "db.internal"),
    ("x = 1  # see api.test", "api.test"),
    ('s = f"{a}evil.xyz"', "evil.xyz"),
    ('d = """\nline evil.xyz\n"""', "evil.xyz"),
    ('name = "host。example。xyz"', "example"),
    ('name = "evil%252Exyz"', "evil"),
])
def test_unit_hosts_in_python_strings_and_comments_are_redacted(line, gone):
    out = "\n".join(_redact_source("x = 1\n" + line + "\n"))
    assert gone not in out
    assert L.REDACTED in out


def test_unit_columns_follow_non_ascii_text_before_a_string():
    out = _redact_source('x = "日本語" + "evil.xyz"  # 備考 self.info\n')[0]
    assert "evil.xyz" not in out
    assert "self.info" in out or "備考" in out


def test_unit_syntax_error_files_redact_identifiers_too():
    source = "y = evil.xyz\nz = (\n"
    assert R.python_text_spans(source) is None
    assert "evil.xyz" not in _redact_source(source)[0]
    broken = "y = evil.xyz\nx = = 1\n"
    assert "evil.xyz" not in "\n".join(_redact_source(broken))


def test_unit_unterminated_string_is_a_failure():
    assert R.python_text_spans('x = "abc\n') is None


PY_NEW = (
    "import logging\n"
    "logger = logging.getLogger('x')\n"
    "class A:\n"
    "    def f(self):\n"
    "        logger.info('x')\n"
    "        self.local = 1\n"
    "        y = evil.xyz\n"
    "        name = \"leak.xyz\"\n"
    "        u = \"https://db.internal\"\n"
    "        # see api.test\n"
    "        return y\n")


def test_api_diff_keeps_identifiers_and_hides_literal_hosts(ops_env):
    approval_id, _, _ = _scenario(ops_env, PY_NEW, CFG)
    body = ops_env.service().approval_change(OPERATOR, approval_id)
    lines = [line for f in body["diff"]["files"] if f["path"] == "plugin.py"
             for h in f["hunks"] for line in h["lines"]]
    assert "+        logger.info('x')" in lines
    assert "+        self.local = 1" in lines
    assert "+        y = evil.xyz" in lines
    dumped = json.dumps(body)
    for gone in ("leak.xyz", "db.internal", "api.test"):
        assert gone not in dumped
    assert dumped.count(L.REDACTED) >= 3


def test_api_diff_of_a_syntax_error_file_redacts_identifiers(ops_env):
    approval_id, _, _ = _scenario(ops_env, "y = evil.xyz\nz = (\n", CFG)
    dumped = json.dumps(ops_env.service().approval_change(OPERATOR, approval_id))
    assert "evil.xyz" not in dumped and L.REDACTED in dumped


def test_api_diff_of_a_test_file_uses_the_same_rule(ops_env):
    from ._change_fixtures import (approval_payload, insert_approval, make_live, make_staging,
                                   make_version)
    plugins = ops_env.plugins_root
    old = make_version(plugins, "sma_cross", test="def test_x():\n    pass\n")
    make_live(plugins, "sma_cross", old)
    digest = make_staging(plugins, 7, "sma_cross",
                          test="def test_x():\n    logger.info('x')\n    s = 'leak.xyz'\n")
    approval_id = insert_approval(ops_env, approval_payload("sma_cross", digest=digest))
    body = ops_env.service().approval_change(OPERATOR, approval_id)
    lines = [line for f in body["diff"]["files"] if f["path"] == "test_plugin.py"
             for h in f["hunks"] for line in h["lines"]]
    assert "+    logger.info('x')" in lines
    assert "leak.xyz" not in json.dumps(body)


def test_api_config_yaml_is_free_text_in_the_diff(ops_env):
    approval_id, _, _ = _scenario(ops_env, "x = 2\n", CFG + "  where: evil.xyz\n")
    body = ops_env.service().approval_change(OPERATOR, approval_id)
    assert "evil.xyz" not in json.dumps(body)


def test_web_page_keeps_identifiers_and_hides_literal_hosts(ops_env):
    approval_id, _, _ = _scenario(ops_env, PY_NEW, CFG)
    page = _page(ops_env, approval_id)
    assert "logger.info(" in page and "self.local = 1" in page
    for gone in ("leak.xyz", "db.internal", "api.test"):
        assert gone not in page


# ---- 口座らしい key の値は日付の形でも伏せる

@pytest.mark.parametrize("text", [
    "account=2026-10-19", "口座: 2026-10-19", "acct: 2026-10-19", "customer_id = 2026-10-19",
    "iban=2026-10-19", "card_no: '2026-10-19'", "login=2026-10-19", "user_id: 2026-10-19",
    "phone_number: 2026-10-19", "account_no=\"2026-10-19\"",
])
def test_dates_after_account_like_keys_are_redacted(text):
    assert "2026-10-19" not in R.redact(text)
    assert L.REDACTED in R.redact(text)


@pytest.mark.parametrize("text", [
    "from 2026-10-01 to 2026-10-19", "2026-10-19", "since: 2026-10-19", "now=2026-10-19",
    "note: 2026-10-19", "created_at=2026-10-19T12:34:56Z",
])
def test_dates_without_an_account_like_key_stay(text):
    assert R.redact(text) == text


def test_params_value_of_an_account_like_key_is_redacted_even_as_a_date(ops_env):
    approval_id, _, _ = _scenario(ops_env, "x = 2\n", CFG + "  account_no: 2026-10-19\n")
    body = ops_env.service().approval_change(OPERATOR, approval_id)
    assert "2026-10-19" not in json.dumps(body["change"]["params"])
    detail = ops_env.service().approval_detail(OPERATOR, approval_id)
    assert "2026-10-19" not in json.dumps(detail["change"]["params"])


def test_account_date_in_a_plugin_line_is_redacted_in_the_diff(ops_env):
    approval_id, _, _ = _scenario(ops_env, "x = 2  # account=2026-10-19\n", CFG)
    assert "2026-10-19" not in json.dumps(ops_env.service().approval_change(OPERATOR, approval_id))


# ---- detail は diff を作らない

def test_detail_does_not_compute_a_diff_and_the_page_computes_each_file_once(
        ops_env, monkeypatch):
    approval_id, _, _ = _scenario(ops_env, "x = 2\n", CFG)
    calls = []
    real = C._hunks

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(C, "_hunks", counting)
    service = ops_env.service()
    service.approval_detail(OPERATOR, approval_id)
    assert calls == []
    body = service.approval_change(OPERATOR, approval_id)
    alone = len(calls)
    assert alone == len([f for f in body["diff"]["files"] if f["status"] in ("added", "changed")])
    del calls[:]
    _page(ops_env, approval_id)
    assert len(calls) == alone
