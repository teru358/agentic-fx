"""P1 submit / P2 approve / P3 bless の SQL sequence (プラン 10 Task 11d、
設計書 §5.1・§8.1-28・§8.1-29・§8.1-41)。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_settings
from agentic_fx.plugin import switch, version_store
from agentic_fx.store import approvals as approvals_store
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_switch_journal as journal_store

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


# --- 段 0 M12: P2 が「稼働中 live symlink を新版へ差し替える」本命経路 ---

def _write_candidate_v(dirpath: Path, v: float) -> None:
    """`_write_candidate` の内容を可変にした版 (段 0 probe 転用)。同一内容
    だと content_hash が一致して switch_required が正典でも 0 になり
    恒真化するため、2 つの候補の内容を必ず変えること。"""
    dirpath.mkdir(parents=True)
    (dirpath / "plugin.py").write_text(
        f"def compute(df, params):\n    return {{'v': {v}}}\n")
    (dirpath / "config.yaml").write_text(CONFIG_YAML)
    (dirpath / "test_plugin.py").write_text(TEST_PY_OK)


def test_approve_upgrades_live_symlink_to_the_new_version(env, monkeypatch):
    """段 0 M12 の killer: `switch_required` の算出から `old_target ==
    new_target` の項を落とすと、稼働中 (live symlink 有り) の名前を別の版へ
    アップグレードする P2 の本命経路が無検証のまま緑になる — この経路を
    直接踏む。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    _write_candidate_v(plugins_dir / "_staging" / "1" / "sma", 1.0)
    a1 = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)
    switch.approve_candidate(conn, a1, decided_by="h", now=NOW,
                             plugins_root=plugins_dir, settings=settings)
    live = plugins_dir / "sma"
    assert live.is_symlink()
    old_target = live.readlink().as_posix()

    _write_candidate_v(plugins_dir / "_staging" / "2" / "sma", 2.0)  # 別内容
    a2 = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "2",
        candidate_origin="staging", mission_id=2, backlog_id=None,
        settings=settings, now=NOW)
    import json
    ah2 = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (a2,)).fetchone()["payload_json"])["artifact_hash"]
    switch.approve_candidate(conn, a2, decided_by="h", now=NOW,
                             plugins_root=plugins_dir, settings=settings)

    row2 = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (a2,)).fetchone()
    assert row2["status"] == "approved"
    assert live.readlink().as_posix() == f".versions/sma/{ah2}"
    assert live.readlink().as_posix() != old_target
    j = conn.execute("SELECT phase, switch_required FROM plugin_switch_journal "
                     "WHERE approval_id=?", (a2,)).fetchone()
    assert (j["phase"], j["switch_required"]) == ("decided", 1)


