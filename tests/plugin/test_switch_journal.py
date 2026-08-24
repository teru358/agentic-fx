"""plugin_switch_journal の phase 遷移・収束規則 (プラン 10 Task 11c、
設計書 §5.1-1・§8.1-30・§8.1-31)。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

# precheck 2026-08-22 wave2: T11-B1
from agentic_fx.config import load_settings
from agentic_fx.plugin import switch
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")  # B-1: reconcile_switch_journals が settings を要求する


@pytest.fixture
def conn(tmp_path):
    c = db_store.connect(tmp_path / "agentic.db")
    db_store.init_db(c)
    return c


def _plugins_root(tmp_path) -> Path:
    root = tmp_path / "plugins"
    root.mkdir(exist_ok=True)
    return root


# --- 表 1: preparing の temp_path locator (§8.1-31) ---

def test_begin_switch_journal_derives_temp_path_from_op_id(conn):
    """temp_path は 'plugins/.<name>.link-<op_id>' として INSERT 時に確定
    (op_id 起点の locator、§5.1 手順 1・§8.1-31)。"""
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=".versions/sma/" + "a" * 64,
        switch_required=True, actor="human", now=NOW, commit=True)
    row = conn.execute(
        "SELECT temp_path, phase FROM plugin_switch_journal WHERE op_id=?",
        (op_id,)).fetchone()
    assert row["temp_path"] == f"plugins/.sma.link-{op_id}"
    assert row["phase"] == "preparing"


def test_non_terminal_journal_rows_limited_to_one_per_name(conn):
    """部分 UNIQUE index: 非終端 phase (decided/reverted 以外) は name ごとに
    高々 1 件 (§8.1-31)。"""
    switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=".versions/sma/" + "a" * 64,
        switch_required=True, actor="human", now=NOW, commit=True)
    with pytest.raises(Exception):  # sqlite3.IntegrityError
        switch.begin_switch_journal(
            conn, kind="approve", approval_id=2, name="sma", old_kind="absent",
            old_target=None, new_target=".versions/sma/" + "b" * 64,
            switch_required=True, actor="human", now=NOW, commit=True)


def test_decided_and_reverted_rows_do_not_count_toward_unique(conn):
    """decided/reverted (終端) は部分 UNIQUE の対象外 — 同名で新しい非終端行
    を作れる。"""
    op1 = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=".versions/sma/" + "a" * 64,
        switch_required=True, actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op1, phase="versioned", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op1, phase="recorded", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op1, phase="switched", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op1, phase="decided", now=NOW, commit=True)
    op2 = switch.begin_switch_journal(  # 例外にならない
        conn, kind="approve", approval_id=2, name="sma", old_kind="symlink",
        old_target=".versions/sma/" + "a" * 64,
        new_target=".versions/sma/" + "b" * 64,
        switch_required=True, actor="human", now=NOW, commit=True)
    assert op2 != op1


# --- 表 1: switched の収束規則 (3 分岐、old_kind ごと) ---

def test_switched_recovery_live_equals_new_target_delegates_to_retry_approval(
        tmp_path, conn, monkeypatch):
    """switched かつ live==new_target の完遂は reconcile 自身が phase を
    'decided' に書き換えるのではなく、retry_approval (11e、内部で
    approve_candidate → apply_decision の同一 tx) に委譲する (統合裁定、
    簡略化 2 の解消)。11c 単体では approve_candidate の全機構 (gate/版/git)
    は未定義のため、ここでは委譲そのものを spy で確認し、実際に
    phase='decided' へ進むことの確認は 11d/11e の統合テストに任せる。"""
    root = _plugins_root(tmp_path)
    new_rel = f".versions/sma/{'a' * 64}"
    (root / "sma").symlink_to(new_rel)
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=new_rel, switch_required=True,
        actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="switched", now=NOW, commit=True)

    calls = []
    # raising=False: retry_approval は 11e で switch.py に追記される名前。
    # 11c 単体の実装段階ではまだモジュールに存在しないため raising=False で
    # 差し替える (同一ファイル switch.py を 11c→11d→11e の順で継ぎ足す構造 —
    # 11e 完了後に本テストを再実行すると raising=False が無くても通る)。
    monkeypatch.setattr(
        switch, "retry_approval",
        lambda c, approval_id, *, decided_by, now, plugins_root, settings,
               activity=None: calls.append(
            (approval_id, decided_by)), raising=False)  # B-1: plugins_root/settings も受ける。B3: activity も透過される

    switch.reconcile_switch_journals(conn, plugins_root=root, now=NOW, settings=SETTINGS)

    assert calls == [(1, "system_reconcile")]
    # reconcile 自身は phase を書き換えない (decided への遷移は
    # apply_decision と同一 tx でしか起きない契約)
    row = conn.execute("SELECT phase FROM plugin_switch_journal WHERE op_id=?",
                       (op_id,)).fetchone()
    assert row["phase"] == "switched"


def test_switched_recovery_live_equals_old_target_reverts_symlink(tmp_path, conn):
    root = _plugins_root(tmp_path)
    old_rel = f".versions/sma/{'o' * 64}"
    (root / "sma").symlink_to(old_rel)  # rename が実は起きていなかった
    new_rel = f".versions/sma/{'n' * 64}"
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="symlink",
        old_target=old_rel, new_target=new_rel, switch_required=True,
        actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="switched", now=NOW, commit=True)

    switch.reconcile_switch_journals(conn, plugins_root=root, now=NOW, settings=SETTINGS)
    row = conn.execute("SELECT phase FROM plugin_switch_journal WHERE op_id=?",
                       (op_id,)).fetchone()
    assert row["phase"] == "reverted"
    assert Path(root / "sma").readlink().as_posix() == old_rel


def test_switched_recovery_neither_target_is_error_and_untouched(tmp_path, conn, caplog):
    """live が old でも new でもない (第三者が触った) → activity ERROR で
    人間待ち、live には触らない。"""
    root = _plugins_root(tmp_path)
    third_party = f".versions/sma/{'z' * 64}"
    (root / "sma").symlink_to(third_party)
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=f".versions/sma/{'n' * 64}",
        switch_required=True, actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="switched", now=NOW, commit=True)

    switch.reconcile_switch_journals(conn, plugins_root=root, now=NOW, settings=SETTINGS)
    row = conn.execute("SELECT phase FROM plugin_switch_journal WHERE op_id=?",
                       (op_id,)).fetchone()
    assert row["phase"] == "switched"  # 触らない (非終端のまま)
    assert Path(root / "sma").readlink().as_posix() == third_party


def test_switched_recovery_neither_target_writes_unrecognized_live_target_activity(
        tmp_path, conn):
    """確定-16 (B-16): else 分岐 (第三者に触られた) の activity 記録は
    `test_switched_recovery_neither_target_is_error_and_untouched` が
    phase の未変化だけを見ており、activity へ渡す文字列そのものは
    pin していなかった (`b16_src_drop_else_activity` が文字列だけを
    差し替えても SURVIVED)。"""
    from agentic_fx.activity import ActivityLog

    root = _plugins_root(tmp_path)
    third_party = f".versions/sma/{'z' * 64}"
    (root / "sma").symlink_to(third_party)
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=f".versions/sma/{'n' * 64}",
        switch_required=True, actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="switched", now=NOW, commit=True)

    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    switch.reconcile_switch_journals(conn, plugins_root=root, now=NOW, settings=SETTINGS,
                                     activity=activity)

    log_text = (tmp_path / "logs" / "activity.log").read_text()
    assert "switch_reconcile_unrecognized_live_target" in log_text


def test_reconcile_one_row_failure_does_not_block_other_rows(tmp_path, conn, monkeypatch):
    """検収 m10 の pin: `reconcile_switch_journals` は per-row try/except を
    持たなければならない — §5.1-1 の収束規則は「行ごとの規則」であり、1 行
    (name='sma') の異常が別の name ('wma') の収束を止めてはならない。
    旧稿はループに try/except が無く、1 行の例外がループ全体を中断させて
    いた (acceptance-task11.md m10 の実測)。"""
    root = _plugins_root(tmp_path)

    # 行 1 (sma): live==new_target → retry_approval へ委譲する分岐だが、
    # ここを monkeypatch で例外送出させ「異常のある行」を模す。
    new_rel = f".versions/sma/{'n' * 64}"
    (root / "sma").symlink_to(new_rel)
    op_id_1 = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=new_rel, switch_required=True,
        actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id_1, phase="switched", now=NOW, commit=True)

    # 行 2 (wma): live==old_target → revert 分岐 (異常なし、正常に収束すべき)
    old_rel = f".versions/wma/{'o' * 64}"
    (root / "wma").symlink_to(old_rel)
    op_id_2 = switch.begin_switch_journal(
        conn, kind="approve", approval_id=2, name="wma", old_kind="symlink",
        old_target=old_rel, new_target=f".versions/wma/{'w' * 64}",
        switch_required=True, actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id_2, phase="switched", now=NOW, commit=True)

    def _boom(*a, **kw):
        raise RuntimeError("simulated per-row failure (sma)")

    monkeypatch.setattr(switch, "retry_approval", _boom)

    from agentic_fx.activity import ActivityLog
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    switch.reconcile_switch_journals(conn, plugins_root=root, now=NOW,
                                     settings=SETTINGS, activity=activity)

    row_1 = conn.execute("SELECT phase FROM plugin_switch_journal WHERE op_id=?",
                         (op_id_1,)).fetchone()
    row_2 = conn.execute("SELECT phase FROM plugin_switch_journal WHERE op_id=?",
                         (op_id_2,)).fetchone()
    # 行 1 は例外を吸収され非終端のまま残る (次回 reconcile が再試行)
    assert row_1["phase"] == "switched"
    # 行 2 (異常の無い行) は行 1 の異常に巻き込まれずに正常収束する — これが
    # m10 の中核 pin (旧稿では per-row try/except が無いため、行 1 の例外が
    # ループを中断し行 2 が 'switched' のまま放置されていた)
    assert row_2["phase"] == "reverted"
    assert Path(root / "wma").readlink().as_posix() == old_rel

    log_text = (tmp_path / "logs" / "activity.log").read_text()
    assert "switch_reconcile_row_failed" in log_text
    assert "sma" in log_text


def test_switched_recovery_absent_old_kind_with_no_live_reverts(tmp_path, conn):
    """確定-13: reconcile の「old_kind=absent かつ live 不在」復帰分岐
    (`row["old_kind"] == "absent" and live_target is None`) が未 pin
    だった (findings.json #234)。既存の switched 収束テスト 3 本はいずれも
    live symlink が存在する状態しか作らない。この分岐が無いと「absent から
    切替中に落ち、live がまだ作られていない」行が else (第三者改変扱い) に
    落ちて非終端のまま残り、確定-1 の閉塞に合流する。"""
    root = _plugins_root(tmp_path)
    new_rel = f".versions/sma/{'a' * 64}"
    # old_kind="absent" の通常呼び出し元 (approve_candidate) は必ず
    # old_target=None を渡すため、`live_target == old_norm` (disjunct 1)
    # だけで `None == None` が成立し disjunct 2 は実質到達不能になって
    # しまう (実測で確認済み — mutation b34_drop_absent_disjunct はこの
    # シナリオでは無効変異になる)。disjunct 2 が実際にコード上に存在する
    # 意味を確かめるため、ここでは begin_switch_journal を直接呼び
    # old_target に非 None 値を持つ absent 行を作る (§5.1 の型ヒントは
    # old_kind=absent なら old_target=None を期待するが、journal_store 層
    # は DB カラムとして両者を独立に受け付ける — 分岐そのものを exercise する)。
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target="bogus-stale-old-target", new_target=new_rel,
        switch_required=True, actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="switched", now=NOW, commit=True)
    # live symlink をまだ作っていない (switched マーク直後・切替直前で
    # クラッシュした想定) — force_revert_op_id は使わず通常の起動時
    # reconcile 経路を通す。
    assert not (root / "sma").exists()

    switch.reconcile_switch_journals(conn, plugins_root=root, now=NOW, settings=SETTINGS)

    row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE op_id=?", (op_id,)).fetchone()
    assert row["phase"] == "reverted", (
        "old_kind=absent かつ live 不在の switched 行が収束せず非終端のまま "
        "残った (確定-13 の欠陥)")
    assert not (root / "sma").exists()


# --- 段 0 独立発見 (1): `_PHASE_ORDER` の実行時強制 ---

def test_advance_switch_journal_raises_on_backward_phase_move(conn):
    """`_PHASE_ORDER` (preparing→versioned→recorded→switched→decided→
    reverted) は段 0 変異スイープの時点で定義行以外に参照が無い死にコード
    だった。`advance_switch_journal` はここで初めて単調性を実行時に検査
    する: 既に到達した phase より前の phase へ `advance` しようとすると
    `ValueError`。"""
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=f".versions/sma/{'a' * 64}",
        switch_required=True, actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="versioned", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="recorded", now=NOW, commit=True)
    with pytest.raises(ValueError, match="order violation"):
        switch.advance_switch_journal(conn, op_id, phase="versioned", now=NOW, commit=True)
    # 後方移動を試みても現在の phase は変わっていない
    row = conn.execute("SELECT phase FROM plugin_switch_journal WHERE op_id=?",
                       (op_id,)).fetchone()
    assert row["phase"] == "recorded"


def test_advance_switch_journal_raises_value_error_on_unknown_op_id(conn):
    """E2 裁定 (2026-08-25): 存在しない op_id への `advance_switch_journal`
    は `row["phase"]` の `TypeError`(不透明) ではなく明示的な
    `ValueError(f"op_id={op_id} not found")` を送出する。"""
    with pytest.raises(ValueError, match=r"op_id=999999 not found"):
        switch.advance_switch_journal(conn, 999999, phase="versioned", now=NOW, commit=True)


# --- 表 3: switch_required=0 は switched を経ない ---

def test_switch_required_false_skips_switched_phase(conn):
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="symlink",
        old_target=f".versions/sma/{'a' * 64}",
        new_target=f".versions/sma/{'a' * 64}",  # 旧新同一
        switch_required=False, actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="versioned", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="recorded", now=NOW, commit=True)
    with pytest.raises(ValueError, match="switch_required=0"):
        switch.advance_switch_journal(conn, op_id, phase="switched", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="decided", now=NOW, commit=True)  # OK


# --- 表 2: 割込操作の巻き戻し (old_kind ごと) ---

def test_interrupt_reverts_absent_by_removing_live_symlink(tmp_path, conn):
    root = _plugins_root(tmp_path)
    new_rel = f".versions/sma/{'a' * 64}"
    (root / "sma").symlink_to(new_rel)
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=new_rel, switch_required=True,
        actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="switched", now=NOW, commit=True)
    # 割込: reject が別プロセスから来た想定 (reconcile が先に巻き戻す)
    switch.reconcile_switch_journals(conn, plugins_root=root, now=NOW, settings=SETTINGS,
                                     force_revert_op_id=op_id)
    assert not (root / "sma").exists()


def test_interrupt_reverts_symlink_by_restoring_old_target(tmp_path, conn):
    root = _plugins_root(tmp_path)
    old_rel = f".versions/sma/{'o' * 64}"
    new_rel = f".versions/sma/{'n' * 64}"
    (root / "sma").symlink_to(new_rel)
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="symlink",
        old_target=old_rel, new_target=new_rel, switch_required=True,
        actor="human", now=NOW, commit=True)
    switch.advance_switch_journal(conn, op_id, phase="switched", now=NOW, commit=True)
    switch.reconcile_switch_journals(conn, plugins_root=root, now=NOW, settings=SETTINGS,
                                     force_revert_op_id=op_id)
    assert Path(root / "sma").readlink().as_posix() == old_rel
