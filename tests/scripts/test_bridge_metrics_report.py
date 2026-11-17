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
        "lock_state": lock_state,
        "wait_ms": wait if measured else None, "hold_ms": hold if measured else None,
        "wait_max_ms": wait if measured else None, "hold_max_ms": hold if measured else None,
        "acquisitions": 1 if measured else None, "elapsed_ms": 10.0, "failure": failure,
        "timeout_s": 5.0,
    }


def _write(base: Path, suffix: str | None, rows: list[dict], extra: list[str] | None = None) -> Path:
    """`base` (書込み中の file) または `base.<日付>` (ローテーション済み) に行を書く。"""
    base.parent.mkdir(parents=True, exist_ok=True)
    path = base if suffix is None else base.with_name(f"{base.name}.{suffix}")
    lines = [json.dumps(r) for r in rows] + (extra or [])
    path.write_text("\n".join(lines) + "\n")
    return path


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


def test_friday_boundary_is_the_actual_open_to_close_edge_of_the_calendar():
    from agentic_fx.core import market_hours

    # 2026-12-18 is a normal winter Friday; 2026-12-25 is Friday but a holiday (already closed).
    normal = datetime(2026, 12, 18, 21, 30, tzinfo=timezone.utc)
    holiday = datetime(2026, 12, 25, 21, 30, tzinfo=timezone.utc)
    assert market_hours.is_market_open(normal) and not market_hours.is_market_open(holiday)
    picked = report.friday_close_rows([_row("2026-12-18T21:30:00.000Z"),
                                       _row("2026-12-25T21:30:00.000Z"),
                                       _row("2027-01-01T21:30:00.000Z")])
    assert [r["ts"] for r in picked] == ["2026-12-18T21:30:00.000Z"]
    assert report.friday_close_utc(date(2026, 12, 25)) is None
    assert report.friday_close_utc(date(2027, 1, 1)) is None
    assert report.friday_close_utc(date(2026, 12, 18)) == datetime(2026, 12, 18, 22, 0,
                                                                   tzinfo=timezone.utc)


def test_friday_window_agrees_with_the_calendar_for_every_friday_of_a_year():
    from datetime import timedelta
    from agentic_fx.core import market_hours

    day = date(2026, 1, 2)
    while day.year == 2026:
        close = report.friday_close_utc(day)
        edge = datetime(day.year, day.month, day.day, 22, 0, tzinfo=timezone.utc)
        for candidate in (edge - timedelta(hours=1), edge):
            opens_then_closes = (market_hours.is_market_open(candidate - timedelta(microseconds=1))
                                 and not market_hours.is_market_open(candidate))
            if opens_then_closes:
                assert close == candidate
        if close is not None:
            assert market_hours.is_market_open(close - timedelta(microseconds=1))
            assert not market_hours.is_market_open(close)
        day += timedelta(days=7)


def test_load_rows_reads_rotated_files_and_filters_by_the_row_timestamp(tmp_path):
    base = tmp_path / "bridge-metrics.jsonl"
    _write(base, "2026-11-09", [_row("2026-11-09T23:59:59.999Z"), _row("2026-11-10T00:00:00.000Z")],
           extra=["not json", "[1]", '{"ts": 5}'])
    _write(base, "2026-11-10", [_row("2026-11-10T12:00:00.000Z")])
    _write(base, None, [_row("2026-11-11T23:59:59.999Z"), _row("2026-11-12T00:00:00.000Z")])
    (tmp_path / "bridge-metrics.jsonl.bak").write_text(json.dumps(_row("2026-11-10T01:00:00.000Z")))
    (tmp_path / "unrelated.jsonl").write_text(json.dumps(_row("2026-11-10T01:00:00.000Z")))
    rows, bad = report.load_rows(base, date(2026, 11, 10), date(2026, 11, 11))
    assert sorted(r["ts"] for r in rows) == [
        "2026-11-10T00:00:00.000Z", "2026-11-10T12:00:00.000Z", "2026-11-11T23:59:59.999Z"]
    assert bad == 3


def test_load_rows_of_a_missing_file_is_empty(tmp_path):
    assert report.load_rows(tmp_path / "none.jsonl", date(2026, 11, 10), date(2026, 11, 11)) == ([], 0)


def _both(tmp_path: Path) -> tuple[Path, Path]:
    bridge = tmp_path / "bridge" / "bridge-metrics.jsonl"
    client = tmp_path / "logs" / "bridge-metrics" / "client.jsonl"
    _write(bridge, None, [
        _row("2026-11-13T10:00:00.000Z"),
        _row("2026-11-13T21:30:00.000Z", caller="cli", route="/ohlcv/{symbol}"),
    ])
    _write(client, None, [
        _row("2026-11-13T10:00:00.000Z"),
        _row("2026-11-13T21:30:00.000Z", failure="timeout", status=None, lock_state="missing"),
        _row("2026-11-13T21:31:00.000Z", failure="timeout", status=None, lock_state="missing"),
    ])
    return bridge, client


def test_main_aggregates_the_bridge_file_and_the_client_file_separately(tmp_path, capsys):
    bridge, client = _both(tmp_path)
    args = ["--bridge-file", str(bridge), "--client-file", str(client),
            "--from", "2026-11-13", "--to", "2026-11-13"]
    assert report.main(args) == 0
    out = capsys.readouterr().out
    assert "bridge 側" in out and "client 側" in out and "金曜閉場" in out
    assert report.main(args + ["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["bridge"]["rows"] == 2 and data["client"]["rows"] == 3
    assert data["bridge"]["overall"][0]["count"] >= 1
    assert sum(g["count"] for g in data["bridge"]["friday_close"]) == 1
    assert sum(g["timeouts"] for g in data["client"]["friday_close"]) == 2
    assert data["client"]["bad_lines"] == 0


def test_main_defaults_to_the_root_layout_and_survives_missing_files(tmp_path, capsys):
    _write(tmp_path / "logs" / "bridge-metrics" / "client.jsonl", None,
           [_row("2026-11-13T10:00:00.000Z")])
    assert report.main(["--root", str(tmp_path), "--from", "2026-11-13", "--to", "2026-11-13",
                        "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["client"]["rows"] == 1 and data["bridge"]["rows"] == 0
    assert report.main(["--root", str(tmp_path / "empty"), "--from", "2026-11-13",
                        "--to", "2026-11-13"]) == 0
    assert "0 件" in capsys.readouterr().out
    with pytest.raises(SystemExit) as caught:
        report.main(["--root", str(tmp_path), "--from", "2026-11-14", "--to", "2026-11-13"])
    assert caught.value.code == 2
    with pytest.raises(SystemExit):
        report.main(["--root", str(tmp_path), "--from", "garbage"])


def test_script_uses_only_the_standard_library_and_the_market_calendar():
    import ast

    tree = ast.parse(SCRIPT.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__", "agentic_fx"}
    imported = {node.module for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
                and node.module.split(".")[0] == "agentic_fx"}
    assert imported == {"agentic_fx.core"} or imported == {"agentic_fx.core.market_hours"}
