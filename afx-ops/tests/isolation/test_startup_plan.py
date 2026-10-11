"""機能ごとの起動 plan は固定で、production の起動口から隔離境界を広げられないこと。"""
from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

import afx_ops
from afx_ops import api_client
from afx_ops.config import OpsConfig
from afx_ops.isolation import startup
from afx_ops.isolation.startup import PLANS, StartupPlan
from _keydir import build_key_dir
from _test_plans import check_feature_only, test_plan

# plan の検査で止まる経路では設定の中身は使われない
_UNUSED_CONFIG = OpsConfig(instance_root=Path("/nonexistent/root"),
                           key_dir=Path("/nonexistent/keys"), config_dir_fd=None)

PACKAGE = Path(afx_ops.__file__).resolve().parent


def test_plans_are_fixed_per_feature():
    assert set(PLANS) == {"bot", "web", "notify"}
    assert PLANS["bot"].connect_ports == (443,)
    assert PLANS["notify"].connect_ports == (443,)
    assert PLANS["web"].connect_ports == ()
    assert [f for f, p in PLANS.items() if p.listener] == ["web"]
    with pytest.raises(TypeError):
        PLANS["bot"] = PLANS["web"]  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        PLANS["bot"].connect_ports = (443, 80)  # type: ignore[misc]


