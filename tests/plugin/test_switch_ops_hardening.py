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


def _phase_from_another_connection(root, op_id):
    """別コネクションから journal の phase を読む (AC-9d、T10)。
    **commit されていない書込はここからは見えない** — これが
    「lock の内側で commit まで完了しているか」の観測装置になる。"""
    other = db_store.connect(root / _DB)
    try:
        row = journal_store.get(other, op_id)
        return row["phase"] if row is not None else None
    finally:
        other.close()


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


@pytest.mark.slow
def test_t01_force_revert_stale_when_phase_advances_but_stays_non_terminal(
        tmp_path, monkeypatch):
    """1 周目トリアージ T-01 = 段 0 pin (S0-47): `_revert_under_lock` の
    stale 判定は `phase_now in _TERMINAL_PHASES` と
    `phase_now != row["phase"]` の 2 条件からなる。`force_revert_op_id`
    経路 (`expect_class=None`) では後者だけが検出できる場合がある —
    競合者が lock 待ちの間に行を **終端化はせず別の非終端 phase へ前進**
    させたケース (`test_ac9b_iii` は `expect_class` ありの switched 行しか
    covered しておらず、そこでは非終端 → 非終端の前進が起きないため等価に
    なる — この経路でだけ差が出る)。この項を削除すると stale row を
    見落として二重に巻き戻す。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    # 呼び出し元 (reconcile) が list_non_terminal で拾った時点の
    # 「古い」行を模す。
    journal_store.set_phase(conn, row["op_id"], phase="recorded", now=NOW,
                            commit=True)
    stale_row = journal_store.get(conn, row["op_id"])
    assert stale_row["phase"] == "recorded"
    # 競合者が lock を取る前に非終端のまま前進させる (recorded → switched)。
    journal_store.set_phase(conn, row["op_id"], phase="switched", now=NOW,
                            commit=True)

    result = plugin_switch._revert_under_lock(
        conn, stale_row, plugins_root=plugins_root, now=NOW,
        activity=_activity(root), expect_class=None)

    assert result is False, "stale (phase 前進) を見落として巻き戻した"
    assert _rows(conn) == [(row["op_id"], "switched")], \
        "stale 行を誤って reverted にしてはならない"
    text = _activity_text(root)
    assert "switch_reconcile_skipped_stale_row" in text
    assert "phase_before=recorded phase_now=switched" in text


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


# ---------------------------------------------------------------- AC-9d (T10、
# 巻き戻しの commit が lock 内で完了していることの pin。**本体コードは
# 1 行も変えない** — 現実装が既に lock 内 commit であることは設計書
# §3.3.2 の全数表が記録している。段 0 の未 pin S0-54 / S0-55 / S0-75 を殺す
# (置換前 → 置換後で red → green を確認する変異テストの流儀)。

@pytest.mark.slow
def test_ac9d_reconcile_revert_is_durable_from_another_connection(tmp_path, monkeypatch):
    """AC-9d(a) / S0-54 の killer: `_revert_under_lock` の
    `conn.commit()` を落とすと、ActivityLog は DB を触らずファイルにしか
    書かないので (Step 10-a で確認済)、`not_switched` 枝の後には commit する
    箇所が無く未 commit のまま残る。別コネクションから読んで `reverted` に
    なっていることを確認する — 同一コネクションからの観測 (`_rows`) だけでは
    未 commit でも見えてしまうため区別する。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"

    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS)

    assert _rows(conn) == [(row["op_id"], "reverted")], \
        "前提: 自分のコネクションからは reverted に見えるはず"
    assert _phase_from_another_connection(root, row["op_id"]) == "reverted", \
        "巻き戻しが lock 内で commit されていない (別コネクションから未 commit)"


