"""web の状態の印の割当表 (afx-ops) と daemon の語彙が一致する。

afx-ops は本体 package を import しない別 package なので、afx-ops の source を子 process に読ませて
割当表の語の集合を取り出し、daemon の定義と突き合わせる (一段目: daemon の語彙 → afx-ops の写し)。
写し → 割当表の突き合わせは afx-ops 側の test (二段目) が持つ。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from agentic_fx.core import econ_window
from agentic_fx.datafeed import outage
from agentic_fx.ops import service as ops_service

ROOT = Path(__file__).resolve().parents[2]
AFX_OPS = ROOT / "afx-ops"

CHILD = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from afx_ops.web import marks
print(json.dumps({name: sorted(getattr(marks, name)) for name in
                  ("DATA_STATES", "BOT_HEALTH", "CALENDAR", "WORKER", "CAPACITY")}))
"""


def _tables() -> dict[str, set[str]]:
    done = subprocess.run([sys.executable, "-I", "-c", CHILD, str(AFX_OPS)], capture_output=True,
                          text=True, check=True, timeout=60)
    return {name: set(words) for name, words in json.loads(done.stdout).items()}


def test_data_states_match_the_daemon_outage_vocabulary():
    assert _tables()["DATA_STATES"] == set(outage.DATA_STATES)


def test_every_state_the_outage_machine_assigns_is_in_the_vocabulary():
    # DATA_STATES の定義が実装と離れないように、state へ代入・比較される文字列も数える
    tree = ast.parse((ROOT / "src/agentic_fx/datafeed/outage.py").read_text(encoding="utf-8"))
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "state" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                literals.add(node.value.value)
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == "state"):
            literals |= {c.value for c in node.comparators
                         if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    assert literals and literals <= set(outage.DATA_STATES), literals


def test_calendar_worker_and_capacity_words_match_the_daemon():
    tables = _tables()
    assert tables["CALENDAR"] == set(econ_window.FRESHNESS_VALUES)
    assert tables["WORKER"] == set(ops_service._ECON_WORKER_STATES)
    assert tables["CAPACITY"] == set(ops_service._ECON_LEVELS)


def test_bot_health_words_match_the_daemon_contract_golden():
    golden = json.loads((ROOT / "tests/fixtures/bot_health_contract.json").read_text(encoding="utf-8"))
    assert _tables()["BOT_HEALTH"] == set(golden["states"])
