"""afx-ops は本体 package を import せず、本体に依存もしない。"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from conftest import AFX_OPS

_WALK = """
import importlib, json, pkgutil, sys
import afx_ops
names = ["afx_ops"] + [m.name for m in pkgutil.walk_packages(afx_ops.__path__, "afx_ops.")]
for name in names:
    importlib.import_module(name)
print(json.dumps({"imported": names, "loaded": sorted(sys.modules)}))
"""


def _run(code: str, env) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                          timeout=60, env=env)


def test_importing_every_module_does_not_load_agentic_fx(child_env):
    done = _run(_WALK, child_env)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert {"afx_ops.cli", "afx_ops.server_auth", "afx_ops.server_auth_probe",
            "afx_ops.gate"} <= set(result["imported"])
    assert [n for n in result["loaded"] if n == "agentic_fx" or n.startswith("agentic_fx.")] == []


def test_agentic_fx_is_not_installed_in_this_venv(child_env):
    done = _run("import importlib.util; print(importlib.util.find_spec('agentic_fx') is None)",
                child_env)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "True"


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in ("import_module", "__import__") and node.args and isinstance(
                    node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names = [node.args[0].value]
        for name in names:
            if name == "agentic_fx" or name.startswith("agentic_fx."):
                found.append(f"{path}:{node.lineno} {name}")
    return found


def test_no_source_file_imports_agentic_fx():
    files = [p for p in AFX_OPS.rglob("*.py")
             if ".venv" not in p.relative_to(AFX_OPS).parts]
    assert len(files) >= 8
    assert [v for path in files for v in _violations(path)] == []


def test_the_ast_scan_detects_each_import_form(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("import agentic_fx.ops\nfrom agentic_fx import store\n"
                      "import importlib\nimportlib.import_module('agentic_fx.x')\n"
                      "__import__('agentic_fx')\nimport json\n")
    assert len(_violations(sample)) == 4


def test_pyproject_has_no_dependency_on_the_main_package():
    data = tomllib.loads((AFX_OPS / "pyproject.toml").read_text(encoding="utf-8"))
    declared = list(data["project"].get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        declared += [item for item in group if isinstance(item, str)]
    for extra in data["project"].get("optional-dependencies", {}).values():
        declared += extra
    assert not [d for d in declared if d.lower().replace("_", "-").startswith("agentic-fx")]
    uv = data.get("tool", {}).get("uv", {})
    assert "agentic-fx" not in uv.get("sources", {})
    assert "workspace" not in uv
    assert "path" not in str(uv.get("sources", {}))
