from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import os

import pytest

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import approvals
from agentic_fx.store import snapshots
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore
from tests.fixtures.wiring_envs import (
    approve_indicator_v2 as _approve_indicator_v2,
    deploy_strategy as _deploy_strategy,
    shell_env as _shell_env,
    submit_indicator_v2 as _submit_indicator_v2,
)

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example")


def _commands(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    state = StateStore(tmp_path / "s.json")
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "agentic.log").write_text(
        "line1\nline2\nline3\n", encoding="utf-8")
    trade_loop = MagicMock()
    trade_loop.ask_once.return_value = "回答です"
    health_latch = HealthLatch()
    cmds = Commands(conn=conn, state_store=state,
                    broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                    trade_loop=trade_loop, activity=activity,
                    log_dir=tmp_path / "logs", clock=FixedClock(NOW),
                    health_latch=health_latch)
    return conn, state, activity, cmds


def test_status(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("status")
    assert "learning" in out and "1,000,000" in out
    # F3: kill switch と orders 行の表示確認
    assert "アクティブ orders: 0" in out
    assert "ok" in out  # kill_switch_latched=False


def test_status_latched(tmp_path):
    """F3: kill_switch_latched=True の表示確認。"""
    _, state, _, cmds = _commands(tmp_path)
    state.update(kill_switch_latched=True)
    out = cmds.dispatch("status")
    assert "LATCHED" in out


def test_status_shows_process_health_latch_reasons(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    cmds.health_latch.record_failure("disk full")

    out = cmds.dispatch("status")

    assert "health: LATCHED (disk full)" in out


def test_log_tail(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("log 2")
    assert "line2" in out and "line1" not in out


def test_log_zero_returns_empty(tmp_path):
    """W5: `log 0` は全ログを流さず空文字を返す。"""
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("log 0")
    assert out == ""


def test_log_negative_returns_empty(tmp_path):
    """W5: 負の n も同じガードで空文字を返す。"""
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("log -1")
    assert out == ""


def test_activity_filter(tmp_path):
    _, _, activity, cmds = _commands(tmp_path)
    activity.write(Category.TRADE, "order_opened", "x")
    activity.write(Category.NEWS, "collected", "y")
    out = cmds.dispatch("activity 10 TRADE")
    assert "order_opened" in out and "collected" not in out


def test_ask_delegates(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("ask 今の相場は？")
    assert "回答です" in out
    # F3: 質問文が正しく渡されることを確認
    cmds.trade_loop.ask_once.assert_called_once_with("今の相場は？")


def test_approve(tmp_path):
    """F2: approve は DB 状態と activity 記録を検証。"""
    conn, _, activity, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)
    out = cmds.dispatch(f"approve {aid}")
    assert "approved" in out
    # DB に status="approved" が記録されること
    row = conn.execute(
        "SELECT status FROM approval_requests WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "approved"
    # activity に APPROVAL レコードが記録されること
    records = activity.tail(10, Category.APPROVAL)
    assert any("approved" in r for r in records)


def test_approve_plugin_kind_reports_actual_outcome_not_always_approved(tmp_path):
    """検収 m5 の pin: kind='plugin' の approve は `switch.approve_candidate`
    を呼ぶが、同関数は非 pending / 後発失効 / 候補欠損 / hash 不一致 /
    legacy_plain_present などの正常な到達状態でも例外を出さず pending の
    まま return する。旧稿の shell handler はこれを確認せず無条件に
    activity `approved` を書き `"approval #N approved"` を返していた
    (acceptance-task11.md m5)。ここでは候補が存在しない
    (candidate_missing) 状態を作り、承認が実際には成立しなかったことが
    報告に反映されることを確認する。"""
    conn, _, activity, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/sma"},
        now=NOW)  # candidate_path のディレクトリを作らない → candidate_missing

    out = cmds.dispatch(f"approve {approval_id}")

    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending", (
        "candidate_missing で承認は成立しないはず (テスト前提の確認)")
    assert out != f"approval #{approval_id} approved", (
        "実際には approved になっていないのに『approved』と報告した (m5 の欠陥)")
    assert "pending" in out


def test_reject(tmp_path):
    """F2: reject は理由を保存し activity に記録。"""
    conn, _, activity, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)
    reason_text = "テスト理由"
    out = cmds.dispatch(f"reject {aid} {reason_text}")
    assert "rejected" in out
    # DB に status="rejected" と reason が保存されること
    row = conn.execute(
        "SELECT status, reason FROM approval_requests WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "rejected"
    assert row["reason"] == reason_text
    # activity に APPROVAL レコードが記録されること
    records = activity.tail(10, Category.APPROVAL)
    assert any("rejected" in r for r in records)


def test_approve_already_decided(tmp_path):
    """F2: 決定済み approval への二重決定は例外でなくメッセージ。"""
    conn, _, _, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)
    # 1 度目の approve
    cmds.dispatch(f"approve {aid}")
    # 2 度目は決定済みメッセージ
    out = cmds.dispatch(f"reject {aid} 理由")
    assert "決定済み" in out or "already" in out.lower()


def test_approve_nonexistent(tmp_path):
    """F2: 存在しない id への approve は例外でなくメッセージを返す。

    E1 裁定 (2026-08-25、確定-8 と同じ軸の欠陥の是正): この assertion は
    元々「決定済み」という文言を pin していたが、それは E1 是正前の
    `apply_decision` が「ID 不存在」と「CAS 失敗 (決定済み)」を同一例外に
    混同していた頃の副産物だった。存在しない approval を「決定済み」と
    報告するのは E1 が正そうとしている混同そのものであるため、期待する
    文言を「存在しません」に書き換える (既存テスト書き換え禁止の例外 —
    欠陥の是正として申告)。"""
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("approve 9999")
    # 例外を投げずに文字列を返すこと
    assert isinstance(out, str)
    assert "存在しません" in out


def test_killswitch_reset(tmp_path):
    """F2: killswitch reset で activity 記録を確認。"""
    conn, state, activity, cmds = _commands(tmp_path)
    # Set non-default state
    state.update(kill_switch_latched=True, autopilot=True)
    snapshots.add(conn, ts=NOW, balance=1_000_000, equity=980_000,
                  hwm=1_000_000, cashflow=0, source="paper")
    before = [dict(row) for row in conn.execute(
        "SELECT * FROM account_snapshots ORDER BY id")]
    out = cmds.dispatch("killswitch reset")
    after = [dict(row) for row in conn.execute(
        "SELECT * FROM account_snapshots ORDER BY id")]
    # kill_switch_latched=False に変更されること
    assert state.load().kill_switch_latched is False
    # 他の状態は保持されること
    assert state.load().autopilot is True
    assert after == before
    assert "解除" in out
    # F2: activity に SYSTEM レコードが記録されること
    records = activity.tail(10, Category.SYSTEM)
    assert any("kill_switch_reset" in r for r in records)


def test_unknown_shows_help(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("nonsense")
    assert "status" in out
    # help から行が落ちる退化の pin (レビュー 1 周目 ローカル LLM)。
    # `reflect retry` は `reflection_abandoned` からの唯一の復帰手段なので、
    # help に載っていないと運用者が辿り着けない。
    assert "reflect retry" in out


def test_backlog_note_transitions_open_and_writes_activity(tmp_path):
    conn, _, activity, cmds = _commands(tmp_path)
    conn.execute("INSERT INTO improvement_backlog "
                 "(id, idea, source, status, created_at, updated_at) "
                 "VALUES (5, 'fact', 'user', 'open', ?, ?)",
                 (NOW.isoformat(), NOW.isoformat()))
    conn.commit()

    out = cmds.dispatch("backlog note 5")

    assert "note" in out
    assert conn.execute("SELECT status FROM improvement_backlog WHERE id=5").fetchone()[0] == "note"
    assert any("backlog_noted" in row for row in activity.tail(10, Category.IMPROVE))


def test_backlog_note_rejects_selected_row(tmp_path):
    conn, _, _, cmds = _commands(tmp_path)
    conn.execute("INSERT INTO improvement_backlog "
                 "(id, idea, source, status, created_at, updated_at) "
                 "VALUES (5, 'busy', 'user', 'selected', ?, ?)",
                 (NOW.isoformat(), NOW.isoformat()))
    conn.commit()

    out = cmds.dispatch("backlog note 5")

    assert "できません" in out
    assert conn.execute("SELECT status FROM improvement_backlog WHERE id=5").fetchone()[0] == "selected"


# ---------------------------------------------------------------------------
# [unprofitable-note-hygiene] 設計書 v2.0 §2-7 / §4 N10 (code-review #2):
# 人間コマンド (`backlog note`/`reopen`/`reject`) は origin_mission_id/
# origin_outcome (系譜) を一切触らない。`backlog.set_status` の UPDATE は
# status/last_result/updated_at のみを書くため、専用列方式では構造的に
# 干渉しないはずだが、それを実測で pin する (逆変異: UPDATE 文に
# `origin_outcome=NULL` などを混ぜると red)。
# ---------------------------------------------------------------------------

def _insert_annotated_row(conn, *, id_, status, mission_id=7):
    conn.execute(
        "INSERT INTO improvement_backlog "
        "(id, idea, source, status, created_at, updated_at, idea_norm, "
        "origin_mission_id, origin_outcome) "
        "VALUES (?, ?, 'agent', ?, ?, ?, ?, ?, 'unprofitable')",
        (id_, f"annotated idea {id_}", status, NOW.isoformat(),
         NOW.isoformat(), f"annotated idea {id_}", mission_id))
    conn.commit()


def test_n10_backlog_note_leaves_origin_columns_untouched(tmp_path):
    conn, _, _, cmds = _commands(tmp_path)
    _insert_annotated_row(conn, id_=11, status="open")

    out = cmds.dispatch("backlog note 11")

    assert "note" in out
    row = conn.execute(
        "SELECT origin_mission_id, origin_outcome FROM improvement_backlog "
        "WHERE id=11").fetchone()
    assert row["origin_mission_id"] == 7
    assert row["origin_outcome"] == "unprofitable"


def test_n10_backlog_reopen_leaves_origin_columns_untouched(tmp_path):
    conn, _, _, cmds = _commands(tmp_path)
    _insert_annotated_row(conn, id_=12, status="note")

    out = cmds.dispatch("backlog reopen 12")

    assert "open" in out
    row = conn.execute(
        "SELECT origin_mission_id, origin_outcome FROM improvement_backlog "
        "WHERE id=12").fetchone()
    assert row["origin_mission_id"] == 7
    assert row["origin_outcome"] == "unprofitable"


def test_n10_backlog_reject_leaves_origin_columns_untouched(tmp_path):
    conn, _, _, cmds = _commands(tmp_path)
    _insert_annotated_row(conn, id_=13, status="open")

    out = cmds.dispatch("backlog reject 13")

    assert "rejected" in out
    row = conn.execute(
        "SELECT origin_mission_id, origin_outcome FROM improvement_backlog "
        "WHERE id=13").fetchone()
    assert row["origin_mission_id"] == 7
    assert row["origin_outcome"] == "unprofitable"


def test_transcript_json_not_leaked(tmp_path):
    """Regression: transcript_json (sensitive) must not appear in dispatch output."""
    conn, _, _, cmds = _commands(tmp_path)
    # Insert a mission with sensitive sentinel in transcript_json
    sentinel = "SECRET_TRANSCRIPT_MARKER"
    conn.execute(
        """INSERT INTO missions
        (loop, runner, model, status, started_at, transcript_json)
        VALUES (?, ?, ?, ?, ?, ?)""",
        ("trade", "local", "test-model", "completed", NOW.isoformat(), f'{{"{sentinel}"}}')
    )
    conn.commit()

    out = cmds.dispatch("status")
    # Sentinel must NOT appear, but loop name must
    assert sentinel not in out
    assert "trade" in out


def test_ask_with_exception(tmp_path):
    """F1: trade_loop.ask_once が例外を投げても dispatch は文字列を返す。"""
    conn, state, activity, _ = _commands(tmp_path)
    # trade_loop を例外を投げるモックに置き換える
    cmds = Commands(conn=conn, state_store=state, activity=activity,
                    broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                    trade_loop=MagicMock(ask_once=MagicMock(
                        side_effect=RuntimeError("Test error"))),
                    log_dir=tmp_path / "logs", clock=FixedClock(NOW))
    out = cmds.dispatch("ask 質問")
    # 例外を投げずに文字列を返すこと
    assert isinstance(out, str)
    assert "エラー" in out


def test_status_with_broken_state_json(tmp_path):
    """F1: state.json が破損しても dispatch は文字列を返す。"""
    # state.json に不正 JSON を書き込む
    (tmp_path / "s.json").write_text("{invalid json", encoding="utf-8")
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    state = StateStore(tmp_path / "s.json")
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    (tmp_path / "logs").mkdir(exist_ok=True)
    cmds = Commands(conn=conn, state_store=state, activity=activity,
                    broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                    trade_loop=MagicMock(),
                    log_dir=tmp_path / "logs", clock=FixedClock(NOW))
    out = cmds.dispatch("status")
    # 例外を投げずに文字列を返すこと
    assert isinstance(out, str)
    assert "エラー" in out


def test_reflect_retry_deletes_attempt_row(tmp_path):
    conn, _state, _activity, cmd = _commands(tmp_path)
    now = cmd.clock.now()
    conn.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
                 "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
                 "'closed',?,?)", (now.isoformat(), now.isoformat()))
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    from agentic_fx.store import reflection_attempts
    reflection_attempts.bump(conn, oid, now=now, reason="boom")
    assert cmd.dispatch(f"reflect retry {oid}") == f"order #{oid} を reflection 再試行対象へ戻しました"
    assert reflection_attempts.attempts_of(conn, oid) == 0


def test_reflect_retry_rejects_unknown_order(tmp_path):
    """Task 15: 存在しない order への `reflect retry` は成功メッセージを
    返さない。台帳に無い id を「戻しました」と報告すると、運用者は
    abandon が解けたと誤認して調査をやめる (`reflection_abandoned` の
    activity から辿る唯一の復帰手段がこのコマンドである)。"""
    conn, _state, _activity, cmd = _commands(tmp_path)
    out = cmd.dispatch("reflect retry 999")
    assert "再試行対象へ戻しました" not in out
    assert "does not exist" in out


def test_reflect_retry_rejects_open_order(tmp_path):
    """F3 (レビュー 1 周目 codex Minor-3): `reflect retry` は order の存在
    しか検査していなかった。まだ open な order へ retry を発行すると
    (reflection はそもそも closed 前提のため) 台帳を無意味に触るだけで、
    運用者に誤った成功メッセージを返す。"""
    conn, _state, _activity, cmd = _commands(tmp_path)
    now = cmd.clock.now()
    conn.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
                 "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
                 "'open',?,?)", (now.isoformat(), now.isoformat()))
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    out = cmd.dispatch(f"reflect retry {oid}")
    assert "再試行対象へ戻しました" not in out
    assert "closed ではありません" in out
    assert "status=open" in out


