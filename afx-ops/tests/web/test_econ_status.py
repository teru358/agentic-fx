from __future__ import annotations

import re

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import render
from afx_ops.web.request import Request, handle_request

REASONS = ("freshness_expired", "requeued_expired", "retry_exhausted", "source_inactive",
           "normalization_failed", "deploy_op", "deploy_linked")


class Client:
    def __init__(self, status):
        self._status = status

    def status(self):
        return Reply(200, self._status, None)


def _econ(**over):
    econ = {
        "slice_worker": "alive",
        "decisions": {"total": 5, "by_kind": {"accept": 3, "reject:system:econ_window": 2}},
        "abandoned": {"total": {r: 0 for r in REASONS} | {"retry_exhausted": 4},
                      "latest_day": {r: 0 for r in REASONS} | {"freshness_expired": 1},
                      "latest_day_start": "2026-11-05T22:00:00+00:00",
                      "cleanup": {"total": 2, "latest_day": 1}, "legacy_unknown_total": 9},
        "missions_latest_day": {"system_filtered": 2, "failed": 1, "running": 0},
        "shadow": [{"decision": "accept", "actor": "system", "reject_code": None,
                    "state": "resolved", "result_code": "tp", "n": 3,
                    "avg_pnl_pips": 1.5}],
        "slice_capacity": {
            "signal_daily": 96, "signal_producer": 96, "signal_override": 120,
            "capacity_override_below_producer": True, "rows": 80, "row_capacity": 100,
            "row_level": "warn", "slice_bytes": 7000,
            "slice_bytes_measured_at": "2026-11-06T01:02:03+00:00",
            "used_bytes": 400000, "byte_capacity": 10000000, "byte_level": "ok",
            "base_bytes": 348160, "bytes_per_row": 177, "level": "warn",
            "db_bytes": 11, "wal_bytes": 12, "free_bytes": 13}}
    econ.update(over)
    return econ


def _page(status):
    return handle_request(Request("GET", "/", {}), Client(status)).body.decode()


def _cells(page):
    return dict(re.findall(r"<dt>([^<]*)</dt><dd>([^<]*)</dd>", page))


def test_status_page_shows_every_econ_item_with_its_value():
    cells = _cells(_page({"mode": "paper", "econ_signal": _econ()}))
    assert cells["worker"] == "alive"
    assert cells["decisions"] == "5"
    assert cells["decision accept"] == "3"
    assert cells["decision reject:system:econ_window"] == "2"
    assert cells["abandoned freshness_expired (latest day / total)"] == "1 / 0"
    assert cells["abandoned retry_exhausted (latest day / total)"] == "0 / 4"
    assert cells["abandoned ineligible_cleanup (latest day / total)"] == "1 / 2"
    assert cells["abandoned legacy_unknown (total)"] == "9"
    assert cells["missions latest day system_filtered"] == "2 (filtered, not failed)"
    assert cells["missions latest day failed"] == "1"
    assert cells["shadow accept/system/-/resolved/tp"] == "n=3 avg_pnl_pips=1.5"
    for key, value in (("signal_daily", "96"), ("signal_producer", "96"),
                       ("signal_override", "120"), ("rows", "80"),
                       ("row_capacity", "100"), ("row_level", "warn"),
                       ("slice_bytes", "7000"), ("used_bytes", "400000"),
                       ("byte_level", "ok"), ("base_bytes", "348160"),
                       ("bytes_per_row", "177"), ("db_bytes", "11"), ("wal_bytes", "12"),
                       ("free_bytes", "13"), ("level", "warn"),
                       ("slice_bytes_measured_at", "2026-11-06T01:02:03+00:00"),
                       ("capacity_override_below_producer", "True")):
        assert cells[f"slice {key}"] == value, key
    assert cells["slice capacity warning"] == "warn"


def test_status_page_says_unmeasured_bytes():
    capacity = dict(_econ()["slice_capacity"], slice_bytes=None, used_bytes=None,
                    byte_level=None, slice_bytes_measured_at=None)
    cells = _cells(_page({"econ_signal": _econ(slice_capacity=capacity)}))
    assert cells["slice slice_bytes"] == "未測定"


def test_status_page_omits_fields_outside_the_allowlist():
    econ = _econ()
    econ["secret"] = "leak-secret"
    econ["slice_capacity"]["note"] = "leak-note"
    econ["abandoned"]["extra_reason"] = {"x": 1}
    econ["abandoned"]["total"]["invented"] = 99
    page = _page({"econ_signal": econ, "token": "leak-token",
                  "window": {"freshness": "fresh", "pairs": {"USDJPY": {"in_window": True}},
                             "secret": "leak-window"}})
    for leak in ("leak-secret", "leak-note", "extra_reason", "invented", "leak-token",
                 "leak-window", "USDJPY"):
        assert leak not in page, leak


def test_status_page_escapes_hostile_values_and_unknown_words():
    econ = _econ(slice_worker="<script>x</script>")
    econ["decisions"]["by_kind"]["<b>"] = 1
    econ["shadow"][0]["actor"] = "<i>"
    econ["slice_capacity"]["level"] = "<u>"
    page = _page({"econ_signal": econ})
    assert "<script>" not in page and "<b>" not in page
    assert "<i>" not in page and "<u>" not in page
    assert _cells(page)["worker"] == "-"


@pytest.mark.parametrize("freshness,word", [
    ("unknown", "unknown"), ("fresh", "fresh"), ("stale", "stale"),
    ("<x>", "unknown"), (None, "unknown")])
def test_status_page_shows_the_calendar_state(freshness, word):
    cells = _cells(_page({"window": {"in_window": False, "freshness": freshness}}))
    assert cells["calendar"] == word
    assert cells["in_window"] == "False"


def test_status_page_without_econ_or_window_has_no_such_section():
    page = _page({"mode": "paper"})
    assert "Economic" not in page


def test_unavailable_econ_sections_are_marked():
    cells = _cells(_page({"econ_signal": {
        "slice_worker": "stopped", "decisions": None, "abandoned": None,
        "slice_capacity": None}}))
    assert cells["decisions"] == "unavailable"
    assert cells["abandoned"] == "unavailable"
    assert cells["slice capacity"] == "unavailable"
    assert cells["worker"] == "stopped"


def test_the_copied_abandon_reasons_match_the_daemon_definition():
    # 値の正本は agentic_fx.loops.econ_signal_report.ABANDON_REASONS (別 package なので写し)
    assert render.ECON_ABANDON_REASONS == REASONS
