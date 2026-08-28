"""M1 是正 (プラン10 束D round1、verified-codex-round1.md、2026-08-28):
`mission_worker.py` の `_wait_for_go` 完全重複定義 (`656a5dc` での再転写
混入) を検出できなかった構造的欠陥の pin。CI に静的検査 (ruff 等) が
無く (`pyproject.toml` に `[tool.ruff]` 無し、`.github/workflows/ci.yml`
は `uv sync --dev` + `uv run pytest -v` のみ)、テストスイートも
module-level 関数の重複定義 (Python の「後勝ち」で黙って上書きされる
再転写ミス、`F811` 相当) を一切押さえていなかった。

ruff 導入 (verified doc の「案A」) は今回のスコープ外 — 案B (AST 構造
pin) をここで実装する。`src/agentic_fx/**/*.py` 全体を走査し、資産価値
を上げる (単一ファイル固定にしない)。"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


def test_no_duplicate_module_level_function_or_class_defs():
    src = Path(__file__).resolve().parents[1] / "src" / "agentic_fx"
    offenders: dict[str, list[str]] = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = [
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef))]
        counts = Counter(names)
        dups = sorted(name for name, n in counts.items() if n > 1)
        if dups:
            offenders[str(path.relative_to(src))] = dups
    assert offenders == {}, (
        f"module-level 関数/クラスの重複定義 (後勝ちで黙って上書きされる "
        f"再転写ミス、F811 相当) が見つかった: {offenders}")