def test_reflect_retry_rejects_already_reflected_order(tmp_path):
    """F3: 既に reflection 済みの order へ retry を発行しても、reflection は
    `run_pending` の SQL (`r.order_id IS NULL`) で最初から除外されるため
    台帳を触っても無意味 — 運用者に誤解を与えないよう明示的に拒否する。"""
    from agentic_fx.store import reflections
    conn, _state, _activity, cmd = _commands(tmp_path)
    now = cmd.clock.now()
    conn.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
                 "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
                 "'closed',?,?)", (now.isoformat(), now.isoformat()))
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    reflections.save(conn, oid, "既存の振り返り", now)
    out = cmd.dispatch(f"reflect retry {oid}")
    assert "再試行対象へ戻しました" not in out
    assert "既に reflection 済みです" in out


def test_reflect_retry_notes_when_no_attempt_row_existed(tmp_path):
    """F3: 台帳に試行記録が無い closed order (= まだ一度も失敗していない)
    への retry は成功として扱うが、文言末尾に「(台帳に試行記録なし)」を
    付けて運用者へ「これは何も戻していない」ことを明示する。"""
    conn, _state, _activity, cmd = _commands(tmp_path)
    now = cmd.clock.now()
    conn.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
                 "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
                 "'closed',?,?)", (now.isoformat(), now.isoformat()))
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    out = cmd.dispatch(f"reflect retry {oid}")
    assert out == (f"order #{oid} を reflection 再試行対象へ戻しました"
                   " (台帳に試行記録なし)")


