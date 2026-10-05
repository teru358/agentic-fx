"""plugin worker の隔離 (admission・Landlock・seccomp・二段 protocol) を無効化・緩和する
設定口が、config schema・settings.yaml.example・環境変数・CLI flag のどこにも無いこと。"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from agentic_fx.backtest import cli as backtest_cli
from agentic_fx.config import Settings, load_settings

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config" / "settings.yaml.example"
SRC = REPO / "src" / "agentic_fx"

# 隔離に関わる名前。`sandbox_` で始まる既存の資源上限 (cpu_sec・memory_mb 等) は
# 隔離の有無を切り替えないので対象外にし、切替を思わせる語だけを拒否する
_ISOLATION_NAME = re.compile(
    r"landlock|seccomp|isolat|admission|attest|selftest|fingerprint|two_stage|"
    r"unsandbox|no_sandbox|sandbox_(enable|disable|off|on|mode|bypass|skip|strict)|"
    r"insecure|unsafe|allow_unisolated|skip_verif", re.IGNORECASE)


def _schema_keys(model: type[BaseModel], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for name, field in model.model_fields.items():
        dotted = f"{prefix}{name}"
        keys.append(dotted)
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            keys += _schema_keys(ann, dotted + ".")
    return keys


def _yaml_keys(node, prefix: str = "") -> list[str]:
    keys: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            keys.append(f"{prefix}{k}")
            keys += _yaml_keys(v, f"{prefix}{k}.")
    return keys


def _cli_flags() -> list[str]:
    parser = argparse.ArgumentParser(prog="afx")
    parser.add_argument("--daemon", action="store_true")
    backtest_cli.register_subparsers(parser.add_subparsers(dest="command"))
    flags: list[str] = []

    def walk(p: argparse.ArgumentParser) -> None:
        for action in p._actions:
            flags.extend(action.option_strings)
            if action.dest and not action.option_strings:
                flags.append(action.dest)
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    flags.append(name)
                    walk(sub)

    walk(parser)
    return flags


def test_config_schema_has_no_isolation_switch():
    keys = _schema_keys(Settings)
    assert len(keys) > 50
    assert [k for k in keys if _ISOLATION_NAME.search(k)] == []


def test_settings_example_has_no_isolation_switch():
    keys = _yaml_keys(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    assert keys
    assert [k for k in keys if _ISOLATION_NAME.search(k)] == []


def test_no_environment_variable_read_by_the_code_switches_isolation():
    pattern = re.compile(r"""(?:environ\.get|getenv|environ\[)\(?\s*["']([A-Za-z0-9_]+)""")
    names = {m.group(1) for path in SRC.rglob("*.py")
             for m in pattern.finditer(path.read_text(encoding="utf-8"))}
    assert names
    assert sorted(n for n in names if _ISOLATION_NAME.search(n)) == []


def test_no_cli_flag_of_afx_or_backtest_switches_isolation():
    flags = _cli_flags()
    assert "--plugin" in flags
    assert [f for f in flags if _ISOLATION_NAME.search(f)] == []


@pytest.mark.parametrize("extra", [
    "landlock: false\n", "plugin:\n  sandbox_enabled: false\n",
    "plugin:\n  seccomp: off\n"])
def test_unknown_isolation_keys_leave_the_isolation_on(tmp_path, monkeypatch, extra):
    """未知 key を与えても、読み込みが拒否するか、読み込めても admission は効いたまま。"""
    from agentic_fx.core import runtime_fingerprint
    from agentic_fx.plugin import sandbox
    from agentic_fx.plugin.loader import PluginMeta, content_hash

    text = EXAMPLE.read_text(encoding="utf-8")
    if extra.startswith("plugin:"):
        text = text.replace("\nplugin:\n", "\n" + extra, 1)
    else:
        text += extra
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    try:
        settings = load_settings(path)
    except Exception:  # noqa: BLE001  拒否されるなら隔離は切れていない
        return
    monkeypatch.setattr(sandbox, "_RUNTIME_ADMISSION", runtime_fingerprint.RuntimeAdmission(
        selftest=lambda: runtime_fingerprint.SelftestOutcome(False, "x"), supported=()))
    monkeypatch.setattr(sandbox, "_ADMISSION_RESULT", None)
    d = tmp_path / "p"
    d.mkdir()
    (d / "plugin.py").write_text("def compute(df, params):\n    return {}\n")
    (d / "config.yaml").write_text("kind: indicator\n")
    meta = PluginMeta(name="p", kind="indicator", path=d, params={}, timeframe=None,
                      pairs=(), max_bars=10, content_hash=content_hash(d))
    with pytest.raises(sandbox.SandboxError) as error:
        sandbox.PluginSession(meta, settings=settings.plugin).__enter__()
    assert error.value.code == "sandbox_unavailable"
