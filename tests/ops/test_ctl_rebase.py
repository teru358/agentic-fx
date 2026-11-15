"""`afx ctl killswitch rebase` の確認・再 preview・成功の文。

CLI は server の DTO をそのまま表示し、金額を再計算しない。入力は `rebase <世代>` の
完全一致だけで、それ以外は POST を送らない。"""
from __future__ import annotations

import io

import pytest

from agentic_fx.ops import client
from agentic_fx.ops.contracts import Principal

TOKEN = "ab" * 32
OTHER = "cd" * 32
PREVIEW_PATH = "/v1/killswitch/rebase-preview"
POST_PATH = "/v1/killswitch/rebase"


def _ok(data):
    return client.Reply(200, data, None)


def _status():
    return _ok({"display_timezone": "Asia/Tokyo"})


def _preview(**over):
    base = {
        "eligible": True, "reason": None, "preview_token": TOKEN, "generation": 3,
        "latched": True, "latched_at": "2026-11-13T01:00:00+00:00",
        "source_snapshot_id": 41, "source_snapshot_ts": "2026-11-14T02:00:00+00:00",
        "source_snapshot_source": "paper", "hwm": 1000.0, "balance": 900.0,
        "equity": 900.0, "new_hwm": 900.0, "drawdown_pct": 10.0, "threshold_pct": 8.0,
        "cut": 100.0, "cut_pct": 10.0, "past_cut": 50.0, "past_cut_pct": 4.0,
        "past_count": 1, "cumulative_cut_after": 150.0, "cumulative_cut_pct_after": 14.0,
        "count_after": 2, "additional_headroom": 72.0, "flat": True,
        "cumulative_warning": False, "warning_threshold_pct": 24.0,
    }
    base.update(over)
    return _ok(base)


def _inelig(reason):
    return _ok({"eligible": False, "reason": reason, "preview_token": None,
                "generation": 3, "latched": True})


class _Stub:
    def __init__(self, tmp_path, replies):
        self.root = tmp_path
        self.principal = Principal.APPROVER
        self.replies = list(replies)
        self.sent = []

    def request(self, method, path, *, body=None, params=None, idempotent=False):
        self.sent.append((method, path, body, params, idempotent))
        reply = self.replies.pop(0)
        return reply(method, path) if callable(reply) else reply

    def posts(self):
        return [item for item in self.sent if item[0] == "POST"]


class _Typed:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None


def _runner(tmp_path, replies):
    api = _Stub(tmp_path, replies)
    out = io.StringIO()
    return api, out, client.Runner(api, stdin=io.StringIO(""), stdout=out)


def test_parse_and_sets():
    op = client.parse(["killswitch", "rebase", "3"])
    assert (op.name, op.args) == ("killswitch_rebase", [3])
    assert "killswitch_rebase" in client._APPROVER_OPS
    assert "killswitch_rebase" in client._CONFIRM_OPS
    for bad in (["killswitch", "rebase"], ["killswitch", "rebase", "x"],
                ["killswitch", "rebase", "3", "--yes"], ["killswitch", "rebase", "-1"]):
        with pytest.raises(client.ClientError) as caught:
            client.parse(bad)
        assert caught.value.rc == 2


def test_operator_cannot_send(tmp_path):
    api, _, runner = _runner(tmp_path, [])
    api.principal = Principal.OPERATOR
    with pytest.raises(client.ClientError) as caught:
        runner.run(client.Op("killswitch_rebase", [3]))
    assert caught.value.rc == 3
    assert api.sent == []


def test_non_terminal_sends_nothing(tmp_path):
    api, _, runner = _runner(tmp_path, [])
    with pytest.raises(client.ClientError) as caught:
        runner.run(client.Op("killswitch_rebase", [3]))
    assert caught.value.rc == 2
    assert "端末からの確認" in caught.value.message
    assert api.sent == []


@pytest.mark.parametrize("answer", ["y", "yes", "rebase", "rebase 4", "REBASE 3",
                                    "reset", "", None, "rebase  3", "3"])