def test_reflect_retry_writes_activity(tmp_path):
    """`reflect retry` の介入が activity に残ることの pin (レビュー 1 周目
    ローカル LLM)。`activity.write` を消しても台帳削除の assert は通るため、
    **人間の介入が監査に残らなくなる**変異が生き残っていた。"""
    conn, _state, _activity, cmd = _commands(tmp_path)
    now = cmd.clock.now()
    conn.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
                 "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
                 "'closed',?,?)", (now.isoformat(), now.isoformat()))
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    from agentic_fx.store import reflection_attempts
    reflection_attempts.bump(conn, oid, now=now, reason="boom")
    cmd.dispatch(f"reflect retry {oid}")
    lines = [ln for ln in (tmp_path / "logs" / "activity.log").read_text(
        encoding="utf-8").splitlines() if "reflection_requeued" in ln]
    assert len(lines) == 1
    assert f"order_id={oid}" in lines[0]


def test_reflect_retry_rejects_extra_arguments(tmp_path):
    """`len(args) == 2` の pin (レビュー 1 周目 ローカル LLM)。`>= 2` へ
    緩めると `reflect retry <id> <ゴミ>` を受理してしまい、打ち間違いが
    黙って実行される。"""
    conn, _state, _activity, cmd = _commands(tmp_path)
    now = cmd.clock.now()
    conn.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
                 "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
                 "'closed',?,?)", (now.isoformat(), now.isoformat()))
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    out = cmd.dispatch(f"reflect retry {oid} extra")
    assert "再試行対象へ戻しました" not in out




def test_reject_plugin_kind_routes_through_switch_reject_candidate(tmp_path):
    """確定-3: `reject` の kind='plugin' 分岐は `switch.reject_candidate`
    (plugin flock 経由) を通る。staging 候補が削除される (=
    `_drop_staging_candidate` を通った証跡) ことで配線を pin する
    (SURVIVED 変異 c21_reject_drop_plugin_dispatch の killer)。"""
    conn, _, activity, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    candidate_dir = plugins_dir / "_staging" / "1" / "sma"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "plugin.py").write_text("x")
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/sma"},
        now=NOW)

    out = cmds.dispatch(f"reject {approval_id} no good")

    assert "rejected" in out
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "rejected"
    assert not candidate_dir.exists(), (
        "plugin flock 経由の reject_candidate (staging 掃除を含む) を "
        "通っていない (c21_reject_drop_plugin_dispatch の欠陥)")


def test_reject_plugin_kind_without_plugins_root_reports_unwired(tmp_path):
    """確定-3: `plugins_root` 未配線時、plugin kind の reject は例外でなく
    「未配線」文言を返す (SURVIVED 変異 c21_reject_drop_wiring_guard の
    killer)。"""
    conn, _, activity, cmds = _commands(tmp_path)
    assert cmds.plugins_root is None
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/sma"},
        now=NOW)

    out = cmds.dispatch(f"reject {approval_id} no good")

    assert "未配線" in out
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"


def test_approval_retry_dispatches_to_switch_retry_approval(tmp_path, monkeypatch):
    """確定-3: `approval retry <id>` は `switch.retry_approval` を呼び、
    activity `retry` を記録する (SURVIVED 変異 c21_retry_drop_branch の
    killer)。"""
    conn, _, activity, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS

    calls = []

    def _spy(conn_, approval_id, *, decided_by, now, plugins_root, settings,
            activity=None):
        # [switch-ops-hardening] T5: 実物は **必ず `ApprovalOutcome` を返す**。
        # 旧 spy は `None` を返す「実物より緩い fake」で、シェルが outcome を
        # 文言に写す契約 (設計書 §3.5) を素通りさせていた。
        calls.append((approval_id, decided_by))
        return plugin_switch.ApprovalOutcome(
            outcome="deployed", name="sma", status="approved", op_id=7,
            target=".versions/sma/" + "a" * 64)

    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval", _spy)

    out = cmds.dispatch("approval retry 42")

    assert calls == [(42, "shell")]
    assert "再試行" in out
    assert "配備まで完了しました" in out
    records = activity.tail(10, Category.APPROVAL)
    assert any("retry" in r for r in records)


def test_approval_retry_without_plugins_root_reports_unwired(tmp_path):
    """確定-3: `approval retry` は `plugins_root`/`settings` 未配線時に
    例外でなく「未配線」文言を返す。"""
    conn, _, activity, cmds = _commands(tmp_path)
    assert cmds.plugins_root is None

    out = cmds.dispatch("approval retry 42")

    assert "未配線" in out


