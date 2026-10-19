"""変更要約の計算の端: diff の byte 予算、hunk の分割、展開の決定性、異常時の固定 reason、
派生物を plugins 配下に置かないこと、CLI の表示上限。"""
from __future__ import annotations

import os

import pytest

from agentic_fx.ops import change as C
from agentic_fx.ops import change_limits as L
from agentic_fx.ops import client

from ._change_fixtures import (approval_payload, insert_approval, make_live, make_staging,
                               make_version, write_files)
from .conftest import OPERATOR

HEADER = "@@ -1 +1 @@"
HEADER_COST = len(HEADER) + 1


def _entry(*hunks):
    return {"path": "plugin.py", "status": "changed", "reason": None,
            "hunks": [{"header": HEADER, "lines": list(lines)} for lines in hunks],
            "omitted_lines": 0}


@pytest.mark.parametrize("budget, kept", [(HEADER_COST - 1, 0), (HEADER_COST, 1)])
def test_diff_budget_counts_a_hunk_header_at_its_exact_size(budget, kept):
    entry = _entry([])
    C.apply_diff_budget([entry], budget)
    assert len(entry["hunks"]) == kept


@pytest.mark.parametrize("budget, kept", [(2 * HEADER_COST - 1, 1), (2 * HEADER_COST, 2)])
def test_diff_budget_carries_header_cost_from_one_hunk_to_the_next(budget, kept):
    entry = _entry([], [])
    C.apply_diff_budget([entry], budget)
    assert len(entry["hunks"]) == kept


def test_diff_budget_is_counted_in_utf8_bytes():
    line = "+日本語"
    cost = len(line.encode()) + 1
    fits, short = _entry([line]), _entry([line])
    assert C.apply_diff_budget([fits], HEADER_COST + cost) == 0
    assert fits["hunks"][0]["lines"] == [line]
    assert C.apply_diff_budget([short], HEADER_COST + cost - 1) == 1
    assert short["hunks"] == []


def _lines(count):
    return [f"line {i}" for i in range(count)]


def test_hunk_context_is_three_lines_each_side():
    old = _lines(30)
    new = list(old)
    new[15] = "changed"
    (hunk,) = C._hunks(old, new)
    assert hunk["lines"] == [" " + line for line in old[12:15]] + ["-" + old[15], "+changed"] \
        + [" " + line for line in old[16:19]]


def test_files_at_the_diff_line_limit_are_diffed_and_one_line_over_is_replaced_whole():
    old = _lines(L.DIFF_MAX_LINES)
    new = list(old)
    new[100] = "changed"
    (small,) = C._hunks(old, new)
    assert len(small["lines"]) == 2 * L.DIFF_CONTEXT + 2
    (whole,) = C._hunks(old + ["tail"], new + ["tail"])
    assert len(whole["lines"]) == 2 * (L.DIFF_MAX_LINES + 1)


def test_one_oversized_side_is_enough_to_replace_the_whole_file():
    old = ["a", "b", "c", "d", "e"]
    new = old + [f"n{i}" for i in range(L.DIFF_MAX_LINES)]
    (whole,) = C._hunks(old, new)
    assert [line for line in whole["lines"] if line.startswith("-")] == ["-" + x for x in old]
    assert not any(line.startswith(" ") for line in whole["lines"])
    (reverse,) = C._hunks(new, old)
    assert not any(line.startswith(" ") for line in reverse["lines"])


def test_performance_rows_are_capped_at_sixteen_pairs():
    metrics = {"trades": 1, "pf": 1.0, "avg_r": 0.0}
    rows = C._performance({f"P{i:02d}": metrics for i in range(20)})
    assert [row["pair"] for row in rows] == [f"P{i:02d}" for i in range(16)]


def test_an_empty_mapping_is_one_value_and_keeps_its_key():
    flat, omitted = C._flatten_config({"params": {"x": {}, "y": 1}})
    assert flat == {"params.x": "{}", "params.y": "1"} and omitted == 0


def test_items_kept_past_the_flatten_limit_are_the_first_in_sorted_key_order():
    keys = [f"k{i:03d}" for i in range(L.FLATTEN_MAX_ITEMS + 6)]
    params = {key: i for i, key in enumerate(reversed(keys))}
    flat, omitted = C._flatten_config({"params": params})
    assert sorted(flat) == [f"params.{key}" for key in keys[:L.FLATTEN_MAX_ITEMS]]
    assert omitted == 6