def test_approve_upgrade_reverifies_content_hash_after_switch(env, monkeypatch):
    """§5.1 手順 8a の pin: switch_required=1 の経路は切替直後に
    content_hash を再照合する。M12 (switch_required の誤算出で 0 になる)
    は `if switch_required:` ブロック自体を丸ごとスキップさせるため、8a の
    再照合コードも一緒に実行されなくなる — 実際に不一致を強制して
    RuntimeError が上がることを直接確かめ、8a が「アップグレード」経路で
    実行されることを pin する (living-target の一致だけでは 8a が走った
    ことの証明にならない)。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    _write_candidate_v(plugins_dir / "_staging" / "1" / "sma", 1.0)
    a1 = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)
    switch.approve_candidate(conn, a1, decided_by="h", now=NOW,
                             plugins_root=plugins_dir, settings=settings)

    _write_candidate_v(plugins_dir / "_staging" / "2" / "sma", 2.0)
    a2 = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "2",
        candidate_origin="staging", mission_id=2, backlog_id=None,
        settings=settings, now=NOW)

    monkeypatch.setattr("agentic_fx.plugin.switch.loader.content_hash",
                        lambda p: "0" * 64)
    with pytest.raises(RuntimeError, match="content_hash mismatch"):
        switch.approve_candidate(conn, a2, decided_by="h", now=NOW,
                                 plugins_root=plugins_dir, settings=settings)

    row2 = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (a2,)).fetchone()
    assert row2["status"] == "pending"


# --- 段 0 非致命 4 ---

def test_is_superseded_key_requires_content_hash_match_not_name_alone(env, monkeypatch):
    """段 0 M13 の killer: `_is_superseded` の後発決定 key を `(name,
    content_hash)` から `name` 単独に落としても緑になった。同名・**同一**
    content_hash の approved 決定 B (B.id > A.id) が既にあっても、A は
    陳腐化しない (invalidated にならない) こと — key に content_hash が
    効いていることを直接 pin する (§8.1-39 D4)。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    a = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)
    import json
    content_hash = json.loads(conn.execute(
        "SELECT payload_json FROM approval_requests WHERE id=?",
        (a,)).fetchone()["payload_json"])["content_hash"]

    # 同名・同一 content_hash の approved 決定 B を手作りする (id は a より
    # 後 = B.id > A.id — 手作りなので b の candidate は実在しなくてよい)
    b = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": content_hash,
                 "artifact_hash": "b" * 64, "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/2/sma"}, now=NOW)
    conn.execute("UPDATE approval_requests SET status='approved' WHERE id=?", (b,))
    conn.commit()
    assert b > a

    switch.approve_candidate(conn, a, decided_by="h", now=NOW,
                             plugins_root=plugins_dir, settings=settings)

    row_a = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                         (a,)).fetchone()
    assert row_a["status"] == "approved"  # invalidated にならない


def test_resolve_candidate_dir_rejects_name_mismatch_in_canonical_path(env):
    """段 0 M21 の killer: `resolve_candidate_dir` の正規形検査から
    `m.group(m.lastindex) != name` の項を落としても緑になった。
    candidate_path の末尾セグメントと `name` が一致しない locator を直接
    `ValueError` で拒否することを pin する (§8.1-29)。"""
    root, plugins_dir, conn, settings = env
    with pytest.raises(ValueError, match="does not match the canonical form"):
        switch.resolve_candidate_dir(
            plugins_dir, candidate_origin="human",
            candidate_path="plugins/_human/other", name="sma")


# --- codex 1 周目是正 I2: 候補ディレクトリ自身が symlink でも locator 検査
#     とスナップショット検査を通ってしまう (verified-codex-round1.md I2) ---


def test_resolve_candidate_dir_rejects_toplevel_symlink_candidate(env, tmp_path):
    """候補ディレクトリの最終成分自身が外部ディレクトリへの symlink の場合、
    `resolve_candidate_dir` は追従せず `ValueError` で拒否する (§2.3:
    dirfd 基準の lstat で {不存在/通常ディレクトリ/正規形相対symlink}
    以外は拒否)。対照に通常ディレクトリの `ema` は正常に返ることを assert。"""
    root, plugins_dir, conn, settings = env
    outside = tmp_path / "outside_evil"
    _write_candidate(outside)
    staging_root = plugins_dir / "_staging" / "1"
    staging_root.mkdir(parents=True)
    (staging_root / "sma").symlink_to(outside, target_is_directory=True)
    _write_candidate(staging_root / "ema")

    with pytest.raises(ValueError):
        switch.resolve_candidate_dir(
            plugins_dir, candidate_origin="staging",
            candidate_path="plugins/_staging/1/sma", name="sma")

    # 対照: 通常ディレクトリは正常に受理される
    result = switch.resolve_candidate_dir(
        plugins_dir, candidate_origin="staging",
        candidate_path="plugins/_staging/1/ema", name="ema")
    assert result == staging_root / "ema"


