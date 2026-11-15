"""lock 順の AST scan (core lock と ops lane / state 排他の逆辺を探す)。

取得の形式 (`with` / `async with` / `acquire()` / 別名 / 補助関数への受け渡し) を全部拾い、
保持区間から呼ぶ関数を同じ package 内で名前解決して推移的にたどる。

解決は静的で近似する。`self.m()` は同じ class、裸の名前は同じ module と import 元、
他の object への `x.m()` は同名の関数すべて (一般的な名前は除く)。OpsService の
メソッドへは受け手の名前 (OPS_RECEIVERS) か OpsService 内の `self` からだけ到達とみなす。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

OPS_RECEIVERS = frozenset({"ops", "ops_service", "self.ops", "self._ops", "app.ops",
                           "self.ops_service", "self._ops_service"})
OPS_CLASS = "OpsService"
OPS_ATTRS = frozenset({"_ops_lock", "_ops_lane", "_mutate"})
# lock を引数に受け取って取得する補助関数 (ExitStack.enter_context、OpsService._acquire)。
ACQUIRING_CALLEES = frozenset({"enter_context", "enter_async_context", "_acquire"})
# 他 object への `x.m()` を名前で解決しない一般的な名前 (container / 文字列 / IO)。
GENERIC = frozenset({
    "get", "set", "add", "append", "extend", "update", "pop", "items", "keys", "values",
    "copy", "clear", "remove", "discard", "join", "split", "strip", "format", "encode",
    "decode", "read", "write", "close", "open", "start", "stop", "run", "wait", "is_set",
    "execute", "executemany", "fetchone", "fetchall", "commit", "rollback", "cursor",
    "release", "acquire", "info", "warning", "error", "debug", "exception", "lower",
    "upper", "startswith", "endswith", "replace", "now", "isoformat", "total_seconds",
    "dumps", "loads", "exists", "mkdir", "unlink", "resolve", "sort", "sorted", "index",
    "count", "put", "send", "recv", "name", "value"})


@dataclass(eq=False)
class Func:
    path: str
    qualname: str
    cls: str | None
    node: ast.AST

    @property
    def ident(self) -> str:
        return f"{self.path}:{self.qualname}"


@dataclass
class Index:
    funcs: list[Func] = field(default_factory=list)
    by_name: dict[str, list[Func]] = field(default_factory=dict)
    imports: dict[str, dict[str, str]] = field(default_factory=dict)  # path -> 名前 -> module

    def add_source(self, path: str, source: str) -> None:
        tree = ast.parse(source)
        self.imports[path] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    self.imports[path][alias.asname or alias.name] = node.module

        def visit(body, prefix: str, cls: str | None) -> None:
            for node in body:
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.", node.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func = Func(path, f"{prefix}{node.name}", cls, node)
                    self.funcs.append(func)
                    self.by_name.setdefault(node.name, []).append(func)
                    visit(node.body, f"{prefix}{node.name}.", None)
                else:
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child, ast.stmt):
                            visit([child], prefix, cls)

        visit(tree.body, "", None)

    @classmethod
    def from_tree(cls, root: Path) -> "Index":
        index = cls()
        for path in sorted(root.rglob("*.py")):
            index.add_source(str(path.relative_to(root)), path.read_text(encoding="utf-8"))
        return index


def _text(node: ast.AST) -> str:
    return ast.unparse(node)


def _aliases(func: ast.AST, marker: str) -> set[str]:
    """marker を含む式を代入された名前 (別名)。代入の連鎖は不動点まで追う。"""
    names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(func):
            targets, value = [], None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            if value is None or not _mentions(value, marker, names):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return names


def _mentions(node: ast.AST, marker: str, aliases: set[str]) -> bool:
    return any((isinstance(sub, ast.Name) and (marker in sub.id or sub.id in aliases))
               or (isinstance(sub, ast.Attribute) and marker in sub.attr)
               for sub in ast.walk(node))


def _statement_lists(node: ast.AST):
    for _name, value in ast.iter_fields(node):
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            yield value
    for child in ast.iter_child_nodes(node):
        yield from _statement_lists(child)


def _is_release(stmt: ast.stmt, marker: str, aliases: set[str]) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "release"
            and _mentions(stmt.value.func.value, marker, aliases))


def _acquires_in(stmt: ast.stmt, marker: str, aliases: set[str]) -> bool:
    """この文自体の式 (入れ子の文は除く) が取得を含むか: x.acquire() / 引数として受け渡し。"""
    for expr in _own_expressions(stmt):
        for sub in ast.walk(expr):
            if not isinstance(sub, ast.Call):
                continue
            if (isinstance(sub.func, ast.Attribute) and sub.func.attr == "acquire"
                    and _mentions(sub.func.value, marker, aliases)):
                return True
            if (_callee(sub) in ACQUIRING_CALLEES
                    and any(_mentions(arg, marker, aliases)
                            for arg in [*sub.args, *(k.value for k in sub.keywords)])):
                return True
    return False


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _own_expressions(stmt: ast.stmt):
    for name, value in ast.iter_fields(stmt):
        if name in ("body", "orelse", "finalbody", "handlers", "cases"):
            continue
        if isinstance(value, ast.AST) and not isinstance(value, ast.stmt):
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, ast.withitem):
                    yield item.context_expr
                elif isinstance(item, ast.AST) and not isinstance(item, ast.stmt):
                    yield item


def hold_regions(func: ast.AST, marker: str) -> list[tuple[str, list[ast.stmt]]]:
    """marker (例: "core_lock") の保持区間。(形式, 区間内の文) の一覧。"""
    aliases = _aliases(func, marker)
    regions: list[tuple[str, list[ast.stmt]]] = []
    for stmts in _statement_lists(func):
        for position, stmt in enumerate(stmts):
            if isinstance(stmt, (ast.With, ast.AsyncWith)) and any(
                    _mentions(item.context_expr, marker, aliases) for item in stmt.items):
                form = "async with" if isinstance(stmt, ast.AsyncWith) else "with"
                regions.append((form, list(stmt.body)))
            if isinstance(stmt, (ast.With, ast.AsyncWith)) and not any(
                    _mentions(item.context_expr, marker, aliases) for item in stmt.items):
                continue
            if _acquires_in(stmt, marker, aliases):
                rest = []
                for later in stmts[position + 1:]:
                    if _is_release(later, marker, aliases):
                        break
                    rest.append(later)
                regions.append(("acquire", rest))
    return regions


def acquisition_sites(index: Index, marker: str = "core_lock") -> list[str]:
    """marker を取る (または取得形式で受け渡す) 関数の一覧 (path:qualname、重複なし)。"""
    return sorted({f.ident for f in index.funcs if hold_regions(f.node, marker)})


def _calls(stmts: list[ast.stmt]):
    for stmt in stmts:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call):
                yield sub


def resolve(index: Index, caller: Func, call: ast.Call) -> list[Func]:
    func = call.func
    if isinstance(func, ast.Name):
        same_file = [f for f in index.by_name.get(func.id, []) if f.path == caller.path]
        if same_file:
            return same_file
        module = index.imports.get(caller.path, {}).get(func.id)
        if module is not None:
            tail = module.split(".")[-1]
            return [f for f in index.by_name.get(func.id, [])
                    if Path(f.path).stem == tail and f.cls is None]
        return []
    if isinstance(func, ast.Attribute):
        name, receiver = func.attr, _text(func.value)
        candidates = index.by_name.get(name, [])
        if receiver in ("self", "cls"):
            same = [f for f in candidates if f.cls == caller.cls and f.path == caller.path]
            return same or [f for f in candidates if f.path == caller.path]
        if receiver in OPS_RECEIVERS:
            return [f for f in candidates if f.cls == OPS_CLASS]
        if name in GENERIC:
            return []
        return [f for f in candidates if f.cls != OPS_CLASS]
    return []


def _reached(index: Index, start: Func, stmts: list[ast.stmt]):
    """区間内の呼出しから推移的に到達する関数と、そこへ至る最初の呼出し行。"""
    seen: dict[Func, int] = {}
    queue = []
    for call in _calls(stmts):
        for target in resolve(index, start, call):
            if target not in seen:
                seen[target] = call.lineno
                queue.append(target)
    while queue:
        current = queue.pop()
        for call in _calls([current.node]):  # type: ignore[list-item]
            for target in resolve(index, current, call):
                if target not in seen:
                    seen[target] = seen[current]
                    queue.append(target)
    return seen


def _touches_ops(func: Func) -> bool:
    """ops lane / ops lock を取る関数か。OpsService の他のメソッドは、core 保持の下で
    ops lane の内側から呼ばれる正規の順 (ops → core) なので、それだけでは該当にしない。"""
    return any(isinstance(n, ast.Attribute) and n.attr in OPS_ATTRS
               for n in ast.walk(func.node))


def core_to_ops(index: Index) -> list[str]:
    """core 保持区間から OpsService / ops lane へ届く辺 (形式つき)。"""
    hits: list[str] = []
    for func in index.funcs:
        for form, stmts in hold_regions(func.node, "core_lock"):
            for sub in _calls(stmts):
                if (isinstance(sub.func, ast.Attribute)
                        and (_text(sub.func.value) in OPS_RECEIVERS
                             or sub.func.attr in OPS_ATTRS)):
                    hits.append(f"{func.ident}:{sub.lineno} direct ({form})")
            for sub in (n for s in stmts for n in ast.walk(s)):
                if isinstance(sub, ast.Attribute) and sub.attr in OPS_ATTRS:
                    hits.append(f"{func.ident}:{sub.lineno} attr ({form})")
            for target, line in _reached(index, func, stmts).items():
                if _touches_ops(target):
                    hits.append(f"{func.ident}:{line} -> {target.ident} ({form})")
    return sorted(set(hits))


def state_exclusive_to_core(index: Index) -> list[str]:
    """state の排他保持区間から core lock の取得へ届く逆辺。"""
    acquirers = set(acquisition_sites(index))
    hits: list[str] = []
    for func in index.funcs:
        for form, stmts in hold_regions(func.node, "_exclusive"):
            if any(_mentions(s, "core_lock", set()) for s in stmts):
                hits.append(f"{func.ident} direct ({form})")
            for target, line in _reached(index, func, stmts).items():
                if target.ident in acquirers:
                    hits.append(f"{func.ident}:{line} -> {target.ident} ({form})")
    return sorted(set(hits))
