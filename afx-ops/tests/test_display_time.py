"""表示 tz への換算の部品 (afx_ops/display_time.py、main 側の写し) の test。

正本は repo の `src/agentic_fx/core/display_time.py` と
`tests/fixtures/display_time_vectors.json`。この package の写しは直接編集せず、
正本と byte 一致で置く。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

import pytest

from afx_ops import display_time as dt_mod
from afx_ops.display_time import (
    convert_activity_line,
    convert_log_line,
    convert_time,
    convert_tokens,
    display_header,
)

HERE = Path(__file__).resolve().parent
COPY_MODULE = Path(dt_mod.__file__).resolve()
COPY_VECTORS = HERE / "fixtures" / "display_time_vectors.json"
REPO = HERE.parents[1]
CANON_MODULE = REPO / "src" / "agentic_fx" / "core" / "display_time.py"
CANON_VECTORS = REPO / "tests" / "fixtures" / "display_time_vectors.json"

_PIP_TZDATA = importlib.util.find_spec("tzdata") is not None


def _load_vectors() -> list[dict]:
    return json.loads(COPY_VECTORS.read_text(encoding="utf-8"))["cases"]


def _input_value(case: dict):
    form = case["input_form"]
    if form == "datetime":
        return datetime.fromisoformat(case["input"])
    assert form in ("string", "raw"), case["id"]
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

# system の zoneinfo に互換名が無い環境では、互換名の vector は pip の tzdata が
# 入るまで評価できない。その id は「評価しなかった」ことを別の test で明示する。
_NEEDS_PIP_TZDATA = {case["id"] for case in _load_vectors() if case.get("needs_pip_tzdata")}


def test_vector_ids_are_unique():
    ids = [case["id"] for case in _load_vectors()]
    assert len(ids) == len(set(ids))


def test_every_vector_id_is_consumed_by_a_runner():
    cases = _load_vectors()
    consumed: set[str] = set()
    for case in cases:
        runner = RUNNERS.get(case["kind"])
        assert runner is not None, f"未知の kind: {case['kind']} ({case['id']})"
        if case["id"] in _NEEDS_PIP_TZDATA and not _PIP_TZDATA:
            consumed.add(case["id"])  # 評価は test_compat_zone_vectors_need_pip_tzdata が受け持つ
            continue
        runner(case)
        consumed.add(case["id"])
    assert consumed == {case["id"] for case in cases}
    assert len(cases) >= 100


def test_compat_zone_vectors_need_pip_tzdata():
    if not _PIP_TZDATA:
        warnings.warn(
            "pip の tzdata が未導入のため互換名 (Japan など) の vector を評価していない: "
            + ", ".join(sorted(_NEEDS_PIP_TZDATA))
            + " (afx-ops が tzdata に依存した時点で評価に変わる)", stacklevel=1)
        return
    for case in _load_vectors():
        if case["id"] in _NEEDS_PIP_TZDATA:
            RUNNERS[case["kind"]](case)


def test_copy_is_byte_identical_to_canonical_module():
    if not CANON_MODULE.exists():
        warnings.warn("正本 (src/agentic_fx/core/display_time.py) が無い配布形: "
                      "byte 一致は検査していない", stacklevel=1)
        return
    assert COPY_MODULE.read_bytes() == CANON_MODULE.read_bytes()


def test_copy_is_byte_identical_to_canonical_vectors():
    if not CANON_VECTORS.exists():
        warnings.warn("正本 (tests/fixtures/display_time_vectors.json) が無い配布形: "
                      "byte 一致は検査していない", stacklevel=1)
        return
    assert COPY_VECTORS.read_bytes() == CANON_VECTORS.read_bytes()


def test_module_imports_only_the_standard_library():
    tree = ast.parse(COPY_MODULE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            names.add((node.module or "").split(".")[0])
    assert names
    assert names <= set(sys.stdlib_module_names) | {"__future__"}
