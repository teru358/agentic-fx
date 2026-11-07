"""表示 tz への換算の部品 (core/display_time.py) の test。

正本はこの repo の `src/agentic_fx/core/display_time.py` と
`tests/fixtures/display_time_vectors.json`。afx-ops 側の写しとは byte 一致で置く。
"""
from __future__ import annotations

import ast
import json
import re
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from agentic_fx import activity as activity_module
from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core import display_time as dt_mod
from agentic_fx.core.display_time import (
    ACTIVITY_CATEGORIES,
    BUILDERS,
    PRECISION_MILLISECOND,
    PRECISION_MINUTE,
    PRECISION_SECOND,
    TIME_FIELDS,
    bar_label,
    clear_zone_cache,
    convert_activity_line,
    convert_log_line,
    convert_or_raw,
    convert_time,
    convert_tokens,
    display_header,
    effective_tz_name,
    resolve_zone,
    utc_token,
)

REPO = Path(__file__).resolve().parents[2]
CANON_MODULE = REPO / "src" / "agentic_fx" / "core" / "display_time.py"
CANON_VECTORS = REPO / "tests" / "fixtures" / "display_time_vectors.json"
COPY_MODULE = REPO / "afx-ops" / "afx_ops" / "display_time.py"
COPY_VECTORS = REPO / "afx-ops" / "tests" / "fixtures" / "display_time_vectors.json"

UTC = timezone.utc


# ---- vector -----------------------------------------------------------------

def _load_vectors() -> list[dict]:
    return json.loads(CANON_VECTORS.read_text(encoding="utf-8"))["cases"]


def _input_value(case: dict):
    form = case["input_form"]
    if form == "string":
        return case["input"]
    if form == "datetime":
        return datetime.fromisoformat(case["input"])
    assert form == "raw", case["id"]
    return case["input"]


def _run_convert(case: dict) -> None:
    result = convert_time(_input_value(case), case["tz"], case["precision"])
    if case["expect"] is None:
        assert not result.ok and result.text == "", case["id"]
    else:
        assert result.ok and result.text == case["expect"], case["id"]


def _run_token(case: dict) -> None:
    assert convert_tokens(case["text"], case["tz"]) == case["expect"], case["id"]


def _run_activity_line(case: dict) -> None:
    assert convert_activity_line(case["line"], case["tz"]) == case["expect"], case["id"]


def _run_log_line(case: dict) -> None:
    assert convert_log_line(case["line"], case["tz"]) == case["expect"], case["id"]


def _run_header(case: dict) -> None:
    now = datetime.fromisoformat(case["now"])
    assert display_header(case["tz"], now) == case["expect"], case["id"]


RUNNERS = {
    "convert": _run_convert,
    "token": _run_token,
    "activity_line": _run_activity_line,
    "log_line": _run_log_line,
    "header": _run_header,
}


def test_vector_ids_are_unique():
    ids = [case["id"] for case in _load_vectors()]
    assert len(ids) == len(set(ids))


def test_every_vector_id_is_consumed_by_a_runner():
    cases = _load_vectors()
    consumed: set[str] = set()
    for case in cases:
        runner = RUNNERS.get(case["kind"])
        assert runner is not None, f"未知の kind: {case['kind']} ({case['id']})"
        runner(case)
        consumed.add(case["id"])
    assert consumed == {case["id"] for case in cases}
    assert len(cases) >= 100


def test_vector_covers_the_required_families():
    ids = {case["id"] for case in _load_vectors()}
    for needed in ("new_york_spring_before", "new_york_spring_after",
                   "new_york_fall_first_1_30", "new_york_fall_second_1_30",
                   "compat_name_japan", "overflow_max_year", "tz_empty", "tz_null",
                   "tz_zone_tab", "tz_too_long", "token_neg_offset_then_fraction",
                   "token_neg_z_then_offset", "token_neg_offset_seconds",
                   "token_neg_seven_digit_fraction", "token_before_colon_space",
                   "activity_neg_traceback_continuation", "activity_neg_six_fields_tab_in_body",
                   "activity_neg_truncated_mid_head", "activity_llm_body_untouched"):
        assert needed in ids


# ---- 正本と写しの byte 一致 ----------------------------------------------------