def test_only_the_exact_phrase_sends(tmp_path, answer):
    api, _, runner = _runner(tmp_path, [_status(), _preview(),
                                        _ok({"rebase_id": 1, "snapshot_id": 42,
                                             "generation": 3, "latched": True})])
    terminal = _Typed(answer)
    with pytest.raises(client.ClientError) as caught:
        runner._op_killswitch_rebase(3, terminal=terminal)
    assert caught.value.rc == 1
    assert "取り消しました" in caught.value.message
    assert api.posts() == []


def test_exact_phrase_posts_the_confirmed_token_and_nothing_else(tmp_path):
    api, out, runner = _runner(tmp_path, [_status(), _preview(),
                                          _ok({"rebase_id": 9, "snapshot_id": 42,
                                               "generation": 3, "latched": True})])
    assert runner._op_killswitch_rebase(3, terminal=_Typed("rebase 3")) == 0
    assert api.posts() == [("POST", POST_PATH,
                            {"expected_generation": 3, "preview_token": TOKEN}, None, False)]
    gets = [item for item in api.sent if item[0] == "GET"]
    assert gets[1][1:4] == (PREVIEW_PATH, None, {"expected_generation": 3})


def test_shows_every_item_the_dto_carries_without_recomputing(tmp_path):
    # DTO の値を意図的に整合しない数値にし、表示がそのまま写されることを確かめる。
    api, out, runner = _runner(tmp_path, [
        _status(), _preview(cut=111.11, cut_pct=22.22, past_cut=33.33, past_cut_pct=44.44,
                            past_count=7, cumulative_cut_after=555.55,
                            cumulative_cut_pct_after=66.66, count_after=9,
                            additional_headroom=77.77, hwm=1234.5, equity=888.8,
                            new_hwm=888.8, balance=999.9, drawdown_pct=11.11,
                            threshold_pct=5.5),
        _ok({"rebase_id": 1, "snapshot_id": 2, "generation": 3, "latched": False})])
    terminal = _Typed("rebase 3")
    runner._op_killswitch_rebase(3, terminal=terminal)
    shown = out.getvalue() + "".join(terminal.prompts)
    for text in ("111.11", "22.22", "33.33", "44.44", "555.55", "66.66", "77.77",
                 "1,234.50", "888.80", "999.90", "11.11", "5.50", "id=41", "paper"):
        assert text in shown, text
    # 世代・ラッチ・発動時刻・source の時刻 (表示 tz へ換算)
    assert "gen=3" in shown and "LATCHED" in shown
    assert "2026-11-13 10:00:00 +09:00" in shown
    assert "2026-11-14 11:00:00 +09:00" in shown
    # 注意の固定文
    assert "ラッチを解除しません" in shown
    assert "日次損失・総リスク" in shown
    assert "閉場中は付け直せません" in shown
    assert "flat" in shown


def test_confirmation_prompt_embeds_this_and_cumulative_and_count(tmp_path):
    api, out, runner = _runner(tmp_path, [_status(), _preview(), _ok(
        {"rebase_id": 1, "snapshot_id": 2, "generation": 3, "latched": True})])
    terminal = _Typed("rebase 3")
    runner._op_killswitch_rebase(3, terminal=terminal)
    prompt = terminal.prompts[0]
    for text in ("100.00", "10.00%", "150.00", "14.00%", "2 回目", "rebase 3"):
        assert text in prompt, text


def test_cumulative_warning_is_shown_only_when_the_dto_says_so(tmp_path):
    for flag, shown in ((True, True), (False, False)):
        api, out, runner = _runner(tmp_path, [
            _status(), _preview(cumulative_warning=flag), _ok(
                {"rebase_id": 1, "snapshot_id": 2, "generation": 3, "latched": True})])
        runner._op_killswitch_rebase(3, terminal=_Typed("rebase 3"))
        text = out.getvalue()
        warning = "警告: 累積切下げ率（各回率の合計）が現在の DD 閾値の3倍を超えます。"
        assert (warning in text) is shown
        assert ("過去の損失を受け入れ、追加の損失余地を戻します。" in text) is shown
        if shown:
            assert "24.00%" in text and "閾値を変更すると" in text


