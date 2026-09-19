"""[switch-ops-hardening] 受入テスト (設計書 `docs/superpowers/specs/2026-09-19-switch-ops-hardening-design.md`)。

**実 DB (`data/agentic.db`) と実 `plugins/` には一切触れない** — 全て `tmp_path` 配下
([[tests-touching-real-repo-resources]])。故障注入はモジュール属性の monkeypatch
(`tests/plugin/test_reconcile.py` と同じ流儀)。
"""
from __future__ import annotations

import ast
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.entry import main
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store.state import StateStore

_REPO = Path(__file__).resolve().parents[2]
EXAMPLES = _REPO / "docs" / "examples" / "plugins"
SETTINGS = load_settings(_REPO / "config" / "settings.yaml.example")
NOW = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
_INJECTED = "injected: switch_live failed"
_DB = "data/agentic.db"


# ---------------------------------------------------------------- helpers


def _cli_env(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO / "config" / "settings.yaml.example",
                tmp_path / "config" / "settings.yaml")
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "plugins" / "_human").mkdir(parents=True, exist_ok=True)
    StateStore(tmp_path / "data" / "state" / "app_state.json").update(initialized=True)
    conn = db_store.connect(tmp_path / _DB)
    db_store.init_db(conn)
    conn.close()
    return tmp_path


def _fail_switch_live(monkeypatch, *, times: int):
    real = plugin_switch.switch_live
    calls = {"n": 0}

    def _fail(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= times:
            raise OSError(_INJECTED)
        return real(*a, **kw)

    monkeypatch.setattr(plugin_switch, "switch_live", _fail)
    return real


def _stopped_at_switched(tmp_path, monkeypatch, *, times: int = 1):
    """journal=`switched` / live 未切替 / approval `pending` を作る。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    real = _fail_switch_live(monkeypatch, times=times)
    main(["plugin", "bless", "sma", "--from", "_human"])
    if times == 1:
        monkeypatch.setattr(plugin_switch, "switch_live", real)
    conn = db_store.connect(root / _DB)
    row = journal_store.get_open_by_name(conn, "sma")
    assert row is not None and row["phase"] == "switched", "前提: switched で停止"
    assert not (root / "plugins" / "sma").exists(), "前提: live は未切替"
    return root, conn, row


def _rows(conn):
    return [(r["op_id"], r["phase"]) for r in conn.execute(
        "SELECT op_id, phase FROM plugin_switch_journal ORDER BY op_id")]


def _status(conn, approval_id):
    return conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id,)).fetchone()["status"]


def _activity_text(root) -> str:
    p = root / "logs" / "activity.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _make_foreign(root, row):
    plugins_root = root / "plugins"
    other = plugins_root / ".versions" / "sma" / ("f" * 64)
    other.mkdir(parents=True, exist_ok=True)
    tmp_link = plugins_root / ".sma.foreign"
    tmp_link.symlink_to(f".versions/sma/{'f' * 64}")
    os.rename(tmp_link, plugins_root / "sma")


def _activity(root):
    from agentic_fx.activity import ActivityLog
    return ActivityLog(root / "logs" / "activity.log")


def _walk_own_body(fn):
    """`fn` の本体を走査する。**入れ子の関数定義の中には入らない**。"""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        for child in ast.iter_child_nodes(node):
            stack.append(child)


def test_ac5_classifier_compares_the_raw_readlink_string(tmp_path):
    """AC-5: 分類は `readlink` の**生文字列**で行う (`resolve()` ではない)。

    同じ実体を指すが綴りが違う symlink (**絶対パス**で張られたもの) は
    **`foreign`** — 正規の書き手 (`switch_live`) は必ず plugins_root 相対の
    `.versions/<name>/<hash>` を書くので、綴りが違えば第三者が触った印として
    fail closed に扱う。比較を `resolve()` にすると `switched` に化けて red。
    (`./` 付きの綴りでは試験にならない — `Path.as_posix()` が単一ドットを
    畳んでしまい、生文字列でも一致してしまう。実測して絶対パスに変えた。)"""
    plugins_root = tmp_path / "plugins"
    target = f".versions/sma/{'a' * 64}"
    (plugins_root / target).mkdir(parents=True)
    (plugins_root / "sma").symlink_to((plugins_root / target).resolve())
    row = {"op_id": 1, "name": "sma", "phase": "switched", "switch_required": 1,
           "new_target": target, "old_target": None, "old_kind": "absent"}

    assert plugin_switch.classify_live(plugins_root, row) == "foreign"

    # 正規の綴りなら `switched`
    (plugins_root / "sma").unlink()
    (plugins_root / "sma").symlink_to(target)
    assert plugin_switch.classify_live(plugins_root, row) == "switched"


def test_classifier_refuses_rows_it_does_not_apply_to(tmp_path):
    """分類器は `phase='switched'` かつ `switch_required=1` の行専用 (fail closed)。"""
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    base = {"op_id": 1, "name": "sma", "new_target": ".versions/sma/x",
            "old_target": None, "old_kind": "absent"}
    for phase, required in (("recorded", 1), ("switched", 0)):
        with pytest.raises(ValueError, match="分類器は"):
            plugin_switch.classify_live(
                plugins_root, {**base, "phase": phase, "switch_required": required})
