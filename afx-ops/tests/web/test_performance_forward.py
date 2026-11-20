"""/performance の戦略別タブの機械採点: 表・固定文・不明の扱い・予算。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from afx_ops.web import perf_pages
from tests.web.test_performance_strategy import data_of, strategy_page

NOW = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
CAVEAT_OPTIMISTIC = "機械採点は実約定を含まないので楽観側に出ます"
CAVEAT_UNDECIDED = "採否に至らなかった signal は含みません"


def iso_days(days):
    return (NOW + timedelta(days=days)).isoformat()


def _series(n, *, pnl=1.0):
    level = "none" if n < 5 else ("normal" if n >= 30 else "reference")
    wins = n if pnl > 0 else 0
    return {"n": n, "scored": n, "r_unknown": 0, "wins": wins, "losses": n - wins, "flats": 0,
            "avg_r": pnl if n else None,
            "pf": None if pnl > 0 or not n else 0.0,
            "pf_state": "no_losses" if pnl > 0 and n else "unknown", "pips_sum": 100.0 * n,
            "level": level}


def forward_row(name="alpha", *, n=6, abandoned=2, pnl=1.0, hash_="a" * 64):
    return {
        "name": name, "content_hash": hash_, "state": "ok", "reason": None,
        "generation": {"started_at": iso_days(-42), "source": "journal", "op_id": 3},
        "window": {"start": iso_days(-42), "end": iso_days(0), "weeks": 12},
        "thresholds": {"min_count": 5, "reference_count": 30},
        "series": {"all": _series(n, pnl=pnl), "accept": _series(n // 2, pnl=pnl),
                   "reject": _series(n - n // 2, pnl=pnl)},
        "decisions": {"total": n, "accept": n // 2, "reject": n - n // 2, "gate_passed": n // 2},
        "outcomes": {"tp": n, "sl": 0, "day_close": 0, "data_insufficient": 0, "no_fill": 0,
                     "unterminated": 0},
        "holes": {"abandoned": {"freshness_expired": abandoned} if abandoned else {},
                  "no_fill": 0, "data_insufficient": 0, "unterminated": 0,
                  "as_of_unknown": 0, "oldest_unterminated_sec": None, "r_unknown": 0},
        "reach": {"signals": n + abandoned, "decided": n,
                  "rate": n / (n + abandoned) if n + abandoned else None},
        "concentration": {"days": n, "max_day_count": 1, "max_day_share": 1 / n if n else None},
        "comparison": {"recent": {"signals": n + abandoned, "decided": n,
                                  "rate": n / (n + abandoned) if n + abandoned else None},
                       "previous": {"signals": 0, "decided": 0, "rate": None},
                       "comparable": False}}


def with_forward(*rows, state="ok", omitted=0):
    return data_of(forward={"state": state, "strategies": list(rows), "omitted": omitted})


def forward_part(html):
    return html.split("<h2>機械採点 (配備中の戦略ごと)</h2>", 1)[1].split("</section>", 1)[0]


def cells(html):
    return [[re.sub(r"<[^>]*>", "|", c) for c in re.findall(r"<td>(.*?)</td>", r)]
            for r in re.findall(r"<tr>(.*?)</tr>", forward_part(html)) if "<td>" in r]


def test_the_two_fixed_sentences_are_shown_verbatim():
    text = forward_part(strategy_page(with_forward(forward_row())))
    assert CAVEAT_OPTIMISTIC in text and CAVEAT_UNDECIDED in text


def test_the_fixed_sentences_are_shown_even_when_the_scores_are_unreadable():
    for data in (with_forward(state="unavailable"), data_of()):
        text = forward_part(strategy_page(data))
        assert CAVEAT_OPTIMISTIC in text and CAVEAT_UNDECIDED in text


def test_the_scope_differs_from_the_real_trades_and_the_page_says_so():
    text = forward_part(strategy_page(with_forward(forward_row())))
    assert "実取引の表とは集計の範囲が違います" in text
    assert "今の版" in text and "配備世代の開始から最大 12 週" in text
    assert perf_pages.FORWARD_WINDOW_WEEKS == 12


def test_a_row_shows_values_counts_and_holes():
    row = cells(strategy_page(with_forward(forward_row())))[0]
    flat = " ".join(row)
    assert "alpha" in flat and "aaaaaaaaaaaa" in flat
    assert "6" in row[3]                                   # N
    assert "6 / 0 / 0" in row[4]                           # 勝 / 負 / 分
    assert "+1.00 (参考)" in row[5]                        # 平均 R (6 件は参考)
    assert "損失なし (参考)" in row[6]                      # PF
    assert "accept 3 / reject 3" in row[7]
    assert "6 / 8 (75.0%)" in " ".join(row)                  # 到達率
    assert "abandoned 鮮度切れ 2" in row[10]


def test_below_the_minimum_count_the_values_are_a_dash_not_a_number():
    row = cells(strategy_page(with_forward(forward_row(n=4))))[0]
    assert row[5] == "–" or row[5].startswith("–")
    assert row[6] == "–"


def test_from_thirty_rows_on_the_values_have_no_reference_mark():
    row = cells(strategy_page(with_forward(forward_row(n=30))))[0]
    assert "(参考)" not in row[5] and "(参考)" not in row[6]


def test_a_strategy_whose_generation_is_unknown_has_no_numbers():
    undetermined = {"name": "alpha", "content_hash": "a" * 64, "state": "undetermined",
                    "reason": "no_generation_record",
                    "thresholds": {"min_count": 5, "reference_count": 30}}
    html = forward_part(strategy_page(with_forward(undetermined)))
    assert "判定不能" in html and "配備の記録がありません" in html
    assert "alpha" in html and "勝 / 負 / 分" in html
    assert not re.search(r"\d+ / \d+ / \d+", html.split("<tbody>", 1)[1])


@pytest.mark.parametrize("reason, word", [
    ("generation_mismatch", "合いません"), ("time_unreadable", "読めません"),
    ("journal_unreadable", "読めません"), ("summary_failed", "集計できませんでした"),
    ("something_new", "不明")])
def test_every_reason_has_words_and_an_unknown_one_reads_as_unknown(reason, word):
    row = {"name": "alpha", "content_hash": "a" * 64, "state": "undetermined", "reason": reason,
           "thresholds": {"min_count": 5, "reference_count": 30}}
    assert word in forward_part(strategy_page(with_forward(row)))


def test_an_unreadable_section_is_unknown_not_normal():
    for data in (with_forward(state="unavailable"), data_of()):
        text = forward_part(strategy_page(data))
        assert "機械採点を読めません" in text and "<table" not in text


def test_an_empty_list_says_there_is_no_deployed_strategy():
    text = forward_part(strategy_page(with_forward()))
    assert "配備中の戦略はありません" in text


def test_a_broken_value_becomes_unknown_in_its_own_cell_only():
    row = forward_row()
    row["series"]["all"]["avg_r"] = "x"
    row["reach"]["rate"] = None
    html = cells(strategy_page(with_forward(row)))[0]
    assert "不明" in html[5]
    assert "alpha" in html[0] and "6 / 0 / 0" in html[4]


def test_a_malformed_name_is_not_echoed():
    row = forward_row(name="Alpha<script>")
    html = forward_part(strategy_page(with_forward(row)))
    assert "<script>" not in html and "Alpha" not in html


def test_the_omitted_count_is_stated():
    html = forward_part(strategy_page(with_forward(forward_row(), omitted=3)))
    assert "ほか 3 件" in html


def test_the_section_stays_within_its_own_budget_with_the_worst_rows():
    rows = [forward_row(name=f"s{i:02d}" + "x" * 40, abandoned=9) for i in range(32)]
    for row in rows:
        row["holes"]["abandoned"] = {k: 9999 for k in (
            "freshness_expired", "requeued_expired", "retry_exhausted", "source_inactive",
            "normalization_failed", "deploy_op", "deploy_linked")}
    section = perf_pages._forward_section(with_forward(*rows), "Asia/Tokyo")
    assert len(section.encode("utf-8")) <= perf_pages.FORWARD_TABLE_BUDGET_BYTES
    assert perf_pages.FORWARD_TABLE_BUDGET_BYTES == 12 * 1024


def test_the_style_was_not_touched_so_the_hash_is_unchanged():
    from afx_ops.web import headers
    assert headers.STYLE_HASH == "sha256-G94HANarBHakVDo8Aw12kuCvlDOZ/Yt4Lipfz1zN8Xg="


def test_the_real_trade_table_is_unchanged_by_the_new_section():
    html = strategy_page(with_forward(forward_row()))
    assert "<h2>起動理由ごとの表</h2>" in html and html.index("起動理由ごとの表") < html.index(
        "機械採点 (配備中の戦略ごと)")


def _split_row():
    row = forward_row(n=40)
    row["series"]["accept"] = _series(12, pnl=-1.0)
    row["series"]["reject"] = _series(3, pnl=1.0)
    return row


def side_rows(html):
    return [c for c in cells(html) if len(c) == 6]


def test_the_accept_and_reject_series_are_shown_in_their_own_rows():
    rows = side_rows(strategy_page(with_forward(_split_row())))
    assert len(rows) == 2
    accept, reject = rows
    assert "alpha" in accept[0] and "accept" in accept[1]
    assert "12" in accept[2] and "0 / 12 / 0" in accept[3]
    assert "-1.00" in accept[4] and "損失なし" not in accept[5]
    assert "reject" in reject[1] and "3" in reject[2] and "3 / 0 / 0" in reject[3]


def test_a_small_side_series_shows_a_dash_and_a_reference_mark():
    rows = side_rows(strategy_page(with_forward(_split_row())))
    accept, reject = rows
    assert "(参考)" in accept[4]                      # 12 件は参考
    assert "–" in reject[4]                           # 3 件は「–」
    assert "+1.00" not in reject[4]


def test_the_side_rows_are_not_drawn_for_a_strategy_without_numbers():
    undetermined = {"name": "alpha", "content_hash": "a" * 64, "state": "undetermined",
                    "reason": "no_generation_record",
                    "thresholds": {"min_count": 5, "reference_count": 30}}
    assert side_rows(strategy_page(with_forward(undetermined))) == []


def test_the_side_rows_stay_within_the_same_budget():
    rows = [forward_row(name=f"s{i:02d}" + "x" * 40) for i in range(32)]
    section = perf_pages._forward_section(with_forward(*rows), "Asia/Tokyo")
    assert len(section.encode("utf-8")) <= perf_pages.FORWARD_TABLE_BUDGET_BYTES
