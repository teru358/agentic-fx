from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_fx.store.db import connect, init_db
from tests.plugin.conftest import SETTINGS

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "scripts" / "floor_remeasure.py"
_SPEC = importlib.util.spec_from_file_location("floor_remeasure_module", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
floor = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = floor
_SPEC.loader.exec_module(floor)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _approval(conn, approval_id: int, status: str, *, name="s", kind="strategy",
              content_hash="c" * 64, artifact_hash="a" * 64, mission_id=7,
              plugin_kind="plugin") -> None:
    payload = {"name": name, "kind": kind, "content_hash": content_hash,
               "artifact_hash": artifact_hash, "mission_id": mission_id}
    conn.execute(
        "INSERT INTO approval_requests(id, kind, payload_json, status, created_at) "
        "VALUES (?,?,?,?,?)",
        (approval_id, plugin_kind, json.dumps(payload), status, NOW.isoformat()))
    conn.commit()


def _run(conn, *, content_hash, pair="USDJPY", trades=100, pf=1.2, avg_r=0.1,
         dd=0.05, created=NOW - timedelta(days=1), plugin_ref="plugins/_staging/9/x",
         mission_id=9, scope="in_sample", variant="candidate",
         issued_by="harness") -> int:
    metrics = {"trades": trades, "pf": pf, "avg_r": avg_r, "max_drawdown": dd}
    cur = conn.execute(
        "INSERT INTO backtest_runs(plugin_ref, content_hash, kind, pair, timeframe, "
        "source, base_interval, params_json, period_start, period_end, scope, "
        "issued_by, metrics_json, settings_hash, core_commit, initial_balance, "
        "created_at, variant, mission_id) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (plugin_ref, content_hash, "strategy", pair, "1h", "mt5", "5m", "{}",
         created.isoformat(), created.isoformat(), scope, issued_by,
         json.dumps(metrics), "s", "c", 1.0, created.isoformat(), variant,
         mission_id))
    conn.commit()
    return cur.lastrowid


def test_approved_targets_take_only_approved_strategies_plus_named_rejection(tmp_path):
    conn = _conn(tmp_path)
    _approval(conn, 21, "approved", name="a", content_hash="1" * 64)
    _approval(conn, 22, "approved", name="ind", kind="indicator")
    _approval(conn, 23, "pending", name="p", content_hash="3" * 64)
    _approval(conn, 24, "rejected", name="r", content_hash="4" * 64)
    _approval(conn, 84, "rejected", name="us500", content_hash="8" * 64)
    targets = floor.select_approved_targets(conn)
    assert [(t.label, t.role) for t in targets] == [
        ("approval-21", "approved"), ("approval-84", "rejected_reference")]
    assert targets[0].artifact_hash == "a" * 64 and targets[0].mission_id == 7