def test_approval_detail_shows_in_sample_and_holdout_metrics(tmp_path):
    """[approval-payload-missing-gate-metrics] 是正 (A4 10 回目 claude #69
    観測 A、2026-09-11): `approval <id>` は payload の in_sample/holdout
    (pf/trades/avg_r/max_drawdown) を表示する — approve/reject する前に
    人間が場外 (holdout) の成績を読める。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={
            "name": "sma_cross_usdjpy", "content_hash": "h1",
            "eval_timeframe": "1h",
            "in_sample": {"USDJPY": {
                "pf": 1.474, "trades": 193, "avg_r": 0.188,
                "max_drawdown": 0.0373}},
            "holdout": {
                "pf": 0.904, "trades": 54, "avg_r": -0.037,
                "max_drawdown": 0.0392},
        },
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert f"approval #{approval_id}" in out
    assert "kind=plugin" in out
    assert "in_sample USDJPY: pf=1.474 trades=193 avg_r=0.188" in out
    assert "holdout: pf=0.904 trades=54 avg_r=-0.037 max_drawdown=0.0392" in out


def test_approval_detail_missing_metrics_show_dash(tmp_path):
    """holdout/in_sample が無い旧 payload (indicator kind 等) でも例外に
    ならず `-` を表示する。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "myind", "content_hash": "h1"}, now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert "in_sample: -" in out
    assert "holdout: -" in out


def test_approval_detail_shows_profitability_floor_fields(tmp_path):
    """[profitability-floor] T1 Step 1-8 (2026-09-13): `bless_candidate`
    のフロア警告 payload (`floor_warning`/`floor_detail`/
    `profitability_floor`) が `afx> approval <id>` に表示される。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={
            "name": "myst", "content_hash": "h1",
            "floor_warning": "unprofitable",
            "floor_detail": "in_sample: USDJPY: pf=0.5",
            "profitability_floor": {
                "min_pf": 1.0, "require_positive_avg_r": True,
                "require_holdout_evaluable": False}},
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert "floor_warning=unprofitable" in out
    assert "floor_detail=in_sample: USDJPY: pf=0.5" in out
    assert "profitability_floor=" in out
    assert "min_pf" in out


def test_approval_detail_omits_floor_fields_when_absent(tmp_path):
    """フロア関連 payload キーが無い (通常の合格 approval) 場合は行を
    出さない (fail-soft、T0 実装の既存契約の回帰確認)。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "myind", "content_hash": "h1"}, now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert "floor_warning=" not in out
    assert "floor_detail=" not in out
    assert "profitability_floor=" not in out


def test_approval_detail_shows_archive_path_by_mission_content_hash(tmp_path):
    """approval-quality 設計書 §C ([archive-artifact-hash-vs-submitted]):
    `approval <id>` は payload の `mission_id`/`content_hash` で
    `candidate_archives` を引き、`archive=<path>` を出力に追加する。
    run12 観測 C の実データ形 — payload の `artifact_hash` は self-test
    書き直しでずれるため、archive 行自体は別の `artifact_hash` を持つ
    (それでも content_hash が一致すれば引ける)。"""
    from agentic_fx.store import candidate_archives
    conn, _, _, cmds = _commands(tmp_path)
    candidate_archives.insert(
        conn, mission_id=5, name="sma_cross_usdjpy",
        content_hash="stable-content", artifact_hash="artifact-at-backtest",
        archive_path="plugins/_archive/5/artifact-at-backtest",
        pair="USDJPY", metrics={"pf": 1.25}, now=NOW, commit=True)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "sma_cross_usdjpy", "mission_id": 5,
                "content_hash": "stable-content",
                "artifact_hash": "artifact-submitted-different"},
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert "archive=plugins/_archive/5/artifact-at-backtest" in out


def test_approval_detail_shows_archive_unknown_when_no_archive_row(tmp_path):
    """GC 済み・失敗終端等で archive 行が無いとき「archive 不明」を明示。

    ローカル approval-quality 1 周目 #C1x (2026-09-12): assert を理由文込みに
    厳格化 (4 分岐すべてが「archive=不明」で始まるため、部分一致では分岐の
    取り違えを検出できず、文言すり替え変異が SURVIVED だった)。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "myind", "mission_id": 99, "content_hash": "h1"},
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert "archive=不明 (GC 済み・失敗終端等)" in out


def test_approval_detail_shows_archive_unknown_when_payload_lacks_identity(
        tmp_path):
    """旧 payload に `mission_id`/`content_hash` が欠けていても例外にせず
    「archive 不明」を出す。

    ローカル approval-quality 1 周目 #C1x (2026-09-12): assert を理由文込みに
    厳格化。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin", payload={"name": "myind"}, now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert "archive=不明 (mission_id/content_hash 欠落)" in out


def test_approval_detail_archive_unknown_when_mission_id_is_wrong_type(
        tmp_path):
    """codex 1周目 I3 是正の pin: `mission_id` が int でない (list) payload
    でも例外にせず「archive 不明」を出す。

    ローカル approval-quality 1 周目 #C1x (2026-09-12): assert を理由文込みに
    厳格化。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "myind", "mission_id": [], "content_hash": "h1"},
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert "archive=不明 (mission_id/content_hash 不正)" in out


def test_approval_detail_archive_unknown_when_content_hash_is_wrong_type(
        tmp_path):
    """codex 1周目 I3 是正の pin: `content_hash` が str でない (dict)
    payload でも例外にせず「archive 不明」を出す。

    ローカル approval-quality 1 周目 #C1x (2026-09-12): assert を理由文込みに
    厳格化。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "myind", "mission_id": 5, "content_hash": {}},
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert "archive=不明 (mission_id/content_hash 不正)" in out


def test_approval_detail_archive_unknown_when_only_row_has_null_path(tmp_path):
    """ローカル approval-quality 1 周目 #C2x (2026-09-12): 候補行は在るが
    `archive_path` が NULL (GC 済み等) のとき「archive=不明 (パス欠落…)」を
    出す。この分岐を踏むテストが 1 本も無く、分岐ごと削除して
    `archive=None` を出す変異が SURVIVED だった。I2 是正で非 NULL 行が
    優先されるため、NULL 行だけを 1 件入れて踏む。"""
    from agentic_fx.store import candidate_archives
    conn, _, _, cmds = _commands(tmp_path)
    candidate_archives.insert(
        conn, mission_id=7, name="rsi_v2", content_hash="c7",
        artifact_hash="a7", archive_path=None, pair="USDJPY",
        metrics={"pf": 1.1}, now=NOW, commit=True)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "rsi_v2", "mission_id": 7, "content_hash": "c7"},
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    assert "archive=不明 (パス欠落、GC 済みの可能性)" in out
    assert "archive=None" not in out


