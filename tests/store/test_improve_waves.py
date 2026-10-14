"""improve_waves / improve_wave_slots CRUD (設計書 §3.1)。

**Task 8 が保証する範囲**: wave+slot の 1 tx 作成、slot の CAS 遷移
(reserved->claimed->running->done|failed)、pre-ready 失敗時の
claimed->reserved 巻き戻し (spawn_attempts 管理)、終端直積の SQL。
`go` の実プロトコル (WorkerRunner との handshake) は Task 9 が別途組む。
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_fx.store import improve_waves, missions
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)  # Sat 03:00


_WAVE_NAMES = ("improve_waves", "improve_wave_slots")
_WAVES_MODULE = "agentic_fx.store.improve_waves"


_UNKNOWN = "\0"


def _fold_string(node: ast.AST) -> str | None:
    """Statically fold constant string pieces (`+` chains, f-string constant
    parts). Non-constant pieces become a separator so they never join text."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold_string(node.left), _fold_string(node.right)
        if left is None and right is None:
            return None
        return (_UNKNOWN if left is None else left) + (
            _UNKNOWN if right is None else right)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            folded = (_fold_string(value.value)
                      if isinstance(value, ast.FormattedValue)
                      else _fold_string(value))
            parts.append(_UNKNOWN if folded is None else folded)
        return "".join(parts)
    return None


def _table_references(path: Path) -> set[str]:
    """Return wave-table names found in any string constant, constant
    concatenation or f-string, wherever the string is used; docstrings are
    prose and skipped."""
    tree = ast.parse(path.read_text())
    docstrings = {
        id(n.body[0].value) for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef))
        and n.body and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)}
    references: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr))
                and id(node) not in docstrings):
            text = _fold_string(node)
            if text is not None:
                references.update(n for n in _WAVE_NAMES if n in text)
    return references


