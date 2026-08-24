"""Prevent background children from inheriting the interactive shell's fd 0."""
import ast
from pathlib import Path


def test_subprocess_calls_explicitly_define_stdin_policy():
    src = Path(__file__).resolve().parents[1] / "src"
    # git rev-parse is a short-lived, non-interactive metadata query. It cannot
    # consume shell input, and changing this established helper is outside Task 19.
    allowed = {("agentic_fx/store/backtest_runs.py", 254)}
    missing = []
    seen_allowed = set()
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(src).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not (isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in {"Popen", "run"}):
                continue
            key = (rel, node.lineno)
            if key in allowed:
                seen_allowed.add(key)
            elif not any(kw.arg == "stdin" for kw in node.keywords):
                missing.append(key)
    assert seen_allowed == allowed
    assert missing == []