@pytest.mark.parametrize("latched,must,mustnot", [
    (True, ["HWM を付け直した。新規停止は継続中", "killswitch reset 3"],
     ["現在の DD 条件は解消した"]),
    (False, ["HWM を付け直した。現在の DD 条件は解消した"],
     ["新規停止は継続中", "killswitch reset"]),
])
def test_success_text_follows_the_latch(tmp_path, latched, must, mustnot):
    api, out, runner = _runner(tmp_path, [_status(), _preview(latched=latched), _ok(
        {"rebase_id": 5, "snapshot_id": 6, "generation": 3, "latched": latched})])
    assert runner._op_killswitch_rebase(3, terminal=_Typed("rebase 3")) == 0
    tail = out.getvalue().split("rebase_id=")[1]
    for text in must:
        assert text in tail
    for text in mustnot:
        assert text not in tail


@pytest.mark.parametrize("reason,text", [
    ("market_closed", "閉場中"), ("generation_unavailable", "世代 0"),
    ("exposure_present", "APPROVAL_PENDING"), ("rebase_not_needed", "閾値未満"),
    ("state_unavailable", "killswitch reconcile"), ("broker_not_supported", "PaperBroker"),
    ("account_snapshot_unavailable", "MTM"), ("account_snapshot_changed", "MTM"),
    ("accounting_inconsistent", "不整合"), ("generation_mismatch", "ラッチが更新"),
    ("rebase_unavailable", "使えません"), ("brand_new_reason", "不明"),
])
def test_ineligible_preview_shows_the_reason_and_asks_nothing(tmp_path, reason, text):
    api, out, runner = _runner(tmp_path, [_status(), _inelig(reason)])
    terminal = _Typed("rebase 3")
    with pytest.raises(client.ClientError) as caught:
        runner._op_killswitch_rebase(3, terminal=terminal)
    assert caught.value.rc == 1
    assert text in caught.value.message
    assert terminal.prompts == [] and api.posts() == []


def test_preview_changed_refetches_shows_reason_and_asks_again(tmp_path):
    api, out, runner = _runner(tmp_path, [
        _status(), _preview(), client.Reply(409, None, "preview_changed"),
        _preview(preview_token=OTHER, equity=850.0, cut=150.0),
        _ok({"rebase_id": 1, "snapshot_id": 2, "generation": 3, "latched": True})])
    terminal = _Typed("rebase 3", "rebase 3")
    assert runner._op_killswitch_rebase(3, terminal=terminal) == 0
    assert len(terminal.prompts) == 2
    assert [p[2]["preview_token"] for p in api.posts()] == [TOKEN, OTHER]
    assert "150.00" in terminal.prompts[1]
    assert "表示した後に値が変わった" in out.getvalue()


def test_preview_changed_then_ineligible_shows_the_new_reason_and_stops(tmp_path):
    api, out, runner = _runner(tmp_path, [
        _status(), _preview(), client.Reply(409, None, "preview_changed"),
        _inelig("rebase_not_needed")])
    with pytest.raises(client.ClientError) as caught:
        runner._op_killswitch_rebase(3, terminal=_Typed("rebase 3"))
    assert "閾値未満" in caught.value.message
    assert len(api.posts()) == 1


def test_preview_changed_loop_is_bounded(tmp_path):
    replies = [_status()]
    for n in range(client._REBASE_MAX_ROUNDS):
        replies += [_preview(preview_token=f"{n:064x}"), client.Reply(409, None, "preview_changed")]
    api, out, runner = _runner(tmp_path, replies)
    with pytest.raises(client.ClientError) as caught:
        runner._op_killswitch_rebase(3, terminal=_Typed(*["rebase 3"] * 20))
    assert caught.value.rc == 1
    assert len(api.posts()) == client._REBASE_MAX_ROUNDS


@pytest.mark.parametrize("code", ["request_timeout", "database_busy"])
def test_unapplied_timeout_allows_resending_the_same_token_after_confirming_again(tmp_path, code):
    api, out, runner = _runner(tmp_path, [
        _status(), _preview(), client.Reply(409, None, code),
        _preview(),  # 同じ token = 台帳も HWM も動いていない = 無変更が確定
        _ok({"rebase_id": 1, "snapshot_id": 2, "generation": 3, "latched": True})])
    terminal = _Typed("rebase 3", "rebase 3")
    assert runner._op_killswitch_rebase(3, terminal=terminal) == 0
    assert [p[2]["preview_token"] for p in api.posts()] == [TOKEN, TOKEN]
    assert len(terminal.prompts) == 2


