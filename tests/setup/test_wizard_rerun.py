from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agentic_fx.config import load_settings_text
from agentic_fx.core.scheduler import scheduled_period
from agentic_fx.setup.preflight import PreflightFailure
from agentic_fx.setup.settings_edit import edit_settings
from agentic_fx.setup.wizard import run_wizard
from agentic_fx.store import orders
from agentic_fx.store.db import connect

from tests.setup.wizard_support import (
    NOW, ScriptedWizardIO, fresh_responder, make_deps, make_root, snapshot, LOCK_FILES,
)

CONSISTENT = {
    "datafeed.yfinance.enabled": False, "datafeed.mt5.enabled": True, "backtest.eval_source": "mt5",
    "backtest.base_interval": "5m", "api.enabled": True,
}


def installed_root(tmp_path, *, extra=None, mutate=None):
    """既に設定済みの root (コメントと独自の桁揃えを含む)。"""
    root = make_root(tmp_path)
    example = (root / "config" / "settings.yaml.example").read_text()
    result = edit_settings(example, {**CONSISTENT, **(extra or {})})
    assert result.ok, result.reason
    text = result.text.replace("account_currency: JPY         #", "account_currency: JPY               #")
    if mutate:
        text = mutate(text)
    (root / "config" / "settings.yaml").write_text(text)
    return root, text


def line_diff(old: str, new: str):
    old_lines, new_lines = old.splitlines(), new.splitlines()
    assert len(old_lines) == len(new_lines)
    return [(a, b) for a, b in zip(old_lines, new_lines) if a != b]


def test_answering_one_item_in_a_rerun_changes_exactly_that_line(tmp_path):
    root, before = installed_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("表示タイムゾーン (例", "UTC")))
    assert run_wizard(root, io, deps) == 0, io.text
    after = (root / "config" / "settings.yaml").read_text()
    diff = line_diff(before, after)
    assert len(diff) == 2 and diff[0][1].startswith("display_timezone: UTC")
    assert any("enabled: false" in line for _, line in diff)
    assert diff[0][1].split("#", 1)[1] == diff[0][0].split("#", 1)[1]
    backups = sorted((root / "config").glob("settings.yaml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == before
    assert "再実行です" in io.text and io.asked_with("読みました") == []


def test_a_rerun_with_no_changes_writes_the_same_settings_and_no_backup(tmp_path):
    root, before = installed_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("afx を常駐サービス", "")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert (root / "config" / "settings.yaml").read_text() == before
    assert list((root / "config").glob("settings.yaml.bak-*")) == []
    assert "[段 9 要約]" in io.text


def test_changing_an_attention_item_needs_a_typed_yes(tmp_path):
    root, before = installed_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("口座通貨", "USD"), ("'yes' と打ち込", ("", "y", "yes"))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert len(io.asked_with("'yes' と打ち込")) == 3
    assert "過去の損益と通貨が混ざります" in io.text
    assert "account_currency: USD" in (root / "config" / "settings.yaml").read_text()


def test_without_the_typed_yes_nothing_is_written(tmp_path):
    root, before = installed_root(tmp_path)
    snap = snapshot(root)
    seen = {"yes": 0}
    base = fresh_responder(("口座通貨", "USD"))

    def responder(prompt):
        if "'yes' と打ち込" in prompt:
            seen["yes"] += 1
            if seen["yes"] > 2:
                raise EOFError
            return ""
        return base(prompt)

    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=responder)
    assert run_wizard(root, io, deps) == 1
    after = snapshot(root)
    assert {k: v for k, v in after.items() if k in snap} == snap
    assert set(after) - set(snap) <= LOCK_FILES
    assert (root / "config" / "settings.yaml").read_text() == before


def test_a_duplicate_key_stops_the_rerun_and_names_the_lines(tmp_path):
    root, before = installed_root(tmp_path, mutate=lambda text: text + "display_timezone: UTC\n")
    snap = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("afx を常駐サービス", ""),
                                                    ("この内容で書き込みます", ("d", "y"))))
    assert run_wizard(root, io, deps) == 1
    assert "display_timezone が 2 回書かれています" in io.text and "行 3 と 179" in io.text
    assert {k: v for k, v in snapshot(root).items() if k in snap} == snap


