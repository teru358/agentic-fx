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

# hash の算法の版。規則を変えたら上げ、保存済みの hash は migration が
# 接頭辞の違う行を再計算する。
LOGIC_HASH_PREFIX = "v1:"
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

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        operand = node.operand
        if (isinstance(node.op, (ast.USub, ast.UAdd))
                and isinstance(operand, ast.Constant)
                and isinstance(operand.value, (int, float, complex))
                and not isinstance(operand.value, bool)):
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


# 正規化と dump は AST の深さに比例して再帰する。上限を超える候補は
# 例外にせず計算不能 (None) として扱う。
_MAX_AST_DEPTH = 200


def _ast_depth_exceeds(tree: ast.AST, limit: int) -> bool:
    """再帰を使わずに AST の深さが limit を超えるかを調べる。"""
    stack = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return True
        for child in ast.iter_child_nodes(node):
            stack.append((child, depth + 1))
    return False


def _logic_hash(plugin_py: str, config: Any) -> str | None:
    if not isinstance(config, dict):
        return None
    try:
        tree = ast.parse(plugin_py)
        if _ast_depth_exceeds(tree, _MAX_AST_DEPTH):
            return None
        _strip_docstrings(tree)
        tree = ast.fix_missing_locations(_Normalizer().visit(tree))
        shape = {"keys": _config_shape(config),
                 "indicators": _indicator_plugins(config)}
        payload = ast.dump(tree, include_attributes=False) + "\0" + json.dumps(
            shape, sort_keys=True, ensure_ascii=False)
    except Exception:
        # SyntaxError / ValueError / RecursionError / MemoryError を含む、
        # 計算できない全ての失敗は「比較対象から外す」に畳む。
        return None
    return LOGIC_HASH_PREFIX + hashlib.sha256(
        payload.encode("utf-8")).hexdigest()


def _parse_config(config_yaml: str) -> Any:
    try:
        return yaml.safe_load(config_yaml)
    except Exception:
        return None


def logic_hash_of(plugin_py: str, config_yaml: str) -> str | None:
    """数値リテラル・docstring・非意味的な文字列を落とした AST 構造 hash。
    計算できないとき (parse 不能・深すぎる・資源不足) は None。"""
    return _logic_hash(plugin_py, _parse_config(config_yaml))


def _family_key(config: Any, *, name: str) -> str | None:
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


def family_key_of(config_yaml: str, *, name: str) -> str | None:
    """仮説族 key。strategy は indicator 集合 + 足 + pair 集合、indicator は
    plugin 名 + 足 (pair は持たない)。依存 indicator が空の strategy は None。"""
    return _family_key(_parse_config(config_yaml), name=name)


def read_identity(plugin_dir: Path, *, name: str
                  ) -> tuple[str | None, str | None, dict | None]:
    """`plugin.py` / `config.yaml` を 1 回ずつ読み、(logic_hash, family_key,
    parse 済みの config) を返す。読めない・parse できないものは None。"""
    try:
        plugin_py = (plugin_dir / "plugin.py").read_text(encoding="utf-8")
        config_yaml = (plugin_dir / "config.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None, None
    config = _parse_config(config_yaml)
    return (_logic_hash(plugin_py, config), _family_key(config, name=name),
            config if isinstance(config, dict) else None)


def identity_of_dir(plugin_dir: Path, *, name: str
                    ) -> tuple[str | None, str | None]:
    """`plugin.py` / `config.yaml` のあるディレクトリから (logic_hash,
    family_key) を作る。読めない・parse できないものは None。"""
    logic_hash, family_key, _ = read_identity(plugin_dir, name=name)
    return logic_hash, family_key