@pytest.mark.slow
def test_ac9d_revert_is_committed_before_the_lock_is_released(tmp_path, monkeypatch):
    """AC-9d(b) / S0-55 の killer: `_revert_under_lock` の `conn.commit()` を
    `with _plugin_lock(...)` の外へ出す変異では、lock 解放**直前**の観測が
    まだ `switched` のまま (commit 前) になる。既存 `:250`
    (`test_ac9a_revert_runs_while_the_lock_is_still_held`) と同じ spy の型を
    使い、実 lock を解放する前に別コネクションから phase を読む。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    import contextlib as _ctx
    real_lock = plugin_switch._plugin_lock
    op_id = row["op_id"]
    seen = []

    @_ctx.contextmanager
    def _spy_lock(rt, name):
        with real_lock(rt, name):
            try:
                yield
            finally:
                # **実 lock を解放する前に**別コネクションから読む。
                # commit が with の外に出ていると、ここではまだ見えない。
                seen.append(_phase_from_another_connection(root, op_id))

    monkeypatch.setattr(plugin_switch, "_plugin_lock", _spy_lock)
    plugin_switch.reconcile_switch_journals(
        conn, plugins_root=plugins_root, now=NOW, settings=SETTINGS)

    assert seen == ["reverted"], \
        f"lock 解放直前の観測が reverted になっていない (観測={seen})"


@pytest.mark.slow
def test_ac9d_retry_rollback_is_durable_before_pending_return(tmp_path, monkeypatch):
    """AC-9d(c) / S0-75 の killer: `approval retry` が 0d-2c で停止行を
    巻き戻して閉じた後、候補ディレクトリが消えていると
    `CandidateMissingError` → `_close_own_unfinished_journal_if_any` は
    `op_id is None` で即 return する (0d-2c が既に op_id を None に戻して
    いるため、ここで commit する箇所は無い)。0d-2c 自身の `conn.commit()`
    を落とすと、`still_pending` を返して戻ってきた時点で停止行が
    未 commit のまま残る。"""
    root, conn, row = _stopped_at_switched(tmp_path, monkeypatch)
    plugins_root = root / "plugins"
    shutil.rmtree(root / "plugins" / "_human" / "sma")

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "still_pending"
    assert outcome.reason == "candidate_missing"
    assert outcome.rolled_back_op_id == row["op_id"]
    assert _phase_from_another_connection(root, row["op_id"]) == "reverted", \
        "0d-2c の巻き戻しが lock 内で commit されていない (別コネクションから未 commit)"


@pytest.mark.slow
def test_close_own_unfinished_journal_candidate_missing_commit_is_durable(
        tmp_path, monkeypatch):
    """[switch-ops-hardening] B (T10/AC-9d と同じ流儀の追加 pin、
    `_close_own_unfinished_journal_if_any` の `conn.commit()`、
    `switch.py:1658` 付近)。この helper は `approve_candidate` の pending
    留置 3 経路 (candidate_missing / snapshot_invalid / hash_mismatch) の
    共通部で、閉じた直後にそのまま `return` するため後続の commit が無い
    (T11 の再開判定経路では直後に `begin_switch_journal(commit=True)` が
    続くので等価だが、この 3 経路には続く commit が無く load-bearing —
    プラン T11-M6-scope / 設計書 v1.8b 参照)。

    `_stopped_before` で **switched より前 (preparing)** の行を作ってから
    候補を消し、`candidate_missing` 経路に入らせる (`_stopped_at_switched`
    を使う既存 `test_ac9d_retry_rollback_is_durable_before_pending_return`
    は 0d-2c 自身の commit を見ており、この helper の commit を単独では
    確かめていない — switched 行は 0d-2c が先に op_id を None にするため
    この helper には来ない)。別コネクションから停止行が `reverted` に
    なっていることを読み、lock 内で commit まで完了していることを確認する。
    """
    root, conn, row = _stopped_before(tmp_path, monkeypatch, "versioned", "preparing")
    plugins_root = root / "plugins"
    shutil.rmtree(root / "plugins" / "_human" / "sma")

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "still_pending"
    assert outcome.reason == "candidate_missing"
    assert journal_store.get_open_by_name(conn, "sma") is None, \
        "自分の未完ジャーナルが閉じられていない"
    assert _phase_from_another_connection(root, row["op_id"]) == "reverted", \
        "_close_own_unfinished_journal_if_any の commit が lock 内で完了していない " \
        "(別コネクションから未 commit)"


# ---------------------------------------------------------------- T11 (AC-16c、
# 再開の前提 (3 つ組) が崩れていたら巻き戻して新しい op_id で流し直す。
# ヘルパ `_fail_advance_at` / `_stopped_before` / `_third_party_points_live_at`
# は probe `tmp/review-20260920-soh/r2/cr1-probe/test_probe_cr1.py` を転写
# (機械 diff で 0 を確認済)。`_stopped_before_sw0` は probe に無い新規ヘルパ
# (switch_required=0 の行を preparing/versioned/recorded で止める)。

def _fail_advance_at(monkeypatch, phase_to_fail: str):
    real = plugin_switch.advance_switch_journal

    def _fake(conn, op_id, *, phase, now, commit=False):
        if phase == phase_to_fail:
            raise OSError(f"injected: crash before advance({phase})")
        return real(conn, op_id, phase=phase, now=now, commit=commit)

    monkeypatch.setattr(plugin_switch, "advance_switch_journal", _fake)
    return real


def _stopped_before(tmp_path, monkeypatch, fail_phase: str, expect_phase: str):
    """bless を `fail_phase` への advance 直前で落とし、journal
    (switch_required=1) を `expect_phase` (preparing/versioned/recorded) で
    停止させる (probe `test_probe_cr1.py:77-92` を転写)。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    real = _fail_advance_at(monkeypatch, fail_phase)
    main(["plugin", "bless", "sma", "--from", "_human"])
    monkeypatch.setattr(plugin_switch, "advance_switch_journal", real)
    conn = db_store.connect(root / _DB)
    row = journal_store.get_open_by_name(conn, "sma")
    assert row is not None and row["phase"] == expect_phase, (
        f"前提: {expect_phase} で停止 / 実際={row and row['phase']}")
    assert not (root / "plugins" / "sma").exists(), "前提: live は未切替"
    assert row["switch_required"] == 1, "前提: 保存 switch_required=1"
    return root, conn, row