def test_approval_detail_unknown_id_reports_not_found(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("approval 999")
    assert "存在しません" in out


def test_policy_add_without_policy_path_reports_unwired(tmp_path):
    """確定-3: `policy add` は `_policy_path` 未配線時に例外でなく
    「未配線」文言を返す (SURVIVED 変異 c21_policy_drop_wiring_guard の
    killer)。"""
    conn, _, activity, cmds = _commands(tmp_path)
    assert cmds._policy_path is None

    out = cmds.dispatch("policy add テスト方針")

    assert "未配線" in out


def test_policy_add_appends_to_policy_path(tmp_path):
    """確定-3 の正常系対: `_policy_path` が配線されていれば追記される
    (未配線ガードだけが正常系を潰していないことの回帰 pin)。"""
    conn, _, activity, cmds = _commands(tmp_path)
    policy_path = tmp_path / "policy" / "directives.md"
    cmds._policy_path = policy_path

    out = cmds.dispatch("policy add テスト方針")

    assert "policy" in out
    assert "テスト方針" in policy_path.read_text(encoding="utf-8")


# --- [indicator-consumption-wiring] T4b Step 4-8: 依存 strategy 2 欄 (D1) ---

def test_indicator_approval_detail_lists_dependent_strategies_in_two_columns(
        tmp_path):
    """D1: (i) この候補の hash に pin 済み / (ii) 同名 indicator の別 hash に
    pin (承認すると外れる) の 2 欄。決定順 (id) で表示。"""
    shell, conn, plugins_root = _shell_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    _deploy_strategy(conn, plugins_root, "s_old", pins={"rsi": hashes["rsi"]})
    i2_id, i2_hash = _submit_indicator_v2(conn, plugins_root, "rsi")

    out = shell._approval_detail(i2_id)

    assert "dependent_pinned_here=" in out
    assert "dependent_pinned_elsewhere=s_old" in out
    assert "dependent_pinned_here=-" in out   # まだ誰も I2 に pin していない


def test_dependent_strategy_moves_to_the_first_column_after_relock(tmp_path):
    shell, conn, plugins_root = _shell_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    _deploy_strategy(conn, plugins_root, "s_old", pins={"rsi": hashes["rsi"]})
    i2_id, i2_hash = _submit_indicator_v2(conn, plugins_root, "rsi")
    _approve_indicator_v2(conn, plugins_root, "rsi", approval_id=i2_id)
    # s_old は pin 破れで inventory から外れる → (ii) 欄に残る
    assert "dependent_pinned_elsewhere=s_old" in shell._approval_detail(i2_id)
    # 再ロックした s_new を配備すると (i) 欄へ移る
    _deploy_strategy(conn, plugins_root, "s_new", pins={"rsi": i2_hash})
    out = shell._approval_detail(i2_id)
    assert "dependent_pinned_here=s_new" in out


def test_dependent_pinned_here_includes_not_live_strategies(tmp_path):
    """/code-review 2 周目 CR4 (2026-09-18、設計書 §2.7 v1.6): (i) 欄の
    母集団は `phase1_metas`。

    I1 承認済 → 候補 I2 (pending) → strategy `s_new` を **I2 の hash に
    pin して配備**すると、`approved_plugins` 第 2 相は現承認 hash (I1) と
    不一致なので `s_new` を落とす (= `inventory.metas` に居ない)。
    旧実装は (i) 欄を `inventory.metas` だけで作っていたため、
    「まさにこの候補を承認すれば復帰する strategy」が (i) にも (ii) にも
    出ず、人間の承認判断から完全に隠れていた。"""
    shell, conn, plugins_root = _shell_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    i2_id, i2_hash = _submit_indicator_v2(conn, plugins_root, "rsi")
    # I2 は pending のまま (現承認 hash は I1) — s_new は live にならない
    _deploy_strategy(conn, plugins_root, "s_new", pins={"rsi": i2_hash})

    here, elsewhere = shell._dependent_strategies(
        indicator_name="rsi", candidate_hash=i2_hash)

    assert here == ["s_new"]
    assert elsewhere == []          # 二重掲載しない
    out = shell._approval_detail(i2_id)
    assert "dependent_pinned_here=s_new" in out
    assert "dependent_pinned_elsewhere=-" in out


def test_dependent_strategies_are_listed_in_decision_id_order(tmp_path):
    """D1 (順序、codex plan r1 I9): 表示順は**決定順 (最新承認の approval id
    昇順)** であり、plugin 名の辞書順ではない。"""
    shell, conn, plugins_root = _shell_env(tmp_path)
    from tests.fixtures import indicator_wiring as fx
    fx.write_indicator(plugins_root, "rsi")
    hashes = fx.deploy_approved(conn, plugins_root, ["rsi"], now=fx.NOW)
    # 承認順 = z_first → a_second (`_deploy_strategy` は approval 行を作る)
    _deploy_strategy(conn, plugins_root, "z_first", pins={"rsi": hashes["rsi"]})
    _deploy_strategy(conn, plugins_root, "a_second", pins={"rsi": hashes["rsi"]})
    i2_id, _i2_hash = _submit_indicator_v2(conn, plugins_root, "rsi")

    here, elsewhere = shell._dependent_strategies(
        indicator_name="rsi", candidate_hash=hashes["rsi"])

    assert here == ["z_first", "a_second"]       # 決定順 (名前順なら逆)
    assert elsewhere == []
    out = shell._approval_detail(i2_id)
    assert "dependent_pinned_elsewhere=z_first, a_second" in out


# --- 段 0 r2 (裁定 4、2026-09-19): CR1 が加えた `_plugin_lock` の
#     `ValueError` が shell の 5 つ目の経路 (reject) から漏れないこと ---

# 実測 (段 0 r2): `approve` は `_plugin_lock` まで届かない — その手前の
# `candidate_path` 正規形チェック (`approve_candidate` が lock を取る前に
# 読む `candidate_path_pre`) が先に `ValueError` を投げる。どちらの経路も
# 同じ `except (ValueError, KeyError)` に落ちて人間向け文言になる。
@pytest.mark.parametrize("verb,expected", [
    ("reject", "invalid plugin name for lock: 'Upper'"),
    ("approve", "does not match the canonical form"),
])
def test_plugin_decision_with_a_noncanonical_payload_name_reports_an_error(
        tmp_path, verb, expected):
    """段 0 r2 パート B #5 (ブリーフの 4 経路に無い 5 つ目の経路):
    `switch.reject_candidate` は approval payload の `$.name` を**検証せず**
    `_plugin_lock` を直接呼ぶ。CR1 後はそこで `ValueError` が上がるので、
    CR1 以前に作られた非正規名の payload を人間が reject/approve しようと
    したときに例外が `dispatch` の外へ出ないことを確かめる。

    `dispatch` は既に `except (ValueError, KeyError)` を持つのでこの 5 つ目の
    経路も人間向けメッセージになる (裁定 4 = pin のみ)。この pin が見るのは
    **その except が実際にこの経路を覆っていること**と、例外が出た以上
    決定が成立していない (pending 留置 = fail closed) こと。

    `int(args[0])` も同じ `except ValueError` に落ちるので、文言は
    `_plugin_lock` のメッセージまで含めて確かめる (前置き `エラー:` だけの
    assert では別の ValueError と区別できない)。"""
    conn, _, activity, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "Upper", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/Upper"},
        now=NOW)

    out = cmds.dispatch(f"{verb} {approval_id} no good")

    assert "エラー:" in out
    assert expected in out
    row = conn.execute("SELECT status FROM approval_requests WHERE id=?",
                       (approval_id,)).fetchone()
    assert row["status"] == "pending"
    # lock ファイルも作られない (`.locks` の mkdir より前に落ちる)
    assert not (plugins_dir / ".locks").exists()


# ============================================================
# [switch-ops-hardening] T5 / T7 — `approval list` と retry の結果報告
# ============================================================

def _pending_plugin_approval(conn, name="sma", chash="a" * 64):
    return approvals.create(
        conn, kind="plugin",
        payload={"name": name, "content_hash": chash, "artifact_hash": "b" * 64,
                 "candidate_origin": "staging",
                 "candidate_path": f"plugins/_staging/1/{name}"},
        now=NOW)


