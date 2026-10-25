import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx import status as status_module
from agentic_fx.ops.client import format_status
from agentic_fx.setup.pending_facts import collect_pending_facts
from agentic_fx.setup.pending_steps import MAX_PENDING_STEPS, PendingFacts, pending_steps
from agentic_fx.setup.pending_text import STEP_PARAMS, PendingStep, render_pending_step
from agentic_fx.store import news_health, news_sources
from agentic_fx.store.db import connect, connect_observer_readonly, init_db
from agentic_fx.store.setup_record import write_setup_record, SetupRecord
from tests.status_support import setup_data

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


class Clock:
    def now(self):
        return NOW


def _extra(count):
    return [PendingStep(f"filler_{i}", "info", NOW) for i in range(count)]


def _news(steps):
    return [(s.code, dict(s.params)) for s in steps if s.code.startswith("news_source")]


def _names(*items):
    return tuple(items)


# ---- ordering, folding, limits ---------------------------------------------

BASE = len(pending_steps(PendingFacts(record_state="ok")))


def _facts(existing_total, names):
    """Facts whose non-news steps number exactly `existing_total` (the built-ins count too)."""
    return PendingFacts(record_state="ok", extra_steps=_extra(max(0, existing_total - BASE)),
                        failing_news_sources=names)


def test_news_rows_follow_every_other_step_and_list_five_by_name():
    facts = PendingFacts(record_state="ok", extra_steps=_extra(2),
                         failing_news_sources=_names("z", "a", "b", "c", "d", "e", "f"))
    steps = pending_steps(facts)
    codes = [s.code for s in steps]
    first_news = min(i for i, c in enumerate(codes) if c.startswith("news_source"))
    assert codes[first_news - 2:first_news] == ["filler_0", "filler_1"]
    assert all(c.startswith("news_source") for c in codes[first_news:])
    assert _news(steps) == [("news_source_failing", {"name": n}) for n in "abcde"] \
        + [("news_source_failing_more", {"count": 2})]
    assert all(s.severity == "warn" for s in steps if s.code.startswith("news_source"))


def test_names_outside_the_ident_grammar_fold_into_the_summary_only():
    facts = PendingFacts(record_state="ok", failing_news_sources=_names("Upper", "x" * 65, "ok-one", "a b"))
    assert _news(pending_steps(facts)) == [("news_source_failing", {"name": "ok-one"}),
                                           ("news_source_failing_more", {"count": 3})]


def test_five_or_fewer_valid_names_have_no_summary():
    facts = PendingFacts(record_state="ok", failing_news_sources=_names("e", "d", "c", "b", "a"))
    assert _news(pending_steps(facts)) == [("news_source_failing", {"name": n}) for n in "abcde"]
    assert _news(pending_steps(PendingFacts(record_state="ok"))) == []


@pytest.mark.parametrize("existing,failing,listed,more", [
    (26, 6, 5, 1),    # exactly fills 32
    (27, 6, 4, 2),
    (30, 6, 1, 5),
    (31, 6, 0, 6),    # the last slot becomes the summary, counted over what is not shown
    (31, 1, 1, None),
    (31, 2, 0, 2),
    (32, 6, 0, None),
    (32, 1, 0, None),
    (33, 3, 0, None),
])
def test_global_limit_boundaries(existing, failing, listed, more):
    names = tuple(f"s{i}" for i in range(failing))
    steps = pending_steps(_facts(existing, names))
    assert len(steps) == min(MAX_PENDING_STEPS, max(existing, BASE) + listed + (more is not None))
    assert len(steps) <= MAX_PENDING_STEPS
    assert len([s for s in steps if s.code == "news_source_failing"]) == listed
    summary = [s.params["count"] for s in steps if s.code == "news_source_failing_more"]
    assert summary == ([] if more is None else [more])
    assert sum([listed] + summary) == (failing if (listed or summary) else 0)


