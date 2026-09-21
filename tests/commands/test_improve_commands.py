"""Commands.dispatch の improve/backlog/policy コマンド (プラン10 Task9-7)。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.commands import Commands
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import backlog, db as db_mod
from agentic_fx.store.state import StateStore


class _FixedClock:
    def __init__(self, t):
        self._t = t
    def now(self):
        return self._t


def _fake_settings():
    """最小限のダミー設定。"""
    class Settings:
        class Risk:
            risk_per_trade_pct = 1.0
            drawdown_kill_pct = 10.0
            daily_loss_limit_pct = 5.0
        risk = Risk()
        pairs = ["EURUSD"]
    return Settings()


@pytest.fixture
def commands(tmp_path):
    conn = db_mod.connect(tmp_path / "agentic.db")
    db_mod.init_db(conn)
    state = StateStore(tmp_path / "state.json")
    # StateStore は自動的に初期化される
    broker = PaperBroker(conn, _fake_settings(), _FixedClock(datetime(2026, 8, 22)))
    (tmp_path / "logs").mkdir(exist_ok=True)
    activity = ActivityLog(tmp_path / "logs" / "activity.log")

    class _SupervisorStub:
        def submit_manual(self):
            return 42

    cmds = Commands(conn=conn, state_store=state, broker=broker,
                    trade_loop=None, activity=activity,
                    log_dir=tmp_path / "logs", clock=_FixedClock(datetime(2026, 8, 22)))
    cmds.improve_supervisor = _SupervisorStub()  # 新規属性 (下記 Step 3)
    cmds.conn_improve = conn                      # backlog/policy 用の接続
    return cmds, conn, tmp_path


def test_improve_command_submits_manual_wave(commands):
    cmds, conn, _ = commands
    out = cmds.dispatch("improve")
    assert "42" in out


def test_improve_add_creates_open_backlog_row(commands):
    cmds, conn, _ = commands
    out = cmds.dispatch("improve add EURUSD の RSI 過熱判定を改善したい")
    assert "backlog" in out.lower() or "追加" in out
    rows = backlog.list_open(conn)
    assert any("EURUSD" in r["idea"] for r in rows)


def test_improve_add_without_text_returns_usage(commands):
    cmds, _, _ = commands
    out = cmds.dispatch("improve add")
    assert "usage" in out.lower()


def test_improve_add_echoes_the_registered_idea_text(commands):
    """登録直後に正規化後の課題文を表示し、通常入力には警告しない。"""
    cmds, conn, _ = commands
    result = cmds.dispatch("improve add USDJPY のスプレッドが広い時間帯の指値精度を上げたい")
    assert "backlog #" in result
    assert "「USDJPY のスプレッドが広い時間帯の指値精度を上げたい」" in result
    assert "⚠" not in result


def test_improve_add_warns_on_short_or_placeholder_idea(commands):
    """短文または完全に囲まれたプレースホルダには警告を返す。"""
    cmds, conn, _ = commands
    result_placeholder = cmds.dispatch("improve add <案1>")
    assert "「<案1>」" in result_placeholder
    assert "⚠" in result_placeholder

    result_short = cmds.dispatch("improve add ab")
    assert "⚠" in result_short

    result_bracket = cmds.dispatch("improve add [TODO]")
    assert "⚠" in result_bracket


def test_improve_add_does_not_block_registration_when_warned(commands):
    """警告される入力も open のバックログとして登録する。"""
    cmds, conn, _ = commands
    result = cmds.dispatch("improve add <案1>")
    bid = int(result.split("#")[1].split(" ")[0])
    row = conn.execute(
        "SELECT status, idea FROM improvement_backlog WHERE id=?",
        (bid,)).fetchone()
    assert row["status"] == "open"
    assert row["idea"] == "<案1>"


def test_improve_add_warning_text_does_not_leak_to_backlog_last_result(commands):
    """表示用の echo-back と警告は backlog の結果列へ保存しない。"""
    cmds, conn, _ = commands
    cmds.dispatch("improve add <案1>")
    row = conn.execute(
        "SELECT last_result FROM improvement_backlog ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["last_result"] is None


def test_improve_add_warns_and_notes_removed_chars_for_zero_width_input(commands):
    """ゼロ幅文字だけの入力を短文として警告し、除去数を通知する。"""
    cmds, conn, _ = commands
    idea = "​" * 4
    result = cmds.dispatch(f"improve add {idea}")
    assert "⚠" in result
    assert "表示できない文字を 4 個含みます" in result


def test_improve_add_strips_terminal_control_sequences_from_reply(commands):
    """端末制御文字を含む入力を echo-back しても ESC を返さない。"""
    cmds, conn, _ = commands
    idea = "\x1b[2J値は秘密ではない長めの説明文です"
    result = cmds.dispatch(f"improve add {idea}")
    assert "\x1b" not in result


def test_normalize_idea_display_converts_newline_to_space_before_removing_control_chars():
    """改行は制御文字として除く前に空白へ置換する。"""
    from agentic_fx.commands import _normalize_idea_display

    display, removed = _normalize_idea_display("1行目\n2行目")
    assert display == "1行目 2行目"
    assert removed == 0


def test_improve_add_stores_idea_without_display_normalization(commands):
    """表示用正規化は保存する課題文に適用しない。"""
    cmds, conn, _ = commands
    idea_with_zero_width = "課題​内容"
    result = cmds.dispatch(f"improve add {idea_with_zero_width}")
    bid = int(result.split("#")[1].split(" ")[0])
    row = conn.execute(
        "SELECT idea FROM improvement_backlog WHERE id=?", (bid,)).fetchone()
    assert row["idea"] == idea_with_zero_width
    assert "​" in row["idea"]


def test_normalize_idea_display_strips_leading_and_trailing_whitespace():
    """[ops-first-contact-fixes r2] E3 是正: 段 0 の生存変異
    (`.strip()` を外しても既存テストは前後空白を含む入力を使っておらず
    検出できなかった) の pin。前後の半角スペースと、間に挟んだゼロ幅
    文字 (C* カテゴリ) を同時に含む入力で、表示文字列に前後の空白が
    残らないことを見る (C* 除去とは独立した `.strip()` の効果)。"""
    from agentic_fx.commands import _normalize_idea_display

    display, removed = _normalize_idea_display("  ​hello world​  ")
    assert display == "hello world"
    assert removed == 2


def test_improve_add_does_not_warn_on_unclosed_bracket_prefix(commands):
    """[ops-first-contact-fixes r2] E6 是正: 段 0 の生存変異 (`is_placeholder`
    から `endswith` 判定を外し開き括弧のみで判定しても、既存テストが
    開閉揃ったケースしか使っておらず検出できなかった) の pin。`<`/`[` で
    始まるが閉じ括弧が無く、かつ十分長い (4 文字以上) idea は
    プレースホルダ扱いにならない (短さ判定にも引っかからない) ことを見る。"""
    cmds, conn, _ = commands
    result_angle = cmds.dispatch("improve add <案の詳細説明がここに続きます")
    assert "⚠" not in result_angle

    result_square = cmds.dispatch("improve add [案の詳細説明がここに続きます")
    assert "⚠" not in result_square


def test_improve_add_warns_exactly_at_length_boundary(commands):
    """[ops-first-contact-fixes r2] E7 是正: 段 0 の生存変異 (`< 4` を
    `<= 4` に厳しくしても、既存テストは短文側 [`"ab"`, 2文字] しか見ておらず
    「ちょうど4文字は警告なし」の正例が無いため検出できなかった) の pin。
    ちょうど4文字 (プレースホルダでない) は警告なし、3文字は警告ありを
    同時に確認する。"""
    cmds, conn, _ = commands
    result_four = cmds.dispatch("improve add 利確早い")
    assert "⚠" not in result_four

    result_three = cmds.dispatch("improve add 利確早")
    assert "⚠" in result_three


def test_improve_add_placeholder_detection_uses_normalized_display_not_raw_text(commands):
    """[ops-first-contact-fixes r1-fix] P1 (ローカル cC 確定): `is_placeholder`
    の判定対象が**正規化後**の `display` であることの pin。既存テストの
    生入力 (`<案1>` / `[TODO]` 等) は C* 文字がブラケットの外側に無いため
    `display` 基準・`text` (正規化前) 基準のどちらで判定しても同じ結果に
    なり、判定対象を取り違える変異を検出できなかった。ゼロ幅スペース
    (U+200B、Cf) を `<`/`>` の外側に隣接させた入力 (`_normalize_idea_display`
    で除去後 `<test>` になる) で、`display` 基準なら `is_placeholder=True`
    (警告あり)・`text` 基準なら先頭がゼロ幅文字なので `False` (警告なし)
    に分岐することを利用する。"""
    cmds, conn, _ = commands
    idea = "​<test​>"
    result = cmds.dispatch(f"improve add {idea}")
    assert "⚠" in result


def test_improve_add_does_not_leak_warning_or_display_into_activity_log(commands):
    """[ops-first-contact-fixes r1-fix] P2 (ローカル cD 確定): `improve add`
    の警告文・echo-back・正規化後文字列 (`display`) が `self.activity.write`
    の引数 (実際に書かれる activity ログの行) に混入しないことの pin。
    既存テストは `improvement_backlog.last_result` (常に None) だけを見て
    おり、`activity.write(...)` の第 3 引数 (summary) を検証するテストが
    無かった。警告が出る入力 (`<案1>`) で dispatch した後、activity.log の
    該当行が従来どおり `#<bid> via shell` だけ (タブ区切りの summary
    フィールドで完全一致) であることを見る — `⚠`・`display` の内容
    (`<案1>` 自体) がどちらも log ファイル全体に現れない。"""
    cmds, conn, tmp_path = commands
    result = cmds.dispatch("improve add <案1>")
    bid = int(result.split("#")[1].split(" ")[0])
    log_text = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    lines = [ln.split("\t") for ln in log_text.splitlines() if ln]
    backlog_line = next(f for f in lines if f[2] == "backlog_added" and f[4] == str(bid))
    assert backlog_line[3] == f"#{bid} via shell"
    assert "⚠" not in log_text
    assert "<案1>" not in log_text


def test_improve_add_warning_includes_reject_command_with_actual_bid(commands):
    """[ops-first-contact-fixes r2] E13 是正: 段 0 の生存変異 (警告本文の
    案内文 [`backlog reject {bid}` での訂正手順] をマーカー `⚠` のみに
    短縮しても、案内文自体を検証する assert が無く検出できなかった) の
    pin。警告文に実際の bid を含む `backlog reject {bid}` の案内が
    含まれることを見る。"""
    cmds, conn, _ = commands
    result = cmds.dispatch("improve add ab")
    bid = int(result.split("#")[1].split(" ")[0])
    assert f"backlog reject {bid}" in result


def test_backlog_reject_closes_open_row(commands):
    cmds, conn, _ = commands
    bid = backlog.add(conn, idea="test idea", source="user",
                      now=datetime(2026, 8, 22))
    out = cmds.dispatch(f"backlog reject {bid}")
    row = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row["status"] == "rejected"
    assert row["last_result"] == "human_rejected"


def test_backlog_reopen_returns_done_to_open(commands):
    cmds, conn, _ = commands
    bid = backlog.add(conn, idea="test idea 2", source="user",
                      now=datetime(2026, 8, 22))
    backlog.set_status(conn, bid, "done", datetime(2026, 8, 22),
                       last_result="report:x", commit=True)
    out = cmds.dispatch(f"backlog reopen {bid}")
    row = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row["status"] == "open"
    assert row["last_result"] == "reopened"


def test_backlog_reopen_promotes_note_to_open(commands):
    cmds, conn, _ = commands
    now = datetime(2026, 8, 22)
    bid = backlog.add(conn, "fact becoming task", "agent", now)
    backlog.set_status(conn, bid, "note", now, last_result="human_noted")
    assert "open" in cmds.dispatch(f"backlog reopen {bid}")
    row = conn.execute("SELECT status,last_result FROM improvement_backlog WHERE id=?", (bid,)).fetchone()
    assert dict(row) == {"status": "open", "last_result": "human_reopened"}


def test_backlog_reopen_rejects_from_non_terminal_status(commands):
    """検収 B3 (2026-08-22): §4.3 の「done/rejected → open のみ」制約を
    commands.py 側の遷移ガードで強制する (反転 — 旧テストはガード不在を
    assert していた)。`selected` (Mission 実行中の行) から reopen を打つと
    `open` に落ち、別 Mission の `select_for_mission` CAS が成功しうる
    (§4 が挙げる二重承認申請への到達経路)。ガードは拒否メッセージを返し、
    status/last_result を不変に保つこと。"""
    cmds, conn, _ = commands
    bid = backlog.add(conn, idea="test idea 3", source="user",
                      now=datetime(2026, 8, 22))
    ok = backlog.select_for_mission(conn, bid, now=datetime(2026, 8, 22),
                                    commit=True)
    assert ok
    row_before = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_before["status"] == "selected"

    out = cmds.dispatch(f"backlog reopen {bid}")

    assert "selected" in out
    row_after = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_after["status"] == "selected"
    assert row_after["last_result"] == row_before["last_result"]


def test_backlog_reject_rejects_from_non_terminal_status(commands):
    """検収 B3: reject 側の同型テスト。`selected` からの reject を拒否し
    status/last_result を不変に保つ (§4.3: reject は open|observation
    からのみ)。"""
    cmds, conn, _ = commands
    bid = backlog.add(conn, idea="test idea 4", source="user",
                      now=datetime(2026, 8, 22))
    ok = backlog.select_for_mission(conn, bid, now=datetime(2026, 8, 22),
                                    commit=True)
    assert ok
    row_before = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_before["status"] == "selected"

    out = cmds.dispatch(f"backlog reject {bid}")

    assert "selected" in out
    row_after = dict(conn.execute(
        "SELECT status, last_result FROM improvement_backlog WHERE id=?", (bid,)
    ).fetchone())
    assert row_after["status"] == "selected"
    assert row_after["last_result"] == row_before["last_result"]


def test_backlog_reject_unknown_id_returns_not_found(commands):
    """検収 M-e: 存在しない id への reject/reopen が成功メッセージを返す
    (rowcount を見ない fail-open) のを閉じる。"""
    cmds, _, _ = commands
    out = cmds.dispatch("backlog reject 99999")
    assert "存在しません" in out


def test_backlog_reopen_unknown_id_returns_not_found(commands):
    cmds, _, _ = commands
    out = cmds.dispatch("backlog reopen 99999")
    assert "存在しません" in out


def test_policy_add_appends_to_directives_file(commands):
    cmds, _, root = commands
    cmds._policy_path = root / "policy" / "directives.md"  # 実装時は
                                                             # コンストラクタ
                                                             # 引数化する
                                                             # (下記申し送り)
    out = cmds.dispatch("policy add 新規指針テキスト")
    assert "新規指針テキスト" in cmds._policy_path.read_text(encoding="utf-8")