def test_a_settings_file_newer_than_this_afx_is_never_modified(tmp_path):
    root, before = installed_root(tmp_path, mutate=lambda t: t.replace("schema_version: 2", "schema_version: 99"))
    snap = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("この内容で書き込みます", ("d", "y"))))
    assert run_wizard(root, io, deps) == 1
    assert "新しい版" in io.text
    assert {k: v for k, v in snapshot(root).items() if k in snap} == snap


def test_a_shape_the_migration_cannot_place_is_reported_without_writing(tmp_path):
    def unplaceable(text):
        lines = [line for line in text.splitlines() if not line.startswith(("schema_version", "display_timezone"))]
        index = next(i for i, line in enumerate(lines) if line.startswith("account_currency"))
        lines.insert(index + 1, "display_timezone: Asia/Tokyo")
        return "\n".join(lines) + "\n"

    root, before = installed_root(tmp_path, mutate=unplaceable)
    snap = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 1
    assert "schema_version を入れる位置を決められません" in io.text
    assert {k: v for k, v in snapshot(root).items() if k in snap} == snap


def test_an_old_version_is_migrated_in_the_same_write_and_shown_in_the_summary(tmp_path):
    def versionless(text):
        return "\n".join(line for line in text.splitlines() if not line.startswith("schema_version")) + "\n"

    root, before = installed_root(tmp_path, mutate=versionless)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("afx を常駐サービス", ""),
                                                    ("この内容で書き込みます", ("d", "y"))))
    assert run_wizard(root, io, deps) == 0, io.text
    after = (root / "config" / "settings.yaml").read_text()
    assert after.startswith("schema_version: 2") or "\nschema_version: 2" in after
    assert "移行する行" in io.text and "+schema_version: 2" in io.text
    kept = [line for line in after.splitlines() if not line.startswith("schema_version")]
    assert kept == before.splitlines()
    assert len(list((root / "config").glob("settings.yaml.bak-*"))) == 1


def test_a_multiline_value_that_cannot_be_edited_stops_without_writing(tmp_path):
    def folded(text):
        return text.replace("pairs: [USDJPY]", "pairs:\n  - USDJPY", 1)

    root, before = installed_root(tmp_path, mutate=folded)
    snap = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("対象ペア", "EURUSD")))
    assert run_wizard(root, io, deps) == 1
    assert "手で編集してください" in io.text
    assert {k: v for k, v in snapshot(root).items() if k in snap} == snap


def test_a_decision_timeframe_that_cannot_hold_one_judgement_is_explained_and_asked_again(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("判断足", ("1m", "4h", "1"))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "1m: 判断の上限時間より短いため選べません" in io.text
    assert "4h: 保存が 1h に正規化される派生専用の足で判断足にできない" in io.text
    assert "1〜3 の番号で選んでください。" in io.text
    settings = load_settings_text((root / "config" / "settings.yaml").read_text())
    assert settings.datafeed.decision_timeframes == ["15m"]


def test_timeframe_menu_selects_thirty_minutes_by_number_and_lists_every_interval(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("判断足", "2")))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = load_settings_text((root / "config" / "settings.yaml").read_text())
    assert settings.datafeed.decision_timeframes == ["30m"]
    for label in ("1m:", "5m:", "1. 15m", "2. 30m", "3. 1h (推奨)", "4h:", "1d:"):
        assert label in io.text


def _waves(root):
    conn = sqlite3.connect(root / "data" / "agentic.db")
    try:
        return conn.execute("SELECT period_key, expected FROM improve_waves").fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("now,period,next_text", [
    (datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc), "2026-W39", "2026-10-03 (土) 03:00 JST"),
    (datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc), "2026-W40", "2026-10-10 (土) 03:00 JST"),
    (datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc), "2026-W40", "2026-10-10 (土) 03:00 JST"),
])
def test_waiting_for_the_first_run_marks_exactly_the_period_the_scheduler_would_start(
        tmp_path, now, period, next_text):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, now=lambda: now)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0, io.text
    assert f"次回は {next_text}" in io.text
    assert _waves(root) == [(period, 0)]
    settings = load_settings_text((root / "config" / "settings.yaml").read_text())
    assert scheduled_period(settings, now).period_key == period


