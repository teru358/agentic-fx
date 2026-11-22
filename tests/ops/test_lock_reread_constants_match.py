"""両実装の lock 読み直し設定を比較する。"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAMES = {"LOCK_READ_ATTEMPTS", "LOCK_REREAD_INTERVAL"}


def _constants(path):
    found = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in NAMES:
            found[node.targets[0].id] = ast.literal_eval(node.value)
    assert set(found) == NAMES
    return found


def test_lock_reread_constants_match():
    assert _constants(ROOT / "afx-ops/afx_ops/server_auth.py") == _constants(ROOT / "src/agentic_fx/ops/client.py")
