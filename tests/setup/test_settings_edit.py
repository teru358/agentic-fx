import os
import stat
from pathlib import Path

import pytest

from agentic_fx.setup.settings_edit import atomic_write_settings, edit_settings, find_duplicate_keys


EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example"


def test_edits_only_answered_line_and_preserves_comment_alignment():
    text = EXAMPLE.read_text(encoding="utf-8")

    result = edit_settings(text, {"display_timezone": "UTC"})

    assert result.ok
    assert result.text is not None
    assert result.text.replace("display_timezone: UTC        ", "display_timezone: Asia/Tokyo") == text


def test_edits_two_values_in_one_flow_mapping_line():
    text = EXAMPLE.read_text(encoding="utf-8")

    result = edit_settings(text, {
        "risk.pair_rules.USDJPY.sl_distance_min_pips": 6,
        "risk.pair_rules.USDJPY.assumed_spread_pips": 1.2,
    })

    assert result.ok
    assert result.text is not None
    changed = [line for line in result.text.splitlines() if line not in text.splitlines()]
    assert changed == [
        "    USDJPY: {sl_distance_min_pips: 6, sl_distance_max_pips: 200, assumed_spread_pips: 1.2}",
    ]


def test_fourteen_answers_change_exactly_twelve_lines_without_reformatting():
    text = EXAMPLE.read_text(encoding="utf-8")

    result = edit_settings(text, {
        "display_timezone": "UTC",
        "account_currency": "USD",
        "risk.pair_rules.USDJPY.sl_distance_min_pips": 6,
        "risk.pair_rules.USDJPY.assumed_spread_pips": 1.2,
        "runner.trade.backend": "claude",
        "runner.trade.model": "claude-sonnet",
        "runner.improve.model": "different-model",
        "backtest.initial_balance": 2000000,
        "llama_swap.timeout_sec": 120,
        "datafeed.freshness_max_min": 10,
        "schedule.improve": "daily",
        "schedule.improve_at": "03:00",
        "api.max_connections": 5,
        "logging.level": "DEBUG",
    })

    assert result.ok
    assert result.text is not None
    before = text.splitlines()
    after = result.text.splitlines()
    changed = [number for number, (old, new) in enumerate(zip(before, after), 1)
               if old != new]
    assert len(changed) == 12
    assert len(before) == len(after)
    assert {number: after[number - 1] for number in changed} == {
        3: "display_timezone: UTC          # [init] ログ・status 表示に使う (保存は常に UTC、市場境界は NY 固定で変更不可)",
        4: "account_currency: USD          # [init] 利用者の基準通貨。損益・リスクの表現と換算に使う。取引可能ペアを制限するものではない (設計書 §5)。実取引では MT5 口座通貨が真で、起動時に照合する",
        23: "    USDJPY: {sl_distance_min_pips: 6, sl_distance_max_pips: 200, assumed_spread_pips: 1.2}",
        27: "  trade: {backend: claude, model: claude-sonnet}          # [init] 取引判断は local または claude",
        28: "  improve: {backend: local, model: different-model}                 # [init] 改善は local / opencode / claude / codex。ローカル LLM は opencode を使う (2026-08-30 裁定)、codex は chatgpt 専用",
        43: "  initial_balance: 2000000",
        49: "  timeout_sec: 120",
        58: "  freshness_max_min: 10        # これより古い quote/bar は不健全扱い",
        77: "  improve: daily               # [init] weekly | daily",
        78: "  improve_at: 03:00            # [init] weekly は \"<weekday> HH:MM\"、daily は \"HH:MM\" (表示 TZ)",
        117: "  level: DEBUG",
        126: "  max_connections: 5           # 同時接続。超えた接続は 503",
    }
    assert b"".join(
        line.encode() for number, line in enumerate(after, 1) if number not in changed
    ) == b"".join(
        line.encode() for number, line in enumerate(before, 1) if number not in changed
    )


def test_atomic_write_replaces_only_target_file(tmp_path):
    target = tmp_path / "settings.yaml"
    target.write_text("old\n", encoding="utf-8")

    atomic_write_settings(target, "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_preserves_existing_mode_and_rejects_symlink(tmp_path):
    target = tmp_path / "settings.yaml"
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o640)
    atomic_write_settings(target, "new\n")
    assert target.stat().st_mode & 0o777 == 0o640
    link = tmp_path / "settings-link.yaml"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="手で編集してください"):
        atomic_write_settings(link, "bad\n")
    assert target.read_text(encoding="utf-8") == "new\n"