def _third_party_points_live_at(root, target: str) -> None:
    """第三者が live symlink を外から `target` へ張り替える
    (probe `test_probe_cr1.py:95-101` を転写)。"""
    plugins_root = root / "plugins"
    tmp = plugins_root / ".sma.thirdparty"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target)
    os.rename(tmp, plugins_root / "sma")


_SW0_FAIL_AT = {"preparing": "versioned", "versioned": "recorded"}


def _stopped_before_sw0(tmp_path, monkeypatch, expect_phase: str):
    """**probe に無い新規ヘルパ**: `switch_required=0` の行を
    `preparing`/`versioned`/`recorded` で止める。まず同一候補を 1 回 bless
    して live を配備し (switch_required=1 で完遂)、同じ候補をもう一度
    bless する (2 回目は switch_required=0 の行になる — `test_ac16b_*` と
    同じ作り)。`preparing`/`versioned` は `_fail_advance_at` で止まる
    (advance("versioned")/advance("recorded") を落とす)。`recorded` は
    `advance("switched")` をそもそも呼ばない (§11-a の注のとおり) ので
    `_finalize_decision` を落とす (probe `test_probe_c_reverse_stored_zero_
    recomputed_true` `:199-206` の `_boom` と同形)。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0

    if expect_phase == "recorded":
        real_fin = plugin_switch._finalize_decision
        state = {"n": 0}

        def _boom(*a, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("injected: crash before finalize")
            return real_fin(*a, **kw)

        monkeypatch.setattr(plugin_switch, "_finalize_decision", _boom)
        main(["plugin", "bless", "sma", "--from", "_human"])
        monkeypatch.setattr(plugin_switch, "_finalize_decision", real_fin)
    else:
        real = _fail_advance_at(monkeypatch, _SW0_FAIL_AT[expect_phase])
        main(["plugin", "bless", "sma", "--from", "_human"])
        monkeypatch.setattr(plugin_switch, "advance_switch_journal", real)

    conn = db_store.connect(root / _DB)
    row = journal_store.get_open_by_name(conn, "sma")
    assert row is not None and row["phase"] == expect_phase and row["switch_required"] == 0, (
        f"前提: switch_required=0 / {expect_phase} で停止 / "
        f"実際={dict(row) if row else None}")
    return root, conn, row


@pytest.mark.slow
@pytest.mark.parametrize("fail_phase,expect_phase", [
    ("versioned", "preparing"),
    ("recorded", "versioned"),
    ("switched", "recorded"),
])
def test_ac16c1_resume_with_stale_switch_required_rolls_back_and_redeploys(
        tmp_path, monkeypatch, fail_phase, expect_phase):
    """AC-16c-1 (a): 保存 switch_required=1 / 再計算 False の 3 phase。
    第三者が live を **ちょうど new_target** へ向けた状態から retry する
    と、巻き戻して新しい op_id で流し直し完遂する (現行は 3 通りとも
    ValueError で永久に詰まる)。"""
    root, conn, row = _stopped_before(tmp_path, monkeypatch, fail_phase, expect_phase)
    plugins_root = root / "plugins"
    new_target = row["new_target"]
    _third_party_points_live_at(root, new_target)
    assert (plugins_root / "sma").readlink().as_posix() == new_target

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert _status(conn, row["approval_id"]) == "approved"
    assert journal_store.list_non_terminal(conn) == []
    all_rows = _rows(conn)
    assert len(all_rows) == 2, f"停止行 + 新行の 2 行でなければならない: {all_rows}"
    assert all_rows[0] == (row["op_id"], "reverted")
    new_op_id = all_rows[1][0]
    assert all_rows[1][1] == "decided" and new_op_id != row["op_id"]
    new_row = journal_store.get(conn, new_op_id)
    assert new_row["old_kind"] == "symlink"
    assert new_row["old_target"] == new_target
    assert new_row["switch_required"] == 0
    assert (plugins_root / "sma").readlink().as_posix() == new_target, "live は不変"
    assert outcome.outcome == "deployed_after_rollback"
    assert outcome.rolled_back_op_id == row["op_id"]
    assert outcome.op_id == new_op_id


@pytest.mark.slow
@pytest.mark.parametrize("expect_phase", ["preparing", "versioned", "recorded"])
def test_ac16c2_reverse_stored_zero_recomputed_true_rolls_back(
        tmp_path, monkeypatch, expect_phase):
    """AC-16c-2 (c、逆向き): 保存 switch_required=0 / 再計算 True の 3
    phase。第三者が live を消した状態から retry すると、巻き戻して新しい
    op_id (switch_required=1) で流し直し完遂する (現行は
    `advance_switch_journal` の switch_required=0 ガードで ValueError)。"""
    root, conn, row = _stopped_before_sw0(tmp_path, monkeypatch, expect_phase)
    plugins_root = root / "plugins"
    (plugins_root / "sma").unlink()  # 第三者が live を消す
    rows_before = _rows(conn)
    assert rows_before[-1] == (row["op_id"], expect_phase), \
        f"前提: 停止行が末尾のはず (先行する完遂行 1 本を含む): {rows_before}"

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert journal_store.list_non_terminal(conn) == []
    all_rows = _rows(conn)
    assert len(all_rows) == len(rows_before) + 1, (
        "停止行の巻き戻し (reverted) + 新行 (decided) の計 1 行増でなければ"
        f"ならない: before={rows_before} after={all_rows}")
    assert all_rows[len(rows_before) - 1] == (row["op_id"], "reverted")
    new_op_id = all_rows[-1][0]
    new_row = journal_store.get(conn, new_op_id)
    assert new_row["switch_required"] == 1
    live = plugins_root / "sma"
    assert live.is_symlink()
    assert live.readlink().as_posix() == new_row["new_target"]
    assert _status(conn, row["approval_id"]) == "approved"
    assert outcome.outcome == "deployed_after_rollback"


@pytest.mark.slow
@pytest.mark.parametrize("switch_required,fail_phase,expect_phase", [
    (1, "versioned", "preparing"),
    (1, "recorded", "versioned"),
    (1, "switched", "recorded"),
    (0, None, "preparing"),
    (0, None, "versioned"),
    (0, None, "recorded"),
])
def test_ac16c3_resume_without_drift_reuses_the_same_op_id(
        tmp_path, monkeypatch, switch_required, fail_phase, expect_phase):
    """AC-16c-3 (回帰防止)。**この AC が「常に巻き戻す」への退化を殺す
    唯一の網** — 食い違いが無いときは live を一切触らず retry しても
    op_id を再利用したまま完遂する (`switch_required` の両方 (0 と 1) ×
    3 phase、v1.8a)。"""
    if switch_required:
        root, conn, row = _stopped_before(tmp_path, monkeypatch, fail_phase, expect_phase)
    else:
        root, conn, row = _stopped_before_sw0(tmp_path, monkeypatch, expect_phase)
    plugins_root = root / "plugins"
    # live には一切触れない (no-drift)。
    rows_before = _rows(conn)
    assert rows_before[-1] == (row["op_id"], expect_phase), \
        f"前提: 停止行が末尾のはず: {rows_before}"

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS, activity=_activity(root))

    all_rows = _rows(conn)
    assert all_rows == rows_before[:-1] + [(row["op_id"], "decided")], (
        "停止行と同じ op_id が再利用されているはず "
        f"(新しい行が増えていない): {all_rows}")
    assert outcome.outcome == "deployed"
    assert outcome.rolled_back_op_id is None
    assert "switch_resume_precondition_changed" not in _activity_text(root)


@pytest.mark.slow
def test_ac16c4_precondition_change_leaves_a_dedicated_activity_event(tmp_path, monkeypatch):
    """AC-16c-4: 監査の観測。activity に `switch_resume_precondition_changed`
    が **1 本**残り、その**直後**に `switch_reverted` が並ぶ (2 行になる)。
    巻き戻した行は終端 `reverted` のまま。"""
    root, conn, row = _stopped_before(tmp_path, monkeypatch, "switched", "recorded")
    plugins_root = root / "plugins"
    _third_party_points_live_at(root, row["new_target"])

    plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS, activity=_activity(root))

    lines = [l for l in _activity_text(root).splitlines() if l.strip()]
    precond = [l for l in lines if "switch_resume_precondition_changed" in l]
    assert len(precond) == 1, f"1 本のはず: {precond}"
    line = precond[0]
    assert f"op_id={row['op_id']}" in line
    assert "phase=recorded" in line
    assert "stored_switch_required=1" in line
    assert "recomputed=0" in line
    idx = lines.index(line)
    assert "switch_reverted" in lines[idx + 1], "直後が switch_reverted でない"
    assert _rows(conn)[0] == (row["op_id"], "reverted")


@pytest.mark.slow
def test_ac16c5_resume_rollback_is_committed_inside_the_lock(tmp_path, monkeypatch):
    """AC-16c-5 (IV-6): 巻き戻しは `_plugin_locks` の内側で起き、commit も
    lock の内側で完了している。T10 の観測装置
    (`_phase_from_another_connection`) を再利用する。"""
    root, conn, row = _stopped_before(tmp_path, monkeypatch, "switched", "recorded")
    plugins_root = root / "plugins"
    _third_party_points_live_at(root, row["new_target"])
    import contextlib as _ctx
    real_locks = plugin_switch._plugin_locks
    op_id = row["op_id"]
    seen = []

    @_ctx.contextmanager
    def _spy(rt, names):
        with real_locks(rt, names):
            try:
                yield
            finally:
                # 実 lock を解放する前に別コネクションから読む。
                seen.append(_phase_from_another_connection(root, op_id))

    monkeypatch.setattr(plugin_switch, "_plugin_locks", _spy)
    plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert seen == ["reverted"], \
        f"lock 解放直前の観測が reverted になっていない (観測={seen})"
    # 静的確認 (Step 11-e): `_revert_one` の呼び出し元は 5 箇所のまま
    # (定義行 `def _revert_one(` 自身は除く — 設計書 §3.3.2 の全数表と同じ数え方)。
    src_lines = Path(plugin_switch.__file__).read_text(encoding="utf-8").splitlines()
    call_sites = [l for l in src_lines
                 if "_revert_one(" in l and not l.lstrip().startswith("def ")]
    assert len(call_sites) == 5, call_sites


@pytest.mark.slow
@pytest.mark.parametrize("fail_phase,expect_phase", [
    ("versioned", "preparing"),
    ("recorded", "versioned"),
    ("switched", "recorded"),
])
def test_ac16c6_plain_branch_closes_its_own_unfinished_journal(
        tmp_path, monkeypatch, fail_phase, expect_phase):
    """AC-16c-6: 第三者が live を plain ディレクトリへ差し替えた状態で
    retry すると、`legacy_plain_present` のまま (文言・フィールド不変) で
    非終端 journal が 0 本になり、続く bless が `UnresolvedJournalError`
    にならない。"""
    root, conn, row = _stopped_before(tmp_path, monkeypatch, fail_phase, expect_phase)
    plugins_root = root / "plugins"
    live = plugins_root / "sma"
    if live.exists() or live.is_symlink():
        live.unlink()
    live.mkdir()
    (live / "marker.txt").write_text("plain")

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "legacy_plain_present"
    assert outcome.status == "pending"
    assert journal_store.list_non_terminal(conn) == []

    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0, \
        "非終端行が残っていれば UnresolvedJournalError で rc=1 のはず"


@pytest.mark.slow
def test_ac16c7_crash_between_rollback_commit_and_new_journal_converges(tmp_path, monkeypatch):
    """AC-16c-7 (codex r5 #4): 巻き戻しの commit と新 journal 行の作成の
    間で落ちても、次の retry で素の新規 approve 経路を通って収束する。"""
    root, conn, row = _stopped_before(tmp_path, monkeypatch, "switched", "recorded")
    plugins_root = root / "plugins"
    _third_party_points_live_at(root, row["new_target"])

    real_begin = plugin_switch.begin_switch_journal
    state = {"n": 0}

    def _boom(*a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("injected: crash before begin_switch_journal")
        return real_begin(*a, **kw)

    monkeypatch.setattr(plugin_switch, "begin_switch_journal", _boom)

    with pytest.raises(OSError, match="injected"):
        plugin_switch.retry_approval(
            conn, row["approval_id"], decided_by="human", now=NOW,
            plugins_root=plugins_root, settings=SETTINGS)

    assert _rows(conn) == [(row["op_id"], "reverted")]
    assert journal_store.list_non_terminal(conn) == []
    assert _status(conn, row["approval_id"]) == "pending"
    assert (plugins_root / "sma").readlink().as_posix() == row["new_target"]

    monkeypatch.setattr(plugin_switch, "begin_switch_journal", real_begin)
    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    all_rows = _rows(conn)
    assert len(all_rows) == 2
    assert all_rows[0] == (row["op_id"], "reverted")
    assert all_rows[1][1] == "decided"
    assert _status(conn, row["approval_id"]) == "approved"
    assert outcome.outcome == "deployed"
    assert outcome.rolled_back_op_id is None, \
        "巻き戻したのは前回の呼び出しなので今回は巻き戻していない"


def _write_v2_candidate(human_dir: Path) -> None:
    """AC-16c-8: 別 artifact_hash の候補にする (内容を変えるだけ)。"""
    p = human_dir / "plugin.py"
    p.write_text(p.read_text(encoding="utf-8") + "\n# v2\n", encoding="utf-8")


@pytest.mark.slow
def test_ac16c8_old_target_drift_is_detected(tmp_path, monkeypatch):
    """AC-16c-8 (R13 の主契約): 保存も再計算も switch_required=1 のままで、
    `old_kind='symlink'` の行の live を第三者が **old_target でも
    new_target でもない第 3 の先 Y** へ張り替えると、巻き戻しが起き、
    新しい行の `old_target == Y` になる (v1.7 の述語ではこの drift を
    検出できない)。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0
    conn = db_store.connect(root / _DB)
    plugins_root = root / "plugins"
    old_target = (plugins_root / "sma").readlink().as_posix()

    _write_v2_candidate(plugins_root / "_human" / "sma")
    real = _fail_advance_at(monkeypatch, "switched")
    main(["plugin", "bless", "sma", "--from", "_human"])
    monkeypatch.setattr(plugin_switch, "advance_switch_journal", real)
    row = journal_store.get_open_by_name(conn, "sma")
    assert row is not None and row["phase"] == "recorded"
    assert row["switch_required"] == 1
    assert row["old_kind"] == "symlink" and row["old_target"] == old_target

    y_target = f".versions/sma/{'9' * 64}"
    _third_party_points_live_at(root, y_target)
    rows_before = _rows(conn)
    assert rows_before[-1] == (row["op_id"], "recorded"), \
        f"前提: 停止行が末尾のはず (先行する完遂行 1 本を含む): {rows_before}"

    outcome = plugin_switch.retry_approval(
        conn, row["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert journal_store.list_non_terminal(conn) == []
    all_rows = _rows(conn)
    assert len(all_rows) == len(rows_before) + 1, (
        "停止行の巻き戻し (reverted) + 新行 (decided) の計 1 行増でなければ"
        f"ならない: before={rows_before} after={all_rows}")
    assert all_rows[len(rows_before) - 1] == (row["op_id"], "reverted")
    new_op_id = all_rows[-1][0]
    new_row = journal_store.get(conn, new_op_id)
    assert new_row["old_target"] == y_target, \
        "R13 の主契約: old_target の drift が検出されていない"
    assert outcome.outcome == "deployed_after_rollback"
    assert (plugins_root / "sma").readlink().as_posix() == row["new_target"]


# ---------------------------------------------------------------- T13 (AC-19、
# 版 dir の hash 再照合を切替の要否にかかわらず通す)
#
# **着手時の実測で判明した構成上の注意**: `history_git.record_version` は
# `_advance_to_decided` の**呼び出しのたびに無条件で** (switch_required の
# 値に関係なく) 版 dir の実ファイルを git blob hash で候補の
# (content_hash, artifact_hash) と独立に再照合する (`history_git.py:162-171`)。
# `create_version_dir` の後に単純に版 dir を書き換えるだけでは、
# switch_required=0 の分岐 (T13 が新設する箇所) に到達する**前**にこの
# 既存照合が `HistoryGitError` (`index blob hash mismatch`) を投げてしまい、
# T13 の分岐を検証できない (実測して確認した — 2 回目の `main(["plugin",
# "bless", ...])` を試すと `HistoryGitError` が uncaught で漏れた)。
# `tests/plugin/test_switch_paths.py::
# test_advance_to_decided_detects_in_place_tamper_of_artifact_hash_only`
# (`:1391-1400`) が使う **「record_version を実体で呼んだ直後に改竄する」**
# seam (機械 diff で確認済) を転用し、正しい記録が済んだ**後**に版 dir を
# 改竄することで、T13 が新設する switch_required=0 の照合だけを狙って
# 落とす。

def _record_then_tamper_version_dir(monkeypatch):
    """`test_switch_paths.py:1388-1398` の `_record_then_tamper` と同じ
    流儀 (機械 diff で確認)。"""
    from agentic_fx.plugin import history_git as history_git_mod
    real_record_version = history_git_mod.record_version

    def _record_then_tamper(*a, **kw):
        result = real_record_version(*a, **kw)
        version_dir = kw["version_dir"]
        version_dir.chmod(0o700)
        (version_dir / "plugin.py").chmod(0o600)
        (version_dir / "plugin.py").write_text("def compute(df, params):\n    return {}\n# TAMPERED\n")
        version_dir.chmod(0o500)
        return result

    monkeypatch.setattr("agentic_fx.plugin.switch.history_git.record_version",
                        _record_then_tamper)


def _switch_required_zero_pending_approval(root, conn) -> int:
    """AC-19a/b/c: 同一候補 (`_human/sma`、live は既に配備済み) をもう一度
    `submit_candidate` して `switch_required=0` の pending approval を作る
    (**approve 経路** — AC-19d/e の bless 経路とは意図的に別にする)。"""
    plugins_root = root / "plugins"
    return plugin_switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_root / "_human",
        candidate_origin="human", mission_id=None, backlog_id=None,
        settings=SETTINGS, now=NOW)


@pytest.mark.slow
def test_ac19a_switch_not_required_verifies_version_dir_before_deciding(tmp_path, monkeypatch):
    """AC-19a (IV-7 の主契約): switch_required=0 でも版 dir の照合が走る。
    版 dir を第三者が in-place 編集してから (T13 の分岐の直前で改竄する
    seam を使う) approve すると、approval は pending のまま、
    still_pending(reverify_failed)、非終端行は 0 本、live は不変。
    **現行 (T13 前) は照合を通らず approved になる**。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0
    conn = db_store.connect(root / _DB)
    plugins_root = root / "plugins"
    target = (plugins_root / "sma").readlink().as_posix()

    approval_id = _switch_required_zero_pending_approval(root, conn)
    _record_then_tamper_version_dir(monkeypatch)
    outcome = plugin_switch.approve_candidate(
        conn, approval_id, decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "still_pending"
    assert outcome.reason == "reverify_failed"
    assert outcome.status == "pending"
    assert _status(conn, approval_id) == "pending"
    assert journal_store.list_non_terminal(conn) == [], "名前が凍らない"
    assert (plugins_root / "sma").readlink().as_posix() == target, "live は不変"


@pytest.mark.slow
def test_ac19b_verification_runs_before_finalize(tmp_path, monkeypatch):
    """AC-19b: 照合は decide の前にある — `_finalize_decision` は
    1 度も呼ばれない。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0
    conn = db_store.connect(root / _DB)
    plugins_root = root / "plugins"

    approval_id = _switch_required_zero_pending_approval(root, conn)
    _record_then_tamper_version_dir(monkeypatch)
    calls = []
    real_fin = plugin_switch._finalize_decision

    def _spy(*a, **kw):
        calls.append(1)
        return real_fin(*a, **kw)

    monkeypatch.setattr(plugin_switch, "_finalize_decision", _spy)
    plugin_switch.approve_candidate(
        conn, approval_id, decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert calls == [], "照合の前に _finalize_decision が呼ばれている"


@pytest.mark.slow
def test_ac19c_same_candidate_reapproval_still_succeeds(tmp_path, monkeypatch):
    """AC-19c (正常系の回帰): 版 dir を触らない同じ候補の再 approve は
    従来どおり approved になる (switch_required=0 / recorded → decided)。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0
    conn = db_store.connect(root / _DB)
    plugins_root = root / "plugins"
    target = (plugins_root / "sma").readlink().as_posix()
    # 版 dir は触らない。

    approval_id = _switch_required_zero_pending_approval(root, conn)
    outcome = plugin_switch.approve_candidate(
        conn, approval_id, decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert outcome.outcome == "deployed"
    assert outcome.status == "approved"
    assert _status(conn, approval_id) == "approved"
    j = journal_store.get(conn, outcome.op_id)
    assert j["switch_required"] == 0 and j["phase"] == "decided"
    assert (plugins_root / "sma").readlink().as_posix() == target


@pytest.mark.slow
def test_ac19d_bless_raises_when_version_dir_is_tampered(tmp_path, monkeypatch):
    """AC-19d (§3.7.3): switch_required=0 の bless で版 dir が壊れている
    と RuntimeError が上がり、bless は自分の未完行を閉じない (設計どおりの
    残余) — 解除は reject で閉じられる。

    **申告 (プランからの逸脱)**: 設計 (§3.7.3 / Step 13-a AC-19d) は
    「`afx plugin bless` (CLI) を打つと rc=1・stderr に `エラー: `・
    traceback が出ない」と書くが、現物 `backtest/cli.py` の `_plugin_bless`
    の except は `(UnresolvedJournalError, ValueError, SandboxError)` のみで
    `RuntimeError` を捕らない (`:589-601`)。外側の包括 catch
    (`:781-784`、`(ValueError, KeyError, OSError, sqlite3.Error,
    VerifyBackendGateError)`) にも `RuntimeError` は無い。**CLI で試すと
    traceback が出る** — Global Constraints は本束で足す `except` を 3 つ
    (`_plugin_bless` の `UnresolvedJournalError` / `_plugin_materialize` の
    `ValueError` / T12 の `_plugin_retire`) に限定しており、T13 の担当は
    `switch.py` のみ (cli.py は対象外) なので、4 つ目の `except` を無断で
    追加しない。ここでは `bless_candidate` を直接呼んで RuntimeError と
    残余行を確認する。CLI 経由の rc=1 化 (cli.py の是正) は別途申告する。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0
    conn = db_store.connect(root / _DB)
    plugins_root = root / "plugins"
    human_dir = plugins_root / "_human" / "sma"

    _record_then_tamper_version_dir(monkeypatch)
    with pytest.raises(RuntimeError, match="content_hash mismatch"):
        plugin_switch.bless_candidate(
            conn, name="sma", human_dir=human_dir, settings=SETTINGS,
            now=NOW, decided_by="human_cli")

    non_terminal = journal_store.list_non_terminal(conn)
    assert len(non_terminal) == 1, "bless は自分の未完行を閉じない (設計どおりの残余)"
    residual = non_terminal[0]
    assert residual["phase"] == "recorded"

    plugin_switch.reject_candidate(
        conn, residual["approval_id"], decided_by="human", reason="",
        now=NOW, plugins_root=plugins_root)
    assert journal_store.list_non_terminal(conn) == [], \
        "reject で残余行が閉じられていない"


@pytest.mark.slow
def test_ac19e_bless_residual_resolved_by_approval_retry_when_repaired(tmp_path, monkeypatch):
    """AC-19e (v1.8a 新規、repaired=True の側): bless の残余は `reject` だけ
    でなく `approval retry` でも解除できる — 版 dir を直してから打てば
    `decided`/`approved` まで完遂し、非終端行は残らない。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0
    conn = db_store.connect(root / _DB)
    plugins_root = root / "plugins"
    human_dir = plugins_root / "_human" / "sma"
    target = (plugins_root / "sma").readlink().as_posix()

    _record_then_tamper_version_dir(monkeypatch)
    with pytest.raises(RuntimeError, match="content_hash mismatch"):
        plugin_switch.bless_candidate(
            conn, name="sma", human_dir=human_dir, settings=SETTINGS,
            now=NOW, decided_by="human_cli")
    residual = journal_store.list_non_terminal(conn)[0]

    # 版 dir を直す — patch を外し (real record_version に戻す)、
    # 改竄済み版 dir を消して `create_version_dir` が候補から作り直せる
    # ようにする。
    monkeypatch.undo()
    version_dir = plugins_root / target
    for p in [version_dir, *version_dir.rglob("*")]:
        p.chmod(p.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
    shutil.rmtree(version_dir)

    outcome = plugin_switch.retry_approval(
        conn, residual["approval_id"], decided_by="human", now=NOW,
        plugins_root=plugins_root, settings=SETTINGS)

    assert journal_store.list_non_terminal(conn) == [], \
        "版 dir を直せば非終端行が残らない (bless の残余は永久凍結ではない)"
    assert outcome.outcome == "deployed"
    assert _status(conn, residual["approval_id"]) == "approved"


@pytest.mark.slow
def test_ac19e_bless_residual_unrepaired_retry_raises_history_git_error(tmp_path, monkeypatch):
    """AC-19e (repaired=False の側): **申告 (設計との食い違い)**。
    設計 (Step 13-a #6) は「直さなければ `still_pending(reverify_failed)`
    になり非終端行が 0 本になる」と書くが、実測するとそうならない —
    `_advance_to_decided` は毎回無条件で `history_git.record_version` を
    呼び直すため (このファイル冒頭の T13 節の注)、直さずに retry すると
    **改竄済みの版 dir を `record_version` 自身が独立に再照合して
    `HistoryGitError` を送出する** (T13 の switch_required=0 分岐に到達する
    前)。この例外は `switch.py`・`retry_approval`・呼び出し元のどこにも
    catch されず、**残余行は非終端のまま残り** (`still_pending` にはならず、
    plugin 名は凍ったまま) 例外が upstream へ抜ける。設計の記述と実際の
    挙動が食い違う (指揮者へ要申告) ので、ここでは**実測した実際の挙動**
    を pin する。"""
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")
    assert main(["plugin", "bless", "sma", "--from", "_human"]) == 0
    conn = db_store.connect(root / _DB)
    plugins_root = root / "plugins"
    human_dir = plugins_root / "_human" / "sma"

    _record_then_tamper_version_dir(monkeypatch)
    with pytest.raises(RuntimeError, match="content_hash mismatch"):
        plugin_switch.bless_candidate(
            conn, name="sma", human_dir=human_dir, settings=SETTINGS,
            now=NOW, decided_by="human_cli")
    residual = journal_store.list_non_terminal(conn)[0]

    # 版 dir を直さずそのまま retry する。patch (record_then_tamper) は
    # 維持したまま — 実運用では改竄が残ったまま、という状況の模擬。
    from agentic_fx.plugin import history_git as history_git_mod
    with pytest.raises(history_git_mod.HistoryGitError, match="index blob hash mismatch"):
        plugin_switch.retry_approval(
            conn, residual["approval_id"], decided_by="human", now=NOW,
            plugins_root=plugins_root, settings=SETTINGS)

    assert journal_store.list_non_terminal(conn) == [dict(residual)], \
        "残余行は非終端のまま残る (still_pending への収束はしない)"
    assert _status(conn, residual["approval_id"]) == "pending"
