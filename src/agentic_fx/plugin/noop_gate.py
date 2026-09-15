"""Cheap, execution-free checks for substantive plugin candidates."""
from __future__ import annotations

import ast
from pathlib import Path

import yaml


_DOCSTRING_NODES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def normalized_plugin_ast(path: Path) -> str:
    """Return an AST form that ignores comments, whitespace, and all docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_NODES) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                del node.body[0]
    return ast.dump(tree, include_attributes=False)


def _config_value(plugin_dir: Path):
    return yaml.safe_load((plugin_dir / "config.yaml").read_text(encoding="utf-8"))


def find_noop_copy(plugin_dir: Path, *, source_snapshot_dir: Path,
                   examples_dir: Path, name: str,
                   inventory) -> str | None:
    """コードと config が既存 plugin と両方一致する候補を検出する。

    [indicator-consumption-wiring] §2.7 (codex r4 C2 / r8 I1):
    - 比較は **`same_modulo_pins`** (正規化 AST + `strip_pins(config)`) —
      pin は作者の設計判断ではなくハーネスの派生値なので、「lock しただけの
      コピー」は noop のまま検出される (ロックで noop を回避できない)。
    - ただし**同名**の snapshot plugin との比較だけは
      `is_relock_transition` を例外にする。例外が無いと正式な再ロック経路
      (複製 → pin だけ I1→I2 → 提出) が必ず `noop_copy_of:S` で拒否される。
      「現在 inventory の hash」は `inventory.inventory` (最終 admit 済
      indicator) から引く。
    - 入力 (`source_snapshot_dir` / `examples_dir` / `inventory`) は**明示
      注入**する — 呼び出し元が持っている値を関数内で推測しない。

    AST 同一性は逐語コピー検出の下限。``pass`` 追加・注釈・import 順などの
    無害変形は検出対象外 (設計判断 2026-09-01)。
    """
    from agentic_fx.plugin.resolve import is_relock_transition, same_modulo_pins

    comparisons: list[tuple[str, Path, bool]] = []
    if examples_dir.is_dir():
        comparisons.extend(
            (f"_examples/{item.name}", item, False)
            for item in sorted(examples_dir.iterdir()) if item.is_dir())
    if source_snapshot_dir.is_dir():
        comparisons.extend(
            (item.name, item, item.name == name)
            for item in sorted(source_snapshot_dir.iterdir())
            if item.is_dir() and not item.name.startswith("_")
            and (item / "plugin.py").is_file())

    for label, comparison, is_same_name in comparisons:
        try:
            if not same_modulo_pins(plugin_dir, comparison):
                continue
            if is_same_name and is_relock_transition(
                    plugin_dir, comparison, inventory.inventory):
                continue
        except (OSError, UnicodeError, SyntaxError, yaml.YAMLError):
            continue
        return label
    return None


def count_self_test_functions(path: Path) -> int:
    """Approximately count tests using pytest's default function/class names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    count = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            count += node.name.startswith("test_")
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            count += sum(
                child.name.startswith("test_")
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return count
