"""Prevent background children from inheriting the interactive shell's fd 0."""
import ast
from pathlib import Path


def test_subprocess_calls_explicitly_define_stdin_policy():
    src = Path(__file__).resolve().parents[1] / "src"
    # git rev-parse is a short-lived, non-interactive metadata query. It cannot
    # consume shell input, and changing this established helper is outside Task 19.
    # /code-review 2 周目 CR2 是正 (2026-09-18): `find_matching_approved_
    # metrics` に自己一致除外の分岐を足したため、この allowlist の行番号が
    # 327 → 351 にずれた。行番号で固定する allowlist は同ファイルの
    # 無関係な編集で必ず壊れる (今回で 2 度目) が、「どの呼び出しを許すか」
    # を関数名で書くと同名の別呼び出しを取り違えるので、位置固定のまま
    # ずれたら直す運用を維持する。
    allowed = {("agentic_fx/store/backtest_runs.py", 351)}
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
