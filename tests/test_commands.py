from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import approvals
from agentic_fx.store import snapshots
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

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
        calls.append((approval_id, decided_by))

    monkeypatch.setattr("agentic_fx.plugin.switch.retry_approval", _spy)

    out = cmds.dispatch("approval retry 42")

    assert calls == [(42, "shell")]
    assert "再試行" in out
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
    """GC 済み・失敗終端等で archive 行が無いとき「archive 不明」を明示。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "myind", "mission_id": 99, "content_hash": "h1"},
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    # ローカル approval-quality 1 周目 #C1x (2026-09-12): 4 分岐すべてが
    # 「archive=不明」で始まるため、部分一致 assert では分岐の取り違え
    # (GC 済みとパス欠落の文言すり替え等) を検出できない。理由まで pin する。
    assert "archive=不明 (GC 済み・失敗終端等)" in out


def test_approval_detail_shows_archive_unknown_when_payload_lacks_identity(
        tmp_path):
    """旧 payload に `mission_id`/`content_hash` が欠けていても例外にせず
    「archive 不明」を出す。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin", payload={"name": "myind"}, now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    # ローカル approval-quality 1 周目 #C1x (2026-09-12): 理由まで pin する。
    assert "archive=不明 (mission_id/content_hash 欠落)" in out


def test_approval_detail_archive_unknown_when_mission_id_is_wrong_type(
        tmp_path):
    """codex 1周目 I3 是正の pin: `mission_id` が int でない (list) payload
    でも例外にせず「archive 不明」を出す。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "myind", "mission_id": [], "content_hash": "h1"},
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    # ローカル approval-quality 1 周目 #C1x (2026-09-12): 理由まで pin する。
    assert "archive=不明 (mission_id/content_hash 不正)" in out


def test_approval_detail_archive_unknown_when_content_hash_is_wrong_type(
        tmp_path):
    """codex 1周目 I3 是正の pin: `content_hash` が str でない (dict)
    payload でも例外にせず「archive 不明」を出す。"""
    conn, _, _, cmds = _commands(tmp_path)
    approval_id = approvals.create(
        conn, kind="plugin",
        payload={"name": "myind", "mission_id": 5, "content_hash": {}},
        now=NOW)

    out = cmds.dispatch(f"approval {approval_id}")

    # ローカル approval-quality 1 周目 #C1x (2026-09-12): 理由まで pin する。
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
