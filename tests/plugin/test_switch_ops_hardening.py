"""[switch-ops-hardening] 受入テスト (設計書 `docs/superpowers/specs/2026-09-19-switch-ops-hardening-design.md`)。

**実 DB (`data/agentic.db`) と実 `plugins/` には一切触れない** — 全て `tmp_path` 配下
([[tests-touching-real-repo-resources]])。故障注入はモジュール属性の monkeypatch
(`tests/plugin/test_reconcile.py` と同じ流儀)。
"""
from __future__ import annotations

import ast
import os
import shutil
import stat
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


# ---------------------------------------------------------------- AC-1 / 6 / 7 / 8

@pytest.mark.slow
def test_ac1_retry_of_switched_but_unswitched_deploys(tmp_path, monkeypatch):
    """AC-1 / AC-6: 切替が失敗して journal だけ `switched` になった行を
    `approval retry` すると、**巻き戻して新しい行で配備まで完了する**。
    journal は `reverted` + `decided` の **2 行**、旧行は `reverted` のまま。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    live = plugins_root / "sma"
    assert live.is_symlink()
    assert live.readlink().as_posix().startswith(".versions/sma/")
    assert _status(conn, row["approval_id"]) == "approved"
    rows = _rows(conn)
    assert len(rows) == 2, f"停止行 + 新行の 2 行でなければならない: {rows}"
    assert rows[0] == (row["op_id"], "reverted")
    assert rows[1][1] == "decided" and rows[1][0] != row["op_id"]
    # outcome (T5)
    assert outcome.outcome == "deployed_after_rollback"
    assert outcome.rolled_back_op_id == row["op_id"]
    assert outcome.target == live.readlink().as_posix()
    assert outcome.status == "approved"
    # 段 0 pin (S0-80): `op_id=None` に潰す変異が SURVIVED した。
    # 設計書 §3.5: `deployed_after_rollback` の `op_id` は**新しい方**の行。
    assert outcome.op_id == rows[1][0]


@pytest.mark.slow
def test_ac2_reconcile_on_same_row_reverts_and_keeps_pending(tmp_path, monkeypatch):
    """AC-2: 同じ状態に起動時 reconcile を掛けると `reverted` + `pending`
    (**現行と同じ = 無人経路は前に進めない**)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"

    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS)

    assert _rows(conn) == [(row["op_id"], "reverted")]
    assert _status(conn, row["approval_id"]) == "pending"
    assert not (plugins_root / "sma").exists()


@pytest.mark.slow
def test_ac7_permanent_switch_failure_keeps_one_open_row(tmp_path, monkeypatch):
    """AC-7: `switch_live` が失敗し続けても、retry 1 回につき
    「`reverted` 1 本 + 新しい `switched` 1 本」で **非終端行は常に 1 本**。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch, times=99)
    plugins_root = root / "plugins"

    for expected_rows in (2, 3):
        with pytest.raises(OSError, match="injected"):
            plugin_switch.retry_approval(
                conn, row["approval_id"], decided_by="human", now=NOW,
                plugins_root=plugins_root, settings=SETTINGS)
        rows = _rows(conn)
        assert len(rows) == expected_rows
        assert sum(1 for _op, ph in rows if ph not in ("decided", "reverted")) == 1
        assert _status(conn, row["approval_id"]) == "pending"
        assert not (plugins_root / "sma").exists()


@pytest.mark.slow
def test_ac8_retry_after_success_is_noop(tmp_path, monkeypatch):
    """AC-8: 成功後にもう一度 retry しても何も起きない (`already_decided`)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    plugin_switch.retry_approval(conn, row["approval_id"], decided_by="human",
                                 now=NOW, plugins_root=plugins_root, settings=SETTINGS)
    before_rows, before_link = _rows(conn), (plugins_root / "sma").readlink()

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "already_decided" and outcome.status == "approved"
    assert _rows(conn) == before_rows
    assert (plugins_root / "sma").readlink() == before_link


# ---------------------------------------------------------------- AC-3 (foreign)

def _make_foreign(root, row):
    plugins_root = root / "plugins"
    other = plugins_root / ".versions" / "sma" / ("f" * 64)
    other.mkdir(parents=True, exist_ok=True)
    tmp_link = plugins_root / ".sma.foreign"
    tmp_link.symlink_to(f".versions/sma/{'f' * 64}")
    os.rename(tmp_link, plugins_root / "sma")


