"""資金保護の回帰: HWM の付け直しは人の明示操作だけで、設定・自動処理・LLM から到達しない。

実装の細部ではなく、「誰からも呼ばれない」「設定で動かせない」を source と schema で固定する。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from agentic_fx.config import ConfigError, Settings, load_settings

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "agentic_fx"
EXAMPLE = REPO / "config" / "settings.yaml.example"

REBASE_NAMES = ("rebase_kill_switch", "rebase_preview", "apply_rebase", "evaluate_rebase",
                "rebase_service", "rebase_accounting", "RebaseWiring",
                "kill_switch_hwm_rebases", "manual_ks_rebase", "killswitch/rebase",
                "killswitch_rebase")

# rebase の識別子を書いてよい file。ここに無い file (LLM の tool 表・plugin worker・ask・
# improve・scheduler・reset・reconcile・replay・shell を含む) には現れてはならない。
ALLOWED = {
    "service.py",  # RebaseWiring を ops へ渡す配線だけ
    "cli_surface.py",  # endpoint_code の表
    "ops/service.py", "ops/api_server.py", "ops/rebase_service.py",
    "ops/rebase_accounting.py", "ops/client.py", "store/db.py",
}


def _sources() -> dict[str, str]:
    return {str(path.relative_to(SRC)): path.read_text(encoding="utf-8")
            for path in SRC.rglob("*.py")}


def _fields(model: type[BaseModel], seen: set[type] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    names: set[str] = set()
    if model in seen:
        return names
    seen.add(model)
    for name, info in model.model_fields.items():
        names.add(name)
        for candidate in _annotation_models(info.annotation):
            names |= _fields(candidate, seen)
    return names


def _annotation_models(annotation) -> list[type[BaseModel]]:
    found = []
    stack = [annotation]
    while stack:
        item = stack.pop()
        if isinstance(item, type) and issubclass(item, BaseModel):
            found.append(item)
        stack.extend(getattr(item, "__args__", ()) or ())
    return found


def test_settings_have_no_key_that_disables_or_automates_the_kill_switch_or_rebase():
    names = _fields(Settings)
    sensitive = {n for n in names if re.search(r"kill|latch|rebase|hwm|high_water", n, re.I)}
    assert sensitive == {"drawdown_kill_pct"}
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "rebase" not in text.lower()
    keys = {m.group(1) for m in re.finditer(r"^\s*([A-Za-z_]+):", text, re.M)}
    assert {k for k in keys if re.search(r"kill|latch|rebase|hwm", k, re.I)} == {
        "drawdown_kill_pct"}


@pytest.mark.parametrize("value", [0, -1, 100.1, 9999])
def test_drawdown_kill_pct_range_check_is_still_enforced(tmp_path, value):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["risk"]["drawdown_kill_pct"] = value
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(path)


def test_unknown_rebase_keys_are_rejected_not_silently_accepted(tmp_path):
    import yaml
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw["risk"]["auto_rebase"] = True
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError):
        load_settings(path)


def test_rebase_identifiers_appear_only_in_the_ops_files_and_the_wiring():
    leaks = {}
    for name, text in _sources().items():
        if name in ALLOWED:
            continue
        found = [word for word in REBASE_NAMES if word in text]
        if found:
            leaks[name] = found
    assert leaks == {}


def test_the_scan_sees_the_files_it_is_meant_to_exclude():
    present = {name for name, text in _sources().items()
               if any(word in text for word in REBASE_NAMES)}
    assert present == ALLOWED


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if called == name:
                out.append(node)
    return out


def test_service_methods_are_called_only_by_the_api_dispatch():
    sources = _sources()
    callers = {}
    for method in ("rebase_kill_switch", "rebase_preview"):
        callers[method] = sorted(name for name, text in sources.items()
                                 if _calls(ast.parse(text), method))
    assert callers == {"rebase_kill_switch": ["ops/api_server.py"],
                       "rebase_preview": ["ops/api_server.py"]}
    for function in ("apply_rebase", "evaluate_rebase"):
        users = sorted(name for name, text in sources.items()
                       if _calls(ast.parse(text), function))
        assert users and set(users) <= {"ops/service.py", "ops/rebase_service.py"}, function


def test_ops_service_reset_and_reconcile_never_call_the_rebase_path():
    tree = ast.parse((SRC / "ops" / "service.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
                "reset_kill_switch", "reconcile_kill_switch"):
            body = ast.unparse(node)
            assert "rebase" not in body, node.name


def test_store_state_and_shell_never_mention_rebase():
    for relative in ("store/state.py", "commands.py", "shell.py", "core/scheduler.py",
                     "backtest/runner.py"):
        text = (SRC / relative).read_text(encoding="utf-8")
        assert not re.search(r"rebase_kill_switch|apply_rebase|manual_ks_rebase|"
                             r"kill_switch_hwm_rebases|killswitch rebase", text), relative


def test_only_the_cli_entry_runs_the_client_operations():
    importers = set()
    for name, text in _sources().items():
        if name.startswith("ops/"):
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == "agentic_fx.ops.client":
                importers.add((name, tuple(sorted(a.name for a in node.names))))
            if isinstance(node, ast.ImportFrom) and node.module == "agentic_fx.ops" and any(
                    a.name == "client" for a in node.names):
                importers.add((name, ("client",)))
            if isinstance(node, ast.Import) and any(
                    a.name == "agentic_fx.ops.client" for a in node.names):
                importers.add((name, ("client",)))
    assert {name for name, _ in importers} <= {"entry.py", "setup/post_start.py"}
    assert dict(importers).get("setup/post_start.py") == ("ApiClient", "ClientError")


def test_the_client_op_is_built_only_by_the_argument_parser():
    tree = ast.parse((SRC / "ops" / "client.py").read_text(encoding="utf-8"))
    builders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for call in _calls(node, "Op"):
                if call.args and isinstance(call.args[0], ast.Constant) \
                        and call.args[0].value == "killswitch_rebase":
                    builders.append(node.name)
    assert builders == ["parse"]


def test_no_llm_tool_or_plugin_worker_surface_exposes_a_rebase():
    registry = (SRC / "tools").rglob("*.py")
    for path in registry:
        assert "rebase" not in path.read_text(encoding="utf-8").lower(), path.name
    for relative in ("mission_worker.py",):
        assert "rebase" not in (SRC / relative).read_text(encoding="utf-8").lower()
    for directory in ("plugin", "runners", "loops", "core", "datafeed", "egress"):
        for path in (SRC / directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            # replay の自動解除 (backtest) と同名の語を持つ core の既存 accounting は対象外。
            assert not re.search(r"rebase_kill_switch|apply_rebase|manual_ks_rebase|"
                                 r"kill_switch_hwm_rebases|rebase_preview", text), path


def test_the_afx_ops_package_does_not_reach_the_rebase_endpoints():
    for path in (REPO / "afx-ops" / "afx_ops").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "killswitch/rebase" not in text and "rebase-preview" not in text, path.name


def test_the_legacy_shell_has_no_rebase_entry_and_changes_nothing(tmp_path):
    from tests.test_commands import _commands
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn, state, activity, cmds = _commands(tmp_path)
    state.update(kill_switch_latched=True)
    before = state.load()
    snapshots = conn.execute("SELECT * FROM account_snapshots").fetchall()
    out = cmds.dispatch(f"killswitch rebase {before.kill_switch_generation}")
    assert "付け直" not in out and "rebase" not in out.lower().replace("killswitch rebase", "")
    assert state.load() == before
    assert conn.execute("SELECT * FROM account_snapshots").fetchall() == snapshots
    help_text = cmds.dispatch("help")
    assert "rebase" not in help_text
