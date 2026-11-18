"""カードと重複する・平常時は読み流す行を出さない。異常・不明・欠損のときは必ず出す。"""
from __future__ import annotations

import re

import pytest

from afx_ops.api_client import Reply
from afx_ops.web.request import Request, handle_request
from tests.web import test_econ_status as econ_support
from tests.web import test_improve_pages as improve_support
from tests.web import test_plugin_pages as plugin_support


class Client:
    def __init__(self, status=None, approvals=None):
        self._status, self._approvals = status, approvals

    def status(self):
        return Reply(200, self._status, None)

    def approvals(self, *, limit):
        return Reply(200, self._approvals, None)


def _overview(status) -> str:
    page = handle_request(Request("GET", "/", {}), Client(status)).body.decode()
    return page.split("</nav>")[-1]


def _dts(page: str) -> list[str]:
    return re.findall(r"<dt[^>]*>([^<]*)</dt>", page)


NORMAL = {"mode": "paper", "equity": 10000.5, "balance": 9990, "autopilot": False,
          "kill_switch": {"generation": 3, "latched": False, "reconcile_required": False},
          "health": {"latched": False, "failures": 0}, "data": {"state": "ready"},
          "orders": {"active": 0}}


# --- Status 概要 -----------------------------------------------------------------------

def test_normal_values_that_the_cards_show_are_not_repeated_in_the_dl():
    page = _overview(NORMAL)
    assert 'class="kpi"' in page
    keys = _dts(page)
    for key in ("kill_switch", "health", "autopilot", "mode", "equity", "balance"):
        assert key not in keys, key
    assert "<h2>資金</h2>" not in page and "<h2>安全</h2>" not in page
    assert "data" in keys and "orders" in keys


@pytest.mark.parametrize(("key", "value"), [
    ("kill_switch", {"latched": True, "generation": 2}),
    ("kill_switch", {"latched": False, "reconcile_required": True, "generation": 2}),
    ("kill_switch", {"latched": False}),
    ("kill_switch", {"latched": 0, "reconcile_required": False}),
    ("kill_switch", "x"), ("kill_switch", True),
    ("health", {"latched": False, "failures": 2}),
    ("health", {"latched": True, "failures": 0}),
    ("health", {"latched": False}),
    ("health", {"latched": False, "failures": True}),
    ("health", {"latched": "false", "failures": 0}),
    ("autopilot", 0), ("autopilot", "false"),
])
def test_an_abnormal_unknown_or_not_card_shaped_value_keeps_its_dl_row(key, value):
    status = dict(NORMAL)
    status[key] = value
    shown = _overview(status)
    assert re.search(rf"<dt[^>]*>{key}</dt>", shown), (key, value)


def test_a_latched_kill_switch_still_shows_its_row_with_the_danger_class():
    page = _overview(dict(NORMAL, kill_switch={"latched": True, "generation": 2}))
    assert '<dt class="danger">kill_switch</dt><dd class="danger">' in page


def test_jobs_pnl_and_performance_are_not_in_the_status_allowlist():
    page = _overview(dict(NORMAL, jobs=[{"id": 1}], pnl=5, performance={"pf": 1}))
    for key in ("jobs", "pnl", "performance"):
        assert key not in _dts(page)
    assert "mission" not in _dts(page)


def test_mission_orders_and_data_keep_their_rows():
    page = _overview(dict(NORMAL, mission={"loop": "decision", "status": "done"}))
    assert {"mission", "orders", "data"} <= set(_dts(page))


# --- Status 経済指標 -------------------------------------------------------------------

def _econ_cells(status) -> dict[str, str]:
    page = handle_request(Request("GET", "/", {"tab": "econ"}), Client(status)).body.decode()
    return dict(re.findall(r"<dt>([^<]*)</dt><dd>([^<]*)</dd>", page))


def _status(econ):
    return {"mode": "paper", "econ_signal": econ}


def test_abandoned_rows_that_are_zero_everywhere_are_left_out():
    econ = econ_support._econ()
    zero = [r for r in econ_support.REASONS if r not in ("freshness_expired", "retry_exhausted")]
    cells = _econ_cells(_status(econ))
    for reason in zero:
        assert f"abandoned {reason} (latest day / total)" not in cells, reason
    assert cells["abandoned freshness_expired (latest day / total)"] == "1 / 0"
    assert cells["abandoned retry_exhausted (latest day / total)"] == "0 / 4"
    assert cells["abandoned ineligible_cleanup (latest day / total)"] == "1 / 2"
    assert cells["abandoned legacy_unknown (total)"] == "9"


