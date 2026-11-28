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


def _unpacked_from_state_attribute(name: ast.Name, target: ast.Tuple, parents: dict) -> bool:
    """`a, b = x.state, y` の形で、state の位置の値が属性 `.state` の読み戻しか。"""
    assign = parents[target]
    if not (isinstance(assign, ast.Assign) and assign.targets == [target]
            and isinstance(assign.value, ast.Tuple)
            and len(assign.value.elts) == len(target.elts)):
        return False
    value = assign.value.elts[target.elts.index(name)]
    return isinstance(value, ast.Attribute) and value.attr == "state"


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
        elif isinstance(node, ast.Name) and node.id == "state":
            pass  # 追跡中の変数そのもの (その代入は別に検査される)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and node.slice.value == "state":
            pass  # 保存済みの state を読み戻すだけ
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and node.args
              and isinstance(node.args[0], ast.Constant) and node.args[0].value == "state"):
            if len(node.args) == 2:
                take(node.args[1])
        elif isinstance(node, ast.IfExp):
            take(node.body)
            take(node.orelse)
        else:  # 語を解決できない式 (別の変数・関数の戻り値など) は、不明として失敗させる
            found.add(f"<unresolved: {ast.unparse(node) if node is not None else 'None'}>")

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "state" and isinstance(node.ctx, ast.Store):
            # state を束縛する形は構文を問わず全部拾い、Assign・値のある AnnAssign だけ値を検査する
            parent = parents[node]
            if isinstance(parent, ast.Assign) and node in parent.targets:
                take(parent.value)
            elif isinstance(parent, ast.AnnAssign) and parent.target is node and parent.value is not None:
                take(parent.value)
            elif isinstance(parent, ast.Tuple) and _unpacked_from_state_attribute(node, parent, parents):
                pass  # `state, x = outcome.state, ...`: 検査済みの結果から読み戻すだけ
            elif (isinstance(parent, ast.AnnAssign) and parent.value is None
                  and isinstance(parents[parent], ast.ClassDef)):
                pass  # dataclass の field の宣言
            else:
                found.add(f"<unresolved binding: {type(parent).__name__}>")
        elif isinstance(node, ast.ExceptHandler) and node.name == "state":
            found.add("<unresolved binding: ExceptHandler>")
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
                if isinstance(inner, ast.Return) and not isinstance(inner.value, ast.Dict):
                    take(inner.value)  # dict を返す関数は、その "state" の値を別に検査する
    return found


def _outage_source() -> str:
    return (ROOT / "src/agentic_fx/datafeed/outage.py").read_text(encoding="utf-8")


def _transition_source() -> str:
    return (ROOT / "src/agentic_fx/datafeed/outage_transition.py").read_text(encoding="utf-8")


def _bot_source() -> str:
    return (ROOT / "src/agentic_fx/ops/bot_health.py").read_text(encoding="utf-8")


def test_every_state_the_outage_machine_produces_is_in_the_vocabulary():
    # 状態の語は、読み口 (outage.py) と遷移の規則 (outage_transition.py) の両方が作る
    for source in (_outage_source(), _transition_source()):
        words = _state_words(source)
        assert words and words <= set(outage.DATA_STATES), words


def test_a_new_outage_state_on_any_producing_path_is_caught():
    for source, needle, mutated in (
            (_outage_source(), 'return self._load_row().get("state", "ready")',
             'return "new_state"\n        return self._load_row().get("state", "ready")'),
            (_transition_source(), 'state = "restricted"', 'state = "paused"'),
            (_transition_source(), 'state = "ready"', 'state = "idle"'),
            (_outage_source(), '"state": row.get("state", "ready")',
             '"state": row.get("state", "idle")')):
        assert needle in source, needle
        assert not _state_words(source.replace(needle, mutated, 1)) <= set(outage.DATA_STATES), needle


def test_a_state_word_that_arrives_through_a_variable_or_a_call_is_not_trusted():
    for source in (
            "class A:\n    @property\n    def state(self):\n        new_value = 'paused'\n"
            "        state = new_value\n        return state\n",
            "class A:\n    @property\n    def state(self):\n        new_value = 'paused'\n"
            "        return new_value\n",
            "def f(row):\n    state = compute(row)\n    return state\n",
            "def f(row):\n    return {'state': compute(row)}\n"):
        assert any(word.startswith("<unresolved:") for word in _state_words(source)), source


def test_every_way_of_binding_state_other_than_a_plain_literal_assignment_is_caught():
    vocabulary = set(outage.DATA_STATES)
    for source in (
            "def f():\n    state: str = 'paused'\n    return state\n",
            "def f():\n    state: str\n    return state\n",
            "def f(x):\n    state = 'ready'\n    state += x\n    return state\n",
            "def f(x):\n    (state := x)\n    return state\n",
            "def f(x):\n    state, other = x\n    return state\n",
            "def f(x):\n    [state] = x\n    return state\n",
            "def f(x):\n    for state in x:\n        pass\n    return state\n",
            "def f(x):\n    with x as state:\n        pass\n    return state\n",
            "def f(x):\n    try:\n        pass\n    except Exception as state:\n        pass\n    return state\n",
            "def f(x):\n    state, e = x, 1\n    return state\n",
            "def f(x):\n    state, e = x.state, 1, 2\n    return state\n",
            "def f(x):\n    state = other = compute(x)\n    return state\n"):
        assert not _state_words(source) <= vocabulary, source
    for source in ("def f(o):\n    state, epoch = o.state, o.epoch\n    return state\n",
                   "def f():\n    state = 'ready'\n    return state\n",
                   "def f():\n    state: str = 'degraded'\n    return state\n"):
        assert _state_words(source) <= vocabulary, source


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


def test_the_web_phase_table_and_the_guidance_text_copy_match_the_daemon_vocabulary():
    from agentic_fx.datafeed import guidance_text
    child = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from afx_ops import guidance_text\n"
        "from afx_ops.web import marks\n"
        "print(json.dumps({'table': sorted(marks.DATA_PHASES), 'text': sorted(marks.DATA_PHASE_TEXT),"
        " 'phases': list(guidance_text.PHASES),"
        " 'pairs': sorted(map(list, guidance_text.PAIRS))}))\n")
    done = subprocess.run([sys.executable, "-I", "-c", child, str(AFX_OPS)], capture_output=True,
                          text=True, check=True, timeout=60)
    seen = json.loads(done.stdout)
    assert set(seen["table"]) == set(guidance_text.PHASES)
    assert set(seen["text"]) == set(guidance_text.PHASES)
    assert seen["phases"] == list(guidance_text.PHASES)
    assert seen["pairs"] == sorted(map(list, guidance_text.PAIRS))


def test_the_afx_ops_guidance_text_is_a_byte_copy_of_the_canonical_module():
    canon = ROOT / "src/agentic_fx/datafeed/guidance_text.py"
    copy = AFX_OPS / "afx_ops/guidance_text.py"
    assert copy.read_bytes() == canon.read_bytes()
    assert "agentic_fx" not in "\n".join(
        line for line in canon.read_text(encoding="utf-8").splitlines()
        if line.startswith(("import ", "from ")))
