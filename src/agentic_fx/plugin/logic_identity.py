"""候補 plugin の論理的な同一性 (ロジック hash と仮説族 key) の最小実装。

どちらも親が staging / archive の bytes から計算する。計算できないとき
(ファイルが無い・parse できない) は None を返し、呼び出し側は比較対象から
外す。
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

_DOCSTRING_NODES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# 値が説明文であって判定に効かない keyword 引数 / dict キー
_NON_SEMANTIC_KEYS = frozenset({"rationale", "reason", "description", "note"})


def _is_str_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _blank_text(node: ast.AST) -> ast.AST:
    """文字列リテラル (f-string を含む) を空文字の定数に置き換える。"""
    if _is_str_const(node) or isinstance(node, ast.JoinedStr):
        return ast.Constant(value="")
    return node


class _Normalizer(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        value = node.value
        if isinstance(value, bool):
            return node
        if isinstance(value, (int, float, complex)):
            return ast.Constant(value=0)
        return node

    def visit_keyword(self, node: ast.keyword) -> ast.AST:
        self.generic_visit(node)
        if node.arg in _NON_SEMANTIC_KEYS:
            node.value = _blank_text(node.value)
        return node

    def visit_Dict(self, node: ast.Dict) -> ast.AST:
        self.generic_visit(node)
        for index, key in enumerate(node.keys):
            if _is_str_const(key) and key.value in _NON_SEMANTIC_KEYS:
                node.values[index] = _blank_text(node.values[index])
        return node


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_NODES) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and _is_str_const(first.value):
                del node.body[0]
                if not node.body:
                    node.body.append(ast.Pass())


def _config_shape(value: Any) -> Any:
    """config の構造 (キーと依存 indicator の plugin 名)。値は落とす。"""
    if isinstance(value, dict):
        return {str(key): _config_shape(child) for key, child in sorted(
            value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return [_config_shape(child) for child in value]
    return None


def _indicator_plugins(config: dict) -> list[str]:
    indicators = config.get("indicators")
    names: set[str] = set()
    if isinstance(indicators, dict):
        for spec in indicators.values():
            if isinstance(spec, dict) and isinstance(spec.get("plugin"), str):
                names.add(spec["plugin"])
    return sorted(names)


def logic_hash_of(plugin_py: str, config_yaml: str) -> str | None:
    """数値リテラル・docstring・非意味的な文字列を落とした AST 構造 hash。"""
    try:
        tree = ast.parse(plugin_py)
        config = yaml.safe_load(config_yaml)
    except (SyntaxError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(config, dict):
        return None
    _strip_docstrings(tree)
    tree = ast.fix_missing_locations(_Normalizer().visit(tree))
    shape = {"keys": _config_shape(config),
             "indicators": _indicator_plugins(config)}
    payload = ast.dump(tree, include_attributes=False) + "\0" + json.dumps(
        shape, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def family_key_of(config_yaml: str, *, name: str) -> str | None:
    """仮説族 key。strategy は indicator 集合 + 足 + pair 集合、indicator は
    plugin 名 + 足 (pair は持たない)。依存 indicator が空の strategy は None。"""
    try:
        config = yaml.safe_load(config_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(config, dict):
        return None
    kind = config.get("kind")
    timeframe = str(config.get("timeframe", "1h"))
    if kind == "indicator":
        return f"indicator:{name}@{timeframe}"
    if kind != "strategy":
        return None
    plugins = _indicator_plugins(config)
    if not plugins:
        return None
    pairs = config.get("pairs")
    pair_list = sorted({str(p) for p in pairs}) if isinstance(pairs, list) else []
    return (f"strategy:{','.join(plugins)}@{timeframe}@{','.join(pair_list)}")


def identity_of_dir(plugin_dir: Path, *, name: str
                    ) -> tuple[str | None, str | None]:
    """`plugin.py` / `config.yaml` のあるディレクトリから (logic_hash,
    family_key) を作る。読めない・parse できないものは None。"""
    try:
        plugin_py = (plugin_dir / "plugin.py").read_text(encoding="utf-8")
        config_yaml = (plugin_dir / "config.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    return logic_hash_of(plugin_py, config_yaml), family_key_of(
        config_yaml, name=name)
