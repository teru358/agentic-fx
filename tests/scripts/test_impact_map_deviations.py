import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'src/agentic_fx/commands.py'



def scan_sites(source):
    tree = ast.parse(source)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Commands']
    if len(classes) != 1:
        raise ValueError('Commands class missing or ambiguous')
    methods = {'StateStore': {'reset_kill_switch', 'confirm_latched', 'update', 'save'},
               'OutageStateMachine': {'request_resume'}}
    groups = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []
            self.env = {}

        def receiver(self, node):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'self':
                kind = {'state': 'StateStore', 'outage': 'OutageStateMachine'}.get(node.attr)
                return {kind} if kind else {None}
            if isinstance(node, ast.Name):
                return self.env.get(node.id, {node.id} if node.id in methods else {None})
            if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple, ast.Constant)):
                return {'other'}
            return {None}

        def visit_Assign(self, node):
            self.visit(node.value)
            value = self.receiver(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.env[target.id] = set(value)
                else:
                    self.visit(target)

        def visit_AnnAssign(self, node):
            if node.value is not None:
                self.visit(node.value)
            if isinstance(node.target, ast.Name):
                if node.value is not None:
                    self.env[node.target.id] = set(self.receiver(node.value))
                elif isinstance(node.annotation, ast.Name) and node.annotation.id in methods:
                    self.env[node.target.id] = {node.annotation.id}
                else:
                    self.env[node.target.id] = {None}

        def visit_If(self, node):
            self.visit(node.test)
            before = {k: set(v) for k, v in self.env.items()}
            for stmt in node.body:
                self.visit(stmt)
            left = self.env
            self.env = {k: set(v) for k, v in before.items()}
            for stmt in node.orelse:
                self.visit(stmt)
            right = self.env
            self.env = {k: left.get(k, {None}) | right.get(k, {None})
                        for k in left.keys() | right.keys()}

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            outer = self.env
            self.env = {k: set(v) for k, v in outer.items()}
            class Bindings(ast.NodeVisitor):
                def __init__(self):
                    self.names = set()
                def visit_Name(self, item):
                    if isinstance(item.ctx, (ast.Store, ast.Del)):
                        self.names.add(item.id)
                def visit_FunctionDef(self, item):
                    self.names.add(item.name)
                visit_AsyncFunctionDef = visit_FunctionDef
                def visit_ClassDef(self, item):
                    self.names.add(item.name)
            bindings = Bindings()
            for stmt in node.body:
                bindings.visit(stmt)
            for name in bindings.names:
                self.env[name] = {None}
            args = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                    *((node.args.vararg,) if node.args.vararg else ()),
                    *((node.args.kwarg,) if node.args.kwarg else ()))
            for arg in args:
                self.env[arg.arg] = ({arg.annotation.id} if isinstance(arg.annotation, ast.Name)
                                     and arg.annotation.id in methods else {None})
            for stmt in node.body:
                self.visit(stmt)
            self.env = outer
            self.stack.pop()
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute) and self.stack:
                kinds = self.receiver(func.value)
                candidates = {kind for kind in kinds if kind in methods and func.attr in methods[kind]}
                for kind in candidates:
                    status = 'certain' if kinds == {kind} else 'possible'
                    key = (SOURCE, 'Commands.' + '.'.join(self.stack), kind + '.' + func.attr)
                    groups.setdefault((status, key), []).append(ast.dump(node, annotate_fields=True, include_attributes=False))
                if None in kinds and any(func.attr in names for names in methods.values()):
                    key = (SOURCE, 'Commands.' + '.'.join(self.stack), func.attr)
                    groups.setdefault(('unresolved', key), []).append(ast.dump(node, annotate_fields=True, include_attributes=False))
            self.generic_visit(node)

    visitor = Visitor()
    for node in classes[0].body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visitor.visit(node)
    result = {'certain': set(), 'possible': set(), 'unresolved': set()}
    for (status, key), expressions in groups.items():
        if len(expressions) == 1:
            result[status].add((*key, ''))
        else:
            if len(set(expressions)) != len(expressions):
                raise ValueError('duplicate sites cannot be distinguished')
            result[status].update((*key, expr) for expr in expressions)
    return result