@pytest.mark.slow
def test_ac3_foreign_live_is_never_touched(tmp_path, monkeypatch):
    """AC-3: live が new でも old でもない先を指しているとき、
    retry も reconcile も **live を 1 バイトも変えない** (fail closed)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    _make_foreign(root, row)
    before = (plugins_root / "sma").readlink()

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS,
        activity=_activity(root))
    assert outcome.outcome == "foreign_waiting" and outcome.op_id == row["op_id"]
    # 段 0 pin (S0-69): `status` をリテラル `"approved"` に潰す変異が
    # SURVIVED した — 決定していないのだから `pending` でなければならない。
    assert outcome.status == "pending"
    assert outcome.rolled_back_op_id is None and outcome.target is None
    assert (plugins_root / "sma").readlink() == before
    assert _status(conn, row["approval_id"]) == "pending"
    assert _rows(conn) == [(row["op_id"], "switched")]
    assert "switch_retry_unrecognized_live_target" in _activity_text(root)

    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        activity=_activity(root))
    assert (plugins_root / "sma").readlink() == before
    assert _rows(conn) == [(row["op_id"], "switched")]
    assert "switch_reconcile_unrecognized_live_target" in _activity_text(root)


def _activity(root):
    from agentic_fx.activity import ActivityLog
    return ActivityLog(root / "logs" / "activity.log")


# ---------------------------------------------------------------- AC-9 (lock / stale row)

@pytest.mark.slow
def test_ac9a_reconcile_revert_holds_the_plugin_lock(tmp_path, monkeypatch):
    """AC-9a: reconcile の巻き戻しは `plugins/.locks/<name>.lock` の内側。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    held = []

    @_ctx.contextmanager
    def _spy(rt, name):
        with real(rt, name):
            held.append((name, plugin_switch.journal_store.get(conn, row["op_id"])["phase"]))
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _spy)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS)
    assert held == [("sma", "switched")], "巻き戻しは lock の内側で行われていない"


@pytest.mark.slow
def test_ac9a_revert_runs_while_the_lock_is_still_held(tmp_path, monkeypatch):
    """段 0 pin (S0-87): `test_ac9a_...` の spy は lock の**取得**しか見て
    いないので、「lock を取って即解放し、巻き戻しは lock の外でやる」変異が
    生存した (実測 SURVIVED)。`_revert_one` が呼ばれた**その瞬間**に lock を
    保持しているかを直接観測する ([[mutation-testing]] 6.5 — 落ちたテストが
    狙った防御のテストか)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real_lock = plugin_switch._plugin_lock
    real_revert = plugin_switch._revert_one
    depth = {"n": 0}
    held_during = []

    @_ctx.contextmanager
    def _spy_lock(rt, name):
        with real_lock(rt, name):
            depth["n"] += 1
            try:
                yield
            finally:
                depth["n"] -= 1

    def _spy_revert(conn_, r, **kw):
        held_during.append(depth["n"])
        return real_revert(conn_, r, **kw)

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _spy_lock)
    monkeypatch.setattr(plugin_switch, "_revert_one", _spy_revert)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS)

    assert held_during == [1], \
        f"巻き戻しが lock の外で走っている (保持深さ={held_during})"
    assert _rows(conn) == [(row["op_id"], "reverted")]


@pytest.mark.slow
def test_ac9b_i_stale_row_both_changed(tmp_path, monkeypatch):
    """AC-9b-i: lock 待ちの間に競合者が畳んで配備まで完了した場合、
    reconcile は **何もしない** (live は new のまま、approval は approved)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    done = threading.Event()

    def _competitor():
        c2 = db_store.connect(root / _DB)
        try:
            plugin_switch.retry_approval(
                c2, row["approval_id"], decided_by="competitor", now=NOW,
                plugins_root=plugins_root, settings=SETTINGS)
        finally:
            c2.close()
            done.set()

    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    fired = {"n": 0}

    @_ctx.contextmanager
    def _seam(rt, name):
        # **lock を取る直前**に競合者を走らせ、終わる (= lock を解放する) まで待つ。
        if fired["n"] == 0:
            fired["n"] = 1
            th = threading.Thread(target=_competitor, daemon=True)
            th.start()
            assert done.wait(timeout=30), "競合者が 30 秒で終わらない (deadlock)"
            th.join(timeout=30)
        with real(rt, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _seam)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        activity=_activity(root))

    live = plugins_root / "sma"
    assert live.is_symlink(), "stale 行で巻き戻され、配備が消えた (IV-3 の破れ)"
    assert _status(conn, row["approval_id"]) == "approved"
    rows = _rows(conn)
    assert rows[0] == (row["op_id"], "reverted") and rows[1][1] == "decided"
    assert "switch_reconcile_skipped_stale_row" in _activity_text(root)