def test_copy_in_afx_ops_is_byte_identical_to_canonical_module():
    if not COPY_MODULE.exists():
        pytest.fail("afx-ops 側の写し display_time.py が無い (正本から写す)")
    assert COPY_MODULE.read_bytes() == CANON_MODULE.read_bytes()


def test_copy_in_afx_ops_is_byte_identical_to_canonical_vectors():
    if not COPY_VECTORS.exists():
        pytest.fail("afx-ops 側の写し display_time_vectors.json が無い (正本から写す)")
    assert COPY_VECTORS.read_bytes() == CANON_VECTORS.read_bytes()


def test_module_imports_only_the_standard_library():
    tree = ast.parse(CANON_MODULE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add((node.module or "").split(".")[0])
    assert names
    assert names <= set(sys.stdlib_module_names) | {"__future__"}


# ---- 換算 -------------------------------------------------------------------

def test_offset_comes_from_the_zone_after_the_shift_across_dst():
    before = convert_time("2026-03-08T06:59:59Z", "America/New_York")
    after = convert_time("2026-03-08T07:00:00Z", "America/New_York")
    assert before.text == "2026-03-08 01:59:59 -05:00"
    assert after.text == "2026-03-08 03:00:00 -04:00"
    first = convert_time("2026-11-01T05:30:00Z", "America/New_York")
    second = convert_time("2026-11-01T06:30:00Z", "America/New_York")
    assert first.text == "2026-11-01 01:30:00 -04:00"
    assert second.text == "2026-11-01 01:30:00 -05:00"


@pytest.mark.parametrize("precision, expected", [
    (PRECISION_MINUTE, "2026-11-06 19:04 +09:00"),
    (PRECISION_SECOND, "2026-11-06 19:04:05 +09:00"),
    (PRECISION_MILLISECOND, "2026-11-06 19:04:05,678 +09:00"),
])
def test_precision(precision, expected):
    assert convert_time("2026-11-06T10:04:05.678999Z", "Asia/Tokyo", precision).text == expected


def test_default_precision_is_seconds():
    assert convert_time("2026-11-06T10:04:05Z", "UTC").text == "2026-11-06 10:04:05 +00:00"


def test_aware_datetime_in_any_zone_is_accepted():
    jst = timezone(timedelta(hours=9))
    value = datetime(2026, 11, 6, 19, 4, 5, tzinfo=jst)
    assert convert_time(value, "UTC").text == "2026-11-06 10:04:05 +00:00"


def test_year_below_1000_is_zero_padded():
    result = convert_time(datetime(5, 1, 2, 3, 4, 5, tzinfo=UTC), "UTC")
    assert result.text == "0005-01-02 03:04:05 +00:00"


def test_offset_with_seconds_is_unconvertible():
    # 19 世紀の LMT は分に収まらない offset を持つ
    assert convert_time("1850-01-01T00:00:00Z", "Asia/Tokyo") == (False, "")


HOSTILE_VALUES = [None, 0, 1.5, b"x", [], {}, object(), "", "\x00", "9" * 5000,
                  datetime.max.replace(tzinfo=UTC), datetime.min.replace(tzinfo=UTC),
                  datetime(2026, 1, 1)]
HOSTILE_TZ = [None, 0, [], "", "\x00", "A" * 300, "zone.tab", "../x", "<b>", "Nowhere/City",
              "America", object()]


@pytest.mark.parametrize("value", HOSTILE_VALUES, ids=repr)
@pytest.mark.parametrize("tz", ["Asia/Tokyo", "UTC", *HOSTILE_TZ], ids=repr)
def test_no_function_raises_on_hostile_input(value, tz):
    convert_time(value, tz)
    convert_time(value, tz, "weird")
    convert_tokens(value, tz)
    convert_activity_line(value, tz)
    convert_log_line(value, tz)
    display_header(tz, value if isinstance(value, datetime) else None)
    effective_tz_name(tz)
    resolve_zone(tz)
    utc_token(value)
    bar_label(value)


def test_conversion_never_raises_for_the_extreme_datetimes():
    assert not convert_time(datetime.max.replace(tzinfo=UTC), "Asia/Tokyo").ok
    assert convert_time(datetime.max.replace(tzinfo=UTC), "UTC").ok


# ---- 表示 tz の見出し・解決 ----------------------------------------------------

def test_header_uses_the_offset_at_the_given_moment():
    assert display_header("America/New_York", datetime(2026, 7, 1, tzinfo=UTC)) == \
        "表示 tz: America/New_York (-04:00)"
    assert display_header("America/New_York", datetime(2026, 1, 1, tzinfo=UTC)) == \
        "表示 tz: America/New_York (-05:00)"


def test_header_without_a_moment_uses_now():
    header = display_header("Asia/Tokyo")
    assert header == "表示 tz: Asia/Tokyo (+09:00)"


def test_header_falls_back_to_utc():
    assert display_header("Nowhere/City") == "表示 tz: UTC (+00:00)"
    assert display_header(None) == "表示 tz: UTC (+00:00)"


def test_effective_tz_name():
    assert effective_tz_name("Asia/Tokyo") == "Asia/Tokyo"
    assert effective_tz_name("Nowhere/City") == "UTC"
    assert effective_tz_name(None) == "UTC"


def test_convert_or_raw_converts_when_it_can():
    assert convert_or_raw("2026-11-06T10:00:00+00:00", "Asia/Tokyo") == "2026-11-06 19:00:00 +09:00"
    assert convert_or_raw("2026-11-06T10:00:00+00:00", "Asia/Tokyo",
                          PRECISION_MINUTE) == "2026-11-06 19:00 +09:00"


@pytest.mark.parametrize("value", ["not a time", "2026-11-06T10:00:00", None, 5])
def test_convert_or_raw_returns_the_original_text_when_it_cannot(value):
    assert convert_or_raw(value, "Asia/Tokyo") == str(value)


def test_convert_or_raw_prefers_the_given_raw_text():
    assert convert_or_raw("junk", "Asia/Tokyo", raw="shown") == "shown"


def test_convert_or_raw_with_a_bad_zone_keeps_the_original_unless_utc_is_allowed():
    value = "2026-11-06T10:00:00+09:00"
    assert convert_or_raw(value, "Nowhere/City") == value
    assert convert_or_raw(value, "Nowhere/City", utc_fallback=True) == "2026-11-06 01:00:00 +00:00"
    assert convert_or_raw("junk", "Nowhere/City", utc_fallback=True, raw="?") == "?"


def test_convert_or_raw_never_raises_on_a_value_whose_str_fails():
    class Hostile:
        def __str__(self):
            raise RuntimeError("boom")

    assert convert_or_raw(Hostile(), "Asia/Tokyo") == "?"


def test_time_rule_cutover_body_is_the_stored_capture_serialisation():
    from agentic_fx.core import time_cutover
    capture = {"t_c": "2026-11-06T10:00:00+00:00", "captured_at": "2026-11-06T10:00:00+00:00",
               "orders": [{"id": 1, "pair": "EURUSD", "day_deadline": None, "note": "日本語"}],
               "daily_base": {"legacy": None, "new": {"equity": 1000.5, "snapshot_ts": "x"},
                              "applied": 3},
               "activity_logged": False}
    stored = {k: v for k, v in capture.items() if k != "activity_logged"}
    assert dt_mod.time_rule_cutover(capture)[2] == time_cutover._dump(stored)


def test_time_rule_cutover_body_does_not_stringify_what_cannot_be_serialised():
    body = dt_mod.time_rule_cutover({"t_c": datetime(2026, 11, 6, tzinfo=UTC)})[2]
    assert body == "(本文を組み立てられない)"


@pytest.fixture
def fresh_zone_cache():
    clear_zone_cache()
    yield
    clear_zone_cache()


def test_resolve_zone_does_not_need_a_zone_database_for_utc(fresh_zone_cache, monkeypatch):
    def broken(_name):
        raise OSError("no zone database")

    monkeypatch.setattr(dt_mod, "ZoneInfo", broken)
    assert resolve_zone("UTC") is not None
    assert resolve_zone("Asia/Tokyo") is None
    assert not convert_time("2026-11-06T10:04:05Z", "Asia/Tokyo").ok
    assert convert_tokens("2026-11-06T10:04:05Z", "Asia/Tokyo") == "2026-11-06T10:04:05Z"
    assert display_header("Asia/Tokyo") == "表示 tz: UTC (+00:00)"
    assert convert_time("2026-11-06T10:04:05Z", "UTC").ok


# ---- 自由文 token -----------------------------------------------------------

def test_token_conversion_is_at_seconds_precision_and_keeps_surroundings():
    text = "前 2026-11-06T10:04:05.999Z 後"
    assert convert_tokens(text, "Asia/Tokyo") == "前 2026-11-06 19:04:05 +09:00 後"


def test_token_only_converts_zero_offset():
    nonzero = "x 2026-11-06T10:04:05+09:00 y"
    assert convert_tokens(nonzero, "Asia/Tokyo") == nonzero


# ---- ログ行頭 ---------------------------------------------------------------

def test_activity_line_head_uses_a_real_activity_log_row(tmp_path):
    log = ActivityLog(tmp_path / "activity.log")
    log.write(Category.TRADE, "order_submitted", "buy EURUSD", ref_id="42")
    line = log.tail(1)[0]
    stamp = line.split("\t")[0]
    converted = convert_activity_line(line, "Asia/Tokyo")
    expected = convert_time(stamp, "Asia/Tokyo", PRECISION_SECOND).text
    assert converted.split("\t") == [expected, "TRADE", "order_submitted", "buy EURUSD", "42"]


def test_log_line_head_uses_a_real_agentic_log_row(tmp_path):
    from agentic_fx.logging_setup import setup_technical_logging

    logger = setup_technical_logging(tmp_path, level="INFO")
    try:
        logger.getChild("probe").info("hello %s", "world")
        for handler in logger.handlers:
            handler.flush()
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    line = (tmp_path / "agentic.log").read_text(encoding="utf-8").splitlines()[0]
    assert line.endswith("agentic_fx.probe: hello world")
    converted = convert_log_line(line, "UTC")
    assert converted.split(" +00:00 ")[1] == "INFO agentic_fx.probe: hello world"
    assert converted.split(" +00:00 ")[0] == line.split(" UTC ")[0]
    tokyo = convert_log_line(line, "Asia/Tokyo")
    assert tokyo.endswith(" +09:00 INFO agentic_fx.probe: hello world")


def test_activity_categories_match_activity_py():
    assert ACTIVITY_CATEGORIES == {c.value for c in Category}
    assert activity_module.Category is Category


def test_body_is_converted_only_for_events_in_the_builder_table():
    stamp = "2026-11-06T10:04:17+00:00"
    body = "x 2026-11-06T10:00:00Z y"
    for category, event in BUILDERS:
        line = "\t".join([stamp, category, event, body, "-"])
        assert convert_activity_line(line, "Asia/Tokyo").split("\t")[3] == \
            "x 2026-11-06 19:00:00 +09:00 y"
    other = "\t".join([stamp, "SYSTEM", "not_in_table", body, "-"])
    assert convert_activity_line(other, "Asia/Tokyo").split("\t")[3] == body


# ---- 時刻 field の集合 --------------------------------------------------------

def test_time_fields_contain_the_declared_initial_set():
    initial = {"ts", "created_at", "updated_at", "started_at", "latched_at", "decided_at",
               "computed_at", "observed_at", "effective_at", "deadline_at", "expires_at",
               "first_failed_at", "last_failed_at", "last_attempt_at",
               "expected_last_attempt_at", "restricted_since", "since", "deadline", "until"}
    assert initial <= TIME_FIELDS
    assert isinstance(TIME_FIELDS, frozenset)


def test_time_fields_look_like_time_names():
    pattern = re.compile(r"_at$|^ts$|deadline|until|since")
    assert all(pattern.search(name) for name in TIME_FIELDS)


# ---- utc_token / bar_label ----------------------------------------------------

def test_utc_token_is_seconds_with_z():
    assert utc_token(datetime(2026, 11, 6, 10, 4, 5, 999999, tzinfo=UTC)) == "2026-11-06T10:04:05Z"


def test_utc_token_moves_other_offsets_to_utc():
    jst = timezone(timedelta(hours=9))
    assert utc_token(datetime(2026, 11, 6, 19, 4, 5, tzinfo=jst)) == "2026-11-06T10:04:05Z"


def test_utc_token_marks_naive_instead_of_guessing():
    assert utc_token(datetime(2026, 11, 6, 10, 4, 5)) == "naive:2026-11-06T10:04:05"


def test_naive_token_is_not_converted_afterwards():
    text = f"x {utc_token(datetime(2026, 11, 6, 10, 4, 5))} y"
    assert convert_tokens(text, "Asia/Tokyo") == text


def test_utc_token_out_of_range_does_not_raise():
    far = datetime.min.replace(tzinfo=timezone(timedelta(hours=9)))
    assert utc_token(far).startswith("invalid:")


class _BrokenZone(tzinfo):
    def utcoffset(self, dt):
        raise OSError("zone data unavailable")

    def dst(self, dt):
        raise OSError("zone data unavailable")

    def tzname(self, dt):
        raise OSError("zone data unavailable")


def test_utc_token_and_bar_label_do_not_raise_when_the_tzinfo_fails():
    broken = datetime(2026, 11, 6, 10, 0, tzinfo=_BrokenZone())
    assert utc_token(broken) == "invalid:"
    assert bar_label(broken) == "invalid:"


class _Unprintable:
    def __str__(self):
        raise RuntimeError("no text")


def test_utc_token_and_bar_label_do_not_raise_for_an_unprintable_value():
    assert utc_token(_Unprintable()) == "?"
    assert bar_label(_Unprintable()) == "?"


def test_bar_label_has_minutes_and_utc():
    assert bar_label(datetime(2026, 11, 6, 10, 0, tzinfo=UTC)) == "2026-11-06T10:00 UTC"
    jst = timezone(timedelta(hours=9))
    assert bar_label(datetime(2026, 11, 6, 19, 0, tzinfo=jst)) == "2026-11-06T10:00 UTC"


def test_bar_label_is_never_converted_as_a_token():
    label = bar_label(datetime(2026, 11, 6, 10, 0, tzinfo=UTC))
    assert convert_tokens(f"bar={label}", "Asia/Tokyo") == f"bar={label}"


def test_bar_label_marks_naive():
    assert bar_label(datetime(2026, 11, 6, 10, 0)) == "naive:2026-11-06T10:00"


# ---- builder -----------------------------------------------------------------

T0 = datetime(2026, 11, 6, 10, 30, tzinfo=UTC)

SAMPLES = {
    ("NEWS", "news_fetch_skipped"): lambda: BUILDERS[("NEWS", "news_fetch_skipped")](T0),
    ("NEWS", "econ_fetch_skipped"): lambda: BUILDERS[("NEWS", "econ_fetch_skipped")](T0),
    ("NEWS", "news_source_skipped"):
        lambda: BUILDERS[("NEWS", "news_source_skipped")]("src: 先方の指示により ", T0),
    ("SYSTEM", "datafeed_restricted"):
        lambda: BUILDERS[("SYSTEM", "datafeed_restricted")](3, {"EURUSD"}, T0, T0 + timedelta(hours=2)),
    ("SYSTEM", "time_rule_cutover"):
        lambda: BUILDERS[("SYSTEM", "time_rule_cutover")]({"t_c": T0.isoformat(), "activity_logged": False}),
    ("SYSTEM", "kill_switch_reset"):
        lambda: BUILDERS[("SYSTEM", "kill_switch_reset")](4, T0.isoformat()),
}


def test_builder_table_is_exactly_the_documented_set():
    assert set(BUILDERS) == {
        ("NEWS", "news_fetch_skipped"), ("NEWS", "econ_fetch_skipped"),
        ("NEWS", "news_source_skipped"), ("SYSTEM", "datafeed_restricted"),
        ("SYSTEM", "time_rule_cutover"), ("SYSTEM", "kill_switch_reset")}
    assert set(SAMPLES) == set(BUILDERS)


@pytest.mark.parametrize("key", sorted(BUILDERS))
def test_builder_returns_its_own_category_and_event(key):
    category, event, body = SAMPLES[key]()
    assert (category, event) == key
    assert category in ACTIVITY_CATEGORIES
    assert isinstance(body, str) and body


@pytest.mark.parametrize("key", sorted(BUILDERS))
def test_builder_category_is_a_value_of_activity_category(key):
    assert Category(key[0]).value == key[0]


def test_builder_bodies():
    assert SAMPLES[("NEWS", "news_fetch_skipped")]()[2] == \
        "前回の取得から間もないので次回は 2026-11-06T10:30:00Z"
    assert SAMPLES[("NEWS", "econ_fetch_skipped")]()[2] == \
        "前回の取得から間もないので次回は 2026-11-06T10:30:00Z"
    assert SAMPLES[("NEWS", "news_source_skipped")]()[2] == \
        "src: 先方の指示により 2026-11-06T10:30:00Z まで待つ"
    assert SAMPLES[("SYSTEM", "datafeed_restricted")]()[2] == \
        "epoch=3 stalled=['EURUSD'] expected=2026-11-06T10:30:00Z deadline=2026-11-06T12:30:00Z"
    assert SAMPLES[("SYSTEM", "time_rule_cutover")]()[2] == '{"t_c": "2026-11-06T10:30:00+00:00"}'
    assert SAMPLES[("SYSTEM", "kill_switch_reset")]()[2] == \
        "human explicit reset via shell gen=4 latched_at=2026-11-06T10:30:00Z"


def test_builder_accepts_iso_strings_and_other_offsets():
    jst = timezone(timedelta(hours=9))
    _, _, body = BUILDERS[("SYSTEM", "datafeed_restricted")](
        1, ["B", "A"], "2026-11-06T10:00:00+00:00", datetime(2026, 11, 6, 21, 0, tzinfo=jst))
    assert body == "epoch=1 stalled=['A', 'B'] expected=2026-11-06T10:00:00Z deadline=2026-11-06T12:00:00Z"


def test_builder_marks_naive_times_and_does_not_raise():
    naive = datetime(2026, 11, 6, 10, 30)
    _, _, body = BUILDERS[("NEWS", "news_fetch_skipped")](naive)
    assert body.endswith("naive:2026-11-06T10:30:00")
    _, _, body = BUILDERS[("SYSTEM", "kill_switch_reset")](4, "2026-11-06T09:00:00")
    assert body.endswith("latched_at=naive:2026-11-06T09:00:00")
    _, _, body = BUILDERS[("SYSTEM", "kill_switch_reset")](4, None)
    assert body.endswith("latched_at=None")
    _, _, body = BUILDERS[("SYSTEM", "kill_switch_reset")](4, "garbage")
    assert body.endswith("latched_at=garbage")


def test_builder_does_not_raise_when_the_body_cannot_be_made():
    for args in ((3, 5, T0, T0), (3, [1, "a"], T0, T0)):
        category, event, body = BUILDERS[("SYSTEM", "datafeed_restricted")](*args)
        assert (category, event) == ("SYSTEM", "datafeed_restricted")
        assert body == "(本文を組み立てられない)"
    _, _, body = BUILDERS[("SYSTEM", "time_rule_cutover")]({"t": {1, 2}, "x": object()})
    assert body
    category, event, body = BUILDERS[("SYSTEM", "time_rule_cutover")](None)  # type: ignore[arg-type]
    assert (category, event) == ("SYSTEM", "time_rule_cutover")
    assert body == "(本文を組み立てられない)"


def test_cutover_builder_drops_only_the_logged_flag():
    _, _, body = BUILDERS[("SYSTEM", "time_rule_cutover")](
        {"b": 1, "a": "2026-11-06T10:00:00+00:00", "activity_logged": True})
    assert json.loads(body) == {"a": "2026-11-06T10:00:00+00:00", "b": 1}
    assert body.index('"a"') < body.index('"b"')


@pytest.mark.parametrize("key", sorted(BUILDERS))
def test_builder_to_activity_log_to_converted_display(tmp_path, key):
    category, event, body = SAMPLES[key]()
    log = ActivityLog(tmp_path / "activity.log")
    log.write(Category(category), event, body)
    line = log.tail(1)[0]
    converted = convert_activity_line(line, "Asia/Tokyo")
    fields = converted.split("\t")
    assert fields[1:3] == [category, event]
    assert fields[0].endswith(" +09:00")
    if key != ("SYSTEM", "time_rule_cutover"):
        assert "2026-11-06 19:30:00 +09:00" in fields[3]
    else:
        assert fields[3] == '{"t_c": "2026-11-06 19:30:00 +09:00"}'
    assert not re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]00:00)", fields[3])


def test_builder_output_survives_the_activity_log_whitespace_collapse(tmp_path):
    _, _, body = BUILDERS[("SYSTEM", "time_rule_cutover")]({"k": "a  b", "t": T0.isoformat()})
    log = ActivityLog(tmp_path / "activity.log")
    log.write(Category.SYSTEM, "time_rule_cutover", body)
    fields = log.tail(1)[0].split("\t")
    assert len(fields) == 5
    assert "2026-11-06T10:30:00+00:00" in fields[3]