def test_submit_candidate_refuses_symlinked_staging_dir(env, tmp_path, monkeypatch):
    """統合 pin: `submit_candidate` を symlink 候補で呼ぶと例外が上がり、
    かつ `approval_requests` に行が 1 件も作られないこと (承認台帳の汚染
    阻止が本命)。"""
    root, plugins_dir, conn, settings = env
    outside = tmp_path / "outside_evil2"
    _write_candidate(outside)
    staging_root = plugins_dir / "_staging" / "1"
    staging_root.mkdir(parents=True)
    (staging_root / "sma").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    with pytest.raises(ValueError):
        switch.submit_candidate(
            conn, name="sma", staging_dir=staging_root,
            candidate_origin="staging", mission_id=1, backlog_id=None,
            settings=settings, now=NOW)

    assert approvals_store.pending(conn, kind="plugin") == []


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


# --- 段 0 M09: write-ahead 不変条件 (switched は switch_live の前に commit) ---

def test_switch_live_runs_after_switched_phase_is_committed(env, monkeypatch):
    """段 0 M09 の killer: `advance_switch_journal(phase="switched")` を
    `switch_live` の後へ移す変異は、既存の crash probe
    (`_simulate_crash_after_switch_before_decide`、`_finalize_decision` を
    落とす) では判別できない — その注入点は switch_live と phase 書込みの
    どちらが先でも `phase=='switched'` に見えてしまう。判別できる唯一の
    シームは「switch_live の**間**で落とす」ことなので、本物の switch_live
    を呼んだ直後に raise するラッパへ差し替えて crash を再現し、crash 時点
    で journal の phase が既に 'switched' へ commit 済みであることを直接
    確かめる (設計 §5.1 の phase 表: 「切替 (FS 効果) の前に switched を
    commit する」)。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    real_switch_live = switch.switch_live

    def _crash_after_switch_live(*a, **kw):
        real_switch_live(*a, **kw)
        raise RuntimeError("simulated crash after switch_live")

    monkeypatch.setattr(switch, "switch_live", _crash_after_switch_live)

    with pytest.raises(RuntimeError, match="simulated crash after switch_live"):
        switch.approve_candidate(conn, approval_id, decided_by="human", now=NOW,
                                 plugins_root=plugins_dir, settings=settings)

    journal_row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()
    assert journal_row["phase"] == "switched", (
        "write-ahead 不変条件違反: switch_live 完了時点で phase が既に "
        "'switched' へ commit 済みでなければならない (M09)")
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"


def test_retry_from_recorded_phase_completes_without_order_violation(env, monkeypatch):
    """`_PHASE_ORDER` の実行時強制 (M09 独立発見 (1)) の回帰ガード: 0d の
    「この approval 自身の再試行」経路は、既存ジャーナルが 'preparing'/
    'versioned'/'recorded' で止まっていても `_advance_to_decided` を頭から
    もう一度呼ぶ (§5.1-1 (a))。この再実行は `advance_switch_journal` へ
    'versioned'→'recorded' の**後方**移動を要求することになるため、単調性
    強制を素朴に実装すると (_advance_to_decided 側にスキップガードを
    足さずに advance_switch_journal だけを直そうとすると) この正当な再試行
    経路まで ValueError にしてしまう。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    real_advance = switch.advance_switch_journal

    def _crash_on_switched(conn_, op_id, *, phase, now, commit=False):
        if phase == "switched":
            raise RuntimeError("simulated crash before switched")
        return real_advance(conn_, op_id, phase=phase, now=now, commit=commit)

    monkeypatch.setattr(switch, "advance_switch_journal", _crash_on_switched)
    with pytest.raises(RuntimeError, match="simulated crash before switched"):
        switch.approve_candidate(conn, approval_id, decided_by="human", now=NOW,
                                 plugins_root=plugins_dir, settings=settings)
    monkeypatch.undo()

    journal_row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()
    assert journal_row["phase"] == "recorded"  # 前提の確認

    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    switch.retry_approval(conn, approval_id, decided_by="human", now=NOW,
                          plugins_root=plugins_dir, settings=settings)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "approved"
    journal_row2 = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()
    assert journal_row2["phase"] == "decided"


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


# --- codex 1 周目是正 I1: staging 候補が終端決定 (reject/expired/invalidated)
#     直後に削除されない (verified-codex-round1.md I1) ---


