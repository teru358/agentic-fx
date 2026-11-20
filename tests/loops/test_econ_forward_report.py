"""日次 report の shadow 節 (戦略別の機械採点) と、その値が agent の面へ出ないこと。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.loops import econ_signal_report as esr
from agentic_fx.store import signal_forward as sf
from tests.store.forward_world import NOW, Maker, open_db, target

LABEL = date(2027, 1, 29)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "r.db")
    yield c
    c.close()


def section(text):
    head = "## shadow"
    start = text.index(head)
    rest = text[start:]
    end = rest.find("\n## ", 1)
    return rest if end < 0 else rest[:end]


def report(facts):
    return esr.render_report(facts, label=LABEL, close_at=NOW)


def seeded(conn):
    make = Maker(conn)
    for i, pnl in enumerate((100.0, -100.0, 50.0, 50.0, 100.0, 0.0)):
        make.decide(at=NOW - timedelta(days=i + 1), pnl=pnl, result="tp" if pnl > 0 else "sl",
                    accept=i % 2 == 0)
    make.decide(at=NOW - timedelta(days=9), result="no_fill")
    make.signal(at=NOW - timedelta(days=2), status="abandoned", reason="freshness_expired")
    return make


def test_the_shadow_section_is_split_by_strategy_with_the_fixed_caveats(conn):
    seeded(conn)
    facts = esr.collect_report_facts(
        conn, now=NOW, capacity=None, worker=None,
        forward_targets=lambda c: [target(name="strat"), target(name="zeta", content_hash="q")])
    text = section(report(facts))
    assert "### strat" in text and "### zeta" in text
    assert "実約定を含まない" in text and "採否に至らなかった signal は含みません" in text
    strat = text[text.index("### strat"):text.index("### zeta")]
    assert "全採否: N=6 勝/負/分=4/1/1" in strat
    assert "accept: N=3" in strat and "reject: N=3" in strat
    assert "採否: accept=4 reject=3" in strat
    assert "abandoned: freshness_expired=1" in strat
    assert "no_fill=1" in strat


def test_values_below_the_minimum_count_are_not_shown_and_reference_values_are_marked(conn):
    make = Maker(conn)
    for i in range(4):
        make.decide(at=NOW - timedelta(days=i + 1), pnl=100.0)
    facts = esr.collect_report_facts(conn, now=NOW, capacity=None, worker=None,
                                     forward_targets=lambda c: [target()])
    low = section(report(facts))
    assert "平均R=–" in low and "PF=–" in low
    make.decide(at=NOW - timedelta(days=9), pnl=-100.0)
    facts = esr.collect_report_facts(conn, now=NOW, capacity=None, worker=None,
                                     forward_targets=lambda c: [target()])
    assert "平均R=0.60 (参考)" in section(report(facts))


def test_a_strategy_with_an_unknown_generation_shows_no_numbers(conn):
    seeded(conn)
    facts = esr.collect_report_facts(
        conn, now=NOW, capacity=None, worker=None,
        forward_targets=lambda c: [target(reason="no_generation_record")])
    text = section(report(facts))
    assert "判定不能 (no_generation_record)" in text
    assert "N=" not in text and "平均R" not in text


def test_a_failing_target_reader_makes_only_the_shadow_section_unavailable(conn):
    def broken(_conn):
        raise RuntimeError("plugins unreadable")

    facts = esr.collect_report_facts(conn, now=NOW, capacity=None, worker=None,
                                     forward_targets=broken)
    assert facts["forward"] is None
    text = report(facts)
    assert section(text).strip().endswith("unavailable")
    assert facts["decisions"] is not None


def test_without_a_target_reader_the_section_is_unavailable_not_empty(conn):
    facts = esr.collect_report_facts(conn, now=NOW, capacity=None, worker=None)
    assert section(report(facts)).strip().endswith("unavailable")


def test_no_live_strategy_is_written_as_none(conn):
    facts = esr.collect_report_facts(conn, now=NOW, capacity=None, worker=None,
                                     forward_targets=lambda c: [])
    assert "配備中の戦略はありません" in section(report(facts))


def test_one_unreadable_strategy_does_not_hide_the_others(conn, monkeypatch):
    seeded(conn)
    real = sf.forward_summary

    def flaky(c, t, *, now):
        if t.name == "zeta":
            raise RuntimeError("boom")
        return real(c, t, now=now)

    monkeypatch.setattr(esr.signal_forward, "forward_summary", flaky)
    facts = esr.collect_report_facts(
        conn, now=NOW, capacity=None, worker=None,
        forward_targets=lambda c: [target(name="strat"), target(name="zeta")])
    text = section(report(facts))
    assert "### strat" in text and "N=6" in text
    assert "### zeta" in text and "集計できませんでした" in text


def test_the_ctl_status_shadow_line_does_not_carry_the_machine_scores(conn):
    seeded(conn)
    out = esr.econ_signal_status(conn, now=NOW, capacity=None, worker=None)
    assert all("avg_r" not in row and "pf" not in row for row in out["shadow"])
    assert "forward" not in out


def test_the_daily_callback_writes_the_strategy_section(tmp_path):
    path = tmp_path / "cb.db"
    c = open_db(path)
    seeded(c)
    c.close()
    callback = esr.make_daily_report_callback(
        reports_dir=tmp_path / "econ_signal", open_conn=lambda: open_db(path),
        capacity=None, worker=None, forward_targets=lambda conn: [target()])
    callback(datetime(2027, 1, 29, 22, 0, tzinfo=timezone.utc))
    body = next((tmp_path / "econ_signal").glob("econ-signal-*.md")).read_text()
    assert "### strat" in body


def test_the_service_hands_the_live_plugin_reader_to_the_daily_report():
    import ast
    source = (Path(__file__).resolve().parents[2] / "src" / "agentic_fx"
              / "service.py").read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call)
             and getattr(node.func, "attr", "") == "make_daily_report_callback"]
    assert len(calls) == 1
    assert "forward_targets" in {kw.arg for kw in calls[0].keywords}


# ---------------------------------------------------------------- agent の面へ出ない

_CANARIES = ("forwardcanary", "4747.25", "3737.5", "reach_rate", "max_day_share",
             "oldest_unterminated_sec")


def test_the_forward_values_reach_no_improve_surface(tmp_path):
    from agentic_fx.config import load_settings
    from agentic_fx.loops.improve_context import build_improve_context
    from agentic_fx.loops.prompts_loader import load_prompt

    c = open_db(tmp_path / "i.db")
    make = Maker(c)
    for i in range(6):
        make.decide(at=NOW - timedelta(days=i + 1), plugin="forwardcanary", pnl=4747.25 + i,
                    result="tp")
    make.decide(at=NOW - timedelta(days=8), plugin="forwardcanary", pnl=-3737.5, result="sl")
    facts = esr.collect_report_facts(
        c, now=NOW, capacity=None, worker=None,
        forward_targets=lambda conn: [target(name="forwardcanary")])
    text = report(facts)
    assert "forwardcanary" in text and "N=7" in text           # 人間の面には出る
    settings = load_settings(
        Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    ctx = build_improve_context(c, settings=settings, now=NOW, root=tmp_path,
                                allowed_backlog_ids=None)
    blob = json.dumps(ctx, default=str, ensure_ascii=False)
    prompt = load_prompt("improve_mission")
    for surface in (blob, prompt):
        assert [word for word in _CANARIES if word in surface] == []
    c.close()