def test_starting_right_away_leaves_the_current_period_free_for_the_scheduler(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("最初の 1 本", "2")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert _waves(root) == []


def test_the_schedule_is_interpreted_in_the_chosen_display_timezone_across_a_dst_overlap(tmp_path):
    now = datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc)  # 01:45 EDT on the day 01:30 happens twice
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, now=lambda: now)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("表示タイムゾーン (例", "America/New_York"), ("改善の時刻", "Sun 01:30")))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = load_settings_text((root / "config" / "settings.yaml").read_text())
    assert settings.display_timezone == "America/New_York" and settings.schedule.improve_at == "Sun 01:30"
    assert _waves(root) == [(scheduled_period(settings, now).period_key, 0)]


def _first_run(tmp_path, **kwargs):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path, **kwargs)
    assert run_wizard(root, ScriptedWizardIO(responder=fresh_responder()), deps) == 0
    return root


def test_changing_the_decision_timeframe_lists_strategies_whose_timeframe_no_longer_matches(tmp_path):
    meta = SimpleNamespace(name="breakout", kind="strategy", timeframe="1h")
    root = _first_run(tmp_path, deployed_plugins=lambda *_: (meta,))
    deps, *_ = make_deps(tmp_path, deployed_plugins=lambda *_: (meta,))
    io = ScriptedWizardIO(responder=fresh_responder(("判断足", "1"), ("'yes' と打ち込", "yes")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "食い違い: breakout (足 1h)" in io.text
    assert "signal が静かに効かなくなります" in io.text


def test_open_orders_are_called_out_before_an_attention_item_is_changed(tmp_path):
    root = _first_run(tmp_path)
    conn = connect(root / "data" / "agentic.db")
    orders.insert(conn, pair="USDJPY", direction="buy", entry_type="market", horizon="swing",
                  status="open", now=NOW)
    conn.commit()
    conn.close()
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("口座通貨", "USD"), ("'yes' と打ち込", "yes")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "建玉・未約定が 1 件あります" in io.text


def test_a_question_mark_shows_the_help_for_that_item_and_asks_again(tmp_path):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder(("口座通貨", ("?", ""))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "後から変えると過去の損益と混ざります" in io.text
    assert len(io.asked_with("口座通貨")) == 2


def test_a_discord_webhook_is_reported_by_name_when_the_old_notification_is_enabled(tmp_path):
    root, _ = installed_root(tmp_path, extra={"discord.enabled": True})
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0, io.text
    assert "DISCORD_WEBHOOK_URL (なし)" in io.text
    assert "discord:" in (root / "config" / "settings.yaml").read_text()
    assert "enabled: true" in (root / "config" / "settings.yaml").read_text()


def test_a_failed_whole_settings_check_sends_the_wizard_back_to_the_timeframe_question(tmp_path, monkeypatch):
    import agentic_fx.setup.wizard as wizard

    calls = {"n": 0}
    real = wizard.service._check_mission_ceilings

    def fail_once(settings, activity):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PreflightFailure("mission_ceiling_violation", "判断足 1h (3600 秒) に対し worker 1 回の上限が足幅以上です")
        return real(settings, activity)

    monkeypatch.setattr(wizard.service, "_check_mission_ceilings", fail_once)
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0, io.text
    assert "判断足の段へ戻ります" in io.text
    assert len(io.asked_with("判断足")) == 2 and len(io.asked_with("表示タイムゾーン (例")) == 1
    assert calls["n"] == 2


def test_a_cache_retention_problem_stops_before_anything_is_written(tmp_path, monkeypatch):
    import agentic_fx.setup.wizard as wizard

    def broken(settings):
        raise RuntimeError("cache retention too short")

    monkeypatch.setattr(wizard.service, "_validate_cache_retention", broken)
    root = make_root(tmp_path)
    before = snapshot(root)
    deps, *_ = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 1
    assert "cache retention too short" in io.text and "何も書いていません" in io.text
    after = snapshot(root)
    assert set(after) - set(before) <= LOCK_FILES