def test_reject_deletes_staging_candidate_immediately(env, monkeypatch):
    """§5.1 手順 3 の掃除所有表: staging 候補は終端決定 (rejected 含む) の
    tx 直後に削除される。sweep_orphans (起動時) を呼ばずに直後の削除を
    pin する。"""
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    switch.reject_candidate(conn, approval_id, decided_by="human", reason="no good",
                            now=NOW, plugins_root=plugins_dir)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "rejected"
    assert not (plugins_dir / "_staging" / "1" / "sma").exists()


def test_reject_nonexistent_approval_id_raises_approval_not_found_error(env):
    """E1 裁定 (2026-08-25): `reject_candidate` の `row is None` 分岐は
    `ApprovalNotFoundError` (AlreadyDecidedError のサブクラス) を送出する
    — 「決定済み」文言のまま「ID 不存在」を誤って報告しない。"""
    root, plugins_dir, conn, settings = env
    with pytest.raises(approvals_store.ApprovalNotFoundError):
        switch.reject_candidate(conn, 999999, decided_by="human", reason="no",
                                now=NOW, plugins_root=plugins_dir)


def test_invalidated_by_superseding_decision_deletes_staging_candidate(env, monkeypatch):
    """同名別 content_hash の後発 approved 決定により invalidated へ落ちる
    経路 (0c) でも、staging 候補が直後に削除されること。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    _write_candidate(plugins_dir / "_staging" / "1" / "sma")
    a = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    # 同名・別 content_hash の approved 決定 B (id > a) を手作りする
    b = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "different-hash",
                 "artifact_hash": "b" * 64, "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/2/sma"}, now=NOW)
    conn.execute("UPDATE approval_requests SET status='approved' WHERE id=?", (b,))
    conn.commit()
    assert b > a

    switch.approve_candidate(conn, a, decided_by="h", now=NOW,
                             plugins_root=plugins_dir, settings=settings)

    row_a = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                         (a,)).fetchone()
    assert row_a["status"] == "invalidated"
    assert not (plugins_dir / "_staging" / "1" / "sma").exists()


def test_human_candidate_survives_reject_and_invalidated_decisions(env, monkeypatch):
    """§5.1 手順 3 の掃除所有表: candidate_origin='human' は自動削除しない
    (人間所有)。reject と invalidated の両経路で `_human/sma` が残ることを
    対照として pin する (process_expired_approvals 側は別ファイルで確認)。"""
    root, plugins_dir, conn, settings = env
    _write_candidate(plugins_dir / "_human" / "sma")

    payload_reject = {
        "name": "sma", "kind": "indicator",
        "candidate_origin": "human", "candidate_path": "plugins/_human/sma",
        "content_hash": "h1", "artifact_hash": "a1",
        "metrics": {}, "evaluable": True,
        "mission_id": None, "backlog_id": None,
    }
    approval_id = approvals_store.create(conn, kind="plugin", payload=payload_reject, now=NOW)
    switch.reject_candidate(conn, approval_id, decided_by="human", reason="no",
                            now=NOW, plugins_root=plugins_dir)
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "rejected"
    assert (plugins_dir / "_human" / "sma" / "plugin.py").exists()

    payload_a2 = dict(payload_reject, content_hash="h2")
    approval_id2 = approvals_store.create(conn, kind="plugin", payload=payload_a2, now=NOW)
    b = approvals_store.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h3", "artifact_hash": "b" * 64,
                 "candidate_origin": "human", "candidate_path": "plugins/_human/sma"},
        now=NOW)
    conn.execute("UPDATE approval_requests SET status='approved' WHERE id=?", (b,))
    conn.commit()
    assert b > approval_id2

    switch.approve_candidate(conn, approval_id2, decided_by="h", now=NOW,
                             plugins_root=plugins_dir, settings=settings)
    row2 = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                        (approval_id2,)).fetchone()
    assert row2["status"] == "invalidated"
    assert (plugins_dir / "_human" / "sma" / "plugin.py").exists()


# --- codex 1 周目是正 I3: switched 再開時の版再検証が artifact_hash を見ない ---
#
# `_new_target_hash_ok` は content_hash (2 本) しか見ておらず、
# test_plugin.py だけが版ディレクトリ内で改変されても「健全」と誤判定
# する (verified-codex-round1.md I3)。候補が健全なら候補から再構築して
# approved に進めるべきで、候補も欠損していれば reverted + pending に
# 落ちるべき。


def test_switched_journal_reverify_rejects_version_dir_with_tampered_test_plugin(
        env, monkeypatch):
    """版ディレクトリの test_plugin.py だけが改変されていても
    (plugin.py / config.yaml は不変 = content_hash は一致する)、
    artifact_hash 不一致 + ディレクトリ名不一致を検出し、候補が健全なので
    候補から版を再構築したうえで approved に進むこと。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id, artifact_hash = _simulate_crash_after_switch_before_decide(
        root, plugins_dir, conn, settings, monkeypatch)

    version_dir = plugins_dir / ".versions" / "sma" / artifact_hash
    version_dir.chmod(0o700)
    (version_dir / "test_plugin.py").chmod(0o600)
    (version_dir / "test_plugin.py").write_text("def test_tampered():\n    pass\n")
    version_dir.chmod(0o500)
    # 候補 (staging) は pending の間は残っているので健全なまま
    assert (plugins_dir / "_staging" / "1" / "sma" / "plugin.py").exists()

    activity = ActivityLog(root / "logs" / "activity.log")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    switch.retry_approval(conn, approval_id, decided_by="human", now=NOW,
                          plugins_root=plugins_dir, settings=settings,
                          activity=activity)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "approved", (
        "test_plugin.py だけの改変を検出できず、候補からの再構築に進めなかった")
    journal_row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()
    assert journal_row["phase"] == "decided"

    from agentic_fx.plugin import version_store
    version_dir.chmod(0o700)
    recomputed_artifact = version_store.artifact_hash_bytes(
        (version_dir / "plugin.py").read_bytes(),
        (version_dir / "config.yaml").read_bytes(),
        (version_dir / "test_plugin.py").read_bytes())
    assert version_dir.name == recomputed_artifact  # ディレクトリ名 == 実 artifact_hash
    assert (version_dir / "test_plugin.py").read_text() == TEST_PY_OK  # 候補内容に戻った
    live = plugins_dir / "sma"
    assert live.readlink().as_posix() == f".versions/sma/{recomputed_artifact}"

    # I3 是正 (advisor 指摘): in-place 編集を検出して版を差し替えたことを
    # activity ERROR に残す (設計 §2.3 は loader 側の同種検出に
    # `plugin_artifact_hash_mismatch` の activity ERROR を要求しており、
    # reconcile 側の検出も無言で直してはならない)。
    log_text = (root / "logs" / "activity.log").read_text()
    assert "switch_reverify_version_mismatch" in log_text
    assert f"name=sma" in log_text


