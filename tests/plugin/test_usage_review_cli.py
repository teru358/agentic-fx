"""`afx plugin list --unused`: 未使用の指標だけを出す。daemon なしで読み、通常の一覧は変えない。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest import cli
from agentic_fx.entry import main
from agentic_fx.backtest.plugin_deploy_cli import plugin_list
from tests.plugin.read_model_world import World

NOW = datetime(2027, 1, 31, 12, 0, tzinfo=timezone.utc)
WEEK = timedelta(weeks=1)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.conn.close()


def put(world, name, *, deployed=NOW - 20 * WEEK, origin="staging"):
    meta = world.deploy(name)
    approval = world.request(meta, status="approved", origin=origin)
    if deployed is not None:
        world.journal(name, approval, new_target=f".versions/{name}/{meta.artifact_hash}",
                      at=deployed)
    return meta


def run(world, capsys, **kwargs):
    rc = plugin_list(world.root, **kwargs)
    return rc, capsys.readouterr().out.splitlines()


def test_the_default_table_is_unchanged_by_the_unused_review(world, capsys):
    put(world, "rsi")
    rc, lines = run(world, capsys)
    assert rc == 0
    assert lines[:4] == ["source live: ok", "source pending: ok", "source journal: ok",
                         "NAME  KIND       LIVE          PENDING  OPEN_OP  ISSUES"]
    assert len(lines) == 5 and lines[4].startswith("rsi")
    assert not any("未使用" in line for line in lines)


def test_unused_lists_each_unused_indicator_with_whether_it_can_be_proposed(world, capsys):
    put(world, "aaa")
    put(world, "bbb", origin="human")
    put(world, "ccc", deployed=NOW - WEEK)
    rc, lines = run(world, capsys, unused=True, now=NOW)
    assert rc == 0
    table = [line for line in lines if line.startswith(("NAME", "aaa", "bbb", "ccc"))]
    assert [line.split()[0] for line in table] == ["NAME", "aaa", "bbb"]
    assert "可" in table[1] and "改善の提出" in table[1]
    assert "対象外 (人が提出した版)" in table[2] and "人の提出" in table[2]
    assert lines[3].startswith("未使用の指標 (読取時刻から 8 週")
    assert any("直近 8 週の戦略候補: 0 件" in line for line in lines)
    assert any("afx plugin disable" in line for line in lines)


def test_the_weeks_are_counted_from_the_given_read_time(world, capsys):
    put(world, "rsi", deployed=NOW - 8 * WEEK)
    _, at_now = run(world, capsys, unused=True, now=NOW)
    _, earlier = run(world, capsys, unused=True, now=NOW - WEEK)
    assert any(line.startswith("rsi") for line in at_now)
    assert not any(line.startswith("rsi") for line in earlier)
    assert "未使用の指標はありません" in earlier


def test_nothing_unused_and_could_not_judge_read_differently(world, capsys):
    world.deploy("plainone", plain=True)
    rc, lines = run(world, capsys, unused=True, now=NOW)
    assert "未使用と決まった指標はありません" in lines
    assert "判定できなかった指標: 1 件" in lines
    assert any(line.startswith("  plainone: 配備した記録が見つからない") for line in lines)
    assert rc == 0
    _, empty = run(World(world.root.parent / "other"), capsys, unused=True, now=NOW)
    assert "未使用の指標はありません" in empty


def test_a_used_indicator_is_not_listed(world, capsys):
    meta = put(world, "rsi")
    world.deploy("follow", "strategy", f"indicators:\n  a: {{plugin: rsi, pin: '{meta.content_hash}'}}\n")
    _, lines = run(world, capsys, unused=True, now=NOW)
    assert not any(line.startswith("rsi") for line in lines)
    assert "未使用の指標はありません" in lines


def test_excluded_candidate_rows_are_counted_by_reason(world, capsys):
    put(world, "rsi")
    world.candidate(None, NOW - WEEK)
    world.candidate("garbage", NOW - WEEK)
    world.candidate("strategy:other@1h@USDJPY", "bad time")
    _, lines = run(world, capsys, unused=True, now=NOW)
    summary = next(line for line in lines if line.startswith("直近 8 週の戦略候補"))
    assert "key なし 1" in summary and "key を読めない 1" in summary
    assert "作成時刻を読めない 1" in summary


def test_a_journal_that_cannot_be_read_is_reported_with_a_failing_status(world, capsys):
    put(world, "rsi")
    world.conn.execute("ALTER TABLE plugin_switch_journal RENAME TO moved")
    world.conn.commit()
    rc, lines = run(world, capsys, unused=True, now=NOW)
    assert rc == 1
    assert any("切替の記録を読めなかった" in line for line in lines)


def test_a_candidate_table_that_cannot_be_read_is_unknown_and_fails(world, capsys):
    put(world, "rsi")
    world.conn.execute("ALTER TABLE candidate_archives RENAME TO moved")
    world.conn.commit()
    rc, lines = run(world, capsys, unused=True, now=NOW)
    assert rc == 1
    assert any("戦略候補" in line and "不明" in line for line in lines)
    assert not any("戦略候補: 0 件" in line for line in lines)
    assert any("戦略候補の記録を読めませんでした" in line for line in lines)


def test_unused_with_the_strategy_kind_is_a_usage_error(world, capsys):
    put(world, "rsi")
    rc = plugin_list(world.root, unused=True, kind="strategy", now=NOW)
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "--unused" in captured.err and "indicator" in captured.err


def test_unused_with_the_indicator_kind_still_lists(world, capsys):
    put(world, "rsi")
    _, lines = run(world, capsys, unused=True, kind="indicator", now=NOW)
    assert any(line.startswith("rsi") for line in lines)


def test_the_option_is_wired_through_the_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def refuse(_root):
        raise AssertionError("plugin list must not initialize")

    monkeypatch.setattr(cli, "ensure_initialized", refuse)
    assert main(["plugin", "list", "--unused"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "未使用の指標はありません" in out
    assert list(tmp_path.iterdir()) == []
