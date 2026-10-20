"""変更要約の計算量・展開・応答予算の境界。"""
from __future__ import annotations

import json
import time

from agentic_fx.ops import change as C
from agentic_fx.ops import change_limits as L

from ._change_fixtures import (approval_payload, insert_approval, make_live, make_staging,
                               make_version)
from .conftest import OPERATOR

NAME = "sma_cross"


def test_huge_near_identical_files_are_degraded_without_a_quadratic_diff(ops_env):
    same = "x\n" * 20000
    old_hash = make_version(ops_env.plugins_root, NAME, plugin_py=same, config=same, test=same)
    make_live(ops_env.plugins_root, NAME, old_hash)
    changed = "x\n" * 19999 + "y\n"
    new_hash = make_staging(ops_env.plugins_root, 7, NAME, plugin_py=changed, config=changed,
                            test=changed)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=new_hash))

    started = time.monotonic()
    response = ops_env.service().approval_change(OPERATOR, approval_id)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    files = response["diff"]["files"]
    assert [f["status"] for f in files] == ["changed"] * 3
    assert all(f["diff_degraded"] == L.DIFF_DEGRADED_TOO_MANY_LINES for f in files)
    assert len(json.dumps(response).encode()) <= L.API_DIFF_BYTES


def test_a_file_at_the_cell_limit_is_diffed_and_one_over_is_replaced_whole():
    side = int(L.DIFF_MAX_CELLS ** 0.5)
    old = [f"l{i}" for i in range(side)]
    new = list(old)
    new[10] = "changed"
    (hunk,) = C._hunks(old, new)
    assert len(hunk["lines"]) == 2 * L.DIFF_CONTEXT + 2
    assert not C._diff_degraded(old, new)
    assert C._diff_degraded(old + ["tail"], new + ["tail"])
    (whole,) = C._hunks(old + ["tail"], new + ["tail"])
    assert not any(line.startswith(" ") for line in whole["lines"])


def test_a_diffed_file_carries_no_degraded_note():
    entry = C._file_diff("plugin.py", b"a\nb\n", b"a\nc\n")
    assert "diff_degraded" not in entry


def test_flatten_compares_the_same_key_set_on_both_sides():
    count = L.FLATTEN_MAX_ITEMS + 6
    old = {f"k{i:03d}": i for i in range(count)}
    new = dict(old)
    new["a000"] = 1
    old_yaml = ("params:\n" + "".join(f"  {k}: {v}\n" for k, v in old.items())).encode()
    new_yaml = ("params:\n" + "".join(f"  {k}: {v}\n" for k, v in new.items())).encode()
    result = C._params_diff(old_yaml, new_yaml)
    keys = [item["key"] for item in result["items"]]
    assert keys == ["params.a000"]
    assert result["flatten_omitted"] == count + 1 - L.FLATTEN_MAX_ITEMS


def _oversized(change_extra=None):
    big = "z" * 4000
    change = {"classification": "updated", "reason": None,
              "params": {"items": [{"key": f"k{i}", "old": big, "new": big}
                                   for i in range(10)]}}
    change.update(change_extra or {})
    return {"id": 1, "change": change,
            "claims": {"summary": big, "selection_rationale": big},
            "diff": {"files": [{"path": "plugin.py", "status": "changed", "reason": None,
                                "hunks": [{"header": "@@ -1 +1 @@", "lines": ["+a"]}],
                                "omitted_lines": 0}], "omitted_lines": 0}}


def test_fit_response_drops_claims_then_params_items_before_giving_up_the_summary():
    response = _oversized()
    limit = 20000
    C.fit_response_to_bytes(response, limit)
    assert len(json.dumps(response).encode()) <= limit
    assert response["diff"]["files"] == []
    assert response["change"]["classification"] == "updated"
    assert response["change"]["reason"] is None


def test_fit_response_falls_back_to_reason_only_when_nothing_else_fits():
    response = _oversized({"performance": {"holdout": ["z" * 4000] * 3}})
    limit = 2000
    C.fit_response_to_bytes(response, limit)
    assert len(json.dumps(response).encode()) <= limit
    assert response["change"] == {"classification": "updated", "reason": L.TOO_LARGE}
    assert response["claims"] == {} and response["diff"]["files"] == []