def _waves_module_aliases(tree: ast.AST) -> set[str]:
    """Names bound to the improve_waves module by any import form."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "agentic_fx.store":
                aliases.update(a.asname or a.name for a in node.names
                               if a.name == "improve_waves")
            elif node.module == _WAVES_MODULE:
                aliases.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            aliases.update(a.asname or a.name for a in node.names
                           if a.name == _WAVES_MODULE and a.asname)
    return aliases


def _improve_waves_calls(path: Path) -> set[str]:
    """Names used from the improve_waves module: attribute access on a module
    alias, a full dotted path, or a name imported from the module."""
    tree = ast.parse(path.read_text())
    aliases = _waves_module_aliases(tree)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _WAVES_MODULE:
            used.update(a.name for a in node.names)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in aliases:
                used.add(node.attr)
            elif ast.unparse(node.value) == _WAVES_MODULE:
                used.add(node.attr)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in aliases):
            name = _fold_string(node.args[1])
            if name is not None and _UNKNOWN not in name:
                used.add(name)
    return used


def test_wave_table_readers_are_limited_to_the_consumption_allowlist():
    """消費印を読む経路が、明示的に検討した経路だけに留まることを固定する。"""
    src = Path(__file__).parents[2] / "src" / "agentic_fx"
    direct_references = {
        path.relative_to(src).as_posix(): _table_references(path)
        for path in src.rglob("*.py")
        if _table_references(path)
    }
    assert direct_references == {
        "core/improve_supervisor.py": {"improve_wave_slots"},
        "store/db.py": {"improve_waves", "improve_wave_slots"},
        "store/improve_waves.py": {"improve_waves", "improve_wave_slots"},
    }, (
        "新しい wave table 参照を検出しました。消費印 (expected=0) の扱いを確認して"
        "許可リストに足す"
    )

    consumers = {
        path.relative_to(src).as_posix(): _improve_waves_calls(path)
        for path in src.rglob("*.py")
        if _improve_waves_calls(path)
    }
    assert consumers == {
        "core/improve_supervisor.py": {
            "create_wave_and_slots", "mark_running", "mark_slot_failed",
            "revert_to_reserved",
        },
        "loops/improve_loop.py": {"claim_slot", "list_slots"},
        "setup/wizard.py": {"consume_scheduled_period"},
        "store/missions.py": {"mark_terminal", "recover_stale_slots"},
    }, (
        "新しい wave table 消費先を検出しました。消費印 (expected=0) の扱いを確認して"
        "許可リストに足す"
    )


def test_wave_detectors_see_concatenated_sql_and_getattr_access(tmp_path):
    """許可リスト検出器が、定数連結の SQL と module alias への getattr を見逃さない。"""
    sources = {
        "concat.py": 'q = "SELECT * FROM " + "improve_" + "waves"\n',
        "fstring.py": 'q = f"SELECT * FROM improve_{1}wave_slots"\n'
                      'r = f"x improve_" f"wave_slots"\n',
        "alias_getattr.py":
            "import agentic_fx.store.improve_waves as iw\n"
            'f = getattr(iw, "claim_slot")\n',
        "from_getattr.py":
            "from agentic_fx.store import improve_waves as iw\n"
            'f = getattr(iw, "list_" + "slots")\n',
        "other_getattr.py":
            "import os\n"
            'f = getattr(os, "improve_waves_x")\n',
        "clean.py": 'q = "improve_" + name\n',
    }
    for name, text in sources.items():
        (tmp_path / name).write_text(text)

    assert _table_references(tmp_path / "concat.py") == {"improve_waves"}
    assert _table_references(tmp_path / "fstring.py") == {"improve_wave_slots"}
    assert _table_references(tmp_path / "clean.py") == set()
    assert _improve_waves_calls(tmp_path / "alias_getattr.py") == {"claim_slot"}
    assert _improve_waves_calls(tmp_path / "from_getattr.py") == {"list_slots"}
    assert _improve_waves_calls(tmp_path / "other_getattr.py") == set()


def _mission(c):
    """裁定 D1: `improve_wave_slots.mission_id` に FK が付いたため、
    実在する missions 行の id を使う必要がある。"""
    return missions.start(c, "improve", "local", "m", now=NOW)


def test_create_wave_and_slots_in_one_tx_rowcount_one_means_authority(tmp_path):
    """1 つの短い tx で wave 行 + slot 行 (reserved) を作る。
    `INSERT OR IGNORE` の rowcount=1 が起動権 (period 消費の証)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    created = improve_waves.create_wave_and_slots(
        c, period_key="2026-W34", now=NOW, expected=2)
    assert created is True
    slots = improve_waves.list_slots(c, period_key="2026-W34")
    assert [s["k"] for s in slots] == [0, 1]
    assert all(s["status"] == "reserved" for s in slots)


def test_create_wave_and_slots_second_call_is_noop_period_already_consumed(tmp_path):
    """同一 period_key への 2 回目の呼び出しは False (wave 行が既に在る =
    period 消費済み。実行 0 件でも次 tick で再 CAS しない)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=2)
    second = improve_waves.create_wave_and_slots(
        c, period_key="2026-W34", now=NOW, expected=4)  # M が違っても無視
    assert second is False
    slots = improve_waves.list_slots(c, period_key="2026-W34")
    assert len(slots) == 2  # 最初の M=2 のまま


def test_create_wave_and_slots_m_zero_writes_nothing(tmp_path):
    """M=0 なら何も書かない (period 非消費、次 tick で再試行)。消費印は
    別関数 `consume_scheduled_period` だけが書く。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    created = improve_waves.create_wave_and_slots(
        c, period_key="2026-W34", now=NOW, expected=0)
    assert created is False
    row = c.execute("SELECT 1 FROM improve_waves WHERE period_key=?",
                    ("2026-W34",)).fetchone()
    assert row is None  # wave 行すら作らない


def _weekly_settings():
    return SimpleNamespace(
        schedule=SimpleNamespace(improve="weekly", improve_at="Sat 03:00"),
        display_timezone="UTC")


