"""/improve と /improve/{id}: 実際の DTO の見本から画面を作り、語・allowlist・escape・上限・失敗を確かめる。"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from afx_ops.api_client import ApiError, Reply, WebClient
from afx_ops.web import improve_pages, render
from afx_ops.web.request import Request, handle_request
from afx_ops.web.routes import resolve_route

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DTOS = json.loads((FIXTURES / "improve_run_summary_dtos.json").read_text())


def dto(key: str, run_id: int = 7) -> dict:
    data = copy.deepcopy(DTOS[key])
    data["id"] = run_id
    data["display_timezone"] = "Asia/Tokyo"
    return data


def run_row(run_id: int, **extra) -> dict:
    row = {"id": run_id, "started_at": "2026-11-01T00:00:00+00:00",
           "finished_at": "2026-11-01T00:10:00+00:00", "trigger": "manual",
           "result_code": "gate_failed", "reason_code": "unprofitable", "plugin": "cand",
           "failed_stage": "holdout", "failed_conditions": ["pf"], "failed_pair_count": 2,
           "approval_id": None}
    row.update(extra)
    return row


class Client:
    def __init__(self, listing=None, detail=None):
        self.listing = listing if listing is not None else {
            "runs": [run_row(9), run_row(8, result_code="submitted", reason_code=None,
                                         approval_id=4)],
            "has_more": False, "display_timezone": "Asia/Tokyo"}
        self.detail = detail
        self.calls = []

    def improve_list(self, *, limit):
        self.calls.append(("improve_list", limit))
        return Reply(200, self.listing, None)

    def improve_detail(self, mission_id):
        self.calls.append(("improve_detail", mission_id))
        if isinstance(self.detail, Reply):
            return self.detail
        return Reply(200, self.detail, None)


def get(path: str, client=None, query=None):
    return handle_request(Request("GET", path, query or {}), client or Client())


def page(path: str, client=None, query=None) -> str:
    response = get(path, client, query)
    assert response.status == 200, (path, response.status)
    return response.body.decode()


def body_of(text: str) -> str:
    return text.split("</nav>", 1)[1]


def detail_page(key_or_data, run_id: int = 7) -> str:
    data = dto(key_or_data, run_id) if isinstance(key_or_data, str) else key_or_data
    return body_of(page(f"/improve/{data['id']}", Client(detail=data)))


# ---------------------------------------------------------------- route

@pytest.mark.parametrize("path", ["/improve/0", "/improve/01", "/improve/-1", "/improve/1x",
                                  "/improve/%31", "/improve/" + "9" * 19, "/improve/1/", "/improve/",
                                  "/Improve", "/improve/1/runs"])
def test_the_detail_route_takes_only_a_plain_positive_integer_and_does_not_decode(path):
    assert resolve_route(path) is None


def test_the_improve_routes_resolve_to_the_two_screens():
    assert resolve_route("/improve").route.client_method == "improve_list"
    match = resolve_route("/improve/42")
    assert match.route.client_method == "improve_detail" and match.values == {"id": 42}
    assert resolve_route("/improve/" + "9" * 18).values == {"id": int("9" * 18)}


def test_an_unmatched_id_never_reaches_the_daemon():
    client = Client(detail=dto("submitted"))
    for path in ("/improve/0", "/improve/%31", "/improve/1/"):
        assert get(path, client).status == 404
    assert client.calls == []


# ---------------------------------------------------------------- 一覧

def test_the_list_shows_runs_in_the_daemon_order_with_links_and_display_times():
    text = page("/improve")
    first, second = text.index('<a href="/improve/9">'), text.index('<a href="/improve/8">')
    assert first < second
    assert "2026-11-01 09:00:00 +09:00" in text and "2026-11-01 09:10:00 +09:00" in text
    assert "見送り (床未達)" in text and "候補を提出" in text
    assert '<a href="/plugins/cand">cand</a>' in text
    assert '<a href="/approvals/4">#4</a>' in text
    assert "holdout: PF (2 pair)" in text and "手動" in text


def test_an_empty_list_says_so():
    client = Client(listing={"runs": [], "has_more": False})
    assert "終了した改善 mission はありません" in page("/improve", client)


def test_the_list_asks_the_daemon_for_the_default_or_the_given_limit():
    client = Client()
    page("/improve", client)
    page("/improve", client, {"limit": "20"})
    assert client.calls == [("improve_list", 50), ("improve_list", 20)]


@pytest.mark.parametrize("query", [{"limit": "0"}, {"limit": "101"}, {"limit": "x"},
                                   {"limit": "-1"}, {"after": "3"}, {"limit": "5", "n": "1"}])
def test_a_bad_query_is_400_without_a_daemon_call(query):
    client = Client()
    assert get("/improve", client, query).status == 400
    assert client.calls == []


def test_more_runs_link_doubles_the_limit_up_to_the_maximum():
    client = Client(listing={"runs": [run_row(3)], "has_more": True})
    assert 'href="/improve?limit=100"' in page("/improve", client, {"limit": "50"})
    assert 'href="/improve?limit=100"' not in page("/improve", client, {"limit": "100"})
    assert "afx ctl improve runs" in page("/improve", client, {"limit": "100"})
    assert 'href="/improve?limit=20"' in page("/improve", client, {"limit": "10"})
    assert "さらに古い" not in page("/improve", Client(listing={"runs": [run_row(3)],
                                                               "has_more": False}))


def test_a_list_row_shows_only_named_fields_and_fixed_words():
    marker = "LEAK" + "-" + "canary"
    row = run_row(5, reason_code="brand_new_word", secret=marker, trigger="odd")
    text = page("/improve", Client(listing={"runs": [row], "has_more": False}))
    assert marker not in text
    assert "不明 (brand_new_word)" in text and "不明 (odd)" in text


def test_a_hostile_list_value_is_escaped_or_rendered_as_a_fixed_word():
    hostile = "<script>alert(1)</script>"
    row = run_row(5, plugin=hostile, approval_id=hostile, failed_conditions=[hostile],
                  trigger=hostile, result_code=hostile, reason_code=hostile,
                  started_at=hostile, failed_pair_count=hostile)
    text = page("/improve", Client(listing={"runs": [row], "has_more": False}))
    assert "<script" not in text and hostile not in text


def test_a_huge_list_stays_within_the_response_limit_and_says_what_is_omitted():
    runs = [run_row(10**17 - i, plugin="p" * 64, failed_conditions=["pf"] * 5,
                    approval_id=10**17) for i in range(100)]
    listing = {"runs": runs, "has_more": False}
    response = get("/improve", Client(listing=listing))
    assert response.status == 200 and len(response.body) <= render.MAX_RESPONSE_BYTES
    forms = "<section>" + "x" * 30000 + "</section>"
    body = improve_pages.render_improve_list(listing, forms=forms)
    assert len(body) <= render.MAX_RESPONSE_BYTES and forms.encode() in body
    assert "件省略" in body.decode()


# ---------------------------------------------------------------- 詳細

def test_a_submitted_run_shows_every_summary_item():
    text = detail_page("canary_improved")
    for fragment in ("仮説", "idea for cand", "agent の説明", "test rationale", "cand candidate",
                     "試したもの", "床との差", "床 (閾値)", "配備中の版との比較",
                     "上回った", "holdout で配備中の版を上回る", "USDJPY", "候補を提出",
                     "enforce (採否に反映)", "local", '<a href="/approvals/1">#1</a>',
                     '<a href="/plugins/cand">cand</a>'):
        assert fragment in text, fragment


def test_a_floor_miss_shows_the_failed_stage_conditions_and_the_thresholds():
    text = detail_page("unprofitable_holdout")
    assert "床を割った段: holdout" in text
    assert "PF の下限" in text and "判定理由" in text and "床未達" in text


def test_a_not_improved_run_shows_the_comparison_against_the_deployed_version():
    text = detail_page("not_improved")
    assert "配備中の版との比較" in text and "上回らない" in text


def test_the_handoff_comes_from_the_parent_summary():
    text = detail_page("insufficient_trades")
    assert "次回への申し送り" in text and "strategy:未配備指標@1h@USDJPY" in text
    assert "agent の申告なし" in text


@pytest.mark.parametrize("key", sorted(k for k in DTOS if not k.startswith("_")))
def test_every_real_dto_renders_without_a_stray_python_value(key):
    text = detail_page(key)
    assert "None" not in text and "{'" not in text and "[{" not in text and "Traceback" not in text


def test_a_run_without_a_summary_row_says_the_summary_is_missing_but_shows_the_snapshot():
    data = dto("submitted")
    data["summary"] = None
    text = detail_page(data)
    assert improve_pages.NO_SUMMARY_TEXT in text and "候補を提出" in text
    assert "仮説" not in text


@pytest.mark.parametrize(("state", "note"), [("empty", "要約の材料がありません"),
                                             ("build_failed", "要約を組み立てられませんでした")])
def test_a_summary_that_is_not_ok_says_why_and_shows_no_items(state, note):
    data = dto("submitted")
    data["summary"] = {"version": 1, "state": state, "items": {"hypothesis": {"idea": "SECRET"}}}
    text = detail_page(data)
    assert note in text and "SECRET" not in text


def test_a_summary_state_outside_the_known_words_is_a_generic_note():
    data = dto("submitted")
    data["summary"] = {"version": 1, "state": "weird", "items": {"hypothesis": {"idea": "X1"}}}
    text = detail_page(data)
    assert "要約を表示できません" in text and "X1" not in text


def test_a_mission_without_a_terminal_record_says_so():
    data = {"id": 7, "backend": "local", "snapshot": None, "summary": None, "floor": None,
            "report_state": "none", "approval": None, "display_timezone": "UTC"}
    assert improve_pages.NO_TERMINAL_TEXT in detail_page(data)


def test_the_floor_report_is_the_fallback_when_the_summary_has_no_floor():
    data = dto("unprofitable_holdout")
    data["summary"] = None
    data["floor"] = {"stages": [{"stage": "holdout", "pairs": [
        {"pair": "USDJPY", "trades": 12, "pf": 0.8, "avg_r": -0.1, "evaluable": True,
         "conditions": ["pf"]}]}]}
    text = detail_page(data)
    assert "床との差 (レポートから)" in text and "USDJPY" in text and "0.8" in text


def test_agent_text_is_escaped_and_controls_are_removed():
    hostile = "<img src=x onerror=alert(1)>\x00\x07‮ line2"
    data = dto("canary_improved")
    items = data["summary"]["items"]
    items["hypothesis"]["idea"] = hostile
    items["explanation"]["rationale"] = hostile
    items["explanation"]["artifact_summary"] = hostile
    items["observation"] = hostile
    items["handoff"] = {"family": hostile, "attempts": 1, "outcomes": {"x_y": 1}}
    text = detail_page(data)
    assert "<img" not in text and "&lt;img src=x onerror=alert(1)&gt;" in text
    assert "\x00" not in text and "\x07" not in text and "‮" not in text


def test_long_agent_text_is_cut_with_a_mark():
    data = dto("canary_improved")
    data["summary"]["items"]["hypothesis"]["idea"] = "あ" * 5000
    text = detail_page(data)
    assert "あ" * 5000 not in text and "…" in text


def test_fields_outside_the_allowlist_never_reach_the_page():
    marker = "LEAK" + "-" + "canary"
    data = dto("canary_improved")
    data["internal_path"] = marker
    data["snapshot"]["secret"] = marker
    data["approval"]["secret"] = marker
    data["summary"]["secret"] = marker
    items = data["summary"]["items"]
    items["secret"] = marker
    items["hypothesis"]["secret"] = marker
    items["tried"]["secret"] = marker
    items["floor"]["stages"][0]["pairs"][0]["secret"] = marker
    items["comparison"]["pairs"][0]["candidate"]["secret"] = marker
    assert marker not in detail_page(data)


def test_unknown_words_are_shown_as_unknown_never_as_a_known_result():
    data = dto("submitted")
    data["snapshot"]["reason_code"] = "brand_new_reason"
    data["report_state"] = "weird"
    text = detail_page(data)
    assert "不明 (brand_new_reason)" in text and "不明 (weird)" in text


def test_a_hostile_scalar_in_every_detail_slot_is_neutralised():
    hostile = "<script>x</script>"
    data = dto("canary_improved")
    data.update(backend=hostile, model=hostile, report_state=hostile, elapsed_sec=hostile)
    data["snapshot"].update(trigger=hostile, result_code=hostile, reason_code=hostile,
                            tool_calls=hostile)
    data["approval"].update(id=hostile, plugin=hostile, kind=hostile)
    items = data["summary"]["items"]
    items["tried"].update(plugin=hostile, kind=hostile, origin=hostile, indicators=[hostile])
    items["floor"]["failed_conditions"] = [hostile]
    items["floor"]["stages"][0]["pairs"][0].update(pair=hostile, trades=hostile, pf=hostile,
                                                   failed=[hostile])
    items["comparison"].update(mode=hostile, verdict=hostile, reason=hostile, metrics=[hostile])
    items["comparison"]["pairs"][0].update(pair=hostile, verdict=hostile)
    assert "<script" not in detail_page(data)


def test_a_detail_with_the_largest_allowed_content_fits_the_response_limit():
    data = dto("canary_improved")
    items = data["summary"]["items"]
    text = "<&>あ" * 200
    items["hypothesis"]["idea"] = text
    items["explanation"] = {"rationale": text, "artifact_summary": text}
    items["observation"] = text
    pair = items["floor"]["stages"][0]["pairs"][0]
    items["floor"]["stages"][0]["pairs"] = [dict(pair, pair=f"P{i}" * 10) for i in range(64)]
    items["floor"]["stages"][1]["pairs"] = [dict(pair, pair=f"Q{i}" * 10) for i in range(64)]
    cmp_pair = items["comparison"]["pairs"][0]
    items["comparison"]["pairs"] = [dict(cmp_pair, pair=f"R{i}" * 10) for i in range(64)]
    items["handoff"] = {"family": "<&>" * 200, "attempts": 5,
                        "outcomes": {f"w{i}": i + 1 for i in range(20)}}
    response = get("/improve/7", Client(detail=data))
    assert response.status == 200 and len(response.body) <= render.MAX_RESPONSE_BYTES
    assert b"Output clipped" not in response.body and "件省略" in response.body.decode()


# ---------------------------------------------------------------- 失敗

@pytest.mark.parametrize(("reply", "status"), [
    (Reply(404, None, "not_found"), 404),
    (Reply(403, None, "forbidden"), 502),
    (Reply(500, None, "internal"), 502),
    (Reply(200, {"id": "x"}, None), 502),
    (Reply(200, [], None), 502),
])
def test_detail_failures_are_the_fixed_status_pages(reply, status):
    response = get("/improve/7", Client(detail=reply))
    assert response.status == status
    assert b"forbidden" not in response.body and b"internal" not in response.body


@pytest.mark.parametrize("listing", [{"runs": "x", "has_more": False},
                                     {"runs": [1], "has_more": False},
                                     {"runs": [], "has_more": "no"}, {"runs": []}])
def test_a_list_of_the_wrong_shape_is_502(listing):
    assert get("/improve", Client(listing=listing)).status == 502


def test_a_daemon_failure_is_502_without_the_cause(capsys):
    class Broken(Client):
        def improve_list(self, *, limit):
            raise ApiError("connect_failed", detail="/home/x/secret.sock")

    response = get("/improve", Broken())
    assert response.status == 502 and b"secret" not in response.body
    assert "/home/x" not in capsys.readouterr().err


def test_a_permission_failure_on_the_list_is_502_not_a_leak():
    class Forbidden(Client):
        def improve_list(self, *, limit):
            return Reply(403, None, "forbidden")

    response = get("/improve", Forbidden())
    assert response.status == 502 and b"forbidden" not in response.body


def test_a_non_get_method_is_405():
    for method in ("POST", "PUT", "DELETE"):
        assert handle_request(Request(method, "/improve", {}), Client()).status == 405
        assert handle_request(Request(method, "/improve/3", {}), Client()).status == 405


def test_the_detail_takes_no_query():
    assert get("/improve/3", Client(detail=dto("submitted")), {"x": "1"}).status == 400


# ---------------------------------------------------------------- client

def _client():
    calls = []
    client = WebClient.__new__(WebClient)
    client._get = lambda path, **kwargs: calls.append((path, kwargs)) or "reply"
    return client, calls


def test_the_web_client_reads_the_two_improve_routes():
    client, calls = _client()
    assert client.improve_list(limit=20) == "reply" and client.improve_detail(7) == "reply"
    assert calls == [("/v1/improve/runs", {"limit": 20}), ("/v1/improve/runs/7", {})]


@pytest.mark.parametrize("value", [-1, "1", None, True, 1.5])
def test_a_bad_mission_id_is_refused_before_any_request(value):
    client, calls = _client()
    with pytest.raises(ValueError):
        client.improve_detail(value)
    assert calls == []