@pytest.mark.parametrize("after", [_preview(preview_token=OTHER), _inelig("rebase_not_needed")],
                         ids=["token_moved", "ineligible"])
def test_timeout_with_a_moved_state_is_not_resent_and_is_not_called_applied(tmp_path, after):
    api, out, runner = _runner(tmp_path, [
        _status(), _preview(), client.Reply(409, None, "request_timeout"), after])
    with pytest.raises(client.ClientError) as caught:
        runner._op_killswitch_rebase(3, terminal=_Typed("rebase 3", "rebase 3"))
    assert caught.value.rc == 1
    assert "再送しません" in caught.value.message
    assert len(api.posts()) == 1
    assert "付け直した" not in out.getvalue()


def test_outcome_unknown_is_not_resent_and_points_to_the_ledger(tmp_path):
    api, out, runner = _runner(tmp_path, [
        _status(), _preview(), client.Reply(409, None, "outcome_unknown")])
    with pytest.raises(client.ClientError) as caught:
        runner._op_killswitch_rebase(3, terminal=_Typed("rebase 3", "rebase 3"))
    assert caught.value.rc == 1
    assert "不明" in caught.value.message and "再送しません" in caught.value.message
    assert len(api.posts()) == 1
    assert "付け直した" not in out.getvalue()


def test_lost_response_is_not_resent(tmp_path):
    def lost(method, path):
        raise client._ResponseLost()
    api, out, runner = _runner(tmp_path, [_status(), _preview(), lost])
    with pytest.raises(client.ClientError):
        runner._op_killswitch_rebase(3, terminal=_Typed("rebase 3", "rebase 3"))
    assert len(api.posts()) == 1
    assert api.posts()[0][4] is False  # Idempotency-Key を付けない (client の自動再送なし)


@pytest.mark.parametrize("code,text", [
    ("market_closed", "閉場中"), ("generation_mismatch", "ラッチが更新"),
    ("invalid_state", "reconcile"),
])
def test_post_refusals_are_rc1_with_a_clear_text(tmp_path, code, text):
    api, out, runner = _runner(tmp_path, [_status(), _preview(), client.Reply(409, None, code)])
    with pytest.raises(client.ClientError) as caught:
        runner._op_killswitch_rebase(3, terminal=_Typed("rebase 3"))
    assert caught.value.rc == 1 and text in caught.value.message
    assert "付け直した" not in out.getvalue()
    assert len(api.posts()) == 1


def test_error_texts_for_the_two_new_codes_follow_the_existing_shape():
    for code in ("preview_changed", "market_closed"):
        assert code in client._ERROR_TEXT
        assert client.error_text(code) == client._ERROR_TEXT[code]
    assert "失敗しました" not in client.error_text("preview_changed")
    assert "失敗しました" not in client.error_text("market_closed")


def test_untrusted_dto_text_is_sanitized_and_bad_numbers_show_dash(tmp_path):
    esc = "\x1b[31m"
    api, out, runner = _runner(tmp_path, [
        _status(), _preview(source_snapshot_source=f"pa{esc}per", hwm=float("nan"),
                            cut="1e999", past_count=True),
        _ok({"rebase_id": 1, "snapshot_id": 2, "generation": 3, "latched": True})])
    terminal = _Typed("rebase 3")
    runner._op_killswitch_rebase(3, terminal=terminal)
    shown = out.getvalue() + "".join(terminal.prompts)
    assert "\x1b" not in shown


def test_reset_success_adds_the_fixed_rebase_guidance_after_the_fixed_warning(tmp_path):
    api, out, runner = _runner(tmp_path, [
        _ok({"kill_switch": {"latched": True, "generation": 3, "latched_at": "t"},
             "equity": 1000}),
        _ok({"generation": 3})])
    assert runner._op_killswitch_reset(3, terminal=_Typed("reset")) == 0
    lines = out.getvalue().splitlines()
    assert lines[-3] == "kill switch ラッチを解除しました (gen=3)"
    assert lines[-2] == "DD が閾値以上のままなら、次の新規判断で kill switch が再びラッチします。"
    assert lines[-1] == ("HWM を付け直すには `afx ctl --as approver killswitch rebase 3` を"
                         "実行してください")
    assert out.getvalue().count("閾値以上のまま") == 1
