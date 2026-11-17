from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "bridge_metrics_report.py"
_SPEC = importlib.util.spec_from_file_location("bridge_metrics_report_module", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
report = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = report
_SPEC.loader.exec_module(report)


def _row(ts: str, *, caller="tick", route="/ohlcv/{symbol}", status=200, lock_state="measured",
         wait=1.0, hold=2.0, failure=None, error_code=None) -> dict:
    measured = lock_state in ("measured", "not_locked")
    return {
        "ts": ts, "caller": caller, "route": route, "status": status, "error_code": error_code,
        "metrics_version": "1", "boot_hash": "a" * 12, "lock_state": lock_state,
        "wait_ms": wait if measured else None, "hold_ms": hold if measured else None,
        "wait_max_ms": wait if measured else None, "hold_max_ms": hold if measured else None,
        "acquisitions": 1 if measured else None, "elapsed_ms": 10.0, "failure": failure,
        "history_state": None, "history_id_hash": None,
        "history_wait_ms": None, "history_hold_ms": None,
    }


def _write(root: Path, day: str, rows: list[dict], extra: list[str] | None = None) -> None:
    directory = root / "logs" / "bridge-metrics"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows] + (extra or [])
    (directory / f"{day}.jsonl").write_text("\n".join(lines) + "\n")


def test_percentile_is_nearest_rank_and_ignores_none():
    assert report.percentile([], 50) is None
    assert report.percentile([5.0], 95) == 5.0
    values = [float(i) for i in range(1, 101)]
    assert report.percentile(values, 50) == 50.0
    assert report.percentile(values, 95) == 95.0
    assert report.percentile(values, 100) == 100.0


def test_summary_groups_by_route_and_caller_with_counts(tmp_path):
    rows = [
        _row("2026-11-10T00:00:00.000Z", wait=1, hold=10),
        _row("2026-11-10T00:00:01.000Z", wait=3, hold=30),
        _row("2026-11-10T00:00:02.000Z", wait=5, hold=50),
        _row("2026-11-10T00:00:03.000Z", lock_state="missing", status=200),
        _row("2026-11-10T00:00:04.000Z", lock_state="unavailable"),
        _row("2026-11-10T00:00:05.000Z", failure="timeout", status=None, lock_state="missing"),
        _row("2026-11-10T00:00:06.000Z", failure="connect", status=None, lock_state="missing"),
        _row("2026-11-10T00:00:07.000Z", status=500, error_code="history_copy_failed",
             wait=5, hold=50),
        _row("2026-11-10T00:00:08.000Z", caller="mission", route="/health", wait=0, hold=0,
             lock_state="not_locked"),
    ]
    summary = report.summarize(rows)
    key = ("/ohlcv/{symbol}", "tick")
    group = summary[key]
    assert group["count"] == 8
    assert group["timeouts"] == 1
    assert group["failures"] == 1
    assert group["http_errors"] == 1
    assert group["lock_states"] == {"measured": 4, "missing": 3, "unavailable": 1}
    assert group["missing_rate"] == pytest.approx(4 / 8)
    assert group["wait_ms"]["max"] == 5.0 and group["hold_ms"]["max"] == 50.0
    assert group["hold_ms"]["p50"] == 30.0
    other = summary[("/health", "mission")]
    assert other["count"] == 1 and other["lock_states"] == {"not_locked": 1}
    assert other["missing_rate"] == 0.0


def test_friday_close_window_dst_and_standard_time():
    # 2026-10-30 is a Friday before the US switch back (close 21:00Z); 2026-11-13 is winter (22:00Z).
    assert report.friday_close_utc(date(2026, 10, 30)) == datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)
    assert report.friday_close_utc(date(2026, 11, 13)) == datetime(2026, 11, 13, 22, 0, tzinfo=timezone.utc)
    assert report.friday_close_utc(date(2026, 7, 10)) == datetime(2026, 7, 10, 21, 0, tzinfo=timezone.utc)
    assert report.friday_close_utc(date(2026, 3, 6)) == datetime(2026, 3, 6, 22, 0, tzinfo=timezone.utc)
    assert report.friday_close_utc(date(2026, 3, 13)) == datetime(2026, 3, 13, 21, 0, tzinfo=timezone.utc)
    assert report.friday_close_utc(date(2026, 11, 7)) is None


def test_friday_window_is_one_hour_before_close_to_close():
    rows = [
        _row("2026-11-13T20:59:59.000Z"),
        _row("2026-11-13T21:00:00.000Z"),
        _row("2026-11-13T21:59:59.000Z"),
        _row("2026-11-13T22:00:00.000Z"),
        _row("2026-11-13T22:00:01.000Z"),
        _row("2026-11-12T21:30:00.000Z"),
    ]
    picked = report.friday_close_rows(rows)
    assert [r["ts"] for r in picked] == [
        "2026-11-13T21:00:00.000Z", "2026-11-13T21:59:59.000Z", "2026-11-13T22:00:00.000Z"]


def test_load_rows_respects_period_and_skips_bad_lines(tmp_path):
    _write(tmp_path, "2026-11-09", [_row("2026-11-09T00:00:00.000Z")])
    _write(tmp_path, "2026-11-10", [_row("2026-11-10T00:00:00.000Z")],
           extra=["not json", "[1]", '{"ts": 5}'])
    _write(tmp_path, "2026-11-11", [_row("2026-11-11T00:00:00.000Z")])
    (tmp_path / "logs" / "bridge-metrics" / "notes.txt").write_text("x")
    rows, bad = report.load_rows(tmp_path / "logs" / "bridge-metrics",
                                 date(2026, 11, 10), date(2026, 11, 11))
    assert [r["ts"][:10] for r in rows] == ["2026-11-10", "2026-11-11"]
    assert bad == 3


def test_main_prints_both_tables_and_json(tmp_path, capsys):
    _write(tmp_path, "2026-11-13", [
        _row("2026-11-13T10:00:00.000Z"),
        _row("2026-11-13T21:30:00.000Z", failure="timeout", status=None, lock_state="missing"),
    ])
    assert report.main(["--root", str(tmp_path), "--from", "2026-11-13", "--to", "2026-11-13"]) == 0
    out = capsys.readouterr().out
    assert "/ohlcv/{symbol}" in out and "tick" in out
    assert "金曜閉場" in out
    assert report.main(["--root", str(tmp_path), "--from", "2026-11-13", "--to", "2026-11-13",
                        "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["overall"][0]["count"] == 2
    assert data["friday_close"][0]["count"] == 1
    assert data["rows"] == 2 and data["bad_lines"] == 0


def test_main_without_files_or_bad_arguments(tmp_path, capsys):
    assert report.main(["--root", str(tmp_path), "--from", "2026-11-13", "--to", "2026-11-13"]) == 0
    assert "0 件" in capsys.readouterr().out
    with pytest.raises(SystemExit) as caught:
        report.main(["--root", str(tmp_path), "--from", "2026-11-14", "--to", "2026-11-13"])
    assert caught.value.code == 2
    with pytest.raises(SystemExit):
        report.main(["--root", str(tmp_path), "--from", "garbage"])


def test_script_uses_only_the_standard_library():
    import ast

    tree = ast.parse(SCRIPT.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}
