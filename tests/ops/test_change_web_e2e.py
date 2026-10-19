"""daemon の変更内容と同梱 web の表示を実データでつなぐ (redaction と escape と byte 上限)。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from agentic_fx.ops import change_limits as L

from ._change_fixtures import (ADVERSARIAL_PLUGIN, ADVERSARIAL_SECRETS, SECRET_TOKEN,
                               approval_payload, insert_approval, make_live, make_staging,
                               make_version)
from .conftest import OPERATOR

AFX_OPS = Path(__file__).resolve().parents[2] / "afx-ops"
sys.path.insert(0, str(AFX_OPS))

from afx_ops.api_client import Reply  # noqa: E402
from afx_ops.web import render as web_render  # noqa: E402
from afx_ops.web.request import Request, handle_request  # noqa: E402

NAME = "sma_cross"
OLD_CFG = "kind: strategy\nparams:\n  period: 14\n"


class ServiceClient:
    """web の read client と同じ形で、実 service の応答 (JSON を通したもの) を返す。"""

    def __init__(self, service):
        self.service = service

    def approval(self, approval_id):
        data = json.loads(json.dumps(self.service.approval_detail(OPERATOR, approval_id)))
        return Reply(200, data, None)

    def approval_change(self, approval_id):
        data = json.loads(json.dumps(self.service.approval_change(OPERATOR, approval_id)))
        return Reply(200, data, None)


def _page(env, approval_id) -> str:
    response = handle_request(Request("GET", f"/approvals/{approval_id}", {}),
                              ServiceClient(env.service()))
    assert response.status == 200
    assert len(response.body) < L.WEB_PAGE_BYTES
    return response.body.decode()


def _scenario(env, plugin_py, config, old_py="x = 1\n", old_cfg=OLD_CFG, **payload):
    plugins = env.plugins_root
    old_hash = make_version(plugins, NAME, plugin_py=old_py, config=old_cfg)
    make_live(plugins, NAME, old_hash)
    digest = make_staging(plugins, 7, NAME, plugin_py=plugin_py, config=config)
    data = approval_payload(NAME, digest=digest)
    data.update(payload)
    return insert_approval(env, data), old_hash, digest


def test_ac12_real_page_has_five_sections_and_the_twelve_digit_hashes(ops_env):
    approval_id, old_hash, new_hash = _scenario(
        ops_env, "x = 2\n", "kind: strategy\nparams:\n  period: 10\n")
    page = _page(ops_env, approval_id)
    assert re.findall(r"<h2>(.*?)</h2>", page) == [
        "概要", "params の差分", "成績", "申告 (agent の申告・未検証)", "変更 diff"]
    assert f"<dt>候補</dt><dd>{new_hash[:12]}</dd>" in page
    assert f"<dt>比較対象</dt><dd>{old_hash[:12]}</dd>" in page
    assert "<dt>params.period</dt><dd>14 → 10</dd>" in page
    assert "<dt>分類</dt><dd>更新</dd>" in page
    assert "-x = 1" in page and "+x = 2" in page
    assert "holdout USDJPY" in page and "<h3>selection_rationale</h3>" in page


def test_ac22a_web_never_shows_adversarial_values(ops_env):
    cfg = "kind: strategy\nparams:\n  period: 10\n  where: '" + ADVERSARIAL_SECRETS[0] + " " \
        + ADVERSARIAL_SECRETS[3] + "'\n"
    approval_id, _, _ = _scenario(
        ops_env, ADVERSARIAL_PLUGIN, cfg,
        agent_claims={"summary": "at " + ADVERSARIAL_SECRETS[0] + " " + ADVERSARIAL_SECRETS[5],
                      "selection_rationale": "host " + ADVERSARIAL_SECRETS[1] + " "
                      + ADVERSARIAL_SECRETS[6] + " " + ADVERSARIAL_SECRETS[2]})
    page = _page(ops_env, approval_id)
    for secret in ADVERSARIAL_SECRETS:
        assert secret not in page and html_unescaped(secret) not in page, secret
    assert L.REDACTED in page
    assert "<script" not in page and "```" in page  # コードフェンスは escape されず本文の文字として残る
    # メンション記法の文字は escape されるだけで、桁の長い数字 (口座らしい列) は伏せられる
    assert "@everyone" in page and "123456789012345678" not in page


def html_unescaped(text: str) -> str:
    import html
    return html.escape(text, quote=True)


def test_ac22a_web_hides_only_the_undeterminable_file(ops_env):
    approval_id, _, _ = _scenario(ops_env, "x = 2\n", OLD_CFG + "# c\n")
    leaf = ops_env.plugins_root / "_staging" / "7" / NAME
    from ._change_fixtures import hash_of
    (leaf / "test_plugin.py").write_bytes(b"\xff\xfe broken")
    digest = hash_of((leaf / "plugin.py").read_bytes(), (leaf / "config.yaml").read_bytes(),
                     (leaf / "test_plugin.py").read_bytes())
    payload = json.loads(ops_env.approval(approval_id)["payload_json"])
    payload["artifact_hash"] = digest
    ops_env.conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                         (json.dumps(payload), approval_id))
    ops_env.conn.commit()
    page = _page(ops_env, approval_id)
    assert f"<h3>test_plugin.py</h3><p>{L.REASON_TEXT[L.REDACTED_CODE]}</p>" in page
    assert "<h3>plugin.py</h3><pre>" in page and "+x = 2" in page


@pytest.mark.parametrize("ampersands,expected_tail", [
    (13, f"{'&amp;' * 13} {L.REDACTED}</dd>"),            # escape 後 76 文字: 上限内
    (14, f"{'&amp;' * 14} {L.REDACTED[:-1]}…</dd>"),       # escape 後 81 文字: 上限で切れる
])
def test_ac23a_real_chain_redacts_before_escape_and_cut(ops_env, ampersands, expected_tail):
    # escape で膨らむ記号を表示上限の手前まで並べ、末尾に token を置く
    value = "&" * ampersands + " " + SECRET_TOKEN
    cfg = f"kind: strategy\nparams:\n  note: '{value}'\n"
    approval_id, _, _ = _scenario(ops_env, "x = 1\n", cfg,
                                  old_cfg="kind: strategy\nparams:\n  note: a\n")
    page = _page(ops_env, approval_id)
    assert SECRET_TOKEN not in page and "ghp_" not in page and "Zx81" not in page
    assert f"<dd>a → {expected_tail}" in page


def test_ac12_real_chain_with_markup_heavy_files_stays_under_64_kib(ops_env):
    bomb = "&<>\"'" * 60
    big = "".join(f"v{i} = '{bomb}'\n" for i in range(600))
    cfg = "kind: strategy\nparams:\n" + "".join(f"  p{i:02d}: '{bomb}'\n" for i in range(60))
    old_cfg = "kind: strategy\nparams:\n" + "".join(f"  p{i:02d}: 'old'\n" for i in range(60))
    approval_id, _, _ = _scenario(
        ops_env, big, cfg, old_cfg=old_cfg,
        agent_claims={"summary": bomb * 20, "selection_rationale": bomb * 20})
    response = handle_request(Request("GET", f"/approvals/{approval_id}", {}),
                              ServiceClient(ops_env.service()))
    body = response.body
    assert len(body) < L.WEB_PAGE_BYTES and b"Output clipped" not in body
    page = body.decode()
    assert "行省略)" in page and "holdout" in page


def test_ac12_service_failure_of_change_keeps_the_detail_page(ops_env):
    approval_id, _, _ = _scenario(ops_env, "x = 2\n", OLD_CFG)

    class Down(ServiceClient):
        def approval_change(self, approval_id):
            raise OSError("daemon down")
    response = handle_request(Request("GET", f"/approvals/{approval_id}", {}),
                              Down(ops_env.service()))
    assert response.status == 200
    assert L.REASON_TEXT[L.UNAVAILABLE] in response.body.decode()


def test_ac13_web_constants_match_the_daemon_definitions():
    assert web_render.DIFF_BUDGET_BYTES == L.WEB_DIFF_BYTES
    assert web_render.OTHER_BUDGET_BYTES == L.WEB_OTHER_BYTES
    assert web_render.FILE_QUOTA_BYTES == L.WEB_FILE_QUOTA_BYTES
    assert web_render.PAGE_LIMIT_BYTES == L.WEB_PAGE_BYTES
    assert web_render.VALUE_CHARS == L.PARAM_VALUE_CHARS
    assert web_render.CLAIM_CHARS == L.CLAIM_WEB_CHARS
    assert web_render.PARAMS_MAX == L.PARAMS_DIFF_MAX
    assert web_render.CLASS_LABELS == L.CLASS_LABELS
    for code, text in web_render.REASON_TEXT.items():
        assert L.REASON_TEXT[code] == text


def test_ac14_bot_never_calls_the_change_endpoint():
    from afx_ops.api_client import BotClient, WebClient
    assert not hasattr(BotClient, "approval_change") and hasattr(WebClient, "approval_change")
    for path in (AFX_OPS / "afx_ops" / "bot", AFX_OPS / "afx_ops" / "gate"):
        for source in path.rglob("*.py"):
            text = source.read_text(encoding="utf-8")
            assert "approval_change" not in text and "/change" not in text, source.name


CONTROL_SPLIT_SECRETS = ("sk-ab‍cdefghijklmnopq123", "10.0.0.​1", "/h‍ome/bob")


def test_control_characters_do_not_hide_secrets_from_the_web_page(ops_env):
    code = "".join(f"v{i} = {s!r}\n" for i, s in enumerate(CONTROL_SPLIT_SECRETS))
    approval_id, _, _ = _scenario(
        ops_env, code, OLD_CFG + "# c\n",
        agent_claims={"summary": "at " + " ".join(CONTROL_SPLIT_SECRETS),
                      "selection_rationale": "see " + CONTROL_SPLIT_SECRETS[0]})
    page = _page(ops_env, approval_id)
    for secret in CONTROL_SPLIT_SECRETS:
        joined = secret.replace("‍", "").replace("​", "")
        assert joined not in page and secret not in page, secret
    assert "sk-abcdefghijklmnopq123" not in page and "10.0.0.1" not in page
    assert "/home/bob" not in page and L.REDACTED in page


def test_control_characters_do_not_hide_secrets_from_the_cli_change_lines():
    from agentic_fx.ops.client import format_change_lines
    change = {"classification": "updated", "name": "sma_cross", "kind": "strategy",
              "params": {"items": [{"key": "where", "old": CONTROL_SPLIT_SECRETS[0],
                                    "new": CONTROL_SPLIT_SECRETS[1] + " " + CONTROL_SPLIT_SECRETS[2]}]}}
    text = "\n".join(format_change_lines(change))
    assert "sk-abcdefghijklmnopq123" not in text and "10.0.0.1" not in text
    assert "/home/bob" not in text
    assert "where: [redacted] → [redacted] [redacted]" in text