@pytest.mark.slow
def test_ac9b_ii_live_changed_phase_same(tmp_path, monkeypatch):
    """AC-9b-ii: phase は `switched` のまま live だけ第三者が張り替えた場合
    (**分類の再確認**だけが検出できる)。live は触られない。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    fired = {"n": 0}

    @_ctx.contextmanager
    def _seam(rt, name):
        if fired["n"] == 0:
            fired["n"] = 1
            _make_foreign(root, row)  # phase は変えない
        with real(rt, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _seam)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        activity=_activity(root))

    assert (plugins_root / "sma").is_symlink(), "第三者の symlink を消してはならない"
    assert (plugins_root / "sma").readlink().as_posix().endswith("f" * 64)
    assert _rows(conn) == [(row["op_id"], "switched")]
    text = _activity_text(root)
    assert "switch_reconcile_skipped_stale_row" in text
    assert "class_now=foreign" in text


@pytest.mark.slow
def test_ac9b_iii_phase_changed_live_same(tmp_path, monkeypatch):
    """AC-9b-iii: live の分類は同じまま phase だけ終端化した場合
    (**行の再取得**だけが検出できる)。終端行に二重の巻き戻しを記録しない。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    fired = {"n": 0}

    @_ctx.contextmanager
    def _seam(rt, name):
        if fired["n"] == 0:
            fired["n"] = 1
            c2 = db_store.connect(root / _DB)
            try:  # 競合者は巻き戻しだけして配備に進まない
                plugin_switch._revert_one(
                    c2, journal_store.get(c2, row["op_id"]),
                    plugins_root=plugins_root, now=NOW)
                c2.commit()
            finally:
                c2.close()
        with real(rt, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _seam)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        activity=_activity(root))

    assert _rows(conn) == [(row["op_id"], "reverted")]
    text = _activity_text(root)
    assert "switch_reconcile_skipped_stale_row" in text
    # 競合者は activity を渡していないので、**正しい実装なら
    # `switch_reverted` は 1 行も出ない**。行の再取得を落とすと reconcile が
    # 終端行に対して `_revert_one` を再実行し、この行が 1 本出て red になる。
    assert "switch_reverted" not in text, "終端行への二重の巻き戻し記録"


@pytest.mark.slow
def test_revert_under_lock_returns_false_for_a_stale_row(tmp_path, monkeypatch):
    """段 0 pin (S0-48): stale のとき `True` を返す変異が SURVIVED した。
    戻り値を見ている呼び出し元は indicator_unresolved 枝の
    `if not _revert_under_lock(...): continue` だけで、その枝を stale で
    通すテストが無い。**戻り値そのもの**を直接 pin する
    ([[mutation-testing]] 6.16 — 本体が red でも呼び出し側は別の変異)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"

    assert plugin_switch._revert_under_lock(
        conn, row, plugins_root=plugins_root, now=NOW, activity=None,
        expect_class="not_switched") is True, "巻き戻せた回は True"

    # 同じ (古い) 行でもう一度呼ぶ = 競合者が畳んだ後と同じ stale 状態。
    assert plugin_switch._revert_under_lock(
        conn, row, plugins_root=plugins_root, now=NOW, activity=None,
        expect_class="not_switched") is False, \
        "stale を True で返すと呼び出し元が巻き戻していない行を巻き戻した扱いにする"
    assert _rows(conn) == [(row["op_id"], "reverted")]


@pytest.mark.slow
def test_ac9c_force_revert_takes_the_lock_and_refetches(tmp_path, monkeypatch):
    """AC-9c: `force_revert_op_id` も lock + 行の再取得を通る
    (**分類の一致は求めない** — phase 無視の割込という既存の意味論)。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real = plugin_switch._plugin_lock
    held = []

    @_ctx.contextmanager
    def _spy(rt, name):
        held.append(name)
        with real(rt, name):
            yield

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _spy)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS,
        force_revert_op_id=row["op_id"])
    assert held == ["sma"]
    assert _rows(conn) == [(row["op_id"], "reverted")]


# ---------------------------------------------------------------- AC-16 (2 段ガード)