def test_for_feature_takes_no_boundary_arguments():
    params = set(inspect.signature(StartupPlan.for_feature).parameters)
    assert params == {"feature", "web_port"}


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
def test_for_feature_uses_the_fixed_plan_and_xdg_dirs(feature, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    plan = StartupPlan.for_feature(feature)
    assert plan.connect_ports == PLANS[feature].connect_ports
    assert plan.secret_files == PLANS[feature].secret_files
    assert plan.check_unit and plan.require_notify
    assert plan.config_dir == tmp_path / "cfg" / "agentic-fx" / "ops" / feature / "config"
    assert plan.state_dir == tmp_path / "st" / "agentic-fx" / "ops" / feature
    dirs = startup.feature_dirs(feature)
    assert dirs.secret == tmp_path / "cfg" / "agentic-fx" / "ops" / feature / "secret"
    assert plan.secret_dir == dirs.secret
    # secret は設定の Landlock 許可 (config) の外にある
    assert dirs.config not in dirs.secret.parents and dirs.secret not in dirs.config.parents
    assert startup.check_plan(plan) is None
    # 何も作らない
    assert not (tmp_path / "cfg").exists() and not (tmp_path / "st").exists()


def test_xdg_defaults_follow_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    dirs = startup.feature_dirs("bot")
    assert dirs.config == tmp_path / ".config" / "agentic-fx" / "ops" / "bot" / "config"
    assert dirs.state == tmp_path / ".local" / "state" / "agentic-fx" / "ops" / "bot"
    # 相対 path の XDG 変数は仕様どおり無視する
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    assert startup.feature_dirs("bot").state == tmp_path / ".local" / "state" / "agentic-fx" / \
        "ops" / "bot"


@pytest.mark.parametrize("change", [
    {"connect_ports": (443, 22)},
    {"connect_ports": ()},
    {"check_unit": False},
    {"require_notify": False},
    {"config_dir": Path("/")},
    {"state_dir": Path("/tmp")},
    {"secret_dir": Path("/tmp")},
    {"secret_files": ("discord.token", "extra")},
    {"secret_files": ()},
    {"feature": "web"},
])
def test_a_widened_production_plan_is_rejected(change, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    plan = dataclasses.replace(StartupPlan.for_feature("bot"), **change)
    with pytest.raises(startup.StartupError) as info:
        startup.check_plan(plan)
    assert info.value.reason == "plan_invalid"


def test_the_plan_type_carries_no_test_only_field():
    names = {f.name for f in dataclasses.fields(StartupPlan)}
    assert names == {"feature", "config_dir", "secret_dir", "secret_files", "state_dir",
                     "web_port", "connect_ports", "check_unit", "require_notify"}
    assert not hasattr(StartupPlan, "_for_tests")


def test_a_loose_plan_given_to_the_production_run_is_plan_invalid(tmp_path, capsys):
    plan = test_plan("bot", connect_ports=(8443,),
                     config_dir=tmp_path / "c", state_dir=tmp_path / "s")
    called = []
    rc = startup.run(plan, _UNUSED_CONFIG, lambda iso: called.append(iso) or 0)
    assert rc == startup.RC_ISOLATION_FAILED
    assert capsys.readouterr().err == "afx-ops: plan_invalid\n"
    assert called == []
    # 試験の検査を注入したときだけ同じ plan が検査を通る
    assert check_feature_only(plan) is None


@pytest.mark.parametrize(("owner", "other"), [("bot", "web"), ("bot", "notify"),
                                             ("notify", "bot"), ("web", "bot")])
def test_a_plan_reading_another_features_secret_is_plan_invalid_in_the_production_run(
        owner, other, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    theirs = startup.feature_dirs(other).secret
    theirs.mkdir(parents=True, mode=0o700)
    for name in PLANS[other].secret_files:
        (theirs / name).write_text("theirs")
        (theirs / name).chmod(0o600)
    read = []
    monkeypatch.setattr(startup, "read_secrets", lambda plan: read.append(plan) or {})
    for change in ({"secret_dir": theirs},
                   {"secret_dir": theirs, "secret_files": PLANS[other].secret_files}):
        plan = dataclasses.replace(StartupPlan.for_feature(owner), **change)
        called = []
        rc = startup.run(plan, _UNUSED_CONFIG, lambda iso: called.append(iso) or 0)
        assert rc == startup.RC_ISOLATION_FAILED
        assert capsys.readouterr().err == "afx-ops: plan_invalid\n"
        assert called == [] and read == []


def _under(a: Path, b: Path) -> bool:
    return a == b or b in a.parents


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
def test_no_secret_dir_is_inside_or_above_a_landlock_rule(feature, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    plan = StartupPlan.for_feature(feature, web_port=0)
    rules = startup.landlock_plan(plan, "/proc/1",
                                  startup.PreparedDirs(config_fd=-1, state_fd=-1)).path_rules
    secrets = [startup.feature_dirs(f).secret for f in PLANS]
    bad = [(r.path, str(s)) for r in rules for s in secrets
           if _under(s, Path(r.path)) or _under(Path(r.path), s)]
    assert bad == []
    # config / state の規則は在るので、上の判定は空振りではない
    assert {Path(r.path) for r in rules} >= {plan.config_dir, plan.state_dir}


def test_run_and_isolate_check_the_plan_with_the_production_check_by_default():
    for func in (startup.run, startup.isolate):
        param = inspect.signature(func).parameters["plan_check"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is startup.check_plan
    # server 認証の待ちも試験だけが差し替え、既定は鍵を送らない本物の待ち
    for func in (startup.run, startup.announce_ready):
        param = inspect.signature(func).parameters["wait"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is api_client.wait_ready


# --- production の起動口から test 用の組立てに届かないこと (AST) ------------------------

def _calls(tree: ast.AST) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_for_tests":
            found.append((node.lineno, "_for_tests"))
        if isinstance(node, ast.Name) and node.id == "_for_tests":
            found.append((node.lineno, "_for_tests"))
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in ("StartupPlan", "FeaturePlan", "__new__"):
                found.append((node.lineno, name))
            # dataclasses.replace だけ (os.replace / str.replace は対象外)
            if (isinstance(fn, ast.Attribute) and fn.attr == "replace"
                    and isinstance(fn.value, ast.Name) and fn.value.id == "dataclasses"):
                found.append((node.lineno, "replace"))
        if isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
            found += [(node.lineno, f"import {a.name}") for a in node.names
                      if a.name == "replace"]
        if isinstance(node, ast.alias) and node.name == "_for_tests":
            found.append((0, "import _for_tests"))
        # plan 検査・認証待ちの差し替え (試験だけが使う) は production の呼出しに現れない
        if isinstance(node, ast.Call):
            for k in node.keywords:
                if k.arg in _SEAMS:
                    found.append((node.lineno, f"{k.arg}="))
        # `run(plan, body, **extra)` のような展開は中身を静的に確かめられないので、起動の
        # 入口 (run / isolate) への展開そのものを拒む
        if isinstance(node, ast.Call) and _callee(node) in ("run", "isolate", "announce_ready") and any(
                k.arg is None for k in node.keywords):
            found.append((node.lineno, "**"))
        # 展開に使う dict の key に差し替え口の名前を書いたもの
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and k.value in _SEAMS:
                    found.append((node.lineno, f"{k.value} key"))
    return found


_SEAMS = ("plan_check", "wait")


def _callee(node: ast.Call) -> str:
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")


def _package_violations() -> list[tuple[str, int, str]]:
    out = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, name in _calls(tree):
            out.append((rel, line, name))
    return out


def _allowed_in_startup(tree: ast.Module) -> set[int]:
    """startup.py の中で StartupPlan / FeaturePlan を組み立ててよい行 (plan の定義と PLANS) と、
    `run` が受けた plan 検査を `isolate` へ、認証待ちを `announce_ready` へそのまま渡す行、
    および body 用 config から fd を消す置換を許可する。"""
    lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "StartupPlan":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "for_feature":
                    lines |= set(range(item.lineno, item.end_lineno + 1))
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id == "PLANS":
                lines |= set(range(node.lineno, node.end_lineno + 1))
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            for call in ast.walk(node):
                kws = ([(k.arg, getattr(k.value, "id", None)) for k in call.keywords]
                       if isinstance(call, ast.Call) else None)
                if (getattr(getattr(call, "func", None), "id", "") == "isolate"
                        and kws == [("plan_check", "plan_check"),
                                    ("release_config_dir", None)]):
                    lines.add(call.lineno)
                if (getattr(getattr(call, "func", None), "id", "") == "announce_ready"
                        and kws == [("wait", "wait")]):
                    lines.add(call.lineno)
        if isinstance(node, ast.FunctionDef) and node.name == "isolate":
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "dataclasses" and call.func.attr == "replace"
                        and len(call.args) == 1 and isinstance(call.args[0], ast.Name)
                        and call.args[0].id == "config"
                        and [(key.arg, getattr(key.value, "value", None)) for key in call.keywords]
                        == [("config_dir_fd", None)]):
                    lines.add(call.lineno)
    return lines


def test_production_code_never_builds_a_plan_outside_for_feature():
    startup_tree = ast.parse((PACKAGE / "isolation" / "startup.py").read_text(encoding="utf-8"))
    allowed = _allowed_in_startup(startup_tree)
    bad = [(rel, line, name) for rel, line, name in _package_violations()
           if not (rel == "isolation/startup.py" and line in allowed and name != "_for_tests")
           and rel != "api_client.py"]
    assert bad == []


def test_production_client_paths_do_not_call_test_key_or_token_seams():
    tree = ast.parse((PACKAGE / "isolation" / "startup.py").read_text(encoding="utf-8"))
    client_for = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "client_for")
    calls = [node.func.attr for node in ast.walk(client_for) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)]
    assert calls == ["from_token"]


def test_the_scan_detects_a_test_plan_in_production_code(tmp_path):
    sample = ast.parse("from afx_ops.isolation.startup import StartupPlan\n"
                       "p = StartupPlan._for_tests('bot', read_secrets=None)\n"
                       "q = StartupPlan(feature='bot')\n"
                       "import dataclasses, os\nr = dataclasses.replace(p, connect_ports=(22,))\n"
                       "os.replace('a', 'b')\n'x'.replace('x', 'y')\n"
                       "run(q, body, plan_check=lambda p: None)\n"
                       "run(q, config, body, wait=lambda e: None)\n")
    assert [name for _l, name in sorted(_calls(sample))] == ["_for_tests", "StartupPlan",
                                                             "replace", "plan_check=", "wait="]


@pytest.mark.parametrize(("source", "expected"), [
    ("startup.run(plan, body, **{'plan_check': bypass})\n", ["**", "plan_check key"]),
    ("run(plan, body, **extra)\n", ["**"]),
    ("startup.isolate(plan, **kwargs)\n", ["**"]),
    ("extra = {'plan_check': bypass}\n", ["plan_check key"]),
    ("extra = dict(plan_check=bypass)\n", ["plan_check="]),
    ("functools.partial(startup.run, plan_check=bypass)\n", ["plan_check="]),
    ("startup.run(plan, config, body, **{'wait': skip})\n", ["**", "wait key"]),
    ("startup.announce_ready(sock, endpoint, wait=skip)\n", ["wait="]),
    ("startup.announce_ready(sock, endpoint, **extra)\n", ["**"]),
])
def test_the_scan_detects_an_unpacked_plan_check(source, expected, tmp_path):
    # 一時 module として書き出したものを、package の走査と同じ関数で検査する
    module = tmp_path / "sample.py"
    module.write_text(source, encoding="utf-8")
    found = sorted(name for _l, name in _calls(ast.parse(module.read_text(encoding="utf-8"))))
    assert found == sorted(expected)


def test_the_scan_leaves_unpacking_into_other_callees_alone():
    # 名前が run / isolate の呼出しへの展開は、受け手を問わず拒む (受け手の解決は静的に
    # 確かめきれないので、production に要るときは名前で区別できる形に書く)
    assert _calls(ast.parse("print(*args, **kwargs)\nopen_thing(cmd, **opts)\n")) == []


def test_web_without_a_port_source_stops_at_the_listener_before_isolation(tmp_path,
                                                                         monkeypatch):
    # unit の外の判定 (NoNewPrivs 等) だけを外し、listener の段で止まることを見る
    monkeypatch.setattr(startup, "check_preconditions", lambda plan, proc: None)
    monkeypatch.delenv(startup.NOTIFY_ENV, raising=False)
    plan = test_plan("web", config_dir=tmp_path / "c",
                     state_dir=tmp_path / "s")
    (tmp_path / "c").mkdir(mode=0o755)
    assert plan.web_port is None
    config = OpsConfig(instance_root=tmp_path / "root", key_dir=build_key_dir(tmp_path / "keys"),
                       config_dir_fd=None)
    with pytest.raises(startup.StartupError) as info:
        startup.isolate(plan, config, plan_check=check_feature_only)
    assert info.value.reason == "listener_invalid"
    assert "no port source until web config" in str(info.value)
