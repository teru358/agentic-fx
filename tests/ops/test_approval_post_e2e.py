"""daemon の変更要約と同梱 bot の承認投稿を実データでつなぐ (redaction → escape → 表示上限の順)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ._change_fixtures import (ADVERSARIAL_PLUGIN, ADVERSARIAL_SECRETS, SECRET_TOKEN,
                               approval_payload, insert_approval, make_live, make_staging,
                               make_version)
from .conftest import OPERATOR

AFX_OPS = Path(__file__).resolve().parents[2] / "afx-ops"
sys.path.insert(0, str(AFX_OPS))

from afx_ops.gate.render import render_approval, render_terminal  # noqa: E402
from afx_ops.gate.state import ApprovalRecord  # noqa: E402

NAME = "sma_cross"
OLD_CFG = "kind: strategy\nparams:\n  period: 14\n"
RECORD = ApprovalRecord(approval_id=7, message_id=101, guild_id=12345678901234567,
                        channel_id=12345678901234568, payload_sha256="a" * 64,
                        display_generation=1, state="fresh")


def _post(env, approval_id) -> str:
    detail = json.loads(json.dumps(env.service().approval_detail(OPERATOR, approval_id)))
    return render_approval(RECORD, detail)


def _scenario(env, config, summary, plugin_py="x = 2\n", old_cfg=OLD_CFG):
    plugins = env.plugins_root
    old_hash = make_version(plugins, NAME, plugin_py="x = 1\n", config=old_cfg)
    make_live(plugins, NAME, old_hash)
    digest = make_staging(plugins, 7, NAME, plugin_py=plugin_py, config=config)
    data = approval_payload(NAME, digest=digest)
    data["agent_claims"] = {"summary": summary, "selection_rationale": "RATIONALE_CANARY"}
    return insert_approval(env, data)


def test_ac22b_adversarial_values_do_not_leak_into_the_discord_post(ops_env):
    cfg = ("kind: strategy\nparams:\n  period: 10\n  where: '" + ADVERSARIAL_SECRETS[0] + " "
           + ADVERSARIAL_SECRETS[3] + "'\n  api_token: '" + SECRET_TOKEN + "'\n")
    summary = ("at " + ADVERSARIAL_SECRETS[0] + " " + ADVERSARIAL_SECRETS[5] + " "
               + ADVERSARIAL_SECRETS[1] + " " + ADVERSARIAL_SECRETS[8] + " ```\n@everyone "
               "<@123456789012345678> <@&123456789012345679>")
    approval_id = _scenario(ops_env, cfg, summary, plugin_py=ADVERSARIAL_PLUGIN,
                            old_cfg=OLD_CFG + "  where: a\n  api_token: b\n")
    content = _post(ops_env, approval_id)
    for secret in ADVERSARIAL_SECRETS:
        assert secret not in content, secret
    assert "[redacted]" in content.replace("\\", "")
    for dangerous in ("@everyone", "```", "<@123456789012345678>", "<@&123456789012345679>",
                      "RATIONALE_CANARY", "selection_rationale"):
        assert dangerous not in content
    assert "holdout" not in content.lower() and len(content) <= 2000


def test_ac23b_the_summary_is_redacted_then_escaped_then_cut_at_300(ops_env):
    def claim(stars):
        approval_id = _scenario(ops_env, "kind: strategy\nparams:\n  period: 10\n",
                                "*" * stars + " " + SECRET_TOKEN)
        section = [s for s in _post(ops_env, approval_id).split("\n\n")
                   if s.startswith("agent の申告")][0]
        assert SECRET_TOKEN not in section and "ghp_" not in section and "Zx81" not in section
        return section.split("\n")[1]
    redacted = "\\[redacted\\]"
    # 293 / 299 / 300 / 301 字 (escape 後)。token は伏せられたまま上限内に収まる
    assert claim(140) == "> " + "\\*" * 140 + " " + redacted
    assert claim(143) == "> " + "\\*" * 143 + " " + redacted
    assert claim(144) == "> " + "\\*" * 144 + " \\[redacted…"
    assert claim(160) == "> " + "\\*" * 149 + "…"


def test_ac23b_a_token_at_the_end_of_a_near_limit_body_never_appears(ops_env):
    for pad in range(0, 60, 3):
        items = "".join(f"  p{i:02d}: '{'*' * 40}'\n" for i in range(8))
        old = "".join(f"  p{i:02d}: 'old'\n" for i in range(8))
        cfg = ("kind: strategy\nparams:\n  period: 10\n" + items + "  last: '" + "*" * pad
               + " " + SECRET_TOKEN + "'\n")
        approval_id = _scenario(ops_env, cfg, "*" * 100 + " " + SECRET_TOKEN,
                                old_cfg=OLD_CFG + old + "  last: a\n")
        content = _post(ops_env, approval_id)
        assert len(content) <= 2000, pad
        assert SECRET_TOKEN not in content and "ghp_" not in content and "Zx81" not in content
        assert "詳細: web の承認 #7" in content


def test_the_terminal_post_of_a_real_decided_approval_keeps_the_summary(ops_env):
    approval_id = _scenario(ops_env, "kind: strategy\nparams:\n  period: 10\n", "実装の申告")
    ops_env.conn.execute("UPDATE approval_requests SET status='rejected',decided_by='api:x',"
                         "decided_at='2026-10-19T14:16:00+00:00',reason='REJECT_CANARY' "
                         "WHERE id=?", (approval_id,))
    ops_env.conn.commit()
    detail = json.loads(json.dumps(ops_env.service().approval_detail(OPERATOR, approval_id)))
    content = render_terminal(RECORD, detail)
    assert "状態: 却下 (2026-10-19 14:16 UTC)" in content and "実装の申告" in content
    assert "api:x" not in content and "REJECT_CANARY" not in content


def test_ac7_the_real_detail_carries_the_request_deadline_and_the_post_shows_it(ops_env):
    approval_id = _scenario(ops_env, "kind: strategy\nparams:\n  period: 10\n", "申告")
    assert _post(ops_env, approval_id).count("期限: なし") == 1
    ops_env.conn.execute("UPDATE approval_requests SET expires_at='2026-10-26T09:30:00+00:00' "
                         "WHERE id=?", (approval_id,))
    ops_env.conn.commit()
    detail = json.loads(json.dumps(ops_env.service().approval_detail(OPERATOR, approval_id)))
    assert detail["expires_at"] == "2026-10-26T09:30:00+00:00"
    assert "期限: 2026-10-26 09:30 UTC" in render_approval(RECORD, detail)