def test_switched_journal_reverify_reverts_when_version_tampered_and_candidate_missing(
        env, monkeypatch):
    """版ディレクトリの test_plugin.py が改変され、かつ候補も欠損していれば
    健全判定してはならない — reverted + pending + activity ERROR に
    落ちること (これが I3 の本命の killer: 現行実装は artifact_hash を
    見ないため candidate 側を一切確認せずに approved へ進んでしまう)。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    approval_id, artifact_hash = _simulate_crash_after_switch_before_decide(
        root, plugins_dir, conn, settings, monkeypatch)

    version_dir = plugins_dir / ".versions" / "sma" / artifact_hash
    version_dir.chmod(0o700)
    (version_dir / "test_plugin.py").chmod(0o600)
    (version_dir / "test_plugin.py").write_text("def test_tampered():\n    pass\n")
    version_dir.chmod(0o500)
    import shutil
    shutil.rmtree(plugins_dir / "_staging" / "1" / "sma")  # 候補も消す

    activity = ActivityLog(root / "logs" / "activity.log")
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    switch.retry_approval(conn, approval_id, decided_by="human", now=NOW,
                          plugins_root=plugins_dir, settings=settings,
                          activity=activity)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending", (
        "改変された版を健全と誤判定して approved へ確定してしまった (I3 の欠陥)")
    journal_row = conn.execute(
        "SELECT phase FROM plugin_switch_journal WHERE approval_id=?",
        (approval_id,)).fetchone()
    assert journal_row["phase"] == "reverted"

    log_text = (root / "logs" / "activity.log").read_text()
    assert "switch_reverify_failed" in log_text


# ============================================================
# 束 E ローカルレビュー是正 (2026-08-25 裁定) — 確定-1/確定-2/確定-4/確定-5
# ============================================================

def test_stuck_preparing_journal_is_closed_when_candidate_no_longer_matches(env, monkeypatch):
    """確定-1 (Critical): `approve_candidate` の pending 留置 2 経路が、
    自分の未完ジャーナル (preparing/versioned/recorded) を `_revert_one` で
    閉じてから return する。旧実装は journal に一切触れず、以後 name が
    永久に `UnresolvedJournalError` で封鎖されていた
    (verified-local-round1.md 確定-1)。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    candidate_dir = plugins_dir / "_staging" / "1" / "sma"
    _write_candidate(candidate_dir)
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    def _boom(*a, **kw):
        raise OSError("simulated crash during create_version_dir")
    monkeypatch.setattr(version_store, "create_version_dir", _boom)
    with pytest.raises(OSError):
        switch.approve_candidate(conn, approval_id, decided_by="human", now=NOW,
                                 plugins_root=plugins_dir, settings=settings)
    monkeypatch.undo()
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    j = journal_store.get_open_by_name(conn, "sma")
    assert j is not None and j["phase"] == "preparing"

    # 候補が改変された (人間の編集想定) — hash 不一致で pending 留置経路へ
    (candidate_dir / "plugin.py").write_text(
        "def compute(df, params):\n    return {'v': 2.0}\n")

    switch.retry_approval(conn, approval_id, decided_by="human", now=NOW,
                          plugins_root=plugins_dir, settings=settings)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"
    assert journal_store.get_open_by_name(conn, "sma") is None, (
        "自分の未完ジャーナルが閉じられず name が永久封鎖された (確定-1 の欠陥)")


