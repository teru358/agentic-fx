"""再評価の依頼の承認カード・確認文・終端文言。通常の承認と古い daemon の応答は変わらない。"""
from __future__ import annotations

import pytest

from afx_ops import display_time, display_zone
from afx_ops.api_client import Reply
from afx_ops.bot.adapter import BotAdapter
from afx_ops.gate.render import _event_text_for_code, render_approval, render_terminal
from afx_ops.gate.state import ApprovalRecord
from afx_ops.gate.types import GateResult, Interaction, ViewModel, encode_custom_id

RECORD = ApprovalRecord(approval_id=7, message_id=101, guild_id=12345678901234567,
                        channel_id=12345678901234568, payload_sha256="a" * 64,
                        display_generation=1, state="fresh")
CHANGE = {"name": "sma_cross", "kind": "strategy", "classification": "same",
          "candidate_hash": "b" * 64, "baseline_hash": "b" * 64,
          "computed_at": "2026-11-08T03:17:42+00:00", "reason": None,
          "params": {"items": [], "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0,
                     "reason": None},
          "performance": {"in_sample": [{"pair": "USDJPY", "trades": 9, "pf": 1.1, "avg_r": 0.1}],
                          "holdout": []}, "summary": None}
METRICS = {"pf": 1.25, "trades": 40, "avg_r": 0.12, "max_drawdown": 0.08}
VIEW = {"name": "sma_cross", "reevaluates_approval_id": 3,
        "previous_at": "2026-10-01T00:00:00+00:00", "eval_now": "2026-11-08T03:17:00+00:00",
        "rows": [
            {"pair": "USDJPY", "scope": "in_sample",
             "previous": {"metrics": {"pf": 1.5, "trades": 30, "avg_r": 0.2, "max_drawdown": 0.05},
                          "source": "payload", "run_id": None}, "current": METRICS},
            {"pair": "USDJPY", "scope": "holdout",
             "previous": {"metrics": {"pf": 1.1, "trades": 12, "avg_r": 0.1, "max_drawdown": 0.09},
                          "source": "run", "run_id": 55}, "current": METRICS},
            {"pair": "EURUSD", "scope": "holdout",
             "previous": {"metrics": None, "source": "unknown", "run_id": None},
             "current": None}]}


@pytest.fixture(autouse=True)
def _fresh_zone_state(monkeypatch):
    monkeypatch.setattr(display_zone, "_warned", False)
    display_time.clear_zone_cache()
    yield
    display_time.clear_zone_cache()


def _plain(text):
    """Discord の escape (\\) を外した本文。"""
    return text.replace("\\", "")


def _detail(**fields):
    data = {"status": "pending", "change": dict(CHANGE), "expires_at": None,
            "display_timezone": "Asia/Tokyo", "candidate_origin": "reevaluation",
            "reevaluation": VIEW, "deploy_phase": None}
    data.update(fields)
    return data


def test_the_card_heading_says_reevaluation_and_the_code_is_unchanged():
    content = _plain(render_approval(RECORD, _detail()))
    heading = content.split("\n")[0]
    assert "再評価" in heading and "sma_cross" in heading and "同一版" in heading
    assert "コード変更なし" in content


def test_the_card_lists_previous_and_current_per_pair_with_the_source():
    content = _plain(render_approval(RECORD, _detail()))
    assert "pf=1.5 trades=30 avg_r=0.2 max_drawdown=0.05" in content
    assert "承認時の記録" in content and "run #55" in content
    assert "pf=1.25 trades=40 avg_r=0.12 max_drawdown=0.08" in content
    assert "EURUSD holdout: 前回 不明 → 今回 -" in content


def test_the_card_shows_the_evaluation_time_in_the_zone_with_seconds_and_an_offset():
    content = _plain(render_approval(RECORD, _detail()))
    assert "今回 2026-11-08 12:17:00 +09:00" in content
    assert "前回 2026-10-01 09:00:00 +09:00" in content
    assert "期間が違う比較です" in content


def test_the_card_warns_that_rejecting_stops_the_deployment():
    assert "却下すると strategy sma_cross の配備を停止します" in _plain(
        render_approval(RECORD, _detail()))


def test_an_ordinary_approval_card_has_no_reevaluation_text():
    content = render_approval(RECORD, _detail(candidate_origin=None, reevaluation=None))
    assert "再評価" not in content and "前回" not in content


def test_a_response_from_an_old_daemon_without_the_fields_renders_as_before():
    data = _detail()
    for key in ("candidate_origin", "reevaluation", "deploy_phase"):
        del data[key]
    content = render_approval(RECORD, data)
    assert "再評価" not in content and "成績 (in-sample)" in content


@pytest.mark.parametrize("bad", [None, "x", 5, [], {"rows": "x"}, {"rows": [None, 3]},
                                 {"name": 5, "rows": [{"pair": 1, "scope": "holdout"}]}])
def test_a_malformed_view_does_not_break_the_card(bad):
    content = render_approval(RECORD, _detail(reevaluation=bad))
    assert content.startswith("承認 #7 再評価")


@pytest.mark.parametrize("phase,expected", [
    ("done", "却下し、配備を停止しました"),
    ("needs_retry", "取引は止まりましたが symlink を外せていません"),
    ("foreign", "plugins/sma_cross が想定と違うため"),
])
def test_the_terminal_card_of_a_rejection_follows_the_deploy_phase(phase, expected):
    content = _plain(render_terminal(RECORD, _detail(
        status="rejected", deploy_phase=phase, decided_at="2026-11-08T04:00:00+00:00")))
    assert expected in content and "状態: 却下" in content