def test_zero_cleanup_and_legacy_rows_are_left_out_too():
    econ = econ_support._econ()
    econ["abandoned"]["cleanup"] = {"total": 0, "latest_day": 0}
    econ["abandoned"]["legacy_unknown_total"] = 0
    cells = _econ_cells(_status(econ))
    assert "abandoned ineligible_cleanup (latest day / total)" not in cells
    assert "abandoned legacy_unknown (total)" not in cells


@pytest.mark.parametrize("bad", [None, "0", False, True, -1, 0.0, [], {}])
def test_a_missing_or_odd_count_keeps_the_abandoned_row(bad):
    econ = econ_support._econ()
    econ["abandoned"]["total"]["source_inactive"] = bad
    econ["abandoned"]["latest_day"]["source_inactive"] = 0
    cells = _econ_cells(_status(econ))
    assert "abandoned source_inactive (latest day / total)" in cells


def test_a_missing_abandoned_key_keeps_every_row_and_the_unavailable_word():
    econ = econ_support._econ()
    econ["abandoned"]["total"] = {}
    econ["abandoned"]["latest_day"] = {}
    cells = _econ_cells(_status(econ))
    for reason in econ_support.REASONS:
        assert cells[f"abandoned {reason} (latest day / total)"] == "- / -"
    econ["abandoned"] = None
    assert _econ_cells(_status(econ))["abandoned"] == "unavailable"


COMPUTATION_ROWS = ("base_bytes", "bytes_per_row", "db_bytes", "wal_bytes")


def test_the_four_computation_rows_of_the_slice_capacity_are_left_out():
    cells = _econ_cells(_status(econ_support._econ()))
    for key in COMPUTATION_ROWS:
        assert f"slice {key}" not in cells, key


def test_every_other_slice_row_stays():
    cells = _econ_cells(_status(econ_support._econ()))
    for key in ("signal_daily", "signal_producer", "signal_override", "rows", "row_capacity",
                "row_level", "slice_bytes", "slice_bytes_measured_at", "used_bytes",
                "byte_capacity", "byte_level", "free_bytes", "level", "measure_outcome",
                "measure_elapsed_sec", "slice_bytes_stale",
                "capacity_override_below_producer"):
        assert f"slice {key}" in cells, key
    assert "slice capacity warning" in cells
    assert len([k for k in cells if k.startswith("slice ")]) == 18


def test_a_slice_that_was_not_measured_and_a_stale_one_still_say_so():
    econ = econ_support._econ()
    econ["slice_capacity"]["slice_bytes"] = None
    econ["slice_capacity"]["slice_bytes_stale"] = True
    cells = _econ_cells(_status(econ))
    assert cells["slice slice_bytes"] == "未測定" and cells["slice slice_bytes_stale"] == "古い"


# --- 承認待ち一覧 ----------------------------------------------------------------------

def _approvals(items, journals=None) -> str:
    data = {"approvals": items}
    if journals is not None:
        data["open_journals"] = journals
    return handle_request(Request("GET", "/approvals", {}), Client(approvals=data)).body.decode()


ITEM = {"id": 7, "kind": "plugin", "name": "rsi", "created_at": "2026-11-18T01:02:03+00:00",
        "content_hash": "0123456789abcdef" * 4}


def test_pending_approvals_are_one_table_with_a_twelve_character_hash_and_a_link():
    page = _approvals([ITEM, dict(ITEM, id=8, name="mom")])
    assert page.count("<table>") == 1
    assert re.findall(r'<a href="(/approvals/\d+)">', page) == ["/approvals/7", "/approvals/8"]
    assert "0123456789ab" in page and "0123456789abc" not in page
    assert "<dt>content_hash</dt>" not in page
    heads = re.findall(r"<th[^>]*>([^<]*)</th>", page)
    assert heads == ["id", "kind", "name", "created_at", "content_hash"]
    assert "2026-11-18 10:02:03 +09:00" in page or "2026-11-18 01:02:03 +00:00" in page


