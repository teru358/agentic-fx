"""全角・percent-encoding・ASCII 外の文字で検出をすり抜ける値を、API・CLI・web の 3 面で伏せる。"""
from __future__ import annotations

import json
import urllib.parse

import pytest

from agentic_fx.ops import change_limits as L
from agentic_fx.ops import client
from agentic_fx.ops import redactor as R

from .conftest import OPERATOR
from .test_change_web_e2e import _page, _scenario

# (入力, 表示面に現れてはならない断片)
CASES = {
    "fullwidth_key": ("ａｐｉ＿ｋｅｙ=hunter2xyz", ["hunter2xyz"]),
    "encoded_url": ("https%3A%2F%2Fdb%2Einternal", ["db%2Einternal", "db.internal"]),
    "encoded_host": ("connect broker%2Eprivate-example%2Ecom now",
                     ["private-example", "broker%2E"]),
    "encoded_abs_path": ("%2Fhome%2Fsomeone%2Fprivate%2Farea", ["someone", "private%2Farea"]),
    "nfkc_token": ("ｇｈｐ_Zx81QwErTyUiOp", ["Zx81QwErTyUiOp"]),
    "homoglyph_key": ("аpi_key=hunter2xyz", ["hunter2xyz"]),
    "homoglyph_scheme": ("hтtps://secrethost.zzz/path", ["secrethost"]),
    "non_ascii_host": ("bröker-secret.internal", ["secret.internal", "ker-secret"]),
    "encoded_fullwidth_key": ("%EF%BD%81pi_key=hunter2xyz", ["hunter2xyz"]),
}
IDS = list(CASES)


def _lines(value: str) -> str:
    return f"x = 2\n# {value}\nNOTE = '{value}'\n"


@pytest.mark.parametrize("case", IDS)
def test_unit_each_evasion_hides_the_value(case):
    text, fragments = CASES[case]
    out = R.redact_value("see " + text + " end")
    for fragment in fragments:
        assert fragment not in out, (case, out)
    assert L.REDACTED in out


def test_percent_decoding_never_leaks_into_the_output():
    out = R.redact_value("name%3Dvalue %41BC")
    assert out == "name%3Dvalue %41BC"


@pytest.mark.parametrize("case", IDS)
def test_api_diff_never_shows_the_evasion(ops_env, case):
    text, fragments = CASES[case]
    approval_id, _, _ = _scenario(ops_env, _lines(text),
                                  "kind: strategy\nparams:\n  period: 10\n")
    body = ops_env.service().approval_change(OPERATOR, approval_id)
    dumped = json.dumps(body, ensure_ascii=False)
    for fragment in fragments:
        assert fragment not in dumped, (case, fragment)
    assert L.REDACTED in dumped


@pytest.mark.parametrize("case", IDS)
def test_cli_never_prints_the_evasion(ops_env, case):
    text, fragments = CASES[case]
    approval_id, _, _ = _scenario(
        ops_env, "x = 2\n", "kind: strategy\nparams:\n  period: 10\n  where: '" + text + "'\n",
        agent_claims={"summary": "see " + text})
    detail = ops_env.service().approval_detail(OPERATOR, approval_id)
    printed = client.format_approval_detail(detail)
    lines = "\n".join(client.format_change_lines(detail["change"]))
    for fragment in fragments:
        assert fragment not in printed and fragment not in lines, (case, fragment)
    # daemon 側を信用せず、CLI 自身の redactor が掛かることも確かめる
    detail["change"]["params"]["items"].append({"key": "raw", "old": text, "new": text})
    for fragment in fragments:
        assert fragment not in "\n".join(client.format_change_lines(detail["change"])), \
            (case, fragment)


@pytest.mark.parametrize("case", IDS)
def test_web_never_shows_the_evasion(ops_env, case):
    text, fragments = CASES[case]
    approval_id, _, _ = _scenario(
        ops_env, _lines(text),
        "kind: strategy\nparams:\n  period: 10\n  where: '" + text + "'\n",
        agent_claims={"summary": "see " + text, "selection_rationale": "r"})
    page = _page(ops_env, approval_id)
    unquoted = urllib.parse.unquote(page)
    for fragment in fragments:
        assert fragment not in page and fragment not in unquoted, (case, fragment)
    assert L.REDACTED in page


@pytest.mark.parametrize("text", ["\u65e5\u672c\u8a9e: \u8aac\u660e", "\u8aac\u660e\u6587\u5b57\u5217 = 1",
                                  "# \u30b3\u30e1\u30f3\u30c8 period: 14"])
def test_plain_non_ascii_labels_are_not_over_redacted(text):
    assert R.redact_value(text) == text


@pytest.mark.parametrize("unit", ["日", "a日", "日.", "日=", "日/", "%41",
                                  "а_", "xа://"])
def test_redaction_time_stays_linear_on_a_value_at_the_target_limit(unit):
    import time
    value = unit * (L.MAX_REDACT_VALUE_BYTES // len(unit.encode()))
    start = time.perf_counter()
    R.redact_value(value)
    assert time.perf_counter() - start < 2.0
