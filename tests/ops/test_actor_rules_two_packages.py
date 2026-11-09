"""daemon の actor の規則と afx-ops の client の規則が、principal ごとに一致する。

afx-ops は本体 package を import しない別 package なので、afx-ops の source を子 process に
読ませ、同じ候補に対する判定と表の値を突き合わせる。片方だけ変えると赤になる。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agentic_fx.ops import contracts
from agentic_fx.ops.contracts import OpsError, Principal

AFX_OPS = Path(__file__).resolve().parents[2] / "afx-ops"

CHILD = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from afx_ops import api_client, keys
from afx_ops.api_client import ApiError
case = json.load(sys.stdin)
def accepts(principal, value):
    try:
        api_client._check_actor_text(value, principal)
        return True
    except ApiError:
        return False
print(json.dumps({
  "table": {p: [rule[0].pattern, rule[1]] for p, rule in api_client.ACTOR_RULES.items()},
  "verdicts": {p: [accepts(p, v) for v in case["values"]] for p in case["principals"]},
  "known": sorted(keys.KNOWN_PRINCIPALS),
  "ops": sorted(keys.OPS_PRINCIPALS),
}))
"""

VALUES = [
    "discord:12345678901234567", "discord:123456789012345678", "discord:" + "9" * 20,
    "discord:" + "9" * 21, "discord:1234567890123456", "discord:１２３４５６７８９０１２３４５６７",
    "discord:123456789012345678\n", "Discord:123456789012345678",
    "web:a", "web:alice", "web:a-b_c9", "web:" + "a" * 32, "web:" + "a" * 33, "web:",
    "web:A", "web:1a", "web:-a", "web:a b", "web:alice\n", "web:ａ", "web:a.b", "WEB:alice",
    "", " ", "alice", "slack:alice", "web:alice:bob", "web:" + "a" * 31 + "é",
]


def _other_package_view():
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(AFX_OPS)],
        input=json.dumps({"values": VALUES,
                          "principals": [p.value for p in Principal]}),
        capture_output=True, text=True, check=True, cwd=str(AFX_OPS.parent))
    return json.loads(done.stdout)


def _daemon_accepts(principal: Principal, value: str) -> bool:
    try:
        contracts.validate_asserted_actor(principal, value)
        return True
    except OpsError:
        return False


def test_actor_rules_agree_across_packages_for_every_principal():
    other = _other_package_view()
    assert other["table"] == {
        p.value: [rule.pattern.pattern, rule.max_bytes]
        for p, rule in contracts.ACTOR_RULES.items()}
    for principal in Principal:
        mine = [_daemon_accepts(principal, value) for value in VALUES]
        assert other["verdicts"][principal.value] == mine, principal.value
    # 取り違えが起きない: どの候補も 2 つの principal の両方には通らない。
    bot = other["verdicts"]["ops-bot"]
    web = other["verdicts"]["ops-web-write"]
    assert not any(a and b for a, b in zip(bot, web))
    assert any(bot) and any(web)


def test_principal_lists_agree_across_packages():
    other = _other_package_view()
    assert set(other["known"]) == {p.value for p in Principal}
    assert set(other["ops"]) == {p.value for p in Principal
                                 if p not in contracts.INITIAL_PRINCIPALS}
