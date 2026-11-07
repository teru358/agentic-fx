"""反応統計の人間面 (status の節・ctl 要約・日次 report) と、人間面の集計の再現。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from agentic_fx.analytics.econ_reactions import WindowValue
from agentic_fx.loops import econ_signal_report as esr
from agentic_fx.store import econ_reaction_values as rv
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)
PAIRS = {"EURUSD": ("EUR", "USD"), "USDJPY": ("USD", "JPY")}


def _conn(tmp_path):
    conn = connect(tmp_path / "r.db")
    init_db(conn)
    return conn


def _slice_set(conn, event, ts):
    end = (datetime.fromisoformat(ts) + timedelta(hours=4)).isoformat()
    set_id = conn.execute(
        "INSERT INTO ohlcv_slice_sets(event_id,pair,range_start,range_end,expected_rows,"
        "state,created_at,completed_at,rule_version) VALUES(?,?,?,?,?,'complete',?,?,1)",
        (event, "USDJPY", ts, end, 255, ts, ts)).lastrowid
    conn.commit()
    return set_id


def seed(conn, *, currency, importance=3, name="Rate", resolved=0, unresolvable=0,
         country=None, day0=1, change=-2.0, range_pips=10.0):
    """event を (resolved + unresolvable) 件作り、各 event の 3 窓の値を書く。"""
    event_ids = []
    for i in range(resolved + unresolvable):
        ts = (datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
              + timedelta(days=day0 + i)).isoformat()
        event = conn.execute(
            "INSERT INTO econ_events(ts,country,name,importance) VALUES(?,?,?,?)",
            (ts, country or currency, name, importance)).lastrowid
        set_id = _slice_set(conn, event, ts)
        if i < resolved:
            values = tuple(WindowValue(w, "resolved", range_pips + i, change, abs(change),
                                       abs(change) - 1.0) for w in (15, 60, 240))
        else:
            values = tuple(WindowValue(w, "unresolvable", None, None, None, None)
                           for w in (15, 60, 240))
        assert rv.write_set_tx(conn, set_id=set_id, values=values,
                               keys=[(event, currency)], now=NOW).committed
        event_ids.append(event)
    return event_ids


def _row(reaction, currency, kind, key, window):
    rows = [r for r in reaction["bundles"] if (r["currency"], r["kind"], r["key"],
                                                r["window_min"]) == (currency, kind, key, window)]
    assert len(rows) == 1
    return rows[0]


def test_reaction_status_lists_every_bundle_with_n_and_values_even_below_the_floor(tmp_path):
    conn = _conn(tmp_path)
    seed(conn, currency="USD", resolved=5, unresolvable=2)
    reaction = esr.reaction_status(conn, pair_currencies=PAIRS)
    row = _row(reaction, "USD", "importance", "3", 240)
    assert (row["n"], row["unique_ts_n"], row["unresolvable_n"]) == (5, 5, 2)
    assert row["range_median"] == 12.0 and row["range_p75"] == 13.0
    assert row["abs_median"] == 2.0 and row["net_median"] == 1.0
    assert row["close_change_median"] == -2.0
    assert reaction["floor"] == 20
    name_row = _row(reaction, "USD", "name", "Rate", 15)
    assert name_row["n"] == 5


def test_reaction_status_orders_importance_bundles_before_name_bundles(tmp_path):
    conn = _conn(tmp_path)
    seed(conn, currency="USD", name="Alpha", resolved=1)
    seed(conn, currency="JPY", name="Beta", importance=2, resolved=1, day0=50)
    kinds = [r["kind"] for r in esr.reaction_status(conn, pair_currencies=PAIRS)["bundles"]]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "importance" else 1)
    assert kinds[0] == "importance" and kinds[-1] == "name"


def test_reaction_status_counts_unique_timestamps_apart_from_n(tmp_path):
    conn = _conn(tmp_path)
    ts = "2026-09-10T12:00:00+00:00"
    for name in ("A", "B", "C"):
        event = conn.execute("INSERT INTO econ_events(ts,country,name,importance) VALUES(?,?,?,3)",
                             (ts, "USD", name)).lastrowid
        set_id = _slice_set(conn, event, ts)
        values = tuple(WindowValue(w, "resolved", 5.0, 1.0, 1.0, 0.0) for w in (15, 60, 240))
        rv.write_set_tx(conn, set_id=set_id, values=values, keys=[(event, "USD")], now=NOW)
    row = _row(esr.reaction_status(conn, pair_currencies=PAIRS), "USD", "importance", "3", 15)
    assert (row["n"], row["unique_ts_n"]) == (3, 1)


def test_reaction_status_reports_the_backlog_counts(tmp_path):
    conn = _conn(tmp_path)
    seed(conn, currency="USD", resolved=1)
    ts = "2026-09-20T12:00:00+00:00"
    event = conn.execute("INSERT INTO econ_events(ts,country,name,importance) VALUES(?,?,?,3)",
                         (ts, "CHF", "Rate")).lastrowid
    _slice_set(conn, event, ts)
    backlog = esr.reaction_status(conn, pair_currencies=PAIRS)["backlog"]
    assert backlog == {"sets_without_values": 1, "events_without_currency_key": 1}


def test_daily_reaction_report_projects_untrusted_name_keys_to_one_markdown_line():
    reaction = {"bundles": [{"currency": "USD", "kind": "name",
        "key": "Rate\n|*_[bad]`<x>", "window_min": 15, "n": 1,
        "unique_ts_n": 1, "unresolvable_n": 0,
        "range_median": None, "range_p75": None, "abs_median": None,
        "abs_p75": None, "net_median": None, "net_p75": None,
        "close_change_median": None}], "backlog": {}}
    lines = esr._reaction_report_lines(reaction)
    assert len(lines) == 2
    assert "\n" not in lines[0] and "|" not in lines[0]
    assert "*" not in lines[0] and "[" not in lines[0] and "]" not in lines[0]


def test_status_carries_the_reaction_section_only_when_pairs_are_given(tmp_path):
    conn = _conn(tmp_path)
    seed(conn, currency="USD", resolved=1)
    with_pairs = esr.econ_signal_status(conn, now=NOW, capacity=None, worker=None,
                                        pair_currencies=PAIRS)
    assert with_pairs["reaction"]["bundles"]
    without = esr.econ_signal_status(conn, now=NOW, capacity=None, worker=None)
    assert "reaction" not in without


def test_a_broken_reaction_aggregate_makes_only_that_section_unavailable(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("DROP TABLE econ_reaction_values")
    out = esr.econ_signal_status(conn, now=NOW, capacity=None, worker=None,
                                 pair_currencies=PAIRS)
    assert out["reaction"] is None
    assert out["abandoned"] is not None and out["decisions"] is not None


def _lines(reaction):
    econ = {"slice_worker": "alive", "decisions": None, "abandoned": None,
            "missions_latest_day": None, "shadow": None, "reaction": reaction}
    return esr.format_econ_lines(econ)


def test_ctl_lines_summarise_reactions_in_one_line_without_the_table(tmp_path):
    conn = _conn(tmp_path)
    seed(conn, currency="USD", resolved=25)
    seed(conn, currency="JPY", resolved=3, day0=100)
    reaction = esr.reaction_status(conn, pair_currencies=PAIRS)
    assert reaction["bundles"]
    lines = [line for line in _lines(reaction) if "reaction" in line]
    assert len(lines) == 1
    bundles = len(reaction["bundles"])
    above = sum(1 for r in reaction["bundles"] if r["n"] >= 20)
    assert lines[0] == f"  reaction: 束 {bundles} (床 20 以上 {above})、未算定 0"
    assert above == 6
    assert not any("Rate" in line for line in _lines(reaction))


def test_ctl_lines_show_unavailable_for_a_broken_reaction_and_nothing_when_absent():
    assert "  reaction: unavailable" in _lines(None)
    econ = {"slice_worker": "alive", "decisions": None, "abandoned": None,
            "missions_latest_day": None, "shadow": None}
    assert not any("reaction" in line for line in esr.format_econ_lines(econ))


def test_the_daily_report_has_a_reaction_section_with_n_and_values(tmp_path):
    conn = _conn(tmp_path)
    seed(conn, currency="USD", resolved=5, unresolvable=1)
    facts = esr.collect_report_facts(conn, now=NOW, capacity=None, worker=None,
                                     pair_currencies=PAIRS)
    text = esr.render_report(facts, label=date(2026, 10, 30), close_at=NOW)
    assert "## reaction" in text
    assert ("- USD importance=3 240m: n=5 unique_ts_n=5 unresolvable_n=1 "
            "range=12.0/13.0 abs=2.0/2.0 net=1.0/1.0 close_change=-2.0") in text
    assert "- backlog: sets_without_values=0 events_without_currency_key=0" in text


def test_the_daily_report_marks_a_broken_reaction_section_unavailable(tmp_path):
    text = esr.render_report({"reaction": None}, label=date(2026, 10, 30), close_at=NOW)
    assert "## reaction (pips、窓は分、床未満も数値を出す)\nunavailable" in text


def test_the_daily_report_omits_the_section_when_no_pairs_were_given(tmp_path):
    conn = _conn(tmp_path)
    facts = esr.collect_report_facts(conn, now=NOW, capacity=None, worker=None)
    assert "## reaction" not in esr.render_report(facts, label=date(2026, 10, 30), close_at=NOW)


def test_the_report_callback_passes_the_pairs_to_the_report(tmp_path):
    conn_path = tmp_path / "cb.db"
    conn = connect(conn_path)
    init_db(conn)
    seed(conn, currency="USD", resolved=2)
    conn.close()
    reports = tmp_path / "econ_signal"
    callback = esr.make_daily_report_callback(
        reports_dir=reports, open_conn=lambda: connect(conn_path), capacity=None,
        worker=None, pair_currencies=PAIRS)
    callback(NOW)
    assert "## reaction" in next(reports.glob("*.md")).read_text(encoding="utf-8")


def test_importance3_resolved_counts_are_reproduced_from_a_synthetic_set(tmp_path):
    """人間面の n は 4h まで揃った event だけ。USD 38 件中 22 件・JPY 12 件中 9 件を人工に組む。"""
    conn = _conn(tmp_path)
    seed(conn, currency="USD", resolved=22, unresolvable=16)
    seed(conn, currency="JPY", resolved=9, unresolvable=3, day0=200)
    reaction = esr.reaction_status(conn, pair_currencies=PAIRS)
    usd = _row(reaction, "USD", "importance", "3", 240)
    jpy = _row(reaction, "JPY", "importance", "3", 240)
    assert (usd["n"], usd["unresolvable_n"]) == (22, 16)
    assert (jpy["n"], jpy["unresolvable_n"]) == (9, 3)


def test_the_service_status_source_passes_the_pairs_into_the_status(tmp_path):
    from types import SimpleNamespace

    from agentic_fx import service
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)
    seed(conn, currency="USD", resolved=2)
    conn.close()
    out = service._compute_econ_signal(root, SimpleNamespace(now=lambda: NOW), None, None,
                                       PAIRS)
    assert out["reaction"]["bundles"]