def test_approval_list_shows_pending_ids_and_no_metrics(tmp_path):
    """AC-12a / AC-13: pending を id 昇順で列挙し、holdout / in_sample の
    数値は出さない (詳細は `approval <id>`)。"""
    conn, _, _, cmds = _commands(tmp_path)
    a1 = _pending_plugin_approval(conn, "sma")
    a2 = _pending_plugin_approval(conn, "ema", chash="c" * 64)

    out = cmds.dispatch("approval list")

    assert out.index(f"#{a1}") < out.index(f"#{a2}")
    assert "name=sma" in out and "name=ema" in out
    assert "content_hash=aaaaaaaa" in out
    for banned in ("in_sample", "holdout", "pf=", "avg_r"):
        assert banned not in out


def test_approval_list_empty_message(tmp_path):
    """AC-12a: 0 件の文言。未終端 journal が無ければ節ごと出ない。"""
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("approval list")
    assert out == "承認待ちはありません"
    assert "未終端" not in out


def test_approval_list_includes_open_journal_section(tmp_path):
    """AC-12a: 未終端 journal があれば末尾の節に op_id / name / phase / approval_id。"""
    conn, _, _, cmds = _commands(tmp_path)
    aid = _pending_plugin_approval(conn, "sma")
    op_id = plugin_switch.begin_switch_journal(
        conn, kind="bless", approval_id=aid, name="sma", old_kind="absent",
        old_target=None, new_target=".versions/sma/" + "b" * 64,
        switch_required=True, actor="human_cli", now=NOW, commit=True)

    out = cmds.dispatch("approval list")

    assert "-- 未終端の切替ジャーナル --" in out
    assert f"op_id={op_id} name=sma phase=preparing approval_id={aid}" in out


def test_approval_list_line_is_verbatim_and_pending_only(tmp_path):
    """段 0 pin (S0-21/S0-26/S0-27): 一覧は **pending だけ** を、設計書 §3.5 の
    表示項目どおりの**逐語**形式で 1 行 1 件出す。部分一致
    (`"content_hash=aaaaaaaa" in out`) では `WHERE status='pending'` の削除も
    先頭 8 桁の切り詰めの削除も素通りした (実測 SURVIVED)。"""
    conn, _, _, cmds = _commands(tmp_path)
    a1 = _pending_plugin_approval(conn, "sma")
    a2 = _pending_plugin_approval(conn, "ema", chash="c" * 64)
    approvals.apply_decision(conn, a2, "approved", decided_by="t", now=NOW,
                             commit=True)
    created = conn.execute(
        "SELECT created_at FROM approval_requests WHERE id=?", (a1,)
    ).fetchone()["created_at"]

    lines = cmds.dispatch("approval list").splitlines()

    assert lines == [f"#{a1} kind=plugin name=sma created_at={created} "
                     f"content_hash=aaaaaaaa"]


def test_approval_list_journal_line_distinguishes_op_id_from_approval_id(tmp_path):
    """段 0 pin (S0-31): 既存テストは `op_id == approval_id == 1` の縮退
    fixture だったので、`approval_id` を `op_id` に取り違える変異が
    SURVIVED した ([[mutation-testing]] 6.11 ③)。2 つを必ず食い違わせる。"""
    conn, _, _, cmds = _commands(tmp_path)
    _pending_plugin_approval(conn, "old")          # id=1 を消費する捨て駒
    aid = _pending_plugin_approval(conn, "sma")    # id=2
    op_id = plugin_switch.begin_switch_journal(
        conn, kind="bless", approval_id=aid, name="sma", old_kind="absent",
        old_target=None, new_target=".versions/sma/" + "b" * 64,
        switch_required=True, actor="human_cli", now=NOW, commit=True)
    assert op_id != aid, "fixture が縮退している (op_id == approval_id)"

    out = cmds.dispatch("approval list")

    assert f"op_id={op_id} name=sma phase=preparing approval_id={aid}" in out


def test_approval_list_limit_and_cap(tmp_path):
    """AC-12b: `<n>` が効き、既定 20 / 上限 200 で打ち切る。"""
    conn, _, _, cmds = _commands(tmp_path)
    for i in range(25):
        _pending_plugin_approval(conn, "sma", chash=f"{i:064d}")
    assert len(cmds.dispatch("approval list 5").splitlines()) == 5
    assert len(cmds.dispatch("approval list").splitlines()) == 20
    out = cmds.dispatch("approval list 999")
    assert "(上限 200 件で打ち切り)" in out


@pytest.mark.parametrize("payload_json,expected", [
    ('{"name": "sma"}', ("name=sma", "content_hash=-")),                     # キー欠落
    ('{"name": "sma", "content_hash": null}', ("name=sma", "content_hash=-")),  # 値 None
    ('{"name": "sma", "content_hash": ""}', ("name=sma", "content_hash=-")),    # 空
    ("{oops", ("name=-", "content_hash=-")),                                 # 壊れた JSON
    ("", ("name=-", "content_hash=-")),                                      # 空文字列
    # (payload_json は NOT NULL なので NULL は DB 側が弾く — 空文字列が下限)
])
def test_approval_list_is_fail_soft_for_odd_payloads(tmp_path, payload_json,
                                                     expected):
    """段 0 pin (S0-23/S0-24/S0-25/S0-26): payload の 3 値 (キー欠落 / 値 None /
    空) と壊れた JSON / NULL を 1 本も渡していなかったので、`json.loads` の
    try/except・`or ""`・`get('name', '-')`・`[:8] or '-'` の**全部**が
    SURVIVED した ([[mutation-testing]] 6.6)。一覧は落ちずに `-` を出す。"""
    conn, _, _, cmds = _commands(tmp_path)
    aid = _pending_plugin_approval(conn, "sma")
    conn.execute("UPDATE approval_requests SET payload_json=? WHERE id=?",
                 (payload_json, aid))
    conn.commit()

    out = cmds.dispatch("approval list")

    assert out.startswith(f"#{aid} kind=plugin ")
    for part in expected:
        assert part in out, f"{part!r} が出ていない: {out!r}"


def test_approval_list_argument_boundaries(tmp_path):
    """段 0 pin (S0-19/S0-33/S0-93): 設計書 §3.5 の引数の表のうち
    「引数過多 → `_HELP`」と「ちょうど上限 (200) は丸めない」が未 pin で、
    `len(args) <= 3` への緩和も `>` → `>=` も SURVIVED した。
    丸めた件数そのもの (MAX の 2 倍にする変異) も測る。"""
    from agentic_fx.commands import _HELP
    conn, _, _, cmds = _commands(tmp_path)
    for i in range(3):
        _pending_plugin_approval(conn, "sma", chash=f"{i:064d}")

    assert cmds.dispatch("approval list 5 6") == _HELP, "引数過多は _HELP"
    assert "打ち切り" not in cmds.dispatch("approval list 200"), \
        "ちょうど上限では丸めていないので打ち切り文言を出してはならない"
    assert "(上限 200 件で打ち切り)" in cmds.dispatch("approval list 201")
    # 丸めた先の件数が本当に上限ちょうどか (打ち切り文言だけでは測れない)
    for i in range(3, Commands._APPROVAL_LIST_MAX + 5):
        _pending_plugin_approval(conn, "sma", chash=f"{i:064d}")
    out = cmds.dispatch("approval list 999").splitlines()
    assert len(out) == Commands._APPROVAL_LIST_MAX + 1, \
        f"丸めた先の件数が上限 + 打ち切り 1 行と違う: {len(out)}"


