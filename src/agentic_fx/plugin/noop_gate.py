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
                   name: str) -> str | None:
    """Find a candidate whose code and config both match an existing plugin.

    Both must match so parameter-only variants remain valid: changing only
    config can be a substantive plugin candidate even when code is shared.
    Unreadable or invalid comparison targets are ignored; candidate errors are
    allowed to propagate to the gate that owns candidate validation.

    AST 同一性は逐語コピー検出の下限。``pass`` 追加・注釈・import 順などの
    無害変形は検出対象外 (設計判断 2026-09-01)。
    """
    candidate_ast = normalized_plugin_ast(plugin_dir / "plugin.py")
    candidate_config = _config_value(plugin_dir)
    comparisons: list[tuple[str, Path]] = []
    examples = source_snapshot_dir / "_examples"
    if examples.is_dir():
        comparisons.extend(
            (f"_examples/{item.name}", item)
            for item in sorted(examples.iterdir()) if item.is_dir())
    if source_snapshot_dir.is_dir():
        comparisons.extend(
            (item.name, item) for item in sorted(source_snapshot_dir.iterdir())
            if item.is_dir() and not item.name.startswith("_")
            and (item / "plugin.py").is_file())

    for label, comparison in comparisons:
        try:
            comparison_ast = normalized_plugin_ast(comparison / "plugin.py")
            comparison_config = _config_value(comparison)
        except (OSError, UnicodeError, SyntaxError, yaml.YAMLError):
            continue
        if candidate_ast == comparison_ast and candidate_config == comparison_config:
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