def test_a_param_key_that_looks_like_a_secret_is_redacted_in_the_key_column():
    token = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    old = f"params:\n  {token}: 1\n".encode()
    new = f"params:\n  {token}: 2\n".encode()
    (item,) = C._params_diff(old, new)["items"]
    assert item["key"] == f"params.{L.REDACTED}"
    assert token not in repr(item)


def _compute(tmp_path, payload, status="pending"):
    root = tmp_path / "plugins"
    root.mkdir(exist_ok=True)
    return C.compute_change(status=status, payload=payload, journal_rows=[], plugins_root=root,
                            now="2026-10-19T00:00:00+00:00", include_diff=True)


@pytest.mark.parametrize("override", [{"name": "Bad Name"}, {"name": None}, {"name": "../x"},
                                      {"artifact_hash": None}, {"artifact_hash": "a" * 63},
                                      {"artifact_hash": "A" * 64}])
def test_payload_with_an_unusable_name_or_hash_reports_unsafe_source(tmp_path, override):
    payload = approval_payload(digest="a" * 64)
    payload.update(override)
    result = _compute(tmp_path, payload)
    assert result.summary["reason"] == L.UNSAFE_SOURCE
    assert result.summary["classification"] == L.CLASS_UNKNOWN


def test_unexpected_exception_while_locating_the_candidate_is_a_fixed_code(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("/abs/secret/location")
    monkeypatch.setattr(C, "_candidate_source", boom)
    result = _compute(tmp_path, approval_payload(digest="a" * 64))
    assert result.summary["reason"] == L.UNSAFE_SOURCE
    assert "secret" not in repr(result)


def test_a_candidate_directory_that_is_a_symlink_is_unsafe_not_missing(tmp_path):
    root = tmp_path / "plugins"
    digest = write_files(tmp_path / "elsewhere")
    (root / "_staging" / "7").mkdir(parents=True)
    os.symlink(tmp_path / "elsewhere", root / "_staging" / "7" / "sma_cross")
    result = _compute(tmp_path, approval_payload(digest=digest))
    assert result.summary["reason"] == L.UNSAFE_SOURCE


def test_kind_outside_the_known_kinds_is_not_reported(tmp_path):
    result = _compute(tmp_path, approval_payload(kind="<b>evil</b>", digest="a" * 64))
    assert result.summary["kind"] is None


def _tree(root):
    out = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        out[str(path.relative_to(root))] = (info.st_mode, info.st_size, info.st_mtime_ns,
                                           os.readlink(path) if path.is_symlink() else None)
    return out


def test_reading_an_approval_leaves_the_plugins_tree_exactly_as_it_was(ops_env):
    plugins = ops_env.plugins_root
    old = make_version(plugins, "sma_cross", plugin_py="x = 1\n")
    make_live(plugins, "sma_cross", old)
    digest = make_staging(plugins, 7, "sma_cross", plugin_py="x = 2\n")
    approval_id = insert_approval(ops_env, approval_payload(digest=digest))
    before = _tree(plugins)
    service = ops_env.service()
    service.approval_detail(OPERATOR, approval_id)
    service.approval_change(OPERATOR, approval_id)
    assert _tree(plugins) == before


def _change_with(items, **over):
    change = {"name": "sma_cross", "kind": "strategy", "classification": "updated",
              "candidate_hash": "a" * 64, "baseline_hash": "b" * 64, "computed_at": "t",
              "reason": None, "params": {"items": items, "omitted": 0, "invalid_keys": 0,
                                         "flatten_omitted": 0, "reason": None}}
    change.update(over)
    return change


def test_cli_shows_at_most_the_params_diff_limit_rows():
    items = [{"key": f"params.k{i:03d}", "old": "1", "new": "2"}
             for i in range(L.PARAMS_DIFF_MAX + 5)]
    lines = client.format_change_lines(_change_with(items))
    assert sum(1 for line in lines if line.startswith("  params.k")) == L.PARAMS_DIFF_MAX


@pytest.mark.parametrize("name", ["Evil Name", "a\x1b[31mred", "x/../y", "", 5, None])
def test_cli_prints_a_dash_for_a_plugin_name_that_is_not_a_plugin_name(name):
    lines = client.format_change_lines(_change_with([], name=name))
    assert lines[1].startswith("plugin: - (strategy)")


@pytest.mark.parametrize("value", ["z" * 64, "a" * 63, "A" * 64, "a\x1b" + "a" * 62, 5, None])
def test_cli_prints_a_dash_for_a_hash_that_is_not_64_hex_digits(value):
    lines = client.format_change_lines(_change_with([], candidate_hash=value, baseline_hash=value))
    assert lines[2].startswith("候補: - 比較対象: - 計算時刻:")