def test_approval_list_rejects_bad_argument(tmp_path):
    """AC-12c: 0 / 負数 / 非数値は usage。既存の `approval <id>` を壊さない。"""
    conn, _, _, cmds = _commands(tmp_path)
    aid = _pending_plugin_approval(conn, "sma")
    for bad in ("approval list 0", "approval list -1", "approval list abc"):
        assert cmds.dispatch(bad) == "usage: approval list [n]"
    assert f"approval #{aid}" in cmds.dispatch(f"approval {aid}")
    assert "存在しません" in cmds.dispatch("approval 999")


def _fake_outcome(**kw):
    base = dict(outcome="deployed", name="sma", status="approved", op_id=3,
                target=".versions/sma/" + "a" * 64)
    base.update(kw)
    return plugin_switch.ApprovalOutcome(**base)


@pytest.mark.parametrize("outcome,expected", [
    (_fake_outcome(), "配備まで完了しました (plugins/sma → .versions/sma/" + "a" * 64 + ")"),
    (_fake_outcome(outcome="deployed_after_rollback", rolled_back_op_id=2),
     "中断していた切替 (op_id=2) を巻き戻してから再実行し、配備まで完了しました"),
    (_fake_outcome(outcome="foreign_waiting", status="pending", target=None),
     "live が第三者に触られているため自動収束しません (op_id=3)"),
    (_fake_outcome(outcome="still_pending", status="pending", target=None,
                   reason="candidate_missing"),
     "approved になりませんでした (reason=candidate_missing)"),
    (_fake_outcome(outcome="legacy_plain_present", status="pending", target=None),
     "plugins/sma が旧式のディレクトリのままです"),
    (_fake_outcome(outcome="already_decided", status="rejected", target=None),
     "この承認は既に決着しています (status=rejected)"),
    # 段 0 pin (S0-15): `invalidated` の行が無く、タプルから外す変異が
    # SURVIVED した。7 つの outcome を全て写せることを見る。
    (_fake_outcome(outcome="invalidated", status="invalidated", target=None),
     "この承認は既に決着しています (status=invalidated)"),
])
def test_approval_retry_reports_the_locked_outcome(tmp_path, monkeypatch, outcome,
                                                   expected):
    """AC-14a: シェルは lock 内で確定した outcome を文言に写すだけ (全 6 行)。"""
    conn, _, _, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval",
                        lambda *a, **kw: outcome)

    out = cmds.dispatch("approval retry 7")

    assert out.startswith("approval #7 を再試行しました: ")
    assert expected in out


def test_approval_retry_fails_loud_on_unknown_outcome(tmp_path, monkeypatch):
    """AC-14d(3): 未知 / None の outcome は**文言にしない** (fail loud)。
    接頭辞も付かず `エラー: ...` になる。"""
    conn, _, _, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval",
                        lambda *a, **kw: None)

    out = cmds.dispatch("approval retry 7")

    assert out.startswith("エラー: ")
    assert "を再試行しました" not in out


def test_approval_retry_missing_id_is_value_error_with_help(tmp_path, monkeypatch):
    """AC-14a: 存在しない approval への retry は `ValueError` 送出で、
    先行する `except (ValueError, KeyError)` に捕まり `_HELP` が付く。"""
    from agentic_fx.commands import _HELP
    conn, _, _, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS

    def _raise(*a, **kw):
        raise ValueError("approval 42 not found")

    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval", _raise)

    out = cmds.dispatch("approval retry 42")

    assert out == f"エラー: ValueError: approval 42 not found\n{_HELP}"


def test_approval_retry_message_unaffected_by_later_deployment(tmp_path, monkeypatch):
    """AC-14b: lock 解放後に別の正規配備が live を進めても、**この retry の
    報告は自分の target のまま**。シェルが lock 外で読み直さないことの pin。"""
    conn, _, _, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    (plugins_dir / ".versions" / "sma" / ("a" * 64)).mkdir(parents=True)
    (plugins_dir / ".versions" / "sma" / ("z" * 64)).mkdir(parents=True)
    (plugins_dir / "sma").symlink_to(".versions/sma/" + "a" * 64)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    mine = _fake_outcome()

    def _retry_then_competitor(*a, **kw):
        # retry は自分の target まで配備した。その直後に別プロセスが
        # **正規に**次の版を配備して live を進める (IV-3 は「approved に
        # なった瞬間」の条件なので、これは契約違反ではない)。
        tmp_link = plugins_dir / ".sma.next"
        tmp_link.symlink_to(".versions/sma/" + "z" * 64)
        os.rename(tmp_link, plugins_dir / "sma")
        return mine

    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval",
                        _retry_then_competitor)

    out = cmds.dispatch("approval retry 7")

    assert "配備まで完了しました" in out
    assert "a" * 64 in out, "後続配備の target を報告してはならない"
    assert "z" * 64 not in out
    assert "一致しません" not in out


def test_approval_retry_fails_loud_on_unknown_enum_value(tmp_path, monkeypatch):
    """AC-14d(3) の対: **未知の enum 値**も文言にしない (fail loud)。
    `None` は属性参照で落ちるが、この経路は文字列の網羅漏れを狙う。"""
    from types import SimpleNamespace
    conn, _, _, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.retry_approval",
        lambda *a, **kw: SimpleNamespace(outcome="brand_new_outcome", name="sma",
                                         status="pending", op_id=1,
                                         rolled_back_op_id=None, target=None,
                                         reason=None))

    out = cmds.dispatch("approval retry 7")

    assert out.startswith("エラー: ")
    assert "を再試行しました" not in out
    assert "brand_new_outcome" in out


# ============================================================
# [switch-ops-hardening] T9 (設計書 §3.6) — `approve <id>` も lock 内 outcome
# を文言に写す (AC-17a〜AC-17d)。
# ============================================================

_APPROVE_TARGET = ".versions/sma/" + "a" * 64

_APPROVE_OUTCOME_CASES = [
    pytest.param(
        _fake_outcome(op_id=3),
        "approval #{aid} approved: 配備まで完了しました "
        f"(plugins/sma → {_APPROVE_TARGET})",
        True, id="deployed"),
    pytest.param(
        _fake_outcome(outcome="deployed_after_rollback", rolled_back_op_id=2),
        "approval #{aid} approved: 中断していた切替 (op_id=2) を"
        f"巻き戻してから再実行し、配備まで完了しました (plugins/sma → {_APPROVE_TARGET})",
        True, id="deployed_after_rollback"),
    pytest.param(
        _fake_outcome(outcome="foreign_waiting", status="pending", target=None),
        "approval #{aid} は今回の操作では承認されませんでした "
        "(status=pending): live が第三者に触られているため自動収束しません "
        "(op_id=3)。plugins/sma の状態を確認してください",
        False, id="foreign_waiting"),
    pytest.param(
        _fake_outcome(outcome="still_pending", status="pending", target=None,
                     reason="candidate_missing"),
        "approval #{aid} は今回の操作では承認されませんでした "
        "(status=pending): approved になりませんでした (reason=candidate_missing)",
        False, id="still_pending"),
    pytest.param(
        _fake_outcome(outcome="legacy_plain_present", status="pending", target=None),
        "approval #{aid} は今回の操作では承認されませんでした "
        "(status=pending): plugins/sma が旧式のディレクトリのままです "
        "(先に afx plugin retire が要ります)",
        False, id="legacy_plain_present"),
    # 二重 approve (既に approved 済みの承認をもう一度 approve): §3.6.5 の 1。
    # status=approved なのに「承認されませんでした」と言う矛盾を pin する
    # (T9-M4b の killer)。
    pytest.param(
        _fake_outcome(outcome="already_decided", status="approved", target=None),
        "approval #{aid} は今回の操作では承認されませんでした "
        "(status=approved): この承認は既に決着しています (status=approved)",
        False, id="already_decided"),
    pytest.param(
        _fake_outcome(outcome="invalidated", status="invalidated", target=None),
        "approval #{aid} は今回の操作では承認されませんでした "
        "(status=invalidated): この承認は既に決着しています (status=invalidated)",
        False, id="invalidated"),
]