def test_consume_scheduled_period_creates_empty_wave_once_and_recovery_ignores_it(tmp_path):
    """初回設定の消費印は slot を作らず、起動時回復の失敗履歴にもならない。
    期キーは渡した now から導かれ、2 回目は inserted=False。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    settings = _weekly_settings()

    first = improve_waves.consume_scheduled_period(c, settings, now=NOW)
    second = improve_waves.consume_scheduled_period(c, settings, now=NOW)

    assert first == improve_waves.ConsumedPeriod("2026-W34", True)
    assert second == improve_waves.ConsumedPeriod("2026-W34", False)
    assert dict(c.execute(
        "SELECT expected FROM improve_waves WHERE period_key=?", ("2026-W34",)
    ).fetchone()) == {"expected": 0}
    assert improve_waves.list_slots(c, period_key="2026-W34") == []
    assert improve_waves.recover_stale_slots(c, now=NOW) == 0


def test_consume_scheduled_period_keys_the_mark_by_the_now_it_is_given(tmp_path):
    """週境界をまたぐ 2 つの now は別の期に印を付ける (期キーは now から導く)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    settings = _weekly_settings()
    before = datetime(2026, 8, 22, 2, 59, tzinfo=timezone.utc)

    assert improve_waves.consume_scheduled_period(
        c, settings, now=before).period_key == "2026-W33"
    assert improve_waves.consume_scheduled_period(
        c, settings, now=NOW).period_key == "2026-W34"


def test_consume_scheduled_period_reports_existing_normal_wave_as_not_inserted(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=2)

    result = improve_waves.consume_scheduled_period(c, _weekly_settings(), now=NOW)

    assert result == improve_waves.ConsumedPeriod("2026-W34", False)
    assert len(improve_waves.list_slots(c, period_key="2026-W34")) == 2


def test_claim_slot_cas_reserved_to_claimed_with_mission_id(tmp_path):
    """Tx-0 と同じ tx で呼ばれる CAS: reserved -> claimed。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    mid = _mission(c)
    ok = improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=mid, now=NOW)
    assert ok is True
    slot = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot["status"] == "claimed"
    assert slot["mission_id"] == mid
    assert slot["spawn_attempts"] == 1


def test_claim_slot_cas_fails_on_non_reserved(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    ok = improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    assert ok is False


def test_mark_running_cas_claimed_to_running(tmp_path):
    """`ready` 受信後、親が短い tx で claimed->running を commit する。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    ok = improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    assert ok is True
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "running"


def test_revert_to_reserved_on_pre_ready_failure_clears_mission_id(tmp_path):
    """§3.1⑥・codex 12周目I3: 戻すときは同じ tx で mission_id=NULL も戻す。
    再試行上限の判定は Task 9 が行う (`revert_to_reserved` は無条件遷移)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.revert_to_reserved(c, period_key="2026-W34", k=0, now=NOW)
    slot = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot["status"] == "reserved"
    assert slot["mission_id"] is None
    assert slot["spawn_attempts"] == 1  # claim 時の +1 のみ (revert 自体は増やさない)


def test_revert_to_reserved_fails_on_non_claimed(tmp_path):
    """CAS: claimed 以外からは遷移しない (rowcount=0)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    ok = improve_waves.revert_to_reserved(c, period_key="2026-W34", k=0, now=NOW)
    assert ok is False
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "reserved"


