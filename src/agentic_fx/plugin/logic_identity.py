"""候補 plugin の論理的な同一性 (ロジック hash と仮説族 key) の最小実装。

どちらも親が staging / archive の bytes から計算する。計算できないとき
(ファイルが無い・parse できない) は None を返し、呼び出し側は比較対象から
外す。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Iterable

import yaml

from agentic_fx.backtest.timeframes import PLUGIN_TIMEFRAMES
from agentic_fx.plugin.loader import _PLUGIN_NAME_RE

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


DEFAULT_FAMILY_TIMEFRAME = "1h"
FAMILY_KEY_MAX_BYTES = 512
_PAIR_RE = re.compile(r"[A-Z]{6}")


def family_key_from_parts(kind: Any, name: str, timeframe: Any,
                          pairs: Iterable[Any],
                          indicator_names: Iterable[Any]) -> str | None:
    """仮説族 key を作る唯一の関数。値は検査せずに作る。

    indicator は名前 + 足、strategy は依存 indicator 名の集合 + 足 + pair 集合
    (依存が空なら None)。timeframe が None のときは `1h`。明示的な `timeframe: null` の
    indicator だけは旧実装の `@None` から `@1h` に値が変わる (現データに
    該当行は 0)。集合は
    `sorted(set(str(x)))` を `,` で結ぶので、宣言順や重複は key に影響しない。"""
    tf = DEFAULT_FAMILY_TIMEFRAME if timeframe is None else str(timeframe)
    if kind == "indicator":
        return f"indicator:{name}@{tf}"
    if kind != "strategy":
        return None
    plugins = sorted({str(x) for x in indicator_names})
    if not plugins:
        return None
    pair_list = sorted({str(p) for p in pairs})
    return f"strategy:{','.join(plugins)}@{tf}@{','.join(pair_list)}"


def _family_key(config: Any, *, name: str) -> str | None:
    if not isinstance(config, dict):
        return None
    pairs = config.get("pairs")
    return family_key_from_parts(
        config.get("kind"), name, config.get("timeframe"),
        pairs if isinstance(pairs, list) else [], _indicator_plugins(config))


def is_plain_name(value: Any) -> bool:
    """plugin 名の字句文法。"""
    return isinstance(value, str) and _PLUGIN_NAME_RE.fullmatch(value) is not None


def is_plain_pair(value: Any, allowed_pairs: Collection[str]) -> bool:
    """pair の字句文法 (大文字英字 6 文字) と、設定された pair への所属の両方。"""
    return (isinstance(value, str) and _PAIR_RE.fullmatch(value) is not None
            and value in allowed_pairs)


def is_plain_timeframe(value: Any) -> bool:
    return isinstance(value, str) and value in PLUGIN_TIMEFRAMES


def checked_family_key(kind: Any, name: str, timeframe: Any,
                       pairs: Iterable[Any], indicator_names: Iterable[Any],
                       *, allowed_pairs: Collection[str]) -> str | None:
    """配備側の key。各要素に字句文法を先に当て、通らない要素を持つものは
    key を持たない (None)。生成した文字列を分解し直さない。"""
    tf = DEFAULT_FAMILY_TIMEFRAME if timeframe is None else timeframe
    if not is_plain_timeframe(tf):
        return None
    if kind == "indicator":
        if not is_plain_name(name):
            return None
    elif kind == "strategy":
        names = list(indicator_names)
        pair_list = list(pairs)
        if (not names or not pair_list
                or not all(is_plain_name(n) for n in names)
                or not all(is_plain_pair(p, allowed_pairs)
                           for p in pair_list)):
            return None
    return family_key_from_parts(kind, name, tf, pairs, indicator_names)


@dataclass(frozen=True, slots=True)
class ParsedFamilyKey:
    kind: str
    names: tuple[str, ...]
    timeframe: str
    pairs: tuple[str, ...]


def parse_family_key(key: Any, *, allowed_pairs: Collection[str]
                     ) -> ParsedFamilyKey | None:
    """保存済みの key を文法で分解し、再生成値と byte 一致したものだけ返す。

    直接検査は name / tf / pair の字句文法、pair の所属、pair 列が 1 件以上、
    key 全体の長さだけ。昇順・重複なしは直接検査せず、再生成との一致だけが
    担う。外れたものは None。"""
    if not isinstance(key, str):
        return None
    if len(key.encode("utf-8", "surrogatepass")) > FAMILY_KEY_MAX_BYTES:
        return None
    kind, sep, rest = key.partition(":")
    if sep != ":":
        return None
    parts = rest.split("@")
    if kind == "indicator":
        if len(parts) != 2:
            return None
        name, timeframe = parts
        if not is_plain_name(name) or not is_plain_timeframe(timeframe):
            return None
        names: tuple[str, ...] = (name,)
        pairs: tuple[str, ...] = ()
        regenerated = family_key_from_parts(kind, name, timeframe, (), ())
    elif kind == "strategy":
        if len(parts) != 3:
            return None
        names = tuple(parts[0].split(","))
        timeframe = parts[1]
        pairs = tuple(parts[2].split(","))
        if (not all(is_plain_name(n) for n in names)
                or not is_plain_timeframe(timeframe)
                or not pairs
                or not all(is_plain_pair(p, allowed_pairs) for p in pairs)):
            return None
        regenerated = family_key_from_parts(kind, "", timeframe, pairs, names)
    else:
        return None
    if regenerated != key:
        return None
    return ParsedFamilyKey(kind, names, timeframe, pairs)


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