@pytest.mark.parametrize("outcome,template,writes_activity", _APPROVE_OUTCOME_CASES)
def test_approve_plugin_reports_the_locked_outcome(tmp_path, monkeypatch, outcome,
                                                    template, writes_activity):
    """AC-17a: `approve_candidate` の spy が 7 種の outcome を返すとき、
    戻り文字列は §3.6.3 の逐語 (接頭辞込み、`==` で完全一致)。"""
    conn, _, _, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    approval_id = _pending_plugin_approval(conn, "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.approve_candidate",
                        lambda *a, **kw: outcome)

    out = cmds.dispatch(f"approve {approval_id}")

    assert out == template.format(aid=approval_id)


@pytest.mark.parametrize("outcome,template,writes_activity", _APPROVE_OUTCOME_CASES)
def test_approve_plugin_writes_activity_only_when_deployed(tmp_path, monkeypatch,
                                                            outcome, template,
                                                            writes_activity):
    """AC-17b: activity `approved` を書くのは `deployed` / `deployed_after_rollback`
    のときだけ。`already_decided` かつ `status="approved"` (二重 approve) でも
    書かない。"""
    conn, _, activity, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    approval_id = _pending_plugin_approval(conn, "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.approve_candidate",
                        lambda *a, **kw: outcome)

    cmds.dispatch(f"approve {approval_id}")

    records = activity.tail(10, Category.APPROVAL)
    assert any("approved" in r for r in records) == writes_activity


def test_approve_plugin_message_unaffected_by_later_decision(tmp_path, monkeypatch):
    """AC-17c: lock を抜けた後に別プロセスがこの approval を承認しても、
    シェルは **自分の呼び出しが返した outcome** を報告する。lock 外で
    `SELECT status` し直す実装 (v1.1 までの approve ハンドラ) では
    「自分が下していない決定」を自分の結果として報告してしまう。"""
    conn, _, activity, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / ".locks").mkdir()
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "sma", "content_hash": "h1", "artifact_hash": "a1",
                 "candidate_origin": "staging",
                 "candidate_path": "plugins/_staging/1/sma"},
        now=NOW)  # candidate_path のディレクトリを作らない → candidate_missing
    real = plugin_switch.approve_candidate

    def _then_competitor(*a, **kw):
        outcome = real(*a, **kw)          # lock はここで解放される
        # 競合者: 別コネクションで同じ approval を approved にする
        conn2 = connect(tmp_path / "t.db")
        approvals.apply_decision(conn2, approval_id, "approved",
                                 decided_by="competitor", now=NOW, commit=True)
        conn2.close()
        return outcome

    monkeypatch.setattr("agentic_fx.plugin.switch.approve_candidate",
                        _then_competitor)

    out = cmds.dispatch(f"approve {approval_id}")

    # 逐語で見る。`"approved: " not in out` だけでは旧文言
    # `approval #N approved` (コロン無し) と区別できない。
    assert out == (
        f"approval #{approval_id} は今回の操作では承認されませんでした "
        f"(status=pending): approved になりませんでした (reason=candidate_missing)")
    assert not any("approved" in r for r in activity.tail(10, Category.APPROVAL))


def test_approve_plugin_fails_loud_on_unknown_outcome(tmp_path, monkeypatch):
    """AC-17d: spy が `None` を返すと `エラー: ` で始まり、
    接頭辞 (`approval #<id> ...`) は付かない (fail loud)。"""
    conn, _, _, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    approval_id = _pending_plugin_approval(conn, "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.approve_candidate",
                        lambda *a, **kw: None)

    out = cmds.dispatch(f"approve {approval_id}")

    assert out.startswith("エラー: ")
    assert "approval #" not in out.split("\n")[0]


def test_approve_plugin_fails_loud_on_unknown_enum_value(tmp_path, monkeypatch):
    """AC-17d の対: **未知の enum 値**も文言にしない (fail loud)。`None` は
    属性参照 (`outcome.outcome`) で落ちるだけなので `_approval_outcome_text`
    末尾の `raise ValueError` までは届かない — この経路は文字列の網羅漏れ
    (T9-M7) を狙う (`test_approval_retry_fails_loud_on_unknown_enum_value`
    の approve 版)。"""
    from types import SimpleNamespace
    conn, _, _, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    approval_id = _pending_plugin_approval(conn, "sma")
    monkeypatch.setattr(
        "agentic_fx.plugin.switch.approve_candidate",
        lambda *a, **kw: SimpleNamespace(outcome="brand_new_outcome", name="sma",
                                         status="pending", op_id=1,
                                         rolled_back_op_id=None, target=None,
                                         reason=None))

    out = cmds.dispatch(f"approve {approval_id}")

    assert out.startswith("エラー: ")
    assert "approval #" not in out.split("\n")[0]
    assert "brand_new_outcome" in out


def test_approve_plugin_outcome_text_is_shared_with_retry(tmp_path, monkeypatch):
    """同じ `ApprovalOutcome` を `approve` 経路と `approval retry` 経路に流し、
    接頭辞を除いた残りが 1 文字も違わないこと。文言生成の二重実装が入ると
    red になる。"""
    conn, _, _, cmds = _commands(tmp_path)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / ".locks").mkdir(parents=True)
    cmds.plugins_root = plugins_dir
    cmds.settings = SETTINGS
    outcome = _fake_outcome()
    approve_id = _pending_plugin_approval(conn, "sma")
    monkeypatch.setattr("agentic_fx.plugin.switch.approve_candidate",
                        lambda *a, **kw: outcome)

    approve_out = cmds.dispatch(f"approve {approve_id}")

    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval",
                        lambda *a, **kw: outcome)

    retry_out = cmds.dispatch("approval retry 99")

    approve_text = approve_out[len(f"approval #{approve_id} approved: "):]
    retry_text = retry_out[len("approval #99 を再試行しました: "):]
    assert approve_text == retry_text


def test_approve_non_plugin_kind_is_unchanged(tmp_path):
    """§3.6.4: 非 plugin kind の approve は `approve_candidate` を通らず、
    `approval #<id> approved` (逐語) + DB `approved` + activity `approved`
    のまま (非 plugin 枝が巻き添えにならないことの pin)。"""
    conn, _, activity, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)

    out = cmds.dispatch(f"approve {aid}")

    assert out == f"approval #{aid} approved"
    row = conn.execute(
        "SELECT status FROM approval_requests WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "approved"
    assert any("approved" in r for r in activity.tail(10, Category.APPROVAL))