def test_full_existing_list_hides_all_news_rows():
    steps = pending_steps(PendingFacts(record_state="ok", extra_steps=_extra(40),
                                       failing_news_sources=_names("a")))
    assert _news(steps) == [] and len(steps) == MAX_PENDING_STEPS


def test_hidden_status_only_steps_do_not_consume_the_news_slots():
    hidden = [PendingStep(f"hidden_{i}", "info", NOW, visible_in_status=False) for i in range(32)]
    facts = PendingFacts(record_state="ok", extra_steps=hidden, failing_news_sources=_names("a", "b"))
    assert len(_news(pending_steps(facts, for_status=True))) == 2


def test_missing_setup_record_shows_no_news_row():
    steps = pending_steps(PendingFacts(record_state="missing", failing_news_sources=_names("a")))
    assert [s.code for s in steps] == ["setup_record_missing"]


# ---- wording ---------------------------------------------------------------

def test_wording_is_fixed_and_sanitized():
    one = render_pending_step(PendingStep("news_source_failing", "warn", NOW, {"name": "nhk-keizai"}))
    assert one == "源 nhk-keizai が連続で失敗しています。`afx news list` で確認し、不要なら `afx news disable nhk-keizai` で止めてください"
    more = render_pending_step(PendingStep("news_source_failing_more", "warn", NOW, {"count": 3}))
    assert more == "ほか 3 件の源も連続で失敗しています。`afx news list` で確認してください"
    odd = render_pending_step(PendingStep("news_source_failing", "warn", NOW, {"name": "https://x.invalid/?k=1"}))
    assert "x.invalid" not in odd and "源 unknown が" in odd
    assert "ほか 0 件" in render_pending_step(PendingStep("news_source_failing_more", "warn", NOW, {"count": -1}))
    assert STEP_PARAMS["news_source_failing"] == {"name": ("ident", "unknown")}
    assert STEP_PARAMS["news_source_failing_more"] == {"count": ("int", 0)}


# ---- facts from the database -----------------------------------------------

def _db(tmp_path, specs):
    """specs: name -> (enabled, Health | None)."""
    path = tmp_path / "data.db"
    conn = connect(path)
    init_db(conn)
    for i, (name, (enabled, health)) in enumerate(specs.items()):
        news_sources.add(conn, name=name, fetcher="feed", url=f"https://example.invalid/{i}",
                         added_by="user", now=NOW, enabled=enabled)
        if health is not None:
            assert news_health.write_if_unchanged(conn, name, None, health, NOW)
    return path, conn


def _failing(conn, root=None):
    facts = collect_pending_facts(root or Path("/nonexistent-root"), None, conn, deployed_plugins=(),
                                  outage=None, clock=Clock(), landlock_available=True, landlock_abi=8)
    return facts.failing_news_sources


def test_threshold_applies_to_either_counter_and_only_enabled_sources(tmp_path):
    H = news_health.Health
    path, conn = _db(tmp_path, {
        "f2": (True, H(consecutive_failures=2)),
        "f3": (True, H(consecutive_failures=3)),
        "s2": (True, H(retry_after_streak=2)),
        "s3": (True, H(retry_after_streak=3)),
        "off": (False, H(consecutive_failures=9)),
        "fresh": (True, None),
        "ok": (True, H(last_outcome="ok", last_count=2)),
        "enabled-again": (True, H(reset_at=NOW, reset_nonce="n")),
        "corrupt": (True, None),
    })
    conn.execute("INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?)",
                 (news_health.PREFIX + "corrupt", "junk", NOW.isoformat()))
    conn.commit()
    assert sorted(_failing(conn)) == ["f3", "s3"]


def test_unreadable_database_gives_no_news_facts_and_keeps_the_rest(tmp_path):
    path, conn = _db(tmp_path, {"f3": (True, news_health.Health(consecutive_failures=3))})
    assert _failing(None) == ()
    conn.execute("DROP TABLE news_sources")
    conn.commit()
    facts = collect_pending_facts(tmp_path, None, conn, deployed_plugins=(), outage=None, clock=Clock(),
                                  landlock_available=True, landlock_abi=8)
    assert facts.failing_news_sources == ()
    assert facts.record_state in {"missing", "ok", "corrupt"}