def test_the_terminal_card_of_a_confirmed_request_says_the_deployment_is_unchanged():
    content = render_terminal(RECORD, _detail(status="approved"))
    assert "再評価の結果で承認の根拠を差し替えました (配備は変わりません)" in content
    assert "配備されました" not in content


def _event(state, **fields):
    return {"code": "approval_state_changed", "ref": "approval:7", "state": state, **fields}


def _text(payload):
    return _event_text_for_code(payload, "sma_cross", None)


@pytest.mark.parametrize("state,fields,expected", [
    ("approved", {"candidate_origin": "reevaluation"},
     "承認 #7 (plugin sma_cross): 再評価の結果で承認の根拠を差し替えました (配備は変わりません)。"),
    ("rejected", {"candidate_origin": "reevaluation", "deploy_phase": "done"},
     "却下し、配備を停止しました。"),
    ("rejected", {"candidate_origin": "reevaluation", "deploy_phase": "needs_retry"},
     "取引は止まりましたが symlink を外せていません。"),
    ("rejected", {"candidate_origin": "reevaluation", "deploy_phase": "foreign"},
     "plugins/sma_cross が想定と違うため外していません。確認してください。"),
    ("invalidated", {"candidate_origin": "reevaluation", "decision_code": "approve"},
     "依頼が古いため無効にしました。配備は変えていません。再評価をやり直してください。"),
])
def test_the_event_text_follows_the_origin_and_the_deploy_phase(state, fields, expected):
    text = _plain(_text(_event(state, **fields)))
    assert expected in text
    assert "承認・配備されました" not in text and "が却下されました" not in text


def test_a_stale_rejection_names_the_command_that_stops_the_deployment():
    text = _text(_event("invalidated", candidate_origin="reevaluation", decision_code="reject"))
    assert "afx plugin disable" in text
    assert "afx plugin disable" not in _text(
        _event("invalidated", candidate_origin="reevaluation", decision_code="approve"))


def test_ordinary_events_keep_their_wording():
    assert "が却下されました" in _text(_event("rejected"))
    assert "が承認・配備されました" in _text(_event("approved"))
    assert "が却下されました" in _text(_event("rejected", candidate_origin="other"))


class _Client:
    def __init__(self, reply):
        self.reply, self.asked = reply, []

    def approval(self, approval_id):
        self.asked.append(approval_id)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _adapter(client):
    adapter = BotAdapter.__new__(BotAdapter)
    adapter.client = client
    adapter._next_poll_at = {}
    adapter._now = lambda: 100.0
    return adapter


def _request(action):
    return Interaction(custom_id=encode_custom_id(action, 7), guild_id=1, channel_id=2,
                       user_id=3, message_id=4, display_generation=1)


CONFIRM = GateResult("Confirm this decision.", confirm_view=ViewModel(()))


def test_rejecting_a_reevaluation_asks_for_confirmation_with_the_stop_warning():
    client = _Client(Reply(200, _detail(), None))
    result = _adapter(client)._with_reject_warning(_request("reject"), CONFIRM)
    assert result.text == "Reject すると strategy sma_cross の配備を停止します (position は保護を続けます)"
    assert result.confirm_view is CONFIRM.confirm_view and client.asked == [7]


@pytest.mark.parametrize("action", ["claim", "approve", "retry"])
def test_only_a_reject_gets_the_stop_warning(action):
    client = _Client(Reply(200, _detail(), None))
    assert _adapter(client)._with_reject_warning(_request(action), CONFIRM) is CONFIRM
    assert client.asked == []


@pytest.mark.parametrize("reply", [
    Reply(200, _detail(candidate_origin=None), None),
    Reply(200, {"status": "pending", "change": CHANGE}, None),
    Reply(404, None, None)])
def test_an_ordinary_approval_keeps_the_gate_text(reply):
    assert _adapter(_Client(reply))._with_reject_warning(_request("reject"), CONFIRM) is CONFIRM


@pytest.mark.parametrize("reply", [
    Reply(500, None, None), Reply(429, None, 30), Reply(503, None, None),
    RuntimeError("down")])
def test_an_unreadable_approval_gets_the_fixed_possible_stop_warning(reply):
    result = _adapter(_Client(reply))._with_reject_warning(_request("reject"), CONFIRM)
    assert result.text == "Reject で配備を止める可能性があります"
    assert result.confirm_view is CONFIRM.confirm_view


def test_a_throttled_reply_defers_the_next_question_for_that_approval():
    client = _Client(Reply(429, None, 30))
    adapter = _adapter(client)
    adapter._with_reject_warning(_request("reject"), CONFIRM)
    second = adapter._with_reject_warning(_request("reject"), CONFIRM)
    assert client.asked == [7]
    assert second.text == "Reject で配備を止める可能性があります"
    assert adapter._next_poll_at[7] > 100.0


def test_the_comparison_cells_round_like_the_card_numbers():
    from afx_ops import reevaluation_text

    rough = {"pf": 1.0165433487012487, "trades": 40, "avg_r": float("nan"),
             "max_drawdown": 0.14966326623788673}

    assert (reevaluation_text._cells(rough)
            == "pf=1.017 trades=40 avg_r=- max_drawdown=0.1497")
    assert reevaluation_text._cells({"trades": True}) == "pf=- trades=- avg_r=- max_drawdown=-"