@pytest.mark.slow
def test_ac16a_entry_guard_refuses_terminal_row_before_touching_fs(tmp_path, monkeypatch):
    """AC-16a(1): `_advance_to_decided` は終端行を渡されると **FS を触る前**に
    `ValueError`。live も journal も変わらない。"""
    from agentic_fx.plugin.gate_pytest import hashes_of
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    plugin_switch._revert_one(conn, row, plugins_root=plugins_root, now=NOW)
    conn.commit()
    candidate_dir = plugins_root / "_human" / "sma"
    content_hash, artifact_hash = hashes_of(candidate_dir)

    with pytest.raises(ValueError, match="terminal/missing"):
        plugin_switch._advance_to_decided(
            conn, row["approval_id"], op_id=row["op_id"], name="sma",
            plugins_root=plugins_root, candidate_dir=candidate_dir,
            content_hash=content_hash, artifact_hash=artifact_hash,
            switch_required=True, decided_by="probe", now=NOW)

    assert not (plugins_root / "sma").exists(), "FS に触れてから落ちている"
    assert _rows(conn) == [(row["op_id"], "reverted")]
    assert _status(conn, row["approval_id"]) == "pending"


@pytest.mark.slow
def test_ac16a_finalize_guard_refuses_wrong_phase(tmp_path, monkeypatch):
    """AC-16a(2): `_finalize_decision` は期待 phase 以外を `ValueError`。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugin_switch._revert_one(conn, row, plugins_root=root / "plugins", now=NOW)
    conn.commit()
    with pytest.raises(ValueError, match="expects phase='switched'"):
        plugin_switch._finalize_decision(
            conn, row["approval_id"], op_id=row["op_id"], decided_by="probe", now=NOW)
    assert _rows(conn) == [(row["op_id"], "reverted")]
    assert _status(conn, row["approval_id"]) == "pending"


@pytest.mark.slow
def test_ac16b_switch_required_zero_reaches_decided(tmp_path, monkeypatch):
    """AC-16b: 同一候補の再 bless は `switch_required=0` の行を作り、
    `recorded` から `decided` へ進む (ガードを常に `switched` 期待にすると red)。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    seen = []
    real = plugin_switch._finalize_decision

    def _spy(conn_, approval_id, *, op_id, decided_by, now):
        r = journal_store.get(conn_, op_id)
        seen.append((r["phase"], r["switch_required"]))
        return real(conn_, approval_id, op_id=op_id, decided_by=decided_by, now=now)

    monkeypatch.setattr(plugin_switch, "_finalize_decision", _spy)
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0

    assert seen == [("switched", 1), ("recorded", 0)]
    conn = db_store.connect(root / _DB)
    assert [ph for _op, ph in _rows(conn)] == ["decided", "decided"]
    conn.close()


# ---------------------------------------------------------------- AC-14d (網羅性)

def test_ac14d_no_bare_return_in_approval_entrypoints():
    """AC-14d(1): `approve_candidate` / `retry_approval` の**関数本体**に
    bare `return` / `return None` が無い (入れ子関数は対象外 — 内部 helper は
    値を返さなくてよい)。"""
    src = Path(plugin_switch.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fn_name in ("approve_candidate", "retry_approval"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        bad = []
        for node in _walk_own_body(fn):
            if isinstance(node, ast.Return) and (
                    node.value is None
                    or (isinstance(node.value, ast.Constant) and node.value.value is None)):
                bad.append(node.lineno)
        assert not bad, f"{fn_name}: outcome を返さない return が {bad} 行目にある"


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


@pytest.mark.slow
def test_ac14d_outcomes_are_known_enum_values(tmp_path, monkeypatch):
    """AC-14d(2): 主要な `return` 地点が既知 enum の outcome を返す。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    seen = set()

    # foreign_waiting
    _make_foreign(root, row)
    seen.add(plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="h", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS, activity=_activity(root)).outcome)
    # deployed_after_rollback (live を not_switched に戻してから)
    (plugins_root / "sma").unlink()
    seen.add(plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="h", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS).outcome)
    # already_decided
    seen.add(plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="h", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS).outcome)
    assert seen == {"foreign_waiting", "deployed_after_rollback", "already_decided"}
    assert seen <= plugin_switch.APPROVAL_OUTCOMES


@pytest.mark.slow
def test_ac14d_still_pending_on_missing_candidate(tmp_path, monkeypatch):
    """AC-14d(2) の続き: 候補が消えた行は `still_pending(candidate_missing)`。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    shutil.rmtree(plugins_root / "_human" / "sma")
    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="h", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)
    assert outcome.outcome == "still_pending"
    assert outcome.reason == "candidate_missing"
    assert outcome.status == "pending"


@pytest.mark.slow
def test_ac14_invalidated_status_comes_from_the_decision(tmp_path, monkeypatch):
    """段 0 pin (S0-15/S0-68): `invalidated` は 7 つの outcome のうち
    **どのテストも通っていなかった**ので、`status` のリテラルを「lock 内で
    読んだ行の値」(= `pending`) に差し替える変異が SURVIVED した。
    設計書 §3.5: `invalidated` の `status` の出所は **decision 結果**
    (同 lock 内で成功した `apply_decision` に渡したリテラル) であって、
    その前に読んだ行の値ではない。"""
    from agentic_fx.store import approvals as approvals_store
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    # 0c (ⓓ): 同名・別 content_hash の **後発の approved** を置く。
    newer = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "d" * 64,
                 "artifact_hash": "e" * 64, "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/9/sma"}, now=NOW)
    approvals_store.apply_decision(conn, newer, "approved", decided_by="t",
                                   now=NOW, commit=True)

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS, activity=_activity(root))

    assert outcome.outcome == "invalidated"
    assert outcome.status == "invalidated", \
        "status が decision 結果でなく、決定前に読んだ行の値になっている"
    assert outcome.name == "sma"
    assert _status(conn, row["approval_id"]) == "invalidated"


