"""P1 submit / P2 approve / P3 bless の SQL sequence (プラン 10 Task 11d、
設計書 §5.1・§8.1-28・§8.1-29・§8.1-41)。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_settings
from agentic_fx.plugin import switch
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)

INDICATOR_PY = "def compute(df, params):\n    return {'v': 1.0}\n"
CONFIG_YAML = "kind: indicator\n"
TEST_PY_OK = "def test_x():\n    pass\n"


def _write_candidate(dirpath: Path) -> None:
    dirpath.mkdir(parents=True)
    (dirpath / "plugin.py").write_text(INDICATOR_PY)
    (dirpath / "config.yaml").write_text(CONFIG_YAML)
    (dirpath / "test_plugin.py").write_text(TEST_PY_OK)


@pytest.fixture
def env(tmp_path):
    root = tmp_path
    plugins_dir = root / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    conn = db_store.connect(root / "agentic.db")
    db_store.init_db(conn)
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    return root, plugins_dir, conn, settings


def _fake_pytest_ok(plugin_dir, *, settings):
    from agentic_fx.plugin.gate_pytest import GateResult
    return GateResult(passed=True, returncode=0, stdout_tail="1 passed", duration_sec=0.1)


# --- P1: submit ---

def test_submit_creates_pending_without_journal(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    row = approvals_store.pending(conn, kind="plugin")[0]
    assert row["id"] == approval_id
    assert row["status"] == "pending"
    journal_rows = conn.execute("SELECT COUNT(*) c FROM plugin_switch_journal").fetchone()
    assert journal_rows["c"] == 0  # P1 はジャーナルを作らない


def test_submit_takes_plugin_lock_in_locks_dir(env, monkeypatch):
    """§5.1 手順 1 の pin: flock の対象は `plugins/.locks/<name>.lock`
    (実装者追加テスト — plugins_root の逆算経路 (staging_dir.parent.parent)
    を lock ファイルの実在位置で確認する)。"""
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    assert (plugins_dir / ".locks" / "sma.lock").is_file()


def test_submit_staging_candidate_not_deleted_while_pending(env, monkeypatch):
    """§2.3・§5.1 手順 3 の掃除所有 pin (統合裁定、簡略化 7 の解消):
    staging 候補は pending の間は削除されない (削除は終端決定の tx 直後の
    み)。M9 の killer。"""
    root, plugins_dir, conn, settings = env
    candidate_dir = plugins_dir / "_staging" / "1" / "sma"
    _write_candidate(candidate_dir)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    assert candidate_dir.is_dir()
    assert (candidate_dir / "plugin.py").read_text() == INDICATOR_PY


def test_submit_gate_failure_creates_no_approval_row(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")

    def _fake_pytest_fail(plugin_dir, *, settings):
        from agentic_fx.plugin.gate_pytest import GateResult
        return GateResult(passed=False, returncode=1, stdout_tail="1 failed", duration_sec=0.1)

    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_fail)
    with pytest.raises(ValueError):
        switch.submit_candidate(
            conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW)
    assert approvals_store.pending(conn, kind="plugin") == []


# --- P2: approve, live=absent ---

def test_approve_live_absent_creates_version_git_and_switches(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    switch.approve_candidate(conn, approval_id, decided_by="human", now=NOW, plugins_root=plugins_dir, settings=settings)  # B-1

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "approved"
    live = plugins_dir / "sma"
    assert live.is_symlink()
    assert live.readlink().as_posix().startswith(".versions/sma/")
    journal_row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()
    assert journal_row["phase"] == "decided"
    # staging 候補は終端後に削除される
    assert not (plugins_dir / "_staging" / "1" / "sma").exists()


# --- P2: approve, live=plain (legacy) ---

# precheck 2026-08-22 wave2: T11-B6 / T11-M9
def test_approve_live_plain_stays_pending_with_legacy_reason(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "sma")  # legacy plain live
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    switch.approve_candidate(conn, approval_id, decided_by="human", now=NOW, plugins_root=plugins_dir, settings=settings)  # B-1

    row = conn.execute("SELECT status, reason FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"  # 決定しない
    assert row["reason"] == "legacy_plain_present"  # B-6: 厳密一致で書かれること
    assert (plugins_dir / "sma").is_dir() and not (plugins_dir / "sma").is_symlink()
    # 版は作られている (git まで進める) — M-9 是正: 意味不明な式を「非空」に書き直す
    version_dirs = list((plugins_dir / ".versions" / "sma").glob("*"))
    assert version_dirs != []
    journal_rows = conn.execute("SELECT COUNT(*) c FROM plugin_switch_journal").fetchone()
    assert journal_rows["c"] == 0  # ジャーナルは作らない


# --- P3: bless, live=absent ---

def test_bless_human_live_absent_single_tx_creates_pending_plus_journal(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_human" / "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    approval_id = switch.bless_candidate(
        conn, name="sma", human_dir=plugins_dir / "_human" / "sma",
        settings=settings, now=NOW, decided_by="human_cli")

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "approved"
    assert (plugins_dir / "sma").is_symlink()
    # _human は削除されない (人間所有)
    assert (plugins_dir / "_human" / "sma").is_dir()


# --- P3: bless, live=plain ---

def test_bless_human_live_plain_no_journal_stays_pending(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "sma")
    _write_candidate(plugins_dir / "_human" / "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    approval_id = switch.bless_candidate(
        conn, name="sma", human_dir=plugins_dir / "_human" / "sma",
        settings=settings, now=NOW, decided_by="human_cli")

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"
    journal_rows = conn.execute("SELECT COUNT(*) c FROM plugin_switch_journal").fetchone()
    assert journal_rows["c"] == 0


def test_bless_absent_symlink_single_tx_rolls_back_atomically_on_journal_failure(
        env, monkeypatch):
    """§5.1 P3 表 5A の 1-tx 性 fault injection (統合裁定、簡略化 8 の解消、
    M5 の killer): pending+証跡+preparing ジャーナルを作る 1 tx の途中
    (begin_switch_journal 呼び出し内) で例外を注入すると、ロールバックで
    3 つとも作られていない状態に戻ることを確認する — pending+証跡+
    ジャーナルを別々の tx にする変異 (M5) では、この注入で pending 行だけが
    先にコミットされてしまい本テストが失敗する。"""
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_human" / "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    def _raise_inside_5a(*a, **k):
        raise RuntimeError("injected failure inside the 1-tx 5A step")

    monkeypatch.setattr(switch, "begin_switch_journal", _raise_inside_5a)

    with pytest.raises(RuntimeError, match="injected failure"):
        switch.bless_candidate(
            conn, name="sma", human_dir=plugins_dir / "_human" / "sma",
            settings=settings, now=NOW, decided_by="human_cli")

    # ロールバックにより pending も証跡もジャーナルも作られていない
    # (1 tx で揃えていれば、途中の例外は COMMIT 前のロールバックに収束する)
    assert approvals_store.pending(conn, kind="plugin") == []
    journal_rows = conn.execute("SELECT COUNT(*) c FROM plugin_switch_journal").fetchone()
    assert journal_rows["c"] == 0
    backtest_rows = conn.execute("SELECT COUNT(*) c FROM backtest_runs").fetchone()
    assert backtest_rows["c"] == 0


# --- §8.1-41: bless strategy without enough trades creates neither approval nor journal ---

def test_bless_strategy_below_min_trades_creates_nothing(env, monkeypatch):
    """M6 の killer。**held (逸脱)**: `plugin/strategy_gate.py`
    (`evaluate_strategy_adoption_gate`、プラン10 Task 10 の産物) がこの
    worktree にまだ存在しない (Task 10 は docs コミットのみで実コードは
    未着手) — 11d の Step 3 指示は `run_kind_gate` を `_validate_kind` の
    rename/export に留め検証ロジックを変えないことを求めており (二重実装
    禁止)、strategy_gate 経由への切替はここでは行っていない。Task 10 が
    実装され `plugin/strategy_gate.py` が着地するまでは
    `pytest.importorskip` で自動的に有効化される形にして保留する。"""
    pytest.importorskip("agentic_fx.plugin.strategy_gate")
    root, plugins_dir, conn, settings = env
    strategy_py = "def evaluate(df, indicators, signals, params):\n    return {'action': 'hold', 'rationale': 'x'}\n"
    strategy_cfg = "kind: strategy\ntimeframe: 1h\npairs: [USDJPY]\nexit_mode: levels\n"
    d = plugins_dir / "_human" / "st"
    d.mkdir(parents=True)
    (d / "plugin.py").write_text(strategy_py)
    (d / "config.yaml").write_text(strategy_cfg)
    (d / "test_plugin.py").write_text(TEST_PY_OK)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    def _zero_trades_gate(conn, meta, *, settings, now, record_fn=None):
        # strategy_gate.evaluate_strategy_adoption_gate のフェイク
        # (統合裁定 R-i3: bless_candidate はこの関数だけを呼ぶ — run_in_sample
        # を直接叩かない)。evaluable=False で ValueError を上げる契約は
        # 実装 (plugin/strategy_gate.py, Task 10) 側の責務。
        raise ValueError("not evaluable: trades=0 < EVALUABLE_MIN_TRADES")

    monkeypatch.setattr(
        "agentic_fx.plugin.strategy_gate.evaluate_strategy_adoption_gate",
        _zero_trades_gate)
    with pytest.raises(ValueError, match="evaluable"):
        switch.bless_candidate(
            conn, name="st", human_dir=d, settings=settings, now=NOW,
            decided_by="human_cli")
    assert approvals_store.pending(conn) == []
    journal_rows = conn.execute("SELECT COUNT(*) c FROM plugin_switch_journal").fetchone()
    assert journal_rows["c"] == 0


# --- 検収 m6: 未完ジャーナル (別 approval_id) は UnresolvedJournalError ---

def test_approve_raises_unresolved_journal_error_for_different_approval_id(env):
    """検収 m6 是正: 同名で別 approval_id の未完ジャーナルが在るとき、
    `approve_candidate` は bare `ValueError` ではなく `UnresolvedJournalError`
    を送出する (`retire_plugin` と同じ型に統一 — CLI `_plugin_retire` が
    catch する型と揃える)。"""
    root, plugins_dir, conn, settings = env
    approval_1 = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/sma"},
        now=NOW)
    switch.begin_switch_journal(
        conn, kind="approve", approval_id=approval_1, name="sma",
        old_kind="absent", old_target=None,
        new_target=f".versions/sma/{'a' * 64}", switch_required=True,
        actor="human", now=NOW, commit=True)
    approval_2 = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h2", "artifact_hash": "a2",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/2/sma"},
        now=NOW)

    with pytest.raises(switch.UnresolvedJournalError):
        switch.approve_candidate(conn, approval_2, decided_by="human", now=NOW,
                                 plugins_root=plugins_dir, settings=settings)


# --- §8.1-29: candidate_missing ---

def test_approve_candidate_missing_stays_pending(env, monkeypatch):
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)
    # 候補を消してしまう (mission の staging が既に掃除された想定)
    import shutil
    shutil.rmtree(plugins_dir / "_staging" / "1" / "sma")

    switch.approve_candidate(conn, approval_id, decided_by="human", now=NOW, plugins_root=plugins_dir, settings=settings)  # B-1

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"
    assert not (plugins_dir / "sma").exists()


def test_no_direct_decide_calls_in_plugin_module(tmp_path):
    """裁定1: switch.py/approval.py/commands.py は approvals_store.decide を
    直接呼ばない (apply_decision を経由する — grep-zero pin、B-4 是正で
    commands.py を対象に追加)。"""
    import subprocess
    result = subprocess.run(
        ["grep", "-rn", r"approvals_store\.decide(\|approvals\.decide(",
         "src/agentic_fx/plugin/switch.py", "src/agentic_fx/plugin/approval.py",
         "src/agentic_fx/commands.py"],
        capture_output=True, text=True)
    assert result.stdout == "", f"unexpected decide() call sites:\n{result.stdout}"


# --- 検収 B3: 0d early-exit の再開前再検証 (設計書 §5.1-1 (a)) ---
#
# 「switched まで進んだ後に crash → 再起動/再試行」を、approve_candidate を
# 一度 `_finalize_decision` の直前で意図的に止めて再現する (crash probe
# C-HEALTHY と同じ形: journal は phase='switched' まで commit 済み・
# approval はまだ pending)。旧稿 (0d early-exit) はここから直接
# `_finalize_decision` へ進み、新版の存在/hash を一切確かめずに
# approved へ確定してしまっていた (acceptance-task11.md B3)。


def _simulate_crash_after_switch_before_decide(root, plugins_dir, conn, settings,
                                               monkeypatch, *, name="sma"):
    """approve_candidate を「switch_live 完了直後、_finalize_decision の
    直前」で止め、journal を phase='switched' のまま・approval を pending
    のまま残す。戻り値は (approval_id, artifact_hash)。"""
    _write_candidate(plugins_dir / "_staging" / "1" / name)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id = switch.submit_candidate(
        conn, name=name, staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)
    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    import json
    artifact_hash = json.loads(row["payload_json"])["artifact_hash"]

    def _crash(*a, **kw):
        raise RuntimeError("simulated crash before decide")

    monkeypatch.setattr("agentic_fx.plugin.switch._finalize_decision", _crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        switch.approve_candidate(conn, approval_id, decided_by="human", now=NOW,
                                 plugins_root=plugins_dir, settings=settings)
    monkeypatch.undo()  # _finalize_decision と run_gate_pytest の両方を戻す

    journal_row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()
    assert journal_row["phase"] == "switched"  # 前提の確認
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"  # 前提の確認
    return approval_id, artifact_hash


def test_switched_journal_reverify_recreates_missing_version_from_staging_candidate(
        env, monkeypatch):
    """新版が欠損していても、pending の間残る staging 候補から冪等に
    再作成できれば再開は完了する (設計 §5.1-1 (a) 「新版が欠損なら保持
    している staging/_human 候補から再作成」)。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id, artifact_hash = _simulate_crash_after_switch_before_decide(
        root, plugins_dir, conn, settings, monkeypatch)

    # 新版ディレクトリを丸ごと消す (crash 後に版ストアが壊れた/掃除された想定)
    import shutil
    import stat
    version_dir = plugins_dir / ".versions" / "sma" / artifact_hash
    version_dir.chmod(0o700)
    shutil.rmtree(version_dir)
    assert not version_dir.exists()
    # staging 候補はまだ残っている (pending の間は削除されない — §2.3)
    assert (plugins_dir / "_staging" / "1" / "sma" / "plugin.py").exists()

    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    switch.retry_approval(conn, approval_id, decided_by="human", now=NOW,
                          plugins_root=plugins_dir, settings=settings)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "approved"
    journal_row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()
    assert journal_row["phase"] == "decided"
    assert version_dir.is_dir()  # 再作成された
    live = plugins_dir / "sma"
    assert live.is_symlink()
    assert live.readlink().as_posix() == f".versions/sma/{artifact_hash}"


