"""activity の本文に時刻を手で埋めていないかの補助の AST 検査。

時刻を含む本文は `core/display_time.py` の builder だけが作る。daemon の
`ActivityLog` に書く module (`backtest/` と部品の module を除く) で、本文に
日時を埋める書き方を検出する。検出するのは次の 6 つ。

- 日時の format spec を持つ f-string (`{x:%H:%M}`)
- `.format(` の日時指定 (`"{:%H:%M}".format(x)`)
- `strftime(` / `isoformat()`
- 部品の module の外での `utc_token(`
- `_fmt_bar(`

本文の引数 (識別子の `ref_id` は対象外) に直接ある場合のほか、同じ関数内で事前に代入した変数と、同じ
module の helper 関数の戻り値を経由する場合も追う。属性を埋め込むだけの書き方
(`f"latched_at={state.latched_at}"`) は AST で取れない。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "agentic_fx"
EXCLUDED_DIRS = ("backtest",)
EXCLUDED_FILES = ("core/display_time.py",)

_FORMAT_SPEC_DATETIME = re.compile(r"%[A-Za-z]")
_FORMAT_CALL_DATETIME = re.compile(r"\{[^{}]*:[^{}]*%[A-Za-z][^{}]*\}")
_DEPTH = 4


@dataclass(frozen=True)
class Violation:
    path: str
    function: str
    line: int
    reason: str


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_activity_write(node: ast.Call) -> bool:
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "write"):
        return False
    receiver = ast.unparse(func.value)
    if "activit" in receiver:
        return True
    first = node.args[0] if node.args else None
    return (isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name)
            and first.value.id == "Category")


def _is_activity_append(node: ast.Call) -> bool:
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == "append"
            and "activit" in ast.unparse(func.value) and len(node.args) == 1
            and isinstance(node.args[0], ast.Tuple))


class _Module:
    def __init__(self, tree: ast.Module) -> None:
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.setdefault(node.name, node)

    @staticmethod
    def assignments(function: ast.AST | None, name: str) -> list[ast.expr]:
        if function is None:
            return []
        found: list[ast.expr] = []
        for node in ast.walk(function):
            if isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    found.append(node.value)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                target = node.target
                if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                    found.append(node.value)
        return found


def _reasons(expr: ast.AST, module: _Module, scope: ast.AST | None,
             depth: int, seen: set[str]) -> list[str]:
    reasons: list[str] = []
    for node in ast.walk(expr):
        if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            spec = "".join(part.value for part in ast.walk(node.format_spec)
                           if isinstance(part, ast.Constant) and isinstance(part.value, str))
            if _FORMAT_SPEC_DATETIME.search(spec):
                reasons.append("format spec")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "format" and isinstance(node.func, ast.Attribute):
                target = node.func.value
                if (isinstance(target, ast.Constant) and isinstance(target.value, str)
                        and _FORMAT_CALL_DATETIME.search(target.value)):
                    reasons.append(".format")
            elif name in ("strftime", "isoformat"):
                reasons.append(name)
            elif name == "utc_token":
                reasons.append("utc_token")
            elif name == "_fmt_bar":
                reasons.append("_fmt_bar")
            elif depth > 0 and name in module.functions and name not in seen:
                helper = module.functions[name]
                for ret in ast.walk(helper):
                    if isinstance(ret, ast.Return) and ret.value is not None:
                        reasons.extend(_reasons(ret.value, module, helper, depth - 1,
                                                seen | {name}))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and depth > 0:
            for value in module.assignments(scope, node.id):
                if id(value) != id(expr):
                    reasons.extend(_reasons(value, module, scope, depth - 1, seen))
    return reasons


def find_violations(source: str, path: str) -> list[Violation]:
    tree = ast.parse(source)
    module = _Module(tree)
    violations: list[Violation] = []

    def visit(node: ast.AST, function: ast.AST | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node
        if isinstance(node, ast.Call):
            exprs: list[ast.expr] = []
            if _is_activity_write(node):
                exprs = [a for a in node.args[1:] if not isinstance(a, ast.Starred)]
                exprs += [k.value for k in node.keywords if k.arg != "ref_id"]
            elif _is_activity_append(node):
                exprs = list(node.args[0].elts[1:])  # type: ignore[attr-defined]
            if exprs:
                name = getattr(function, "name", "<module>")
                found = {r for e in exprs for r in _reasons(e, module, function, _DEPTH, set())}
                violations.extend(Violation(path, name, node.lineno, r) for r in sorted(found))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return violations


def scanned_files() -> list[Path]:
    files = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative.split("/")[0] in EXCLUDED_DIRS or relative in EXCLUDED_FILES:
            continue
        files.append(path)
    return files


def scan_tree() -> list[Violation]:
    found: list[Violation] = []
    for path in scanned_files():
        found.extend(find_violations(path.read_text(encoding="utf-8"),
                                     path.relative_to(SRC).as_posix()))
    return found


# 手で時刻を埋めている既存の書き込み。builder / bar_label へ載せ替えたら、
# その項目をここから消す (消さないと「もう違反が無い」で落ちる)。
ALLOWED: frozenset[tuple[str, str]] = frozenset()


def test_the_allowed_list_is_empty():
    assert ALLOWED == frozenset()


def test_tree_has_no_hand_written_time_in_activity_bodies_except_the_listed_ones():
    unexpected = [v for v in scan_tree() if (v.path, v.function) not in ALLOWED]
    assert not unexpected, "\n".join(
        f"{v.path}:{v.line} {v.function}: {v.reason}" for v in unexpected)


def test_every_allowed_entry_exists_and_still_violates():
    violating = {(v.path, v.function) for v in scan_tree()}
    for path, function in sorted(ALLOWED):
        file = SRC / path
        assert file.exists(), f"{path} が無い"
        tree = ast.parse(file.read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert function in names, f"{path} に {function} が無い"
        assert (path, function) in violating, (
            f"{path}:{function} はもう違反していない (許容の一覧から消す)")


def test_scan_covers_activity_writing_modules_and_skips_the_excluded_ones():
    relative = {p.relative_to(SRC).as_posix() for p in scanned_files()}
    assert "core/scheduler.py" in relative
    assert "datafeed/outage.py" in relative
    assert "core/display_time.py" not in relative
    assert not any(r.startswith("backtest/") for r in relative)


# ---- 検出の変異 ---------------------------------------------------------------

_HEAD = '''
from agentic_fx.activity import Category
'''

MUTATIONS = {
    "direct_isoformat": '''
class A:
    def go(self, dt):
        self.activity.write(Category.SYSTEM, "e", f"at {dt.isoformat()}")
''',
    "format_spec": '''
class A:
    def go(self, dt):
        self.activity.write(Category.SYSTEM, "e", f"at {dt:%H:%M} UTC")
''',
    "format_call": '''
class A:
    def go(self, dt):
        self.activity.write(Category.SYSTEM, "e", "at {:%H:%M} UTC".format(dt))
''',
    "prior_assignment": '''
class A:
    def go(self, dt):
        when = dt.strftime("%H:%M")
        self.activity.write(Category.SYSTEM, "e", f"at {when}")
''',
    "other_helper": '''
def _stamp(dt):
    return dt.isoformat()


class A:
    def go(self, dt):
        self.activity.write(Category.SYSTEM, "e", f"at {_stamp(dt)}")
''',
    "utc_token_outside_the_module": '''
from agentic_fx.core.display_time import utc_token


class A:
    def go(self, dt):
        self.activity.write(Category.SYSTEM, "e", f"at {utc_token(dt)}")
''',
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_each_mutation_is_detected(name, tmp_path):
    path = tmp_path / "mutant.py"
    path.write_text(_HEAD + MUTATIONS[name], encoding="utf-8")
    found = find_violations(path.read_text(encoding="utf-8"), "mutant.py")
    assert found, f"{name} が検出されない"
    assert {v.function for v in found} == {"go"}


def test_mutation_set_is_the_six_documented_ones():
    assert set(MUTATIONS) == {"direct_isoformat", "format_spec", "format_call",
                              "prior_assignment", "other_helper",
                              "utc_token_outside_the_module"}


def test_fmt_bar_in_a_body_is_detected():
    source = _HEAD + '''
class A:
    def go(self, bar):
        self.activity.write(Category.SYSTEM, "e", f"bar={_fmt_bar(bar)}")
'''
    assert [v.reason for v in find_violations(source, "m.py")] == ["_fmt_bar"]


def test_activities_list_append_is_followed():
    source = _HEAD + '''
def _iso(dt):
    return dt.isoformat()


def go(origin):
    activities = []
    activities.append(("e", f"expected={_iso(origin)}"))
'''
    assert [v.function for v in find_violations(source, "m.py")] == ["go"]


def test_builder_and_bar_label_usage_is_clean():
    source = _HEAD + '''
from agentic_fx.core.display_time import bar_label, news_fetch_skipped


class A:
    def go(self, due, bar, pair):
        self.activity.write(*news_fetch_skipped(due))
        self.activity.write(Category.SYSTEM, "e", f"{pair} bar={bar_label(bar)} n={3:.1%}")
        self.activity.write(Category.SYSTEM, "e2", f"{pair} {len(pair)} {pair!r}")
'''
    assert find_violations(source, "m.py") == []


def test_time_formatting_outside_activity_bodies_is_not_flagged():
    source = _HEAD + '''
class A:
    def go(self, dt):
        label = dt.isoformat()
        self.log.info("at %s", label)
        self.activity.write(Category.SYSTEM, "e", "fixed text")
'''
    assert find_violations(source, "m.py") == []


def test_write_to_a_file_object_is_not_an_activity_write():
    source = '''
def go(fh, dt):
    fh.write(f"{dt.isoformat()}")
'''
    assert find_violations(source, "m.py") == []


def test_mutation_in_a_backtest_module_is_out_of_scope(tmp_path):
    # 走査の対象から backtest/ と部品の module を外す規則そのものを確かめる
    excluded = [p for p in SRC.rglob("*.py")
                if p.relative_to(SRC).as_posix().split("/")[0] in EXCLUDED_DIRS]
    assert excluded
    scanned = set(scanned_files())
    assert not scanned & set(excluded)
    assert SRC / "core" / "display_time.py" not in scanned


def test_ref_id_is_an_identifier_not_a_body():
    source = _HEAD + '''
class A:
    def go(self, t_c):
        self.activity.write(Category.SYSTEM, "e", "fixed", ref_id=f"x:{t_c.isoformat()}")
'''
    assert find_violations(source, "m.py") == []