def test_approval_outcome_rejects_unknown_enum_values():
    """段 0 pin (S0-40): `ApprovalOutcome.__post_init__` の enum 検査を
    外す変異が SURVIVED した (不正な outcome を作るテストが 1 本も無い)。"""
    with pytest.raises(ValueError, match="unknown approval outcome"):
        plugin_switch.ApprovalOutcome(outcome="brand_new", name="sma",
                                      status="pending")
    for known in sorted(plugin_switch.APPROVAL_OUTCOMES):
        plugin_switch.ApprovalOutcome(outcome=known, name="sma", status="pending")
    # 既定値 (下流が取り違えていないかの基準)
    o = plugin_switch.ApprovalOutcome(outcome="deployed", name="sma",
                                      status="approved")
    assert (o.op_id, o.rolled_back_op_id, o.target, o.reason) == (None,) * 4


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


@pytest.mark.slow
def test_ac14_0d2a_deployed_outcome_fields(tmp_path, monkeypatch):
    """段 0 pin (S0-71/72/73): **0d-2a** (journal も live も `switched`) の
    経路は既存テストが 1 本も通っておらず、`target` を `old_target` に、
    `status` を `pending` に潰す変異が全て SURVIVED した。
    設計書 §3.5 の `return` 地点表どおりの値を全フィールド見る。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    # `switch_live` が失敗した後を手で「切替済み」にする = 0d-2a の前提。
    (plugins_root / "sma").symlink_to(row["new_target"])
    assert plugin_switch.classify_live(plugins_root, row) == "switched"

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "deployed"
    assert outcome.op_id == row["op_id"], "その行の op_id を返していない"
    assert outcome.target == row["new_target"], "target が new_target でない"
    assert outcome.status == "approved"
    assert outcome.rolled_back_op_id is None
    assert outcome.name == "sma"
    assert _rows(conn) == [(row["op_id"], "decided")]
    assert _status(conn, row["approval_id"]) == "approved"


@pytest.mark.slow
def test_ac14_0d2a_reverify_failure_reason(tmp_path, monkeypatch):
    """段 0 pin (S0-72): 0d-2a の再検証失敗は `still_pending(reverify_failed)`。
    `reason` を別の理由 (`hash_mismatch`) に取り違える変異が SURVIVED した。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    (plugins_root / "sma").symlink_to(row["new_target"])
    # 参照先の版を消す → `_reverify_switched_journal` が False を返す
    # (版ディレクトリは read-only で作られるので先に書き込み権を戻す)。
    version_dir = plugins_root / row["new_target"]
    for p in [version_dir, *version_dir.rglob("*")]:
        p.chmod(p.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
    shutil.rmtree(version_dir)
    # 候補も消す — 残っていると `_reverify_switched_journal` が版を冪等に
    # 再作成してしまい、この経路に入らない。
    shutil.rmtree(plugins_root / "_human" / "sma")

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS, activity=_activity(root))

    assert outcome.outcome == "still_pending"
    assert outcome.reason == "reverify_failed", "再検証失敗の理由が違う"
    assert outcome.status == "pending"
    assert _status(conn, row["approval_id"]) == "pending"


@pytest.mark.slow
def test_ac14a_already_decided_status_comes_from_the_row(tmp_path, monkeypatch):
    """AC-14a: `already_decided` の `status` は **lock 内で読んだ行の値**。
    リテラル (`"approved"` 等) に潰すと red — reject した approval を retry すると
    `status="rejected"` が返らなければならない。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    plugin_switch.reject_candidate(
        conn, row["approval_id"], decided_by="human", reason="",
        now=NOW, plugins_root=plugins_root)

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "already_decided"
    assert outcome.status == "rejected", "status が行の値でなくリテラルになっている"