def compare(source, entries):
    sites = scan_sites(source)
    actual = sites['certain'] | sites['possible']
    declared = set()
    for row in entries:
        key = (row['path'], row['qualified_symbol'], row['callee_method'], row.get('fixed_expression', ''))
        if key in declared:
            raise ValueError('duplicate ledger key')
        declared.add(key)
    return actual - declared, declared - actual


def test_new_and_removed_sites():
    source = '''\nclass Commands:\n    def reset(self):\n        self.state.reset_kill_switch(1)\n        d = {}\n        d.update(a=1)\n    def resume(self):\n        self.outage.request_resume()\n'''
    allowed = [dict(path=SOURCE, qualified_symbol='Commands.reset', callee_method='StateStore.reset_kill_switch'),
               dict(path=SOURCE, qualified_symbol='Commands.resume', callee_method='OutageStateMachine.request_resume')]
    assert compare(source, allowed) == (set(), set())
    new = source.replace('d.update(a=1)', 'd.update(a=1)\n        self.state.save()')
    assert any(k[2] == 'StateStore.save' for k in compare(new, allowed)[0])
    removed = source.replace('self.outage.request_resume()', 'pass')
    assert any(k[2] == 'OutageStateMachine.request_resume' for k in compare(removed, allowed)[1])


def test_duplicate_site_cannot_hide():
    source = 'class Commands:\n    def f(self):\n        self.state.save()\n        self.state.save()\n'
    allowed = [dict(path=SOURCE, qualified_symbol='Commands.f', callee_method='StateStore.save')]
    with pytest.raises(ValueError):
        compare(source, allowed)


def test_deviations_match_actual_source():
    entries = json.loads((ROOT / 'docs/impact-map/deviations.json').read_text(encoding='utf-8'))
    for row in entries:
        assert all(isinstance(row.get(field), str) and row[field].strip() for field in ('d_id', 'reason', 'ticket', 'approval_boundary'))
    source = (ROOT / SOURCE).read_text(encoding='utf-8')
    sites = scan_sites(source)
    assert not sites['possible'] and not sites['unresolved'], sites
    unexpected, missing = compare(source, entries)
    assert not unexpected and not missing, f'unexpected={sorted(unexpected)} missing={sorted(missing)}'


def test_identifiable_receiver_aliases():
    source = '''
class Commands:
    def f(self, store: StateStore, outage: OutageStateMachine):
        other = self.state
        other.update({})
        store.save()
        outage.request_resume()
        values = {}
        values.update({})
'''
    unexpected, missing = compare(source, [])
    assert not missing
    assert {k[2] for k in unexpected} == {'StateStore.update', 'StateStore.save', 'OutageStateMachine.request_resume'}


def test_branch_join_preserves_possible_receiver():
    source = '''class Commands:
    def f(self, flag):
        store = self.state
        if flag:
            store = {}
        store.save()
'''
    sites = scan_sites(source)
    assert sites['possible'] == {(SOURCE, 'Commands.f', 'StateStore.save', '')}
    assert not sites['certain']


def test_nested_scope_arguments_and_assignments_shadow_alias():
    source = '''class Commands:
    def f(self):
        store = self.state
        def arg(store):
            store.save()
        def assigned():
            store.save()
            store = {}
        store.save()
'''
    sites = scan_sites(source)
    assert sites['certain'] == {(SOURCE, 'Commands.f', 'StateStore.save', '')}
    assert not sites['possible']
    assert {row[1] for row in sites['unresolved']} == {'Commands.f.arg', 'Commands.f.assigned'}


def test_straight_line_reassignment_removes_alias():
    source = '''class Commands:
    def f(self):
        store = self.state
        store = {}
        store.save()
'''
    sites = scan_sites(source)
    assert not sites['certain'] and not sites['possible']