def test_failing_sources_clear_after_success_disable_and_enable(tmp_path):
    H = news_health.Health
    path, conn = _db(tmp_path, {"a": (True, H(consecutive_failures=4))})
    assert _failing(conn) == ("a",)
    news_health.write_if_unchanged(conn, "a", news_health.read(conn, "a").raw, news_health.next_record(
        news_health.read(conn, "a").record, "ok", NOW, count=1), NOW)
    assert _failing(conn) == ()
    news_health.write_if_unchanged(conn, "a", news_health.read(conn, "a").raw, H(consecutive_failures=4), NOW)
    news_sources.set_enabled(conn, 1, False)
    assert _failing(conn) == ()
    news_sources.set_enabled(conn, 1, True)
    assert _failing(conn) == ("a",)
    news_health.initialize_for_enable(conn, "a", NOW)
    conn.commit()
    assert _failing(conn) == ()


# ---- both status paths, and the operations client ----------------------------

def _root_with_failures(tmp_path, count):
    from agentic_fx.store.setup_record import write_setup_record
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    write_setup_record(root, SetupRecord(setup_data()))
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)
    for i in range(count):
        name = f"src{i}"
        news_sources.add(conn, name=name, fetcher="feed", url=f"https://example.invalid/{i}",
                         added_by="user", now=NOW, enabled=True)
        news_health.write_if_unchanged(conn, name, None, news_health.Health(consecutive_failures=3), NOW)
    conn.close()
    return root


def test_offline_status_and_ctl_status_report_the_same_failing_sources(tmp_path):
    from agentic_fx.service import _serialize_status_setup
    root = _root_with_failures(tmp_path, 7)
    ro = connect_observer_readonly(root / "data" / "agentic.db")
    try:
        offline, bad = status_module._pending(root, "free", None, ro)
        facts = collect_pending_facts(root, None, ro, deployed_plugins=(), outage=None, clock=Clock(),
                                      landlock_available=True, landlock_abi=8)
    finally:
        ro.close()
    assert bad is False
    offline_news = [t for t in offline if "afx news" in t]
    assert len(offline_news) == 6 and offline_news[-1] == "ほか 2 件の源も連続で失敗しています。`afx news list` で確認してください"
    setup = _serialize_status_setup(facts)
    items = [i for i in setup["pending_steps"] if i["code"].startswith("news_source")]
    assert [(i["code"], i["params"]) for i in items] == [
        ("news_source_failing", {"name": f"src{n}"}) for n in range(5)] + [
        ("news_source_failing_more", {"count": 2})]
    assert all(i["severity"] == "warn" for i in items)
    ctl = format_status({"setup": setup})
    ctl_news = [line[2:] for line in ctl.splitlines() if "afx news" in line]
    assert ctl_news == offline_news


def test_ctl_status_drops_unknown_news_params_but_keeps_the_allowed_ones():
    setup = {"record_state": "ok", "pending_steps": [
        {"code": "news_source_failing", "severity": "warn", "observed_at": "2026-10-25T12:00:00Z",
         "params": {"name": "nhk", "url": "https://x.invalid/?k=1"}},
        {"code": "news_source_failing_more", "severity": "warn", "observed_at": "2026-10-25T12:00:00Z",
         "params": {"count": 4, "extra": "x"}}]}
    text = format_status({"setup": setup})
    assert "源 nhk が連続で失敗しています" in text and "x.invalid" not in text
    assert "ほか 4 件の源も" in text


def test_a_closed_connection_leaves_other_sections_intact(tmp_path):
    root = _root_with_failures(tmp_path, 2)
    ro = connect_observer_readonly(root / "data" / "agentic.db")
    ro.close()
    result, bad = status_module._pending(root, "free", None, ro)
    # whichever way the broader collection fails, no news row appears
    assert not any("afx news" in t for t in (result or ()))