def test_switched_journal_reverify_fails_closed_when_version_and_candidate_missing(
        env, monkeypatch):
    """検収 B3 の中核 pin: 新版が欠損し、再作成材料の候補も消えている
    (=「保持している候補」が無い) 場合、0d early-exit は無条件に
    approved へ進んではならない — journal を 'reverted' で閉じ、live を
    旧状態 (absent) へ戻し、approval は pending のまま、activity ERROR
    (`switch_reverify_failed`) を書くこと。

    旧稿 (再検証なしの 0d early-exit) はここで `_finalize_decision` を
    直接呼び 'approved'+'decided' へ確定してしまう — これが acceptance
    B3 の実測 (「8a が検出した不整合は次回起動で自動的に approve に化ける」)
    そのもの。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id, artifact_hash = _simulate_crash_after_switch_before_decide(
        root, plugins_dir, conn, settings, monkeypatch)

    import shutil
    version_dir = plugins_dir / ".versions" / "sma" / artifact_hash
    version_dir.chmod(0o700)
    shutil.rmtree(version_dir)
    # 再作成材料の候補も消す (mission の掃除が先に走った/人間が消した想定)
    shutil.rmtree(plugins_dir / "_staging" / "1" / "sma")

    activity = ActivityLog(root / "logs" / "activity.log")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    switch.retry_approval(conn, approval_id, decided_by="human", now=NOW,
                          plugins_root=plugins_dir, settings=settings,
                          activity=activity)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending", (
        "再検証に失敗したのに approved へ確定してしまった (B3 の欠陥)")
    journal_row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()
    assert journal_row["phase"] == "reverted"
    live = plugins_dir / "sma"
    assert not live.exists() and not live.is_symlink()  # old_kind=absent へ復帰

    log_text = (root / "logs" / "activity.log").read_text()
    assert "switch_reverify_failed" in log_text
    assert Category.APPROVAL.value in log_text