def test_a_missing_or_odd_field_is_shown_as_a_dash_and_hostile_text_is_escaped():
    odd = {"id": 'x"><script>', "kind": None, "name": "<b>n</b>", "content_hash": 5}
    page = _approvals([odd])
    assert "<script>" not in page and "<b>" not in page and "&lt;b&gt;n&lt;/b&gt;" in page
    assert re.findall(r'<a href="/approvals/', page) == []
    cells = re.findall(r"<td>(.*?)</td>", page)
    assert len(cells) == 5 and cells[1] == "-" and cells[3] == "-"


def test_no_pending_approval_means_no_table():
    assert "<table>" not in _approvals([])


def test_open_journals_still_show_after_the_table():
    page = _approvals([ITEM], journals=[{"approval_id": 7, "name": "rsi", "phase": "pending"}])
    assert page.index("<table>") < page.index("<dt>approval_id</dt>")


# --- 重複する link と説明 ---------------------------------------------------------------

def test_the_plugin_list_has_no_link_paragraph_to_the_approvals_but_the_band_still_has_it():
    html = plugin_support.page("/plugins")
    assert "承認待ちの一覧 (Pending approvals)" not in html
    assert re.search(r'<nav class="tabs">.*?<a href="/approvals">承認待ち</a>', html, re.S)


def test_the_detail_pages_have_no_link_paragraph_above_the_band():
    html = plugin_support.page("/plugins/rsi")
    assert '<p><a href="/plugins">Plugins</a> / <a href="/approvals">Pending approvals</a></p>' not in html
    assert '<nav><a href="/" ' in html or '<a href="/plugins"' in html
    run = improve_support.page("/improve/7", improve_support.Client(detail=improve_support.dto("submitted")))
    assert '<p><a href="/improve">Improve (一覧)</a> / <a href="/plugins">Plugins</a></p>' not in run
    assert '<a href="/improve"' in run


def test_the_improve_list_has_no_unsorted_explanation_but_a_sorted_one_stays():
    assert "新しい順に並べています" not in improve_support.page("/improve")
    sorted_page = improve_support.page("/improve", None, {"sort": "started"})
    assert "で並べています" in sorted_page


# --- 承認待ちの表の hash と列の定義 ------------------------------------------------------

@pytest.mark.parametrize(("value", "shown"), [
    ("0123456789abcdef" * 4, "0123456789ab"), ("abc", "-"), ("0123456789abcdef" * 3 + "0123456789abcdeg", "-"),
    ("0123456789ABCDEF" * 4, "-"), ("0123456789abcdef" * 4 + "0", "-"), (5, "-"), (None, "-"), ([], "-")])
def test_the_pending_hash_is_cut_only_when_it_is_a_full_hash_otherwise_a_dash(value, shown):
    page = _approvals([dict(ITEM, content_hash=value)])
    assert re.findall(r"<td>(.*?)</td>", page)[4] == shown


def test_the_table_columns_are_the_allowlist_and_a_field_outside_it_is_not_shown():
    from afx_ops.web import render
    page = _approvals([dict(ITEM, zz_new_field="LEAK")])
    assert re.findall(r"<th>(.*?)</th>", page) == list(render._APPROVAL_COLUMNS)
    assert set(render._APPROVAL_COLUMNS) == render.APPROVAL_FIELDS
    assert "LEAK" not in page


def test_headings_and_values_stay_aligned_when_the_columns_are_reordered_or_reduced(monkeypatch):
    from afx_ops.web import render
    expected = {"id": '<a href="/approvals/7">#7</a>', "kind": "plugin", "name": "rsi",
                "created_at": "2026-11-18 10:02:03 +09:00", "content_hash": "0123456789ab"}
    for columns in (("content_hash", "name", "id", "created_at", "kind"),
                    ("name", "id"), ("created_at",)):
        monkeypatch.setattr(render, "_APPROVAL_COLUMNS", columns)
        page = handle_request(Request("GET", "/approvals", {}), Client(approvals={
            "approvals": [ITEM], "display_timezone": "Asia/Tokyo"})).body.decode()
        assert re.findall(r"<th>(.*?)</th>", page) == list(columns)
        assert re.findall(r"<td>(.*?)</td>", page) == [expected[name] for name in columns]
