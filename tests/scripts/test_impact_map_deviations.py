import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'src/agentic_fx/commands.py'



def compare(source, entries):
    tree = ast.parse(source)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Commands']
    if len(classes) != 1:
        raise ValueError('Commands class missing or ambiguous')
    groups = {}
    methods = {'StateStore': {'reset_kill_switch', 'confirm_latched', 'update', 'save'},
               'OutageStateMachine': {'request_resume'}}
    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []
            self.aliases = []
        def receiver_kind(self, node):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                return {"state": "StateStore", "outage": "OutageStateMachine"}.get(node.attr)
            if isinstance(node, ast.Name):
                return (self.aliases[-1].get(node.id) if self.aliases else None) or (node.id if node.id in methods else None)
            return None
        def visit_Assign(self, node):
            kind = self.receiver_kind(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name) and self.aliases:
                    if kind:
                        self.aliases[-1][target.id] = kind
                    else:
                        self.aliases[-1].pop(target.id, None)
            self.generic_visit(node)
        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            aliases = dict(self.aliases[-1]) if self.aliases else {}
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if isinstance(arg.annotation, ast.Name) and arg.annotation.id in methods:
                    aliases[arg.arg] = arg.annotation.id
            self.aliases.append(aliases)
            self.generic_visit(node)
            self.aliases.pop()
            self.stack.pop()
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()
        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute):
                kind = self.receiver_kind(func.value)
                if kind and func.attr in methods[kind] and self.stack:
                    key = (SOURCE, 'Commands.' + '.'.join(self.stack), kind + '.' + func.attr)
                    groups.setdefault(key, []).append(ast.dump(node, annotate_fields=True, include_attributes=False))
            self.generic_visit(node)
    visitor = Visitor()
    for node in classes[0].body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visitor.visit(node)
    actual = set()
    for key, expressions in groups.items():
        if len(expressions) == 1:
            actual.add((*key, ''))
        else:
            if len(set(expressions)) != len(expressions):
                raise ValueError('duplicate sites cannot be distinguished')
            actual.update((*key, expr) for expr in expressions)
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
    unexpected, missing = compare((ROOT / SOURCE).read_text(encoding='utf-8'), entries)
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