def test_stuck_preparing_journal_is_closed_when_candidate_goes_missing(env, monkeypatch):
    """確定-1 の CandidateMissingError 経路側 (switch.py:741-742) も同様に
    自分の未完ジャーナルを閉じることを確認する。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    candidate_dir = plugins_dir / "_staging" / "1" / "sma"
    _write_candidate(candidate_dir)
    approval_id = switch.submit_candidate(
        conn, name="sma", staging_dir=plugins_dir / "_staging" / "1",
        candidate_origin="staging", mission_id=1, backlog_id=None,
        settings=settings, now=NOW)

    def _boom(*a, **kw):
        raise OSError("simulated crash during create_version_dir")
    monkeypatch.setattr(version_store, "create_version_dir", _boom)
    with pytest.raises(OSError):
        switch.approve_candidate(conn, approval_id, decided_by="human", now=NOW,
                                 plugins_root=plugins_dir, settings=settings)
    monkeypatch.undo()
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)

    assert journal_store.get_open_by_name(conn, "sma")["phase"] == "preparing"

    import shutil
    shutil.rmtree(candidate_dir)  # 候補が外部から消えた想定

    switch.retry_approval(conn, approval_id, decided_by="human", now=NOW,
                          plugins_root=plugins_dir, settings=settings)

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"
    assert journal_store.get_open_by_name(conn, "sma") is None


def test_bless_raises_unresolved_journal_error_when_open_journal_exists(env, monkeypatch):
    """確定-2: `bless_candidate` は `approve_candidate` と同様に未完
    ジャーナルガードを持ち、`UnresolvedJournalError` を送出する (旧実装は
    生の `sqlite3.IntegrityError` を漏らしていた)。"""
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    # 別 approval (name="sma") の未完ジャーナルを preparing のまま残す
    op_id = switch.begin_switch_journal(
        conn, kind="approve", approval_id=1, name="sma", old_kind="absent",
        old_target=None, new_target=f".versions/sma/{'a' * 64}",
        switch_required=True, actor="human", now=NOW, commit=True)
    assert journal_store.get(conn, op_id)["phase"] == "preparing"

    human_dir = plugins_dir / "_human" / "sma"
    _write_candidate(human_dir)

    with pytest.raises(switch.UnresolvedJournalError):
        switch.bless_candidate(conn, name="sma", human_dir=human_dir,
                               settings=settings, now=NOW, decided_by="human")


def test_approve_rejects_when_only_artifact_hash_differs(env, monkeypatch):
    """確定-4: ⓐ hash 照合の artifact_hash 側だけが不一致でも pending
    留置になる (payload の artifact_hash を人為的に改変して検証)。"""
    root, plugins_dir, conn, settings = env
    aid, candidate_dir = _submit_helper(env, monkeypatch, name="sma")

    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (aid,)).fetchone()
    import json as _json
    payload = _json.loads(row["payload_json"])
    payload["artifact_hash"] = "0" * 64  # 実際の候補ハッシュと不一致にする
    conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                (_json.dumps(payload), aid))
    conn.commit()

    switch.approve_candidate(conn, aid, decided_by="human", now=NOW,
                             plugins_root=plugins_dir, settings=settings)

    status = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                          (aid,)).fetchone()["status"]
    assert status == "pending"


def test_approve_rejects_when_only_content_hash_differs(env, monkeypatch):
    """確定-4: content_hash 側だけが不一致でも pending 留置になる。"""
    root, plugins_dir, conn, settings = env
    aid, candidate_dir = _submit_helper(env, monkeypatch, name="sma")

    row = conn.execute("SELECT payload_json FROM approval_requests WHERE id=?",
                       (aid,)).fetchone()
    import json as _json
    payload = _json.loads(row["payload_json"])
    payload["content_hash"] = "0" * 64
    conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                (_json.dumps(payload), aid))
    conn.commit()

    switch.approve_candidate(conn, aid, decided_by="human", now=NOW,
                             plugins_root=plugins_dir, settings=settings)

    status = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                          (aid,)).fetchone()["status"]
    assert status == "pending"


def _submit_helper(env, monkeypatch, name="sma", mission_id=1):
    root, plugins_dir, conn, settings = env
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _fake_pytest_ok)
    staging = plugins_dir / "_staging" / str(mission_id)
    candidate_dir = staging / name
    _write_candidate(candidate_dir)
    aid = switch.submit_candidate(
        conn, name=name, staging_dir=staging, candidate_origin="staging",
        mission_id=mission_id, backlog_id=None, settings=settings, now=NOW)
    return aid, candidate_dir


def test_advance_to_decided_refuses_when_candidate_changes_during_full_gate(env, monkeypatch):
    """確定-5: `_run_full_gate` の手順 6 再照合 (ゲート実行中の候補改変検出)
    が生きていることを、ゲート実行の途中で候補を書き換える fake を使って
    確認する。"""
    root, plugins_dir, conn, settings = env
    staging = plugins_dir / "_staging" / "1"
    candidate_dir = staging / "sma"
    _write_candidate(candidate_dir)

    def _tamper_during_gate(plugin_dir, *, settings):
        (plugin_dir / "plugin.py").write_text(
            "def compute(df, params):\n    return {'v': 999.0}\n")
        from agentic_fx.plugin.gate_pytest import GateResult
        return GateResult(passed=True, returncode=0, stdout_tail="1 passed",
                          duration_sec=0.1)
    monkeypatch.setattr("agentic_fx.plugin.switch.run_gate_pytest", _tamper_during_gate)

    with pytest.raises(ValueError, match="content changed during gate"):
        switch.submit_candidate(
            conn, name="sma", staging_dir=staging, candidate_origin="staging",
            mission_id=1, backlog_id=None, settings=settings, now=NOW)

    assert approvals_store.pending(conn, kind="plugin") == []
