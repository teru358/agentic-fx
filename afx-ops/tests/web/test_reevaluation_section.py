"""web の承認ページ: 再評価の依頼は前回 → 今回を pair ごとに並べる。通常の承認と古い daemon は変えない。"""
from __future__ import annotations

import html

import pytest

from afx_ops import display_time, display_zone
from afx_ops.web import render
from tests.gate.test_reevaluation_display import CHANGE, VIEW


@pytest.fixture(autouse=True)
def _fresh_zone_state(monkeypatch):
    monkeypatch.setattr(display_zone, "_warned", False)
    display_time.clear_zone_cache()
    yield
    display_time.clear_zone_cache()


def _page(**fields) -> str:
    data = {"id": 7, "kind": "plugin", "status": "pending", "change": dict(CHANGE),
            "payload_sha256": "a" * 64, "display_timezone": "Asia/Tokyo",
            "candidate_origin": "reevaluation", "reevaluation": VIEW, "deploy_phase": None}
    data.update(fields)
    return html.unescape(render.render_approval(data).decode())


def test_the_page_headlines_the_reevaluation_and_says_the_code_is_unchanged():
    text = _page()
    assert "<h2>再評価 (前回の承認 #3)</h2>" in text
    assert "コード変更なし" in text


def test_the_page_lists_previous_and_current_for_every_pair_and_scope():
    text = _page()
    assert "USDJPY in_sample" in text and "USDJPY holdout" in text and "EURUSD holdout" in text
    assert "pf=1.5 trades=30 avg_r=0.2 max_drawdown=0.05 (承認時の記録)" in text
    assert "(run #55)" in text
    assert "EURUSD holdout</td><td>前回 不明 → 今回 -" in text


def test_the_page_shows_the_evaluation_times_in_the_zone_of_the_response():
    text = _page()
    assert "今回 2026-11-08 12:17:00 +09:00" in text
    assert "前回 2026-10-01 09:00:00 +09:00" in text
    assert "期間が違う比較です" in text


def test_the_page_warns_that_rejecting_stops_the_deployment():
    assert "却下すると strategy sma_cross の配備を停止します (position は保護を続けます)" in _page()


@pytest.mark.parametrize("phase,expected", [
    ("done", "却下し、配備を停止しました"),
    ("needs_retry", "symlink を外せていません"),
    ("foreign", "plugins/sma_cross が想定と違うため"),
])
def test_a_rejected_request_shows_how_far_the_stop_went(phase, expected):
    assert expected in _page(status="rejected", deploy_phase=phase)


def test_an_ordinary_approval_page_has_no_reevaluation_section():
    text = _page(candidate_origin=None, reevaluation=None)
    assert "再評価" not in text and "前回" not in text


def test_a_response_from_an_old_daemon_renders_without_the_section():
    data = {"id": 7, "kind": "plugin", "status": "pending", "change": dict(CHANGE),
            "payload_sha256": "a" * 64}
    assert b"<h2>" in render.render_approval(data)
    assert "前回" not in html.unescape(render.render_approval(data).decode())


@pytest.mark.parametrize("bad", [None, "x", 5, [], {"rows": "x"}, {"rows": [None, 3]},
                                 {"name": "<b>", "rows": [{"pair": "<i>", "scope": "holdout"}]}])
def test_a_malformed_view_does_not_break_the_page_or_inject_markup(bad):
    page = render.render_approval({"id": 7, "kind": "plugin", "status": "pending",
                                   "change": dict(CHANGE), "payload_sha256": "a" * 64,
                                   "candidate_origin": "reevaluation", "reevaluation": bad}).decode()
    assert "<b>" not in page and "<i>" not in page and "再評価" in page
