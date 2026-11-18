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


_STATE_FUNCTIONS = {"state", "state_from", "evaluate"}


def _module_strings(tree: ast.Module) -> dict[str, str]:
    return {t.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            for t in node.targets if isinstance(t, ast.Name)}


def _state_words(source: str) -> set[str]:
    """状態語を作る箇所 (state への代入・"state" の dict 値・state= の引数・.get("state", 既定)・
    状態を返す関数の return) が持つ語。定数の名前は module の文字列定数で引く。"""
    tree = ast.parse(source)
    consts = _module_strings(tree)
    found: set[str] = set()

    def take(node: ast.AST | None) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
        elif isinstance(node, ast.Name) and node.id in consts:
            found.add(consts[node.id])
        elif isinstance(node, ast.IfExp):
            take(node.body)
            take(node.orelse)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "state" for t in node.targets):
            take(node.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "state":
                    take(value)
        elif isinstance(node, ast.keyword) and node.arg == "state":
            take(node.value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and len(node.args) == 2
              and isinstance(node.args[0], ast.Constant) and node.args[0].value == "state"):
            take(node.args[1])
        elif isinstance(node, ast.FunctionDef) and node.name in _STATE_FUNCTIONS:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return):
                    take(inner.value)
    return found


def _outage_source() -> str:
    return (ROOT / "src/agentic_fx/datafeed/outage.py").read_text(encoding="utf-8")


def _bot_source() -> str:
    return (ROOT / "src/agentic_fx/ops/bot_health.py").read_text(encoding="utf-8")


def test_every_state_the_outage_machine_produces_is_in_the_vocabulary():
    words = _state_words(_outage_source())
    assert words and words <= set(outage.DATA_STATES), words


def test_a_new_outage_state_on_any_producing_path_is_caught():
    source = _outage_source()
    for needle, mutated in (
            ('return self._load_row().get("state", "ready")',
             'return "new_state"\n        return self._load_row().get("state", "ready")'),
            ('state = "restricted"', 'state = "paused"'),
            ('"state": row.get("state", "ready")', '"state": row.get("state", "idle")')):
        assert needle in source, needle
        assert not _state_words(source.replace(needle, mutated, 1)) <= set(outage.DATA_STATES), needle


def test_every_state_the_bot_health_evaluation_produces_is_in_the_vocabulary():
    from agentic_fx.ops import bot_health

    words = _state_words(_bot_source())
    assert words and words <= set(bot_health.STATES), words
    assert _tables()["BOT_HEALTH"] == set(bot_health.STATES)


def test_a_new_bot_health_state_on_any_producing_path_is_caught():
    from agentic_fx.ops import bot_health

    source = _bot_source()
    for needle, mutated in (("state = STALLED", 'state = "wedged"'),
                            ("        state = HEALTHY", '        state = "ok"')):
        assert needle in source, needle
        assert not _state_words(source.replace(needle, mutated, 1)) <= set(bot_health.STATES)


def test_calendar_worker_and_capacity_words_match_the_daemon():
    tables = _tables()
    assert tables["CALENDAR"] == set(econ_window.FRESHNESS_VALUES)
    assert tables["WORKER"] == set(ops_service._ECON_WORKER_STATES)
    assert tables["CAPACITY"] == set(ops_service._ECON_LEVELS)


def test_bot_health_words_match_the_daemon_contract_golden():
    golden = json.loads((ROOT / "tests/fixtures/bot_health_contract.json").read_text(encoding="utf-8"))
    assert _tables()["BOT_HEALTH"] == set(golden["states"])