def test_atomic_write_uses_0644_for_a_new_file(tmp_path):
    target = tmp_path / "settings.yaml"
    atomic_write_settings(target, "new\n")
    assert target.stat().st_mode & 0o777 == 0o644


def test_atomic_write_fsyncs_file_before_replace_and_directory_after(monkeypatch, tmp_path):
    target = tmp_path / "settings.yaml"
    target.write_text("old\n", encoding="utf-8")
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    def record_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)
    def record_replace(source, destination):
        events.append("replace")
        return real_replace(source, destination)
    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)
    atomic_write_settings(target, "new\n")
    assert events == ["fsync", "replace", "fsync"]


def test_duplicate_key_returns_key_and_all_line_numbers_without_editing():
    text = EXAMPLE.read_text(encoding="utf-8") + "\ndisplay_timezone: UTC\n"

    result = edit_settings(text, {"display_timezone": "UTC"})

    assert not result.ok
    assert result.text is None
    assert "display_timezone" in result.reason
    assert "3" in result.reason
    assert str(len(text.splitlines())) in result.reason


def test_duplicate_keys_in_flow_and_nested_block_are_detected():
    flow = EXAMPLE.read_text(encoding="utf-8").replace(
        "trade:   {backend: local, model: qwen3.6-35b-a3b_Q4}",
        "trade:   {backend: local, backend: claude, model: qwen3.6-35b-a3b_Q4}",
    )
    nested = "runner:\n  claude:\n    bin: first\n    bin: second\n"
    assert find_duplicate_keys(flow)["runner.trade.backend"] == [27, 27]
    assert find_duplicate_keys(nested) == {"runner.claude.bin": [3, 4]}
    result = edit_settings(flow, {"runner.trade.model": "claude-sonnet"})
    assert not result.ok
    assert result.text is None
    assert "27 と 27" in result.reason


def test_same_key_in_different_blocks_is_not_duplicate():
    assert find_duplicate_keys("first:\n  backend: local\nsecond:\n  backend: claude\n") == {}


def test_nested_scalar_edit_list_crlf_and_no_final_newline_are_preserved():
    text = EXAMPLE.read_text(encoding="utf-8").replace("\n", "\r\n").rstrip("\r\n")
    result = edit_settings(text, {"runner.claude.bin": "custom-claude"})
    assert result.ok
    assert result.text is not None
    assert "    bin: custom-claude" in result.text
    assert "\n" not in result.text.replace("\r\n", "")
    assert not result.text.endswith(("\n", "\r"))
    assert next(line for line in result.text.splitlines() if line.startswith("pairs:")) == \
        next(line for line in text.splitlines() if line.startswith("pairs:"))


def test_missing_block_tab_and_semantic_failure_do_not_edit(monkeypatch):
    absent = edit_settings(EXAMPLE.read_text(encoding="utf-8").replace("runner:\n", ""),
                           {"runner.claude.bin": "claude"})
    tabbed = edit_settings(EXAMPLE.read_text(encoding="utf-8").replace("  claude:\n", "\tclaude:\n"),
                           {"runner.claude.bin": "claude"})
    monkeypatch.setattr("agentic_fx.setup.settings_edit._get_path", lambda *_: object())
    semantic = edit_settings(EXAMPLE.read_text(encoding="utf-8"), {"display_timezone": "UTC"})
    assert not absent.ok and absent.text is None and "runner.claude.bin" in absent.reason
    assert not tabbed.ok and tabbed.text is None and "行 29" in tabbed.reason
    assert not semantic.ok and semantic.text is None and "意味的照合" in semantic.reason


def test_folded_scalar_is_rejected_without_editing():
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "display_timezone: Asia/Tokyo  # [init] ログ・status 表示に使う (保存は常に UTC、市場境界は NY 固定で変更不可)",
        "display_timezone: >\n  Asia/Tokyo",
    )

    result = edit_settings(text, {"display_timezone": "UTC"})

    assert not result.ok
    assert result.text is None
    assert "手で編集してください" in result.reason
    assert "display_timezone" in result.reason


def test_invalid_value_is_rejected_by_settings_validation():
    text = EXAMPLE.read_text(encoding="utf-8")

    result = edit_settings(text, {"display_timezone": "Not/AZone"})

    assert not result.ok
    assert result.text is None
    assert "型検証" in result.reason


def test_atomic_write_refuses_non_regular_file(tmp_path):
    import os
    import pytest
    from agentic_fx.setup.settings_edit import atomic_write_settings
    fifo = tmp_path / "settings.yaml"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        atomic_write_settings(fifo, "a: 1\n")
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
