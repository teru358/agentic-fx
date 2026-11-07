"""status page の反応統計の表 (通貨 × importance が先、name は後、窓ごと、床未満に印)。"""
from __future__ import annotations

import re

from afx_ops.api_client import Reply
from afx_ops.web import render
from afx_ops.web.request import Request, handle_request


class Client:
    def __init__(self, status):
        self._status = status

    def status(self):
        return Reply(200, self._status, None)


def _row(**over):
    row = {"currency": "USD", "kind": "importance", "key": "3", "window_min": 240,
           "n": 25, "unique_ts_n": 24, "unresolvable_n": 3, "range_median": 12.5,
           "range_p75": 14.0, "abs_median": 2.0, "abs_p75": 3.5, "net_median": 1.0,
           "net_p75": 2.5, "close_change_median": -0.5}
    row.update(over)
    return row


def _reaction(rows, **over):
    reaction = {"floor": 20, "bundles": rows, "bundles_omitted": 0,
                "backlog": {"sets_without_values": 4, "events_without_currency_key": 1}}
    reaction.update(over)
    return reaction


def _page(reaction, **extra):
    status = {"econ_signal": {"slice_worker": "alive", "reaction": reaction}, **extra}
    return handle_request(Request("GET", "/", {}), Client(status)).body.decode()


def _table_rows(page, heading):
    """見出し (h4) の直後の表の本体の行を、セルの文字列の list で返す。"""
    start = page.index(f"<h4>{heading}</h4>")
    table = page[start:page.index("</table>", start)]
    return [re.findall(r"<td>([^<]*)</td>", row)
            for row in re.findall(r"<tr>(.*?)</tr>", table) if "<td>" in row]


def test_the_importance_tables_come_before_the_name_tables_and_use_window_headings():
    rows = [_row(window_min=w) for w in (15, 60, 240)]
    rows += [_row(kind="name", key="Rate", window_min=w, n=30) for w in (15, 60, 240)]
    page = _page(_reaction(rows))
    assert page.index("importance 別") < page.index("name 別")
    first = page.index("<h4>15m</h4>")
    assert first < page.index("<h4>1h</h4>") < page.index("<h4>4h</h4>") < page.index("name 別")
    assert page.count("<h4>4h</h4>") == 2


def test_a_row_shows_n_unique_ts_n_unresolvable_and_the_medians_with_p75():
    page = _page(_reaction([_row()]))
    assert _table_rows(page, "4h") == [
        ["USD", "3", "25", "24", "3", "12.5 / 14.0", "2.0 / 3.5", "1.0 / 2.5", "-0.5", ""]]


def test_a_row_below_the_floor_keeps_its_numbers_and_gets_a_mark():
    page = _page(_reaction([_row(n=5, unique_ts_n=5)]))
    (cells,) = _table_rows(page, "4h")
    assert cells[2] == "5" and cells[5] == "12.5 / 14.0"
    assert cells[-1] == "n&lt;20"


def test_the_floor_in_the_dto_decides_the_mark():
    page = _page(_reaction([_row(n=25)], floor=30))
    assert _table_rows(page, "4h")[0][-1] == "n&lt;30"


def test_missing_numbers_show_a_dash():
    page = _page(_reaction([_row(n=0, unique_ts_n=0, range_median=None, range_p75=None,
                                 close_change_median=None)]))
    (cells,) = _table_rows(page, "4h")
    assert cells[5] == "- / -"
    assert cells[8] == "-"


def test_the_backlog_counts_are_shown():
    page = _page(_reaction([_row()]))
    assert re.search(r"<dt>reaction sets_without_values</dt><dd>4</dd>", page)
    assert re.search(r"<dt>reaction events_without_currency_key</dt><dd>1</dd>", page)


def test_an_unavailable_reaction_section_is_marked_and_absent_is_left_out():
    assert re.search(r"<dt>reaction</dt><dd>unavailable</dd>", _page(None))
    page = handle_request(Request("GET", "/", {}), Client(
        {"econ_signal": {"slice_worker": "alive"}})).body.decode()
    assert "Economic reactions" not in page


def test_hostile_names_are_escaped_and_unknown_fields_are_not_shown():
    rows = [_row(kind="name", key="<script>x</script>", secret="leak-secret")]
    page = _page(_reaction(rows))
    assert "<script>" not in page and "leak-secret" not in page
    assert "&lt;script&gt;x&lt;/script&gt;" in page


def test_a_malformed_reaction_is_marked_unavailable_not_a_crash():
    for junk in ({"bundles": "junk"}, ["junk"]):
        assert re.search(r"<dt>reaction</dt><dd>unavailable</dd>", _page(junk))


def test_a_full_size_reaction_table_does_not_clip_the_page():
    currencies = ("USD", "JPY", "EUR", "GBP", "AUD", "CAD", "CHF", "NZD")
    rows = [_row(currency=c, key=str(i), window_min=w)
            for c in currencies for i in (1, 2, 3) for w in (15, 60, 240)]
    rows += [_row(currency=c, kind="name", key=f"Event number {i} of the calendar",
                  window_min=w, n=3)
             for c in currencies for i in range(40) for w in (15, 60, 240)]
    page = _page(_reaction(rows, bundles_omitted=0))
    assert "Output clipped" not in page
    assert "Economic reactions" in page
    assert "省略" in page
    assert len(page.encode()) <= render.MAX_RESPONSE_BYTES


def test_the_style_is_unchanged_so_the_csp_hash_still_matches():
    from afx_ops.web import headers
    import base64
    import hashlib
    digest = base64.b64encode(hashlib.sha256(render.STYLE.encode()).digest()).decode()
    assert digest in headers.STYLE_HASH