def test_approved_targets_skip_unreadable_payloads(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO approval_requests(id, kind, payload_json, status, "
                 "created_at) VALUES (1,'plugin','not json','approved','x')")
    conn.execute("INSERT INTO approval_requests(id, kind, payload_json, status, "
                 "created_at) VALUES (2,'plugin','{\"kind\":\"strategy\"}','approved','x')")
    conn.commit()
    assert floor.select_approved_targets(conn) == []


def test_deep_drawdown_candidate_needs_floor_pass_and_dd_over_limit(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, content_hash="a" * 64, dd=0.15)                      # DD 以内
    _run(conn, content_hash="b" * 64, dd=0.30, pf=0.9)              # 床不合格
    _run(conn, content_hash="c" * 64, dd=0.30, avg_r=-0.1)          # 床不合格
    _run(conn, content_hash="d" * 64, dd=0.30, trades=10)           # 取引数不足
    _run(conn, content_hash="e" * 64, dd=0.30,
         created=NOW - timedelta(days=61))                          # 窓の外
    _run(conn, content_hash="f" * 64, dd=0.30, variant="no_strategy")
    _run(conn, content_hash="9" * 64, dd=0.30, scope="holdout_gate")
    wanted = _run(conn, content_hash="1" * 64, dd=0.31,
                  plugin_ref="plugins/_staging/9/deep_one")
    target = floor.select_deep_drawdown_candidate(
        conn, now=NOW, exclude_hashes=set(), settings=SETTINGS)
    assert target is not None
    assert (target.content_hash, target.name, target.source_row_id,
            target.label, target.role) == (
        "1" * 64, "deep_one", wanted, f"run-{wanted}", "deep_drawdown")


def test_deep_drawdown_candidate_dd_boundary_is_strictly_over(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, content_hash="a" * 64, dd=0.20)
    assert floor.select_deep_drawdown_candidate(
        conn, now=NOW, exclude_hashes=set(), settings=SETTINGS) is None
    _run(conn, content_hash="b" * 64, dd=0.2000001)
    found = floor.select_deep_drawdown_candidate(
        conn, now=NOW, exclude_hashes=set(), settings=SETTINGS)
    assert found is not None and found.content_hash == "b" * 64


def test_deep_drawdown_candidate_skips_excluded_hash_and_picks_newest(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, content_hash="a" * 64, dd=0.30)
    _run(conn, content_hash="b" * 64, dd=0.30)
    _run(conn, content_hash="c" * 64, dd=0.30)
    target = floor.select_deep_drawdown_candidate(
        conn, now=NOW, exclude_hashes={"c" * 64}, settings=SETTINGS)
    assert target is not None and target.content_hash == "b" * 64


def test_deep_drawdown_candidate_judges_every_pair_of_a_hash(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, content_hash="a" * 64, pair="USDJPY", dd=0.30)
    _run(conn, content_hash="a" * 64, pair="EURUSD", dd=0.05, pf=0.8)
    assert floor.select_deep_drawdown_candidate(
        conn, now=NOW, exclude_hashes=set(), settings=SETTINGS) is None


def test_deep_drawdown_limit_and_floor_come_from_the_gate_settings(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, content_hash="a" * 64, dd=0.30)
    settings = SETTINGS.model_copy(deep=True)
    settings.improve.gate.max_drawdown = 0.35
    assert floor.select_deep_drawdown_candidate(
        conn, now=NOW, exclude_hashes=set(), settings=settings) is None
    settings.improve.gate.max_drawdown = 0.10
    assert floor.select_deep_drawdown_candidate(
        conn, now=NOW, exclude_hashes=set(), settings=settings) is not None
    settings.improve.gate.min_pf = 1.5
    assert floor.select_deep_drawdown_candidate(
        conn, now=NOW, exclude_hashes=set(), settings=settings) is None


def test_deep_drawdown_candidate_skips_unreadable_rows_and_reports_them(tmp_path):
    conn = _conn(tmp_path)
    bad_json = _run(conn, content_hash="a" * 64, dd=0.30)
    conn.execute("UPDATE backtest_runs SET metrics_json='not json' WHERE id=?",
                 (bad_json,))
    no_pf = _run(conn, content_hash="b" * 64, dd=0.30)
    conn.execute("UPDATE backtest_runs SET metrics_json=? WHERE id=?",
                 (json.dumps({"trades": 100, "avg_r": 0.1, "max_drawdown": 0.3}), no_pf))
    not_dict = _run(conn, content_hash="c" * 64, dd=0.30)
    conn.execute("UPDATE backtest_runs SET metrics_json='[1]' WHERE id=?", (not_dict,))
    wanted = _run(conn, content_hash="d" * 64, dd=0.30)
    conn.commit()
    skipped: list[int] = []
    target = floor.select_deep_drawdown_candidate(
        conn, now=NOW, exclude_hashes=set(), settings=SETTINGS, skipped=skipped)
    assert target is not None and target.source_row_id == wanted
    assert sorted(skipped) == sorted([bad_json, no_pf, not_dict])


def _artifact(root: Path, *parts: str, meta: dict | None = None) -> Path:
    path = root.joinpath(*parts)
    path.mkdir(parents=True)
    if meta is not None:
        (path / "meta.json").write_text(json.dumps(meta))
    return path


def test_locate_artifact_prefers_version_store_then_archive(tmp_path):
    target = floor.Target(label="x", name="s", content_hash="c" * 64,
                          mission_id=7, artifact_hash="a" * 64)
    assert floor.locate_artifact(tmp_path, target) is None
    archive = _artifact(tmp_path, "_archive", "7", "a" * 64)
    assert floor.locate_artifact(tmp_path, target) == (archive, "archive")
    versions = _artifact(tmp_path, ".versions", "s", "a" * 64)
    assert floor.locate_artifact(tmp_path, target) == (versions, "versions")


def test_locate_artifact_without_artifact_hash_matches_archive_meta(tmp_path):
    target = floor.Target(label="x", name="s", content_hash="c" * 64,
                          mission_id=9)
    _artifact(tmp_path, "_archive", "9", "1" * 64, meta={"content_hash": "d" * 64})
    wanted = _artifact(tmp_path, "_archive", "9", "2" * 64,
                       meta={"content_hash": "c" * 64})
    assert floor.locate_artifact(tmp_path, target) == (wanted, "archive")


def test_locate_artifact_never_crosses_missions_or_names(tmp_path):
    target = floor.Target(label="x", name="s", content_hash="c" * 64,
                          mission_id=7, artifact_hash="a" * 64)
    _artifact(tmp_path, "_archive", "8", "a" * 64)
    _artifact(tmp_path, ".versions", "other", "a" * 64)
    assert floor.locate_artifact(tmp_path, target) is None


def test_reference_row_is_the_latest_in_sample_candidate_row(tmp_path):
    conn = _conn(tmp_path)
    _run(conn, content_hash="a" * 64, trades=10, dd=0.1)
    latest = _run(conn, content_hash="a" * 64, trades=20, dd=0.2)
    _run(conn, content_hash="a" * 64, trades=99, scope="holdout_gate")
    ref = floor.reference_row(conn, "a" * 64, "USDJPY")
    assert ref is not None
    assert (ref["id"], ref["trades"], ref["max_drawdown"]) == (latest, 20, 0.2)
    assert floor.reference_row(conn, "b" * 64, "USDJPY") is None


@pytest.mark.parametrize("trades,bars,expected", [
    (864, 1938, 864 / 1938), (0, 100, 0.0), (5, 0, None), (5, -1, None),
    (True, 10, None), (5, True, None), (5.0, 10, None), ("5", 10, None),
    (-1, 10, None), (5, None, None), (None, 10, None),
])
def test_trades_per_eval_bar_accepts_only_non_negative_int_trades(trades, bars, expected):
    assert floor.trades_per_eval_bar(trades, bars) == expected


def _item(role, name, freq, *, exclusion=None, approval_id=None, source_row_id=None):
    run = {"pair": "USDJPY", "timeframe": "1h", "source": "mt5",
           "base_interval": "5m", "run_id": None, "now": "2026-11-01T12:00:00+00:00",
           "start": "2026-01-01T00:00:00+00:00", "end": "2026-08-01T00:00:00+00:00",
           "settings_hash": "s" * 64, "core_commit": "c" * 40,
           "eval_bars": 1000, "trades": int(freq * 1000) if freq is not None else 0,
           "frequency": freq, "max_drawdown": 0.1, "pf": 1.1, "avg_r": 0.1,
           "reference": {"id": 5, "created_at": "2026-10-01T00:00:00+00:00",
                         "trades": 99, "max_drawdown": 0.12}}
    return {"role": role, "name": name, "content_hash": "h" * 64,
            "artifact_hash": "a" * 64, "artifact_kind": "versions",
            "artifact_path": f"plugins/.versions/{name}/x", "approval_id": approval_id,
            "source_row_id": source_row_id, "label": name,
            "runs": [] if exclusion else [run], "exclusion": exclusion}


def test_frequency_summary_takes_max_per_role_and_ignores_exclusions():
    results = [_item("approved", "a", 0.03, approval_id=1),
               _item("approved", "b", 0.11, approval_id=2),
               _item("approved", "gone", None, exclusion="artifact 欠落"),
               _item("rejected_reference", "r", 0.45, approval_id=84),
               _item("deep_drawdown", "d", 0.44, source_row_id=9)]
    assert floor.frequency_summary(results) == {
        "approved": 0.11, "rejected_reference": 0.45, "deep_drawdown": 0.44}


def test_render_manifest_lists_every_item_and_each_exclusion_with_reason():
    results = [_item("approved", "a", 0.031, approval_id=21),
               _item("approved", "gone", None, exclusion="artifact 欠落",
                     approval_id=22),
               _item("deep_drawdown", "d", 0.444, source_row_id=458)]
    text = floor.render_manifest(results, header={"mode": "dry-run"})
    for needle in ("## 識別", "## 新規実行の条件 (in-sample)",
                   "## 結果 (新規実行) と参考列 (元行)", "## 除外",
                   "#21", "#22", "row 458", "artifact 欠落", "0.031", "0.444",
                   "dry-run (未保存)", "1000"):
        assert needle in text, needle
    exclusion_section = text.split("## 除外")[1].split("## 頻度の要約")[0]
    assert "gone" in exclusion_section and "| a |" not in exclusion_section


def test_render_manifest_without_exclusions_says_none():
    text = floor.render_manifest([_item("approved", "a", 0.03, approval_id=1)],
                                 header={})
    assert text.split("## 除外")[1].split("##")[0].strip() == "なし"


def test_render_manifest_never_mentions_holdout():
    text = floor.render_manifest([_item("approved", "a", 0.03, approval_id=1)],
                                 header={})
    assert "holdout" not in text.replace("holdout の値・期間は含まない", "")


def test_driver_never_calls_the_holdout_runner_or_writes_scope_in_sample():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "run_holdout_gate" not in source
    assert "save_harness_run" not in source
    assert "save_human_run" in source


def test_render_command_rebuilds_manifest_from_results_file(tmp_path):
    results = tmp_path / "r.jsonl"
    old = _item("approved", "a", 0.9, approval_id=1)
    old["label"] = "approval-1"
    new = _item("approved", "a", 0.03, approval_id=1)
    new["label"] = "approval-1"
    results.write_text("\n".join(json.dumps(x) for x in (old, new)) + "\n",
                       encoding="utf-8")
    out = tmp_path / "m.md"
    assert floor.main(["render", "--results", str(results), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "0.030" in text and "0.900" not in text


# ---- run_target (依存を差し替えた配線 test) ---------------------------------

def _fake_environment(monkeypatch, tmp_path, *, pairs, fail_pair=None):
    from types import SimpleNamespace

    from agentic_fx.backtest import holdout
    from agentic_fx.plugin import loader as plugin_loader
    from agentic_fx.plugin import sandbox as plugin_sandbox
    from agentic_fx.plugin import strategy_adapter
    from agentic_fx.plugin import resolve
    from agentic_fx.store import backtest_runs
    from agentic_fx.tools import plugin_loader as tools_plugin_loader

    meta = SimpleNamespace(
        path=tmp_path, name="s", content_hash="h" * 64, artifact_hash="a" * 64,
        kind="strategy", timeframe="1h", pairs=pairs)
    monkeypatch.setattr(floor, "locate_artifact", lambda root, t: (tmp_path, "versions"))
    monkeypatch.setattr(plugin_loader, "discover_one_with_reason",
                        lambda path, name: (meta, None))
    monkeypatch.setattr(plugin_sandbox, "check_source", lambda path: None)
    monkeypatch.setattr(tools_plugin_loader, "approved_plugins",
                        lambda *a, **k: SimpleNamespace(inventory={}))
    monkeypatch.setattr(resolve, "resolve_indicator_deps", lambda *a, **k: {})
    monkeypatch.setattr(holdout, "require_reference_history", lambda *a, **k: None)
    monkeypatch.setattr(strategy_adapter, "build_intent_source",
                        lambda *a, **k: SimpleNamespace(close=lambda: None))
    saved: list[str] = []
    next_id = iter(range(500, 600))

    def fake_save(conn, **kwargs):
        saved.append(kwargs["pair"])
        return next(next_id)

    monkeypatch.setattr(backtest_runs, "save_human_run", fake_save)

    def fake_run_in_sample(settings, *, symbol, record_fn, now, **kwargs):
        if symbol == fail_pair:
            raise RuntimeError("boom")
        record_fn({
            "scope": "in_sample", "pair": symbol, "source": "mt5",
            "base_interval": "5m", "now": now,
            "period": (now - timedelta(days=30), now),
            "settings_hash": "s", "core_commit": "c",
            "metrics": {"trades": 10, "max_drawdown": 0.1, "pf": 1.2,
                        "avg_r": 0.1, "eval_bars": 100}})

    monkeypatch.setattr(holdout, "run_in_sample", fake_run_in_sample)
    settings = SimpleNamespace(backtest=SimpleNamespace(
        dataset=lambda: SimpleNamespace(unconfigured=False)))
    return settings, saved


def _target():
    return floor.Target(label="approval-1", name="s", content_hash="h" * 64,
                        mission_id=1, artifact_hash="a" * 64, approval_id=1)


def test_run_target_records_the_id_the_sink_received_not_a_table_maximum(
        monkeypatch, tmp_path):
    ro = _conn(tmp_path)
    settings, saved = _fake_environment(monkeypatch, tmp_path,
                                        pairs=["USDJPY", "EURUSD"])
    record = floor.run_target(_target(), root=tmp_path, settings=settings,
                              ro_conn=ro, write_conn=object(), now=NOW)
    assert record["exclusion"] is None
    assert [r["run_id"] for r in record["runs"]] == [500, 501]
    assert saved == ["USDJPY", "EURUSD"]


def test_run_target_failure_after_a_saved_pair_lists_the_saved_rows(
        monkeypatch, tmp_path):
    ro = _conn(tmp_path)
    settings, saved = _fake_environment(
        monkeypatch, tmp_path, pairs=["USDJPY", "EURUSD"], fail_pair="EURUSD")
    record = floor.run_target(_target(), root=tmp_path, settings=settings,
                              ro_conn=ro, write_conn=object(), now=NOW)
    assert saved == ["USDJPY"]
    assert record["partial_saved"] is True
    assert "部分保存" in record["exclusion"]
    assert [r["run_id"] for r in record["runs"]] == [500]
    text = floor.render_manifest([record], header={"mode": "write"})
    assert "| 500 |" in text and "部分保存" in text
    assert floor.frequency_summary([record])["approved"] is None


def test_run_target_failure_in_dry_run_is_not_marked_partial(monkeypatch, tmp_path):
    ro = _conn(tmp_path)
    settings, saved = _fake_environment(
        monkeypatch, tmp_path, pairs=["USDJPY", "EURUSD"], fail_pair="EURUSD")
    record = floor.run_target(_target(), root=tmp_path, settings=settings,
                              ro_conn=ro, write_conn=None, now=NOW)
    assert saved == [] and record["partial_saved"] is False
    assert "部分保存" not in record["exclusion"]
    assert "500" not in floor.render_manifest([record], header={})