def test_mark_slot_failed_after_attempts_exhausted(tmp_path):
    """Task 9 が spawn_attempts >= max と判定した後に呼ぶ想定の failed 遷移。
    claimed からでも running からでも failed へ落とし mission_id を NULL に戻す。
    **裁定 D2 (2026-08-24)**: `reserved`/`claimed`/`running` からの CAS
    (以前の「無条件」ではない — `test_mark_slot_failed_does_not_overwrite_done_slot`
    参照)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    m1, m2 = _mission(c), _mission(c)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=m1, now=NOW)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=m2, now=NOW)  # 失敗 (reserved でない) — 無視
    improve_waves.revert_to_reserved(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=m2, now=NOW)
    slot_before_fail = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot_before_fail["spawn_attempts"] == 2  # L08: 定数化変異 (=1) の pin
    ok = improve_waves.mark_slot_failed(c, period_key="2026-W34", k=0, now=NOW)
    assert ok is True
    slot = improve_waves.get_slot(c, period_key="2026-W34", k=0)
    assert slot["status"] == "failed"
    assert slot["mission_id"] is None


def test_mark_slot_failed_does_not_overwrite_done_slot(tmp_path):
    """裁定 D2 (2026-08-24) killer: 終端済み (`done`) の slot に
    `mark_slot_failed` を当てても上書きされない (CAS `False`)。
    以前の「無条件」実装なら `done -> failed` に書き換わっていた。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=0, status="done", now=NOW)
    ok = improve_waves.mark_slot_failed(c, period_key="2026-W34", k=0, now=NOW)
    assert ok is False
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "done"


def test_count_open_slots_counts_reserved_only(tmp_path):
    """`count_open_slots` は `reserved` の数を返す (claimed/running/done/failed は含まない)。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=3)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    assert improve_waves.count_open_slots(c, period_key="2026-W34") == 2
    improve_waves.claim_slot(c, period_key="2026-W34", k=1, mission_id=_mission(c), now=NOW)
    assert improve_waves.count_open_slots(c, period_key="2026-W34") == 1


def test_mark_terminal_running_to_done(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=0, status="done", now=NOW)
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "done"


# precheck 2026-08-22: T8-m6 — parametrize が本体で未使用だった (2 回とも同じ検査)。
# `terminal_status` を実際に使い、有効な終端状態を経由してから無効値を渡す形に直す。
@pytest.mark.parametrize("terminal_status", ["done", "failed"])
def test_mark_terminal_rejects_invalid_status(tmp_path, terminal_status):
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=1)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=0,
                                status=terminal_status, now=NOW)
    with pytest.raises(ValueError):
        improve_waves.mark_terminal(c, period_key="2026-W34", k=0,
                                    status="not_a_status", now=NOW)


def test_recover_stale_slots_fails_reserved_claimed_running(tmp_path):
    """§8.1-20: 再起動時に reserved (mission_id IS NULL を assert)・claimed・
    running を全て failed へ収束する SQL。手動 one-shot (slot 無し) は対象外。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=3)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)  # claimed
    improve_waves.claim_slot(c, period_key="2026-W34", k=1, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=1, now=NOW)                # running
    # k=2 は reserved のまま (mission_id IS NULL)

    n = improve_waves.recover_stale_slots(c, now=NOW)
    assert n == 3
    for k in (0, 1, 2):
        slot = improve_waves.get_slot(c, period_key="2026-W34", k=k)
        assert slot["status"] == "failed"


def test_recover_stale_slots_leaves_done_and_failed_untouched(tmp_path):
    """L-B3 是正 (束D検収, verified-local-round1.md §11 #21): テスト名は
    「done と failed の両方を untouched のまま残す」を謳うが、本体は
    `done` しか作っていなかった。`failed` slot を追加で作り、両方が
    `recover_stale_slots` の対象外のまま残ることを実測する。"""
    c = connect(tmp_path / "t.db"); init_db(c)
    improve_waves.create_wave_and_slots(c, period_key="2026-W34", now=NOW, expected=2)
    improve_waves.claim_slot(c, period_key="2026-W34", k=0, mission_id=_mission(c), now=NOW)
    improve_waves.mark_running(c, period_key="2026-W34", k=0, now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=0, status="done", now=NOW)
    improve_waves.claim_slot(c, period_key="2026-W34", k=1, mission_id=_mission(c), now=NOW)
    improve_waves.mark_terminal(c, period_key="2026-W34", k=1, status="failed", now=NOW)
    n = improve_waves.recover_stale_slots(c, now=NOW)
    assert n == 0
    assert improve_waves.get_slot(c, period_key="2026-W34", k=0)["status"] == "done"
    assert improve_waves.get_slot(c, period_key="2026-W34", k=1)["status"] == "failed"
